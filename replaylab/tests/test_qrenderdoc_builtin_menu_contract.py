from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_CONTEXT_HEADER = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "CaptureContext.h"
CAPTURE_CONTEXT_SOURCE = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "CaptureContext.cpp"
QRD_UTILS_HEADER = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "QRDUtils.h"
REPLAYLAB_SOURCE = (
    REPOSITORY_ROOT
    / "qrenderdoc"
    / "Code"
    / "ReplayLab"
    / "ReplayLabIntegration.cpp"
)
MAIN_WINDOW_SOURCE = REPOSITORY_ROOT / "qrenderdoc" / "Windows" / "MainWindow.cpp"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def test_first_party_replaylab_menu_registration_suppresses_extension_icons() -> None:
    capture_header = _source(CAPTURE_CONTEXT_HEADER)
    capture_source = _source(CAPTURE_CONTEXT_SOURCE)
    replaylab_source = _source(REPLAYLAB_SOURCE)
    main_window_source = _source(MAIN_WINDOW_SOURCE)

    assert "bool m_RegisteringFirstPartyIntegration = false;" in capture_header

    initialize = _section(
        replaylab_source,
        "QString ReplayLabIntegration::Initialize()",
        "QStringList ReplayLabIntegration::AvailableShaderStages",
    )
    scope_marker = (
        "QScopedValueRollback<bool> registrationScope("
        "m_Context.m_RegisteringFirstPartyIntegration,"
    )
    assert scope_marker in initialize
    assert initialize.index(scope_marker) < initialize.index("LoadInternalIntegration(")

    register_window = _section(
        capture_source,
        "void CaptureContext::RegisterWindowMenu(",
        "void CaptureContext::RegisterPanelMenu(",
    )
    assert (
        "const bool showExtensionIcon = !m_RegisteringFirstPartyIntegration;"
        in register_window
    )
    assert "GetBaseMenu(base, submenus[0], showExtensionIcon)" in register_window
    assert register_window.count("showExtensionIcon") >= 4

    add_sorted = _section(
        capture_source,
        "void CaptureContext::AddSortedMenuItem(",
        "void CaptureContext::CleanMenu(",
    )
    assert add_sorted.count("if(addIcon && showExtensionIcon)") == 2
    assert add_sorted.count("Icons::plugin()") == 2

    get_base_menu = _section(
        main_window_source,
        "QMenu *MainWindow::GetBaseMenu(",
        "QList<QAction *> MainWindow::GetMenuActions()",
    )
    assert "if(showExtensionIcon)" in get_base_menu
    assert "menu->setIcon(Icons::plugin());" in get_base_menu


def test_optional_extension_menu_items_retain_their_plugin_identity() -> None:
    capture_source = _source(CAPTURE_CONTEXT_SOURCE)
    menu_item = _source(QRD_UTILS_HEADER)

    assert "bool showExtensionIcon = true;" in menu_item

    register_panel = _section(
        capture_source,
        "void CaptureContext::RegisterPanelMenu(",
        "void CaptureContext::RegisterContextMenu(",
    )
    register_context = _section(
        capture_source,
        "void CaptureContext::RegisterContextMenu(",
        "void CaptureContext::MenuDisplaying(ContextMenu",
    )
    assert (
        "item->showExtensionIcon = !m_RegisteringFirstPartyIntegration;"
        in register_panel
    )
    assert (
        "item->showExtensionIcon = !m_RegisteringFirstPartyIntegration;"
        in register_context
    )

    context_display = _section(
        capture_source,
        "void CaptureContext::MenuDisplaying(ContextMenu",
        "void CaptureContext::MenuDisplaying(PanelMenu",
    )
    panel_display = _section(
        capture_source,
        "void CaptureContext::MenuDisplaying(PanelMenu",
        "IMiniQtHelper &CaptureContext::GetMiniQtHelper()",
    )
    assert "item->showExtensionIcon" in context_display
    assert "item->showExtensionIcon" in panel_display
