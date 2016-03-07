"""
Info widgets:
 * display detailed info about an object
"""
import logging
import pprint
import threading

import urwid
import urwidtrees
from urwid.decoration import BoxAdapter

from sen.tui.chunks.elemental import LayerWidget
from sen.tui.widgets.graph import ContainerInfoGraph
from sen.tui.widgets.list.base import VimMovementListBox
from sen.tui.widgets.list.util import get_map, RowWidget, UnselectableRowWidget
from sen.tui.widgets.table import assemble_rows
from sen.tui.widgets.util import SelectableText, ColorText
from sen.util import humanize_bytes, log_traceback

logger = logging.getLogger(__name__)


class TagWidget(SelectableText):
    """
    so we can easily access image and tag
    """
    def __init__(self, docker_image, tag):
        self.docker_image = docker_image
        self.tag = tag
        super().__init__(str(self.tag))


class ImageInfoWidget(VimMovementListBox):
    """
    display info about image
    """
    def __init__(self, ui, docker_image):
        self.ui = ui
        self.docker_image = docker_image

        self.walker = urwid.SimpleFocusListWalker([])

        # self.widgets = []

        self._basic_data()
        self._containers()
        self._image_names()
        self._layers()
        self._labels()

        super().__init__(self.walker)

        self.set_focus(0)  # or assemble list first and then stuff it into walker

    def _basic_data(self):
        data = [
            [SelectableText("Id", maps=get_map("main_list_green")),
             SelectableText(self.docker_image.image_id)],
            [SelectableText("Created", maps=get_map("main_list_green")),
             SelectableText("{0}, {1}".format(self.docker_image.display_formal_time_created(),
                                              self.docker_image.display_time_created()))],
            [SelectableText("Size", maps=get_map("main_list_green")),
             SelectableText(humanize_bytes(self.docker_image.size))],
            [SelectableText("Command", maps=get_map("main_list_green")),
             SelectableText(self.docker_image.container_command)],
        ]
        self.walker.extend(assemble_rows(data))

    def _image_names(self):
        if not self.docker_image.names:
            return
        self.walker.append(RowWidget([SelectableText("")]))
        self.walker.append(RowWidget([SelectableText("Image Names", maps=get_map("main_list_white"))]))
        for n in self.docker_image.names:
            self.walker.append(RowWidget([TagWidget(self.docker_image, n)]))

    def _layers(self):
        self.walker.append(RowWidget([SelectableText("")]))
        self.walker.append(RowWidget([SelectableText("Layers", maps=get_map("main_list_white"))]))

        i = self.docker_image
        index = 0
        self.walker.append(RowWidget([LayerWidget(self.ui, self.docker_image, index=index)]))
        while True:
            index += 1
            parent = i.parent_image
            if parent:
                self.walker.append(RowWidget([LayerWidget(self.ui, parent, index=index)]))
                i = parent
            else:
                break

    def _labels(self):
        if not self.docker_image.labels:
            return []
        data = []
        self.walker.append(RowWidget([SelectableText("")]))
        self.walker.append(RowWidget([SelectableText("Labels", maps=get_map("main_list_white"))]))
        for label_key, label_value in self.docker_image.labels.items():
            data.append([SelectableText(label_key, maps=get_map("main_list_green")), SelectableText(label_value)])
        self.walker.extend(assemble_rows(data))

    def _containers(self):
        if not self.docker_image.containers():
            return
        self.walker.append(RowWidget([SelectableText("")]))
        self.walker.append(RowWidget([SelectableText("Containers", maps=get_map("main_list_white"))]))
        for container in self.docker_image.containers():
            self.walker.append(RowWidget([SelectableText(str(container))]))

    def keypress(self, size, key):
        logger.debug("%s, %s", key, size)

        def getattr_or_notify(o, attr, message):
            try:
                return getattr(o, attr)
            except AttributeError:
                self.ui.notify_message(message, level="error")

        if key == "d":
            img = getattr_or_notify(self.focus.columns.widget_list[0], "docker_image",
                                    "Focused object isn't a docker image!")
            if not img:
                return
            tag = getattr_or_notify(self.focus.columns.widget_list[0], "tag",
                                    "Focused object doesn't have a tag!")
            if not tag:
                return
            try:
                img.remove_tag(tag)  # FIXME: do this async
            except Exception as ex:
                self.ui.notify_message("Can't remove tag '%s': %s" % (tag, ex), level="error")
                return
            self.walker.remove(self.focus)
            return

        key = super().keypress(size, key)
        return key


