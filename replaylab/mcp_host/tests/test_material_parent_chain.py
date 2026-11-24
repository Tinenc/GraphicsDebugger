"""The direct UEX ShaderMap facade follows only observed, in-root parents."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from src import material_parent_chain, server, uex_profiles


def receipt(document: object, directory: Path | None = None, label: str = "") -> dict:
    stdout = json.dumps(document)
    raw = stdout.encode("utf-8")
    result = {
        "schema": "replaylab-tool-invocation/v1",
        "operation": "uex_material_shader_maps",
        "succeeded": True,
        "stdout": stdout,
        "stdoutTruncated": False,
        "stdoutByteSize": len(raw),
        "stdoutSha256": hashlib.sha256(raw).hexdigest(),
    }
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (label + ".json")
        path.write_bytes(raw)
        result.update({
            "outputDirectory": str(directory),
            "stdoutPath": str(path),
            "stdoutFileTruncated": False,
            "stdoutFileByteSize": len(raw),
            "stdoutFileSha256": hashlib.sha256(raw).hexdigest(),
        })
    return result


def shader_map(
    asset: str,
    status: str = "empty",
    parent: str = "",
    *,
    embed_preview: bool = True,
) -> dict:
    variants = ([{"shaderPlatform": "SP_PCD3D_SM6"}] if status == "parsed" else [])
    material = {"objectName": Path(asset).stem, "variants": variants}
    if embed_preview:
        material["materialPreview"] = preview(asset, parent)[0]
    return {
        "schema": "bread-material-shader-evidence/v1",
        "status": status,
        "readShaderMaps": {"completed": True},
        "coverage": {"complete": True},
        "asset": {"path": asset},
        "materials": [material],
    }


def preview(asset: str, parent: str) -> list[dict]:
    return [{
        "Type": "Material" if not parent else "MaterialInstanceConstant",
        "Name": Path(asset).stem,
        "Package": asset.removesuffix(".uasset"),
        "Properties": ({"Parent": {"ObjectPath": parent}} if parent else {}),
    }]


def test_parent_chain_finds_compiled_owner_inside_configured_complete_root(tmp_path):
    root = tmp_path / "unpacked"
    mount = root / "raw"
    leaf = "SilverPalace/Content/Characters/MI_Leaf.uasset"
    parent = "SilverPalace/Content/Materials/MI_Parent.uasset"
    owner = "SilverPalace/Content/Materials/M_Base.uasset"
    for relative in (leaf, parent, owner):
        target = mount / Path(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"package")

    responses = {
        ("uex_preview_asset", leaf): receipt(
            preview(leaf, "/Game/Materials/MI_Parent.MI_Parent"),
            tmp_path / "evidence", "leaf",
        ),
        ("uex_material_shader_maps", parent): receipt(
            shader_map(parent, parent="Material'/Game/Materials/M_Base.M_Base'"),
            tmp_path / "evidence-parent-shader",
            "parent-shader",
        ),
        ("uex_preview_asset", parent): receipt(
            preview(parent, "Material'/Game/Materials/M_Base.M_Base'"),
            tmp_path / "evidence", "parent",
        ),
        ("uex_material_shader_maps", owner): receipt(shader_map(owner, "parsed")),
    }
    calls = []

    async def run(operation, arguments, unused_timeout):
        calls.append((operation, arguments["asset"]))
        return responses[(operation, arguments["asset"])]

    result = asyncio.run(material_parent_chain.follow_material_parent_chain(
        leaf,
        receipt(shader_map(leaf, embed_preview=False)),
        run_operation=run,
        asset_root=root,
        mount_root=mount,
        game_name="SilverPalace",
        timeout_seconds=0.0,
    ))

    assert result["shaderMapOwner"]["assetPath"] == owner
    assert result["shaderMapOwner"]["variantCount"] == 1
    assert [item["assetPath"] for item in result["materialChain"]] == [leaf, parent, owner]
    assert [item["assetPath"] for item in result["materialOverrideChain"]] == [leaf, parent]
    assert calls == [
        # The initial fixture intentionally lacks an embedded preview and proves
        # compatibility with older readers.  Parent reads reuse their exact-package
        # ShaderMap receipts instead of remounting the whole tree for preview.
        ("uex_preview_asset", leaf),
        ("uex_material_shader_maps", parent),
        ("uex_material_shader_maps", owner),
    ]
    assert result["chainDiagnostics"] == []


def test_embedded_exact_package_previews_avoid_every_tree_mount(tmp_path):
    root = tmp_path / "unpacked"
    mount = root / "raw"
    leaf = "SilverPalace/Content/Characters/MI_Leaf.uasset"
    parent = "SilverPalace/Content/Materials/MI_Parent.uasset"
    owner = "SilverPalace/Content/Materials/M_Base.uasset"
    for relative in (leaf, parent, owner):
        target = mount / Path(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"package")

    responses = {
        parent: receipt(shader_map(
            parent, parent="Material'/Game/Materials/M_Base.M_Base'"
        )),
        owner: receipt(shader_map(owner, "parsed")),
    }
    calls = []

    async def run(operation, arguments, unused_timeout):
        calls.append((operation, arguments["asset"]))
        assert operation == "uex_material_shader_maps"
        return responses[arguments["asset"]]

    result = asyncio.run(material_parent_chain.follow_material_parent_chain(
        leaf,
        receipt(shader_map(leaf, parent="/Game/Materials/MI_Parent.MI_Parent")),
        run_operation=run,
        asset_root=root,
        mount_root=mount,
        game_name="SilverPalace",
        timeout_seconds=0.0,
    ))

    assert result["shaderMapOwner"]["assetPath"] == owner
    assert calls == [
        ("uex_material_shader_maps", parent),
        ("uex_material_shader_maps", owner),
    ]
    assert [item["previewSource"] for item in result["materialChain"][:-1]] == [
        "shader-map-exact-package",
        "shader-map-exact-package",
    ]


def test_game_parent_is_not_widened_above_a_configured_content_subtree(tmp_path):
    root = tmp_path / "Munin"
    root.mkdir()
    leaf = "Default/Materials/MI_Leaf.uasset"

    async def run(operation, arguments, unused_timeout):
        assert (operation, arguments["asset"]) == ("uex_preview_asset", leaf)
        return receipt(preview(leaf, "/Game/Shared/M_Parent"))

    result = asyncio.run(material_parent_chain.follow_material_parent_chain(
        leaf,
        receipt(shader_map(leaf, embed_preview=False)),
        run_operation=run,
        asset_root=root,
        mount_root=root,
        game_name="",
        timeout_seconds=0.0,
    ))

    assert result["shaderMapOwner"] is None
    assert result["chainDiagnostics"][0]["code"] == "parent-out-of-scope"
    assert "complete unpacked root" in result["chainDiagnostics"][0]["message"]


def test_direct_mcp_facade_uses_chain_resolver(tmp_path, monkeypatch):
    root = tmp_path / "unpacked"
    mount = root / "raw"
    leaf = "SilverPalace/Content/Characters/MI_Leaf.uasset"
    owner = "SilverPalace/Content/Materials/M_Base.uasset"
    for relative in (leaf, owner):
        target = mount / Path(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"package")

    async def run(operation, arguments, timeout_seconds=0.0):
        asset = arguments["asset"]
        if operation == "uex_preview_asset":
            payload = receipt(preview(leaf, "/Game/Materials/M_Base"))
        elif asset == owner:
            payload = receipt(shader_map(owner, "parsed"))
        else:
            payload = receipt(shader_map(leaf, embed_preview=False))
        return json.dumps(payload)

    monkeypatch.setattr(server, "run_asset_tool_operation", run)
    monkeypatch.setattr(server, "_unpacked_asset_root", lambda: (root, {}))
    monkeypatch.setattr(uex_profiles, "ensure_profile", lambda unused: {
        "mountRoot": str(mount), "gameName": "SilverPalace"
    })

    result = json.loads(asyncio.run(server.uex_material_shader_maps(leaf)))
    assert "error" not in result, result
    assert result["shaderMapOwner"]["assetPath"] == owner
    assert result["followedParentChain"] is True


def test_generic_asset_operation_also_composes_parent_chain(tmp_path, monkeypatch):
    from src import tool_runner

    root = tmp_path / "unpacked"
    mount = root / "raw"
    leaf = "SilverPalace/Content/Characters/MI_Leaf.uasset"
    owner = "SilverPalace/Content/Materials/M_Base.uasset"
    for relative in (leaf, owner):
        target = mount / Path(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"package")
    operations = [{"name": name, "program": "uex.exe", "kind": "external"}
                  for name in ("uex_material_shader_maps", "uex_preview_asset")]

    def run_operation(operation, supplied, **unused):
        asset = supplied["asset"]
        if operation["name"] == "uex_preview_asset":
            return receipt(preview(leaf, "/Game/Materials/M_Base"))
        return receipt(shader_map(
            asset,
            "parsed" if asset == owner else "empty",
            embed_preview=asset == owner,
        ))

    monkeypatch.setattr(server, "_declared_asset_operations", lambda: operations)
    monkeypatch.setattr(server, "_uex_host_slots", lambda unused: {})
    monkeypatch.setattr(server, "_unpacked_asset_root", lambda: (root, {}))
    monkeypatch.setattr(uex_profiles, "ensure_profile", lambda unused: {
        "mountRoot": str(mount), "gameName": "SilverPalace"
    })
    monkeypatch.setattr(tool_runner, "run_operation", run_operation)
    catalog_calls = []

    def unexpected_catalog_authorization(**unused):
        catalog_calls.append(True)
        raise AssertionError("an exact asset read must not build catalog authorization")

    monkeypatch.setattr(
        server, "_active_asset_catalog_context", unexpected_catalog_authorization
    )

    result = json.loads(asyncio.run(server.run_asset_tool_operation(
        "uex_material_shader_maps", {"asset": leaf}
    )))
    assert "error" not in result, result
    assert result["shaderMapOwner"]["assetPath"] == owner
    assert [item["assetPath"] for item in result["materialChain"]] == [leaf, owner]
    assert not catalog_calls
    assert result["evidencePolicy"] == "reader-facts-not-authorization"
    assert "assetBinding" not in result
    assert "assetContentProofs" not in result


def test_parent_path_traversal_and_missing_packages_are_rejected(tmp_path):
    root = tmp_path / "root"
    mount = root / "raw"
    mount.mkdir(parents=True)
    for reference in ("/Game/../Secret/M_Base", "/Game/Missing/M_Base"):
        try:
            material_parent_chain.resolve_parent_asset(
                reference,
                asset_root=root,
                mount_root=mount,
                game_name="Game",
            )
        except material_parent_chain.MaterialParentChainError:
            pass
        else:
            raise AssertionError("unsafe or missing Parent was accepted")


def test_requested_asset_accepts_root_relative_and_mount_relative_spelling(tmp_path):
    root = tmp_path / "unpacked"
    mount = root / "raw"
    relative = "SilverPalace/Content/Materials/M_Base.uasset"
    target = mount / Path(*relative.split("/"))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"package")

    assert material_parent_chain.normalize_requested_asset(
        "raw/" + relative,
        asset_root=root,
        mount_root=mount,
        game_name="SilverPalace",
    ) == relative
    assert material_parent_chain.normalize_requested_asset(
        relative,
        asset_root=root,
        mount_root=mount,
        game_name="SilverPalace",
    ) == relative
    assert material_parent_chain.normalize_requested_asset(
        "/Game/Materials/M_Base",
        asset_root=root,
        mount_root=mount,
        game_name="SilverPalace",
    ) == relative
