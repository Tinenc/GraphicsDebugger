"""Modeless in-application Markdown/HTML/text reader with native flow diagrams."""
from pathlib import Path
import queue
import threading
import re

from PySide2 import QtCore, QtGui, QtWidgets
from .document_content import load_document
from .project_shader_flow_graph import ShaderFlowGraphView

_windows = []


class LocalBrowser(QtWidgets.QTextBrowser):
    def loadResource(self, kind, url):
        if url.isRelative():
            url = self.document().baseUrl().resolved(url)
        if url.scheme() != "file":
            return None
        path = Path(url.toLocalFile()).resolve()
        try:
            path.relative_to(Path(self.document().baseUrl().toLocalFile()).resolve())
            reader = QtGui.QImageReader(str(path)); size = reader.size()
            if path.stat().st_size <= 32 * 1024 * 1024 and 0 < size.width()*size.height() <= 64*1024*1024:
                return reader.read()
        except (OSError, ValueError):
            pass
        return None


class DocumentWindow(QtWidgets.QDialog):
    def __init__(self, path, parent=None, navigate=None):
        QtWidgets.QDialog.__init__(self, parent)
        self.path, self.navigate = str(path), navigate
        self.setObjectName("breadDocumentViewer")
        self.setWindowTitle(QtCore.QCoreApplication.translate("ReplayLabProjects", "Bread document viewer"))
        self.resize(1100, 780)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        layout = QtWidgets.QVBoxLayout(self)
        toolbar = QtWidgets.QHBoxLayout(); layout.addLayout(toolbar)
        self.path_label = QtWidgets.QLabel(self.path); self.path_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        toolbar.addWidget(self.path_label, 1)
        reload_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate("ReplayLabProjects", "Reload document"))
        reload_button.clicked.connect(self.reload); toolbar.addWidget(reload_button)
        copy_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate("ReplayLabProjects", "Copy path"))
        copy_button.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.path)); toolbar.addWidget(copy_button)
        self.status = QtWidgets.QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.tabs = QtWidgets.QTabWidget(); layout.addWidget(self.tabs, 1)
        self.browser = LocalBrowser(); self.browser.setOpenLinks(False); self.browser.anchorClicked.connect(self.open_link)
        self.browser.document().setDefaultStyleSheet("h1{font-size:24pt} h2{font-size:18pt} pre,code{font-family:monospace} th{font-weight:bold} td,th{padding:7px;border:1px solid #808080} blockquote{margin-left:20px}")
        self.tabs.addTab(self.browser, QtCore.QCoreApplication.translate("ReplayLabProjects", "Document"))
        self.source = QtWidgets.QPlainTextEdit(); self.source.setReadOnly(True)
        self.tabs.addTab(self.source, QtCore.QCoreApplication.translate("ReplayLabProjects", "Source text"))
        self.diagram_tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.diagram_tabs, QtCore.QCoreApplication.translate("ReplayLabProjects", "Flowcharts"))
        self.timer = QtCore.QTimer(self); self.timer.setInterval(40); self.timer.timeout.connect(self.poll)
        self.reload()
    def reload(self):
        self.pending = queue.Queue(1)
        target, path = self.pending, self.path
        self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Loading document..."))
        def work():
            try:
                target.put({"model": load_document(path)})
            except Exception as error:
                target.put({"error": str(error)})
        thread = threading.Thread(target=work); thread.daemon = True; thread.start(); self.timer.start()
    def poll(self):
        try:
            result = self.pending.get_nowait()
        except queue.Empty:
            return
        self.timer.stop()
        if "error" in result:
            self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Could not read document: %1").replace("%1", result["error"]))
            return
        model = result["model"]
        self.source.setPlainText(model["text"])
        self.browser.document().setBaseUrl(QtCore.QUrl.fromLocalFile(str(Path(self.path).parent) + "/"))
        self.browser.setHtml(model["html"])
        while self.diagram_tabs.count():
            widget = self.diagram_tabs.widget(0); self.diagram_tabs.removeTab(0); widget.deleteLater()
        for index, diagram in enumerate(model["diagrams"]):
            view = ShaderFlowGraphView(); view.set_graph(diagram["graph"])
            if self.navigate:
                view.nodeActivated.connect(self.node_clicked)
            self.diagram_tabs.addTab(view, str(index + 1))
        diagnostics = model["diagnostics"]
        if diagnostics["questionMarkRuns"] or diagnostics["replacementCharacters"]:
            self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "The file contains damaged or placeholder text. Changing display encoding cannot recover characters already replaced with question marks."))
        elif model["previewTruncated"]:
            self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "The rich preview is truncated. Full content is available in Source text."))
        elif any(row["unparsedLines"] for row in model["diagrams"]):
            self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "The diagram preview contains unsupported syntax. Inspect Source text for complete details."))
        else:
            self.status.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Document loaded. Diagrams support flowchart nodes and arrows; full source remains available."))
    def node_clicked(self, node):
        identifier = str(node.get("id") or node.get("nodeId") or "") if isinstance(node, dict) else str(node)
        match = re.fullmatch(r"EID(\d+)", identifier, re.I)
        if match and self.navigate:
            self.navigate(int(match.group(1)))
    def open_link(self, url):
        diagram = re.fullmatch(r"diagram-(\d+)", url.fragment())
        if diagram:
            self.tabs.setCurrentWidget(self.diagram_tabs)
            self.diagram_tabs.setCurrentIndex(max(0, int(diagram.group(1)) - 1))
            return
        if url.isRelative():
            url = self.browser.document().baseUrl().resolved(url)
        if url.scheme() == "file" and Path(url.toLocalFile()).suffix.lower() in (".md", ".markdown", ".html", ".htm", ".txt"):
            self.path = url.toLocalFile(); self.path_label.setText(self.path); self.reload()
        elif url.scheme() in ("https", "http"):
            QtGui.QDesktopServices.openUrl(url)
        elif url.scheme() == "rdoc" and url.host() in ("event", "eid") and self.navigate and url.path().strip("/").isdigit():
            self.navigate(int(url.path().strip("/")))
        elif not url.fragment():
            return
        else:
            self.browser.scrollToAnchor(url.fragment())


def open_document(path, parent=None, navigate=None):
    window = DocumentWindow(path, parent, navigate)
    _windows.append(window)
    window.finished.connect(lambda unused: _windows.remove(window) if window in _windows else None)
    window.show(); window.raise_(); window.activateWindow()
    return window


def shutdown():
    for window in list(_windows):
        try:
            window.close()
        except RuntimeError:
            pass
    _windows[:] = []