class Process:
    """
    single process returned for container.stats() query

    so we can hash the object
    """
    def __init__(self, data):
        self.data = data

    @property
    def pid(self):
        return self.data["PID"]

    @property
    def ppid(self):
        return self.data["PPID"]

    @property
    def command(self):
        return self.data["COMMAND"]

    def __str__(self):
        return "[{}] {}".format(self.pid, self.command)

    def __repr__(self):
        return self.__str__()


class ProcessList:
    """
    util functions for process returned by container.stats()
    """

    def __init__(self, data):
        self.data = [Process(x) for x in data]
        self._nesting = {x.pid: [] for x in self.data}
        for x in self.data:
            try:
                self._nesting[x.ppid].append(x)
            except KeyError:
                pass

        logger.debug(pprint.pformat(self._nesting, indent=2))
        self._pids = [x.pid for x in self.data]
        self._pid_index = {x.pid: x for x in self.data}

    def get_parent_process(self, process):
        return self._pid_index.get(process.ppid, None)

    def get_root_process(self):
        # FIXME: error handling
        root_process = [x for x in self.data if x.ppid not in self._pids]
        return root_process[0]

    def get_first_child_process(self, process):
        try:
            return self._nesting[process.pid][0]
        except (KeyError, IndexError):
            return

    def get_last_child_process(self, process):
        try:
            return self._nesting[process.pid][-1]
        except (KeyError, IndexError):
            return

    def get_next_sibling(self, process):
        children = self._nesting.get(process.ppid, [])
        if len(children) <= 0:
            return None
        try:
            p = children[children.index(process) + 1]
        except IndexError:
            return
        return p

    def get_prev_sibling(self, process):
        children = self._nesting.get(process.ppid, [])
        if len(children) <= 0:
            return None
        logger.debug("prev of %s has children %s", process, children)
        prev_idx = children.index(process) - 1
        if prev_idx < 0:
            # when this code path is not present, tree navigation is seriously messed up
            return None
        else:
            return children[prev_idx]


class ProcessTreeBackend(urwidtrees.Tree):
    def __init__(self, data):
        """

        :param data: dict, response from container.top()
        """
        super().__init__()
        self.data = data
        self.process_list = ProcessList(data)
        self.root = self.process_list.get_root_process()

    def __getitem__(self, pos):
        logger.debug("do widget for %s", pos)
        return RowWidget([SelectableText(str(pos))])

    # Tree API
    def parent_position(self, pos):
        v = self.process_list.get_parent_process(pos)
        logger.debug("parent of %s is %s", pos, v)
        return v

    def first_child_position(self, pos):
        logger.debug("first child process for %s", pos)
        v = self.process_list.get_first_child_process(pos)
        logger.debug("first child of %s is %s", pos, v)
        return v

    def last_child_position(self, pos):
        v = self.process_list.get_last_child_process(pos)
        logger.debug("last child of %s is %s", pos, v)
        return v

    def next_sibling_position(self, pos):
        v = self.process_list.get_next_sibling(pos)
        logger.debug("next of %s is %s", pos, v)
        return v

    def prev_sibling_position(self, pos):
        v = self.process_list.get_prev_sibling(pos)
        logger.debug("prev of %s is %s", pos, v)
        return v


class ProcessTree(urwidtrees.TreeBox):
    def __init__(self, data):
        tree = ProcessTreeBackend(data)

        # We hide the usual arrow tip and use a customized collapse-icon.
        t = urwidtrees.ArrowTree(
                tree,
                arrow_att="tree",  # lines, tip
                icon_collapsed_att="tree",  # +
                icon_expanded_att="tree",  # -
                icon_frame_att="tree",  # [ ]
        )
        super().__init__(t)


