"""The bundled exporter and naming adapter agree on real UnityPy field types."""
import json
from pathlib import Path
import types

import pytest

from src import material_naming
from replaylab_agent.unitypy_reader_script import SCRIPT


@pytest.fixture
def script():
    pytest.importorskip("UnityPy")
    namespace = {"__name__": "unitypy_reader_test"}
    exec(compile(SCRIPT, "unitypy_reader.py", "exec"), namespace)
    return namespace


def material():
    from UnityPy.classes.generated import Material, UnityPropertySheet, UnityTexEnv, FastPropertyName
    from UnityPy.classes.math import Vector2f, ColorRGBA
    from UnityPy.classes.PPtr import PPtr

    return Material(
        m_Name="M_Rock", m_Shader=PPtr(m_FileID=0, m_PathID=0),
        m_SavedProperties=UnityPropertySheet(
            m_Floats=[(FastPropertyName(name="_Glossiness"), 0.4417283)],
            m_Colors=[("_Color", ColorRGBA(0.1273844, 0.9182736, 0.3746152, 1.0))],
            m_Ints=[("_Layer", 3)],
            m_TexEnvs=[("_MainTex", UnityTexEnv(
                m_Texture=PPtr(m_FileID=2, m_PathID=1234567890123),
                m_Scale=Vector2f(2.0, 3.0), m_Offset=Vector2f(0.25, 0.5)))]))


def test_typed_unitypy_material_exports_to_host_with_full_unresolved_pointer(script):
    document = script["_material_json"](material(), source_file="bundle/scene::CAB-001")
    # A missing external does not erase the serialized reference or numeric fields.
    document = json.loads(json.dumps(document, allow_nan=False))
    props = document["m_SavedProperties"]
    assert props["m_TexEnvs"][0]["second"]["m_Texture"] == {
        "m_FileID": 2, "m_PathID": 1234567890123, "m_Name": ""}
    parsed = material_naming.parse_material_parameters(document)
    assert parsed["parameterSource"] == "unity"
    assert parsed["scalars"][0] == {"name": "_Glossiness", "value": 0.4417283}
    assert parsed["integerParameters"] == [{"name": "_Layer", "value": 3}]
    texture = parsed["textures"][0]
    assert texture["objectReference"] == {"fileId": 2, "pathId": 1234567890123,
        "sourceFile": "bundle/scene::CAB-001", "isNull": False}
    assert texture["scale"] == [2.0, 3.0]
    assert texture["offset"] == [0.25, 0.5]
    result = material_naming.propose_member_names(parsed, [{"bind": 7, "space": 2,
        "floats": [0.0, 0.4417283]}], fill_gaps=False)
    assert [(p["parameter"], p["byteOffset"]) for p in result["proposals"]] == [("_Glossiness", 4)]


def test_describe_and_export_keep_materials_in_different_serialized_files_separate(script, tmp_path, capsys, monkeypatch):
    rows = []
    for name in ("CAB-one", "CAB-two"):
        rows.append(types.SimpleNamespace(type=types.SimpleNamespace(name="Material"), path_id=5,
            assets_file=types.SimpleNamespace(name=name), byte_size=80, read=material))
    env = types.SimpleNamespace(objects=rows, container={"mat/a": rows[0], "mat/b": rows[1]}, files={})
    monkeypatch.setattr(script["UnityPy"], "load", lambda unused: env)
    source = str(tmp_path / "one.bundle")
    script["describe"](source, 10)
    described = json.loads(capsys.readouterr().out)
    assert [row["container"] for row in described["objects"]] == ["mat/a", "mat/b"]
    assert len(described["exports"]) == 2
    with pytest.raises(material_naming.MaterialNamingError, match="multiple material"):
        material_naming.parse_material_parameters(described)
    output = tmp_path / "out"
    script["export"](source, str(output), "Material", "M_Rock")
    capsys.readouterr()
    index = json.loads((output / "index.json").read_text(encoding="utf-8"))
    assert index["written"] == 2
    assert len({row["file"] for row in index["entries"]}) == 2
    for row in index["entries"]:
        document = json.loads((output / row["file"]).read_text(encoding="utf-8"))
        parsed = material_naming.parse_material_parameters(document)
        assert parsed["textures"][0]["objectReference"]["sourceFile"] == row["sourceFile"]


def test_single_material_describe_is_directly_consumable(script, tmp_path, monkeypatch, capsys):
    row = types.SimpleNamespace(type=types.SimpleNamespace(name="Material"), path_id=5,
        assets_file=types.SimpleNamespace(name="CAB-one"), byte_size=80, read=material)
    monkeypatch.setattr(script["UnityPy"], "load", lambda unused: types.SimpleNamespace(
        objects=[row], container={}, files={}))
    script["describe"](str(tmp_path / "one.bundle"), 10)
    document = json.loads(capsys.readouterr().out)
    assert document["materialsTruncated"] is False
    parsed = material_naming.parse_material_parameters(document)
    assert parsed["objectName"] == "M_Rock"
    assert parsed["scalars"][0]["name"] == "_Glossiness"
