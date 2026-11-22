"""Exercise production debug entry points across a simulated nested event-loop publication."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _method(path, marker):
    source = path.read_text(encoding="utf-8")
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows MSVC regression fixture")
def test_debug_requests_keep_matching_reflection_and_pipeline_across_modal_publication(tmp_path):
    from setuptools._distutils.ccompiler import new_compiler

    windows = ROOT / "qrenderdoc/Windows"
    names = [f"{api}PipelineStateViewer" for api in ("D3D11", "D3D12", "GL", "Vulkan")]
    declarations = []
    methods = []
    calls = []
    for name in names + ["BufferViewer"]:
        member = "meshDebugSelector_beginDebug" if name == "BufferViewer" else "computeDebugSelector_beginDebug"
        path = windows / (name + ".cpp") if name == "BufferViewer" else windows / "PipelineState" / (name + ".cpp")
        declarations.append(f"struct {name} : View {{ void {member}(const rdcfixedarray<uint32_t,3>&, const rdcfixedarray<uint32_t,3>&); }};")
        methods.append(_method(path, f"void {name}::{member}("))
        calls.append(f"{{ {name} view; run(view, [&]() {{ view.{member}({{1,2,3}}, {{0,0,0}}); }}); }}")
    methods.append(_method(windows / "PixelHistoryView.cpp", "void PixelHistoryView::startDebug("))
    messages = (windows / "ShaderMessageViewer.cpp").read_text(encoding="utf-8").split(
        "QObject::connect(m_debugDelegate, &ButtonDelegate::messageClicked", 1
    )[1].split("  if(m_gotoDelegate)", 1)[0]
    message_body = messages[messages.index("    const ShaderMessage msg"):messages.rindex("  });")]
    methods.append("void ShaderMessageViewer::debugMessage() { int msgIdx = 0;\n" + message_body + "\n}")

    source = tmp_path / "modal_reflection.cpp"
    source.write_text(r'''
#define RENDERDOC_PLATFORM_WIN32
#define RENDERDOC_EXPORTS
#define RENDERDOC_NO_EXPORTS
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <memory>
#include <vector>
#include <iostream>
#include "api/replay/rdcpair.h"
#include "api/replay/pipestate.h"
void RENDERDOC_OutOfMemory(uint64_t) { std::abort(); }
ResourceId testId(uint64_t value) {
  ResourceId id; static_assert(sizeof(id)==sizeof(value), "ResourceId representation");
  memcpy(&id, &value, sizeof(value)); return id;
}
struct QString {
  QString() = default;
  template<class T> QString(const T &) {}
  template<class T> QString arg(const T &) const { return {}; }
  QString &operator+=(const QString &) { return *this; }
};
#define lit QString
#define QFormatStr QString
struct QThread { static void msleep(int) {} };
struct RDDialog { template<class... T> static void critical(T...) {} };
enum class DockReference { AddTo, MainToolArea };
struct IReplayController {
  ShaderDebugTrace *trace() { auto *t = new ShaderDebugTrace; t->debugger = reinterpret_cast<ShaderDebugger *>(1); return t; }
  template<class... T> ShaderDebugTrace *DebugThread(T...) { return trace(); }
  template<class... T> ShaderDebugTrace *DebugMeshThread(T...) { return trace(); }
  template<class... T> ShaderDebugTrace *DebugVertex(T...) { return trace(); }
  template<class... T> ShaderDebugTrace *DebugPixel(T...) { return trace(); }
  void FreeTrace(ShaderDebugTrace *value) { delete value; }
};
struct ReplayQueue {
  IReplayController controller;
  template<class F> void AsyncInvoke(F fn) { fn(&controller); }
};
struct IShaderViewer {
  void *Widget() { return this; }
  void ToggleBreakpointOnDisassemblyLine(int) {}
  void RunForward() {}
};
struct Pipeline {
  std::unique_ptr<ShaderReflection> reflection;
  ResourceId id;
  const ShaderReflection *GetShaderReflection(ShaderStage) { return reflection.get(); }
  ResourceId GetComputePipelineObject() { return id; }
  ResourceId GetGraphicsPipelineObject() { return id; }
};
struct Context {
  Pipeline pipe; ReplayQueue queue; ActionDescription action; APIProperties props;
  IShaderViewer viewer; int viewers=0;
  const ActionDescription *CurAction() { return &action; }
  Pipeline &CurPipelineState() { return pipe; }
  ReplayQueue &Replay() { return queue; }
  APIProperties &APIProps() { props.shaderDebugging=true; return props; }
  void SetEventID(std::initializer_list<void *>, uint32_t, uint32_t) {}
  IShaderViewer *DebugShader(const ShaderReflection *reflection, ResourceId pipeline,
                            ShaderDebugTrace *trace, const QString &) {
    assert(reflection != pipe.reflection.get());
    assert(reflection->entryPoint == "original debug request");
    assert(reflection->rawBytes == bytebuf({1,2,3}));
    assert(reflection->interfaces == rdcarray<rdcstr>({"nested reflection value"}));
    assert(pipeline == testId(1));
    ++viewers; delete trace; return &viewer;
  }
  void AddDockWindow(void *, DockReference, void *) {}
};
struct View {
  Context m_Ctx;
  QString tr(const char *) { return {}; }
};
std::function<void()> duringModal;
int dialogs=0;
template<class T, class F> void ShowProgressDialog(T *, const QString &, F done) {
  assert(done()); ++dialogs; duringModal();
}
struct EventTag { uint32_t eventId=42, primitive=1; };
struct Point { int x() { return 2; } int y() { return 3; } };
struct PixelHistoryView : View {
  struct { Subresource subresource; } m_Display;
  Point m_Pixel; uint32_t m_View=0;
  void startDebug(EventTag);
};
struct ShaderMessageViewer : View {
  std::vector<ShaderMessage> m_Messages;
  void debugMessage();
};
''' + "\n".join(declarations + methods) + r'''
template<class T, class F> void run(T &view, F invoke) {
  view.m_Ctx.pipe.reflection.reset(new ShaderReflection);
  view.m_Ctx.pipe.reflection->entryPoint="original debug request";
  view.m_Ctx.pipe.reflection->rawBytes={1,2,3};
  view.m_Ctx.pipe.reflection->interfaces={"nested reflection value"};
  view.m_Ctx.pipe.reflection->debugInfo.debuggable=true;
  view.m_Ctx.pipe.id=testId(1);
  auto priorModal=duringModal;
  duringModal=[&]() {
    view.m_Ctx.pipe.reflection.reset();
    view.m_Ctx.pipe.id=testId(2);
    if(priorModal) priorModal();
  };
  invoke(); assert(view.m_Ctx.viewers==1);
  // Missing reflection returns before any replay, progress dialog or debugger construction.
  int previousDialogs=dialogs;
  invoke(); assert(view.m_Ctx.viewers==1 && dialogs==previousDialogs);
  duringModal={};
}
int main() {
''' + "\n".join(calls) + r'''
  { PixelHistoryView view; run(view, [&]() { view.startDebug({}); }); }
  { ShaderMessageViewer view;
    ShaderMessage message; message.stage=ShaderStage::Compute; message.disassemblyLine=-1;
    view.m_Messages.push_back(message);
    duringModal=[&]() { view.m_Messages.clear(); view.m_Messages.shrink_to_fit(); };
    run(view, [&]() { if(view.m_Messages.empty()) view.m_Messages.push_back(message); view.debugMessage(); });
  }
  assert(dialogs==7);
  std::cout << "Seven production debug paths retained original reflection and pipeline\n";
}
''', encoding="utf-8")
    compiler = new_compiler(compiler="msvc")
    objects = compiler.compile(
        [str(source)], output_dir=str(tmp_path), include_dirs=[str(ROOT / "renderdoc")],
        extra_postargs=["/EHsc", "/std:c++14", "/UNDEBUG"],
    )
    executable = tmp_path / "modal_reflection.exe"
    compiler.link_executable(objects, str(executable), target_lang="c++")
    result = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=15)
    assert "Seven production debug paths" in result.stdout