class ContainerInfoWidget(VimMovementListBox):
    """
    display info about image
    """
    def __init__(self, ui, docker_container):
        self.ui = ui
        self.docker_container = docker_container

        self.walker = urwid.SimpleFocusListWalker([])

        self.stop = threading.Event()

        self._basic_data()
        self._image()
        self._process_tree()
        self._resources()
        self._labels()

        super().__init__(self.walker)

        self.set_focus(0)  # or assemble list first and then stuff it into walker

    def _basic_data(self):
        data = [
            [SelectableText("Id", maps=get_map("main_list_green")),
             SelectableText(self.docker_container.container_id)],
            [SelectableText("Status", maps=get_map("main_list_green")),
             SelectableText(self.docker_container.status)],
            [SelectableText("Created", maps=get_map("main_list_green")),
             SelectableText("{0}, {1}".format(self.docker_container.display_formal_time_created(),
                                              self.docker_container.display_time_created()))],
            [SelectableText("Command", maps=get_map("main_list_green")),
             SelectableText(self.docker_container.command)],
            [SelectableText("IP Address", maps=get_map("main_list_green")),
             SelectableText(self.docker_container.ip_address)],
        ]
        self.walker.extend(assemble_rows(data))

    def _image(self):
        self.walker.append(RowWidget([SelectableText("")]))
        self.walker.append(RowWidget([SelectableText("Image", maps=get_map("main_list_white"))]))
        self.walker.append(RowWidget([LayerWidget(self.ui, self.docker_container.image)]))

    def _resources(self):
        self.walker.append(RowWidget([SelectableText("")]))
        self.walker.append(RowWidget([SelectableText("Resource Usage",
                                                     maps=get_map("main_list_white"))]))
        cpu_g = ContainerInfoGraph("graph_lines_cpu_inv", "graph_lines_cpu")
        mem_g = ContainerInfoGraph("graph_lines_mem_inv", "graph_lines_mem")
        cpu_label = ColorText("CPU ", "graph_lines_cpu_inv")
        cpu_value = ColorText("0.0 %", "graph_lines_cpu_inv")
        mem_label = ColorText("Memory ", "graph_lines_mem_inv")
        mem_value = ColorText("0.0 %", "graph_lines_mem_inv")
        self.walker.append(urwid.Columns([
            BoxAdapter(cpu_g, 12),
            BoxAdapter(mem_g, 12),
            BoxAdapter(urwid.ListBox(urwid.SimpleFocusListWalker([
                UnselectableRowWidget([(7, cpu_label), cpu_value]),
                UnselectableRowWidget([(7, mem_label), mem_value])
            ])), 12)
        ]))

        @log_traceback
        def realtime_updates():
            cpu_data = cpu_g.data[0]
            mem_data = mem_g.data[0]
            for update in self.docker_container.stats().response:
                if self.stop.is_set():
                    break
                logger.debug(update)
                cpu_percent = update["cpu_percent"]
                cpu_value.text = "%.2f %%" % cpu_percent
                cpu_data = cpu_data[1:] + [[int(cpu_percent)]]
                cpu_g.set_data(cpu_data, 100)

                mem_percent = update["mem_percent"]
                mem_current = humanize_bytes(update["mem_current"])
                mem_value.text = "%.2f %% (%s)" % (mem_percent, mem_current)
                mem_data = mem_data[1:] + [[int(mem_percent)]]
                mem_g.set_data(mem_data, 100)

        self.thread = threading.Thread(target=realtime_updates, daemon=True)
        self.thread.start()

    def _labels(self):
        if not self.docker_container.labels:
            return []
        data = []
        self.walker.append(RowWidget([SelectableText("")]))
        self.walker.append(RowWidget([SelectableText("Labels", maps=get_map("main_list_white"))]))
        for label_key, label_value in self.docker_container.labels.items():
            data.append([SelectableText(label_key, maps=get_map("main_list_green")), SelectableText(label_value)])
        self.walker.extend(assemble_rows(data))

    def _process_tree(self):
        top = self.docker_container.top().response
        if top:
            self.walker.append(RowWidget([SelectableText("")]))
            self.walker.append(RowWidget([SelectableText("Process Tree",
                                                         maps=get_map("main_list_white"))]))
            logger.debug("len=%d, %s", len(top), top)
            self.walker.append(BoxAdapter(ProcessTree(top), len(top)))

    def keypress(self, size, key):
        logger.debug("%s, %s", key, size)

        def getattr_or_notify(o, attr, message):
            try:
                return getattr(o, attr)
            except AttributeError:
                self.ui.notify_message(message, level="error")

        if key == "d":
            img = getattr_or_notify(self.focus.columns.widget_list[0], "docker_image",
                                    "Focused object isn't a docker image!")
            if not img:
                return
            tag = getattr_or_notify(self.focus.columns.widget_list[0], "tag",
                                    "Focused object doesn't have a tag!")
            if not tag:
                return
            try:
                img.remove_tag(tag)  # FIXME: do this async
            except Exception as ex:
                self.ui.notify_message("Can't remove tag '%s': %s" % (tag, ex), level="error")
                return
            self.walker.remove(self.focus)
            return

        key = super().keypress(size, key)
        return key

    def destroy(self):
        self.stop.set()
