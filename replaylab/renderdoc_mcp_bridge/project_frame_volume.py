"""Cached, capture-bound Texture3D slice browsing (Python 3.6 compatible)."""
from __future__ import print_function

from collections import OrderedDict
import os
import re


MAX_CACHED_SLICES = 64
MAX_SLICE_IMAGES = MAX_CACHED_SLICES * 5
CHANNEL_ORDER = ("rgb", "r", "g", "b", "a")


def normalize_volume_preview(value, parent):
    """Accept only bounded Host receipts, never paths synthesized from names."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Volume preview must be an object")
    depth, mip = value.get("depth"), value.get("mip", 0)
    if type(depth) is not int or not 1 <= depth <= 65536 or type(mip) is not int or not 0 <= mip <= 31:
        raise ValueError("Volume preview requires valid mip depth")
    first, count = value.get("viewSliceFirst", 0), value.get("viewSliceCount", depth)
    if (type(first) is not int or type(count) is not int or first < 0 or count <= 0 or
            first + count > depth):
        raise ValueError("Volume preview view must be within the mip depth")
    mapping = parent.get("displayMapping") or {}
    if int(mapping.get("mip") or 0) != mip:
        raise ValueError("Volume preview mip must match its parent texture")
    rows = value.get("slices")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SLICE_IMAGES:
        raise ValueError("Volume preview requires bounded cached slices")
    clean, seen, indices = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Volume slice must be an object")
        z, channel = row.get("slice"), row.get("channel")
        if type(z) is not int or not first <= z < first + count or channel not in CHANNEL_ORDER:
            raise ValueError("Volume slice or channel is outside the captured texture")
        if (z, channel) in seen:
            raise ValueError("Volume preview repeats a slice channel")
        component_count = parent.get("componentCount", 0)
        if component_count and not set(channel) <= set("rgba"[:component_count]):
            raise ValueError("Volume preview channel is absent from the texture")
        path, sha = row.get("path", ""), row.get("sha256", "")
        if (not isinstance(path, str) or not os.path.isabs(path) or
                os.path.splitext(path)[1].lower() != ".png" or
                not isinstance(sha, str) or not re.match(r"^[0-9a-f]{64}$", sha)):
            raise ValueError("Volume slice requires a verified absolute PNG receipt")
        for field in ("eventId", "resourceId"):
            if field in row and str(row[field]).replace("ResourceId::", "") != str(parent.get(field, "")).replace("ResourceId::", ""):
                raise ValueError("Volume slice must belong to its parent snapshot")
        for field in ("width", "height"):
            if type(row.get(field)) is not int or not 0 < row[field] <= 16384:
                raise ValueError("Volume slice dimensions are invalid")
        clean.append({"slice": z, "channel": channel, "path": path, "sha256": sha,
                      "width": row["width"], "height": row["height"]})
        seen.add((z, channel))
        indices.add(z)
    if len(indices) > MAX_CACHED_SLICES:
        raise ValueError("Volume preview has too many cached depth slices")
    selected = value.get("selectedSlice", first + count // 2)
    if type(selected) is not int or not first <= selected < first + count:
        raise ValueError("Selected volume slice is outside the captured texture")
    clean.sort(key=lambda row: (CHANNEL_ORDER.index(row["channel"]), row["slice"]))
    return {"depth": depth, "mip": mip, "selectedSlice": selected, "slices": clean,
            "viewSliceFirst": first, "viewSliceCount": count, "complete": value.get("complete") is True}


def volume_channel_slices(volume, channel):
    return [row for row in volume["slices"] if row["channel"] == channel]


def nearest_slice_index(rows, z):
    return min(range(len(rows)), key=lambda index: (abs(rows[index]["slice"] - z), rows[index]["slice"]))


try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:
    QtCore = None


if QtCore is not None:
    class VolumePreviewPopup(QtWidgets.QFrame):
        """Disk reads are asynchronous; dragging never invokes capture replay."""
        def __init__(self, preview, owner, decode, navigate):
            super(VolumePreviewPopup, self).__init__(owner, QtCore.Qt.Popup)
            self._owner, self._decode, self._navigate = owner, decode, navigate
            self._preview, self._volume = preview, preview["volumePreview"]
            self._alive, self._pending, self._cache = True, {}, OrderedDict()
            self._rows, self._pixmap = [], None
            self.setObjectName("breadFrameFlowVolume")
            self.setFrameShape(QtWidgets.QFrame.StyledPanel)
            self.setAutoFillBackground(True)
            self.setPalette(owner.palette())
            self.setWindowTitle(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "%1 · 3D texture slices").replace("%1", preview["title"]))
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(14, 12, 14, 12)
            header = QtWidgets.QHBoxLayout()
            title = QtWidgets.QLabel(self.windowTitle(), self)
            title.setTextFormat(QtCore.Qt.PlainText)
            title.setWordWrap(True)
            font = title.font()
            font.setBold(True)
            font.setPointSizeF(max(12.0, font.pointSizeF()))
            title.setFont(font)
            header.addWidget(title, 1)
            close = QtWidgets.QToolButton(self)
            icon = QtGui.QPixmap(18, 18)
            icon.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(icon)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setPen(QtGui.QPen(owner.palette().color(QtGui.QPalette.Text), 2))
            painter.drawLine(4, 4, 14, 14)
            painter.drawLine(14, 4, 4, 14)
            painter.end()
            close.setIcon(QtGui.QIcon(icon))
            close.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Close"))
            close.setAccessibleName(QtCore.QCoreApplication.translate("ReplayLabProjects", "Close"))
            close.clicked.connect(self.close)
            header.addWidget(close)
            layout.addLayout(header)
            self.image = QtWidgets.QLabel(self)
            self.image.setAlignment(QtCore.Qt.AlignCenter)
            self.image.setMinimumSize(320, 220)
            self.image.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
            layout.addWidget(self.image, 1)
            controls = QtWidgets.QHBoxLayout()
            controls.addWidget(QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Channel"), self))
            self.channel = QtWidgets.QComboBox(self)
            groups = {group["channel"]: group for group in preview.get("channelGroups", [])}
            channels = {row["channel"] for row in self._volume["slices"]}
            for channel in CHANNEL_ORDER:
                if channel in channels:
                    label = groups.get(channel, {}).get("label", "")
                    # i18n-ignore: RGBA channel identifiers and captured semantic labels are data.
                    self.channel.addItem(channel.upper() + (": " + label if label else ""), channel)
            controls.addWidget(self.channel)
            self.slice_label = QtWidgets.QLabel(self)
            self.slice_label.setTextFormat(QtCore.Qt.PlainText)
            controls.addWidget(self.slice_label, 1, QtCore.Qt.AlignRight)
            layout.addLayout(controls)
            self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, self)
            self.slider.setAccessibleName(QtCore.QCoreApplication.translate("ReplayLabProjects", "Depth slice"))
            layout.addWidget(self.slider)
            self.description = QtWidgets.QLabel(self)
            self.description.setTextFormat(QtCore.Qt.PlainText)
            self.description.setWordWrap(True)
            layout.addWidget(self.description)
            self.status = QtWidgets.QLabel(self)
            self.status.setTextFormat(QtCore.Qt.PlainText)
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            open_slice = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Show this slice in the Texture Viewer"), self)
            open_slice.clicked.connect(self._open_slice)
            layout.addWidget(open_slice, 0, QtCore.Qt.AlignRight)
            self.channel.currentIndexChanged.connect(self._change_channel)
            self.slider.valueChanged.connect(self._select_slice)
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(40)
            self._timer.timeout.connect(self._load)
            self._change_channel()
            available = QtWidgets.QApplication.desktop().availableGeometry(owner)
            self.resize(min(760, available.width() - 32), min(620, available.height() - 48))
            origin = owner.mapToGlobal(owner.rect().center()) - QtCore.QPoint(self.width() // 2, self.height() // 2)
            self.move(max(available.left() + 8, min(origin.x(), available.right() - self.width() - 8)),
                      max(available.top() + 8, min(origin.y(), available.bottom() - self.height() - 8)))
            QtWidgets.QApplication.instance().installEventFilter(self)
            self.show()
            self._timer.start()

        def _change_channel(self, unused=None):
            z = self._rows[self.slider.value()]["slice"] if self._rows else self._volume["selectedSlice"]
            self._rows = volume_channel_slices(self._volume, self.channel.currentData())
            self.slider.blockSignals(True)
            self.slider.setRange(0, len(self._rows) - 1)
            self.slider.setValue(nearest_slice_index(self._rows, z))
            self.slider.setEnabled(len(self._rows) > 1)
            self.slider.blockSignals(False)
            group = next((group for group in self._preview.get("channelGroups", [])
                          if group["channel"] == self.channel.currentData()), {})
            self.description.setText(group.get("description") or self._preview.get("description", ""))
            self._select_slice()

        def _select_slice(self, unused=None):
            z = self._rows[self.slider.value()]["slice"]
            self.slice_label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Depth slice %1 of %2").replace("%1", str(z + 1)).replace("%2", str(self._volume["depth"])))
            if len(self._rows) < self._volume["viewSliceCount"]:
                self.slider.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "%1 cached slices across %2 depth slices").replace(
                    "%1", str(len(self._rows))).replace("%2", str(self._volume["viewSliceCount"])))
            else:
                self.slider.setToolTip("")
            self._pixmap = None
            self._load()

        def _load(self):
            if not self._alive:
                return
            for path, future in list(self._pending.items()):
                if not future.done():
                    continue
                del self._pending[path]
                try:
                    image = future.result()
                    self._cache[path] = QtGui.QPixmap.fromImage(image) if image is not None else None
                except Exception:
                    self._cache[path] = None
                while len(self._cache) > 24:
                    self._cache.popitem(last=False)
            current = self._rows[self.slider.value()]["path"]
            if current in self._cache:
                previous = self._pixmap
                self._pixmap = self._cache[current]
                self._cache.move_to_end(current)
                self.status.setText("" if self._pixmap is not None else QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Could not read the saved slice preview."))
                if previous is not self._pixmap:
                    self._draw_image()
            elif self._pixmap is None:
                self.image.clear()
                self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Loading preview…"))
            # Decode the current image first, then its neighbours. Fast slider
            # movement never queues a replay or an unbounded list of disk reads.
            selected = self.slider.value()
            for index in (selected, selected + 1, selected - 1):
                if 0 <= index < len(self._rows):
                    path = self._rows[index]["path"]
                    if path not in self._cache and path not in self._pending and len(self._pending) < 3:
                        self._pending[path] = self._owner._executor.submit(self._decode, path)

        def _draw_image(self):
            if self._pixmap is not None:
                self.image.setPixmap(self._pixmap.scaled(self.image.size(), QtCore.Qt.KeepAspectRatio,
                                                        QtCore.Qt.SmoothTransformation))
            else:
                self.image.clear()

        def _open_slice(self):
            selected = self._rows[self.slider.value()]
            preview = dict(self._preview, channel=selected["channel"])
            preview["displayMapping"] = dict(preview.get("displayMapping") or {},
                                             mip=self._volume["mip"], slice=selected["slice"])
            self._navigate(preview)

        def resizeEvent(self, event):
            super(VolumePreviewPopup, self).resizeEvent(event)
            self._draw_image()

        def eventFilter(self, watched, event):
            if (self._alive and event.type() == QtCore.QEvent.MouseButtonPress and
                    not self.frameGeometry().contains(event.globalPos())):
                # A combo's popup is a separate window but part of this control.
                combo_popup = self.channel.view().window()
                if not combo_popup.isVisible() or not combo_popup.frameGeometry().contains(event.globalPos()):
                    self.close()
            return False

        def keyPressEvent(self, event):
            if event.key() == QtCore.Qt.Key_Escape:
                self.close()
                event.accept()
            else:
                super(VolumePreviewPopup, self).keyPressEvent(event)

        def closeEvent(self, event):
            self._alive = False
            self._timer.stop()
            QtWidgets.QApplication.instance().removeEventFilter(self)
            for future in self._pending.values():
                future.cancel()
            self._pending.clear()
            self._cache.clear()
            super(VolumePreviewPopup, self).closeEvent(event)
