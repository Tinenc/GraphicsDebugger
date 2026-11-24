"""Adopt native frame-flow dock placeholders without changing the user's layout."""

try:
    from PySide2 import QtCore, QtWidgets
    HAS_QT = True
except ImportError:
    HAS_QT = False


def find_frame_flow_placeholder(main_window):
    if not HAS_QT or main_window is None:
        return None
    for widget in main_window.findChildren(QtWidgets.QWidget, "breadFrameRenderFlow"):
        if widget.property("breadFrameFlowPlaceholder") is True:
            return widget
    return None


def retain_dock_ancestors(widget):
    """Keep borrowed native container wrappers alive while Python owns their children.

    PySide can invalidate a native child's wrapper when a temporary parent wrapper is
    collected, even though the C++ dock still exists. Retain just this ownership chain,
    not all application widgets, and refresh it after native layout restoration.
    """
    owners = []
    parent = widget.parentWidget()
    while parent is not None:
        owners.append(parent)
        parent = parent.parentWidget()
    return owners


if HAS_QT:
    class FrameFlowLayoutController(QtCore.QObject):
        """Fill only layout-requested docks; never open a tab omitted by saved layout."""
        def __init__(self, main_window, ensure_panel, existing_panel=None):
            super(FrameFlowLayoutController, self).__init__(main_window)
            self.main_window, self.ensure_panel = main_window, ensure_panel
            self.existing_panel = existing_panel
            self._alive = True
            self.timer = QtCore.QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.synchronize)
            main_window.installEventFilter(self)
            self.synchronize()

        def eventFilter(self, watched, event):
            if (self._alive and watched is self.main_window and
                    event.type() == QtCore.QEvent.DynamicPropertyChange and
                    event.propertyName() == QtCore.QByteArray(b"breadFrameFlowLayoutGeneration")):
                # Layout restore is synchronous. Wait until the native restore and orphan
                # handling return, then populate the existing dock without raising it.
                self.timer.start(0)
            return False

        def synchronize(self):
            if not self._alive:
                return
            panel = self.existing_panel() if callable(self.existing_panel) else None
            if find_frame_flow_placeholder(self.main_window) is not None:
                panel = self.ensure_panel()
            if panel is not None:
                panel._refresh_dock_ancestors()

        def close(self):
            self._alive = False
            self.timer.stop()
            self.main_window.removeEventFilter(self)
            self.deleteLater()
