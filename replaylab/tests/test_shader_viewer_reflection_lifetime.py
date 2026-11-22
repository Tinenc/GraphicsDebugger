"""Keep the production ShaderViewer reflection alive across queued disassembly."""
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows MSVC regression fixture")
def test_shader_viewer_owns_input_and_queued_native_call(tmp_path):
    from setuptools._distutils.ccompiler import new_compiler

    header = (ROOT / "qrenderdoc/Windows/ShaderViewer.h").read_text(encoding="utf-8")
    source = (ROOT / "qrenderdoc/Windows/ShaderViewer.cpp").read_text(encoding="utf-8")
    declaration = next(line.strip() for line in header.splitlines() if "m_ShaderDetails;" in line)
    method = source.split("void ShaderViewer::debugShader(", 1)[1]
    assignment = next(line.strip() for line in method.splitlines() if "m_ShaderDetails =" in line)
    queued = method.split("QPointer<ShaderViewer> me(this);", 1)[1].split("GUIInvoke::call(this,", 1)[0]
    production_queue = "QPointer<ShaderViewer> me(this);" + queued + "});"
    native = tmp_path / "viewer.cpp"
    native.write_text(r'''
#define RENDERDOC_PLATFORM_WIN32
#define RENDERDOC_EXPORTS
#define RENDERDOC_NO_EXPORTS
#include <cassert>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <memory>
#include <vector>
#include "api/replay/rdcpair.h"
#include "api/replay/shader_types.h"
void RENDERDOC_OutOfMemory(uint64_t) { std::abort(); }
struct IReplayController {
  std::function<void()> duringRead;
  ShaderReflection expected;
  int reads=0;
  rdcarray<rdcstr> GetDisassemblyTargets(bool pipeline) {
    return pipeline ? rdcarray<rdcstr>{"default", "pipeline"} : rdcarray<rdcstr>{"default"};
  }
  rdcstr DisassembleShader(ResourceId, const ShaderReflection *reflection, const char *) {
    if(duringRead) duringRead();
    assert(reflection);
    assert(reflection->entryPoint==expected.entryPoint);
    assert(reflection->rawBytes==expected.rawBytes);
    assert(reflection->interfaces==expected.interfaces);
    ++reads;
    return "disassembly";
  }
};
struct Context {
  std::vector<std::function<void(IReplayController*)>> work;
  Context &Replay() { return *this; }
  void AsyncInvoke(std::function<void(IReplayController*)> fn) { work.push_back(fn); }
};
template<class T> struct QPointer {
  std::weak_ptr<int> alive;
  QPointer(T *value) : alive(value->alive) {}
  explicit operator bool() const { return !alive.expired(); }
};
struct ShaderViewer {
  std::shared_ptr<int> alive=std::make_shared<int>(1);
  Context &m_Ctx;
  ResourceId m_Pipeline;
''' + declaration + r'''
  ShaderViewer(Context &ctx) : m_Ctx(ctx) {}
  void setInput(const ShaderReflection *shader) {
''' + assignment + r'''
  }
  void queue(ResourceId pipeline = ResourceId()) {
''' + production_queue + r'''
  }
};
int main() {
  Context context; IReplayController controller;
  std::unique_ptr<ShaderReflection> original(new ShaderReflection);
  original->entryPoint="long entry point copied from a temporary pipeline publication";
  original->rawBytes={11,22,33};
  original->interfaces={"nested allocated reflection data must be independently owned"};
  controller.expected=*original;
  std::unique_ptr<ShaderViewer> viewer(new ShaderViewer(context));
  viewer->setInput(original.get());
  assert(viewer->m_ShaderDetails.get()!=original.get());
  auto weak=std::weak_ptr<const ShaderReflection>(viewer->m_ShaderDetails);
  viewer->queue();
  original->entryPoint="mutated";original->rawBytes.clear();original->interfaces.clear();original.reset();
  context.work[0](&controller);
  assert(controller.reads==1);
  // The native call can overlap closing the viewer. Its captured reflection must survive both
  // the original publication and the UI member, and be freed when the queued closure retires.
  controller.duringRead=[&]() { viewer.reset(); assert(!weak.expired()); };
  context.work[0](&controller);
  assert(controller.reads==2 && !weak.expired());
  context.work.clear();assert(weak.expired());
  ShaderViewer empty(context);empty.setInput(nullptr);assert(!empty.m_ShaderDetails);
  std::cout << "ShaderViewer owned input, queued lifetime, release, null passed\n";
}
''', encoding="utf-8")
    compiler = new_compiler(compiler="msvc")
    compiled = compiler.compile([str(native)], output_dir=str(tmp_path),
                                include_dirs=[str(ROOT / "renderdoc")],
                                extra_postargs=["/EHsc", "/std:c++14", "/UNDEBUG"])
    executable = tmp_path / "viewer.exe"
    compiler.link_executable(compiled, str(executable.with_suffix("")), target_lang="c++")
    result = subprocess.run([str(executable)], capture_output=True, text=True, check=True, timeout=15)
    assert "queued lifetime, release, null passed" in result.stdout
