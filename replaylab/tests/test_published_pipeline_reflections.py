"""Compile the production snapshot against real API types; no replay or driver is started."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows MSVC regression fixture")
def test_publication_owns_reflections_after_driver_releases_them(tmp_path):
    from setuptools._distutils.ccompiler import new_compiler

    header = (ROOT / "qrenderdoc/Code/CaptureContext.h").read_text(encoding="utf-8")
    snapshot = "struct PublishedPipelineState" + header.split(
        "struct PublishedPipelineState", 1
    )[1].split("void PublishPipelineState", 1)[0]
    production = (ROOT / "qrenderdoc/Code/CaptureContext.cpp").read_text(encoding="utf-8")
    constructor = "CaptureContext::PublishedPipelineState::PublishedPipelineState" + production.split(
        "CaptureContext::PublishedPipelineState::PublishedPipelineState", 1
    )[1].split("void CaptureContext::PublishPipelineState", 1)[0]
    pipe_source = (ROOT / "renderdoc/api/replay/pipestate.inl").read_text(encoding="utf-8")
    query = "const ShaderReflection *PipeState::GetShaderReflection" + pipe_source.split(
        "const ShaderReflection *PipeState::GetShaderReflection", 1
    )[1].split("ResourceId PipeState::GetComputePipelineObject", 1)[0]

    stages = {
        "d3d11": ("vertex", "hull", "domain", "geometry", "pixel", "compute"),
        "d3d12": ("vertex", "hull", "domain", "geometry", "pixel", "compute", "amp", "mesh"),
        "gl": ("vertex", "tessControl", "tessEval", "geometry", "fragment", "compute"),
        "vulkan": ("vertex", "tessControl", "tessEval", "geometry", "fragment", "compute", "task", "mesh"),
    }
    setup = []
    checks = []
    for api, names in stages.items():
        for index, name in enumerate(names):
            original = "first" if index % 2 == 0 else "second"
            setup.append(f"controller.{api}.{name}Shader.reflection = {original}.get();")
            checks.append(
                f"check(snapshot->{api}->{name}Shader.reflection, {index % 2});"
            )
    source = tmp_path / "reflection_snapshot.cpp"
    source.write_text(r'''
#define RENDERDOC_PLATFORM_WIN32
#define RENDERDOC_EXPORTS
#define RENDERDOC_NO_EXPORTS
#include <cassert>
#include <cstdlib>
#include <iostream>
#include <map>
#include <memory>
#include <vector>
#include "api/replay/rdcpair.h"
#include "api/replay/pipestate.h"
void RENDERDOC_OutOfMemory(uint64_t) { std::abort(); }
struct IReplayController {
  D3D11Pipe::State d3d11; D3D12Pipe::State d3d12; GLPipe::State gl; VKPipe::State vulkan;
  PipeState pipeline; bool empty = false;
  const D3D11Pipe::State *GetD3D11PipelineState() { return empty ? nullptr : &d3d11; }
  const D3D12Pipe::State *GetD3D12PipelineState() { return empty ? nullptr : &d3d12; }
  const GLPipe::State *GetGLPipelineState() { return empty ? nullptr : &gl; }
  const VKPipe::State *GetVulkanPipelineState() { return empty ? nullptr : &vulkan; }
  const PipeState &GetPipelineState() { return pipeline; }
  uint64_t GetShaderReplacementGeneration(uint32_t) { return 0; }
};
struct CaptureContext {
''' + snapshot + "};\n" + constructor + query + r'''
int main() {
  IReplayController controller;
  std::unique_ptr<ShaderReflection> first(new ShaderReflection), second(new ShaderReflection);
  first->entryPoint = "first shader entry point with a non-inline owning string";
  second->entryPoint = "second shader entry point with a non-inline owning string";
  first->rawBytes = {11, 22, 33}; second->rawBytes = {44, 55, 66};
  first->interfaces = {"first nested owning string with independent storage"};
  second->interfaces = {"second nested owning string with independent storage"};
  const ShaderReflection expected[] = {*first, *second};
  const ShaderReflection *originals[] = {first.get(), second.get()};
''' + "\n".join(setup) + r'''
  controller.d3d11.inputAssembly.bytecode = first.get();
  std::vector<std::unique_ptr<CaptureContext::PublishedPipelineState>> snapshots;
  controller.pipeline.SetState(&controller.d3d11);
  snapshots.emplace_back(new CaptureContext::PublishedPipelineState(&controller));
  controller.pipeline.SetState(&controller.d3d12);
  snapshots.emplace_back(new CaptureContext::PublishedPipelineState(&controller));
  controller.pipeline.SetState(&controller.gl);
  snapshots.emplace_back(new CaptureContext::PublishedPipelineState(&controller));
  controller.pipeline.SetState(&controller.vulkan);
  snapshots.emplace_back(new CaptureContext::PublishedPipelineState(&controller));
  first->entryPoint = "changed"; first->rawBytes[0] = 255; first->interfaces[0] = "changed";
  second->entryPoint = "changed"; second->rawBytes[0] = 255; second->interfaces[0] = "changed";
  first.reset(); second.reset();
  for(const auto &snapshot : snapshots) {
    assert(snapshot->reflections.size() == 2);
    auto check = [&](const ShaderReflection *value, size_t index) {
      assert(value && value != originals[index]);
      assert(value->entryPoint == expected[index].entryPoint);
      assert(value->rawBytes == expected[index].rawBytes);
      assert(value->interfaces == expected[index].interfaces);
      assert(value == snapshot->reflections.at(originals[index]).get());
    };
''' + "\n".join(checks) + r'''
    check(snapshot->d3d11->inputAssembly.bytecode, 0);
    assert(snapshot->d3d11->inputAssembly.bytecode == snapshot->d3d11->vertexShader.reflection);
    check(snapshot->pipeline.GetShaderReflection(ShaderStage::Vertex), 0);
    check(snapshot->pipeline.GetShaderReflection(ShaderStage::Compute), 1);
    assert(snapshot->pipeline.GetShaderReflection(ShaderStage::Count) == nullptr);
  }
  // No shader and a controller without any API state must remain valid empty snapshots.
  controller = IReplayController();
  CaptureContext::PublishedPipelineState unbound(&controller);
  assert(unbound.reflections.empty());
  assert(unbound.d3d11->inputAssembly.bytecode == nullptr);
  assert(unbound.d3d12->pixelShader.reflection == nullptr);
  controller.empty = true;
  CaptureContext::PublishedPipelineState empty(&controller);
  assert(empty.reflections.empty());
  assert(!empty.d3d11 && !empty.d3d12 && !empty.gl && !empty.vulkan);
  std::cout << "Owned reflection snapshots: four APIs, 28 stages, layout, null, dedup passed\n";
}
''', encoding="utf-8")
    compiler = new_compiler(compiler="msvc")
    objects = compiler.compile(
        [str(source)], output_dir=str(tmp_path), include_dirs=[str(ROOT / "renderdoc")],
        extra_postargs=["/EHsc", "/std:c++14", "/UNDEBUG"],
    )
    executable = tmp_path / "reflection_snapshot.exe"
    compiler.link_executable(objects, str(executable), target_lang="c++")
    result = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=15)
    assert "28 stages, layout, null, dedup passed" in result.stdout
