"""Inspect the frozen tool catalogue and SDK bytes without touching a Unity project."""
import asyncio
import hashlib
import json
from pathlib import Path
import types
import argparse

from PyInstaller.archive.readers import CArchiveReader
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]


def constants(code):
    values = code.co_consts if isinstance(code, types.CodeType) else code
    for value in values:
        if isinstance(value, types.CodeType):
            yield from constants(value)
        elif isinstance(value, (tuple, list, frozenset)):
            yield from constants(value)
        elif isinstance(value, str):
            yield value


def semantics(code):
    if isinstance(code, types.CodeType):
        return (code.co_code, code.co_flags, code.co_names, code.co_varnames,
                code.co_freevars, code.co_cellvars, tuple(semantics(value) for value in code.co_consts))
    return code


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--runtime-root",type=Path,default=ROOT / "x64/Development/replaylab")
    parser.add_argument("--output-directory",type=Path,default=ROOT / "output/bread-dual-view-restore-20261101")
    options=parser.parse_args()
    runtime = options.runtime_root
    archive = CArchiveReader(str(runtime / "BreadMCP.exe"))
    files = {}
    for key in archive.toc:
        normalized = key.replace("\\", "/")
        if normalized.startswith("bread_unity/"):
            source = ROOT / "replaylab/unity" / normalized.removeprefix("bread_unity/")
            if source.is_file():
                value = archive.extract(key)
                files[normalized] = hashlib.sha256(value).hexdigest() == hashlib.sha256(source.read_bytes()).hexdigest()
    pyz = archive.open_embedded_archive("PYZ.pyz")
    integration = set(constants(pyz.extract("src.unity_integration")))
    source_modules = ["unity_fidelity_tools", "unity_layered_validation", "unity_restoration_tools", "unity_view_validation",
                      "unity_temporal_validation", "unity_handoff_review", "unity_difference_diagnosis", "task_completion_review", "task_report",
                      "project_http_helper", "active_projects", "internal_host", "internal_tool_service", "unity_integration",
                      "project_restore", "file_content_cache", "restore_file_preparation", "shader_workspace.status_snapshot",
                      "projects", "project_read_cache", "shader_workspace.alias_display_mirror", "server"]
    for module in source_modules:
        source = ROOT / "replaylab/mcp_host/src" / (module.replace(".","/") + ".py")
        assert semantics(pyz.extract("src." + module)) == semantics(compile(source.read_text(encoding="utf-8"), "", "exec", dont_inherit=True)), module
    internal_pyz = CArchiveReader(str(runtime / "ReplayLabInternal.exe")).open_embedded_archive("PYZ.pyz")
    for module in ("internal_tool_service", "project_restore", "server", "unity_integration"):
        source = ROOT / "replaylab/mcp_host/src" / (module + ".py")
        assert semantics(internal_pyz.extract("src." + module)) == semantics(
            compile(source.read_text(encoding="utf-8"), "", "exec", dont_inherit=True)), "internal:" + module
    assert files and all(files.values()), files
    assert {"previousManagedVersions", "retainedInstalledPackages"} <= integration
    async with stdio_client(StdioServerParameters(command=str(runtime / "BreadMCP.exe"), args=["stdio"])) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listing = await session.list_tools()
    catalog = {item.name: item for item in listing.tools}
    names = ["get_event_buffer_data", "compare_unity_effect_layers", "trace_unity_effect_dependencies",
             "export_unity_volume_texture", "export_unity_texture_data", "plan_unity_asset_layout",
             "write_restoration_document", "open_restoration_document", "review_unity_view_evidence",
             "review_unity_temporal_stability", "review_unity_restoration_handoff", "localize_unity_effect_difference", "finish_shader_workspace_organization"]
    assert all(name in catalog for name in names)
    agent_archive = CArchiveReader(str(runtime / "replaylab-ai.exe"))
    frozen_policy = types.ModuleType("replaylab_agent._frozen_policy_probe")
    frozen_policy.__package__ = "replaylab_agent"
    exec(agent_archive.open_embedded_archive("PYZ.pyz").extract("replaylab_agent.policy"), frozen_policy.__dict__)
    frozen_policy.validate_registered_tool_effect_manifest(catalog.keys())
    prompts = agent_archive.open_embedded_archive("PYZ.pyz").extract("replaylab_agent.agent_prompts")
    assert semantics(prompts) == semantics(compile((ROOT/"replaylab/src/replaylab_agent/agent_prompts.py").read_text(encoding="utf-8"),"","exec",dont_inherit=True))
    review=agent_archive.open_embedded_archive("PYZ.pyz").extract("replaylab_agent.task_review")
    assert semantics(review)==semantics(compile((ROOT/"replaylab/src/replaylab_agent/task_review.py").read_text(encoding="utf-8"),"","exec",dont_inherit=True))
    for module in ("skills", "portable_capabilities", "portable_tools", "portable_desktop", "connection_probe", "connection_readiness", "doctor", "cli", "desktop_capabilities"):
        source = ROOT / "replaylab/src/replaylab_agent" / (module + ".py")
        frozen = agent_archive.open_embedded_archive("PYZ.pyz").extract("replaylab_agent." + module)
        assert semantics(frozen) == semantics(compile(source.read_text(encoding="utf-8"), "", "exec", dont_inherit=True)), module
    assert "decision_request" in catalog["record_task_review"].inputSchema["properties"]
    internal_pyz = CArchiveReader(str(runtime/"ReplayLabInternal.exe")).open_embedded_archive("PYZ.pyz")
    for module in ("file_content_cache","shader_workspace.status_snapshot","project_restore","restore_file_preparation",
                   "projects", "project_read_cache", "shader_workspace.alias_display_mirror", "server",
                   "unity_temporal_validation", "unity_handoff_review", "unity_difference_diagnosis", "task_completion_review", "task_report",
                   "project_http_helper", "active_projects", "internal_host"):
        source=ROOT/"replaylab/mcp_host/src"/(module.replace(".","/")+".py")
        assert semantics(internal_pyz.extract("src."+module)) == semantics(compile(source.read_text(encoding="utf-8"),"","exec",dont_inherit=True)), module
    skill_files = {}
    for key in agent_archive.toc:
        normalized = key.replace("\\", "/")
        if normalized.startswith(".agents/skills/") and (ROOT / normalized).is_file():
            skill_files[normalized] = agent_archive.extract(key) == (ROOT / normalized).read_bytes()
    assert skill_files and all(skill_files.values()), skill_files
    policy = catalog[names[-1]].inputSchema["properties"]["source_review_policy"]
    assert policy["default"] == "advisory", policy
    mesh = catalog["get_post_vs_data"].inputSchema["properties"]
    assert (mesh["max_vertices"]["minimum"], mesh["max_vertices"]["maximum"]) == (1,16384)
    assert (mesh["max_indices"]["minimum"], mesh["max_indices"]["maximum"]) == (1,49152)
    result = dict(sdkFilesVerified=len(files), sdkMatchesCurrentSource=True,
                  sdkUpgradeCompatibilityIncluded=True, tools=names, toolEffectManifestCoherent=True,
                  hostModulesMatchCurrentSource=source_modules,
                  internalRestoreModulesMatchCurrentSource=True, agentGuidanceMatchesCurrentSource=True,
                  agentUserDecisionHandlingMatchesCurrentSource=True,
                  skillFilesVerified=len(skill_files), skillsMatchCurrentSource=True, defaultSourceReviewPolicy=policy["default"])
    output = options.output_directory / "frozen-runtime-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
