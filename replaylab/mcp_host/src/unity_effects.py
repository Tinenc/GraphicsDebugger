"""Bounded execution plans for captured compute/fullscreen effects.

Missing history can be initialized or warmed up by an Agent-authored algorithm.
Those inputs carry their actual inferred/captured/native provenance; this module
never labels a chosen initialization as observed capture bytes.
"""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping


def validate_effect(value: Mapping, assets: list[dict]) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("unity.effect must describe its resources and executable passes")
    effect = copy.deepcopy(dict(value))
    resources, passes = effect.get("resources"), effect.get("passes")
    if not isinstance(resources, list) or not 1 <= len(resources) <= 128:
        raise ValueError("effect.resources requires 1 to 128 resources")
    if not isinstance(passes, list) or not 1 <= len(passes) <= 128:
        raise ValueError("effect.passes requires 1 to 128 executable passes")
    names, pixel_budget = set(), 0
    declared = {asset["path"]: asset.get("kind") for asset in assets}
    for resource in resources:
        if not isinstance(resource, dict) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", str(resource.get("name", ""))):
            raise ValueError("effect resource names must be stable identifiers")
        if resource["name"] in names:
            raise ValueError("duplicate effect resource")
        names.add(resource["name"])
        for field in ("width", "height"):
            if type(resource.get(field)) is not int or not 1 <= resource[field] <= 16384:
                raise ValueError("effect resources require explicit bounded dimensions")
        pixel_budget += resource["width"] * resource["height"]
        if not isinstance(resource.get("format"), str) or not resource["format"]:
            raise ValueError("effect resource requires an explicit Unity GraphicsFormat")
        initial = resource.get("initialization")
        if not isinstance(initial, dict) or initial.get("kind") not in {"clear", "texture"}:
            raise ValueError("effect resource needs an explicit clear or texture initialization")
        provenance = initial.get("provenance")
        if (not isinstance(provenance, dict) or provenance.get("kind") not in {"captured", "inferred", "unity-native"}
                or not isinstance(provenance.get("reason"), str) or not provenance["reason"].strip()):
            raise ValueError("effect initialization must explain its captured/inferred/native origin")
        if initial["kind"] == "clear":
            color = initial.get("value")
            if (not isinstance(color, list) or len(color) != 4 or any(type(v) not in (float, int) or not math.isfinite(v) for v in color)):
                raise ValueError("clear initialization requires four finite components")
        else:
            texture_property, texture_asset = initial.get("textureProperty"), initial.get("textureAsset")
            if bool(texture_property) == bool(texture_asset):
                raise ValueError("texture initialization selects exactly one imported textureProperty or manifested textureAsset")
            if texture_property and not isinstance(texture_property, str):
                raise ValueError("textureProperty must be a property name")
            if texture_asset and (not isinstance(texture_asset, str) or declared.get(texture_asset) != "data"):
                raise ValueError("textureAsset must name a manifested data asset")
        if resource.get("historyFrom") is not None and not isinstance(resource["historyFrom"], str):
            raise ValueError("historyFrom selects the previous iteration output")
    if pixel_budget > 64 * 1024 * 1024:
        raise ValueError("effect resource allocation exceeds the pixel budget")
    for resource in resources:
        if resource.get("historyFrom") and (resource["historyFrom"] not in names or resource["historyFrom"] == resource["name"]):
            raise ValueError("history resources require a separate produced source")
    ids = set()
    for step in passes:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not step["id"] or step["id"] in ids:
            raise ValueError("effect pass ids must be unique")
        ids.add(step["id"])
        kind = step.get("kind")
        if kind not in {"compute", "fullscreen", "copy", "custom"}:
            raise ValueError("effect pass kind must be compute, fullscreen, copy or custom")
        if step.get("output") not in names:
            raise ValueError("effect pass output must select an allocated resource")
        if kind in {"compute", "fullscreen", "custom"}:
            expected = {"compute": "compute", "fullscreen": "shader", "custom": "script"}[kind]
            if declared.get(step.get("asset")) != expected:
                raise ValueError("effect pass implementation must name a manifested " + expected + " asset")
        if kind == "compute" and not step.get("kernel"):
            raise ValueError("compute pass needs its actual kernel")
        if kind == "custom" and not step.get("componentType"):
            raise ValueError("custom pass needs an IBreadCaptureEffectPass componentType")
        if not step.get("entryPoint") or not step.get("insertionPoint"):
            raise ValueError("effect pass records its actual entryPoint and insertionPoint")
        if step["insertionPoint"] != "AfterRendering":
            raise ValueError("the effect runner executes at AfterRendering; other insertion points require an actual RendererFeature or CustomPass adapter")
        if kind == "copy":
            implementation = "Packages/com.bread.renderdoc/Runtime/BreadCaptureEffect.cs"
            if step.get("asset") not in (None, implementation):
                raise ValueError("copy implementation must identify the actual BreadCaptureEffect.cs package asset")
            step["asset"] = implementation
        if step.get("source") is not None and step["source"] not in names:
            raise ValueError("effect pass source is unavailable")
        if kind in {"fullscreen", "copy"} and step.get("source") == step["output"]:
            raise ValueError("fullscreen/copy passes require separate input and output resources")
        if kind == "copy" and not step.get("source"):
            raise ValueError("copy pass requires a source")
        for key in ("reads", "writes"):
            if not isinstance(step.get(key), list) or any(not isinstance(v, str) for v in step[key]):
                raise ValueError("effect pass needs explicit resource-version reads and writes")
        if not step.get("outputVersion") or step["outputVersion"] not in step["writes"]:
            raise ValueError("effect outputVersion must identify its written version")
        textures = step.get("textures", {})
        if not isinstance(textures, dict) or any(not isinstance(k, str) or v not in names for k, v in textures.items()):
            raise ValueError("effect texture bindings select resource names")
        groups = step.get("groups")
        if groups is not None and (not isinstance(groups, list) or len(groups) != 3 or any(type(v) is not int or not 1 <= v <= 65535 for v in groups)):
            raise ValueError("dispatch groups require three positive bounded integers")
    warmup = effect.get("warmupFrames", 0)
    if type(warmup) is not int or not 0 <= warmup <= 64:
        raise ValueError("effect warmupFrames must be between zero and 64")
    if effect.get("output") not in names:
        raise ValueError("effect.output must select its visible result")
    effect["warmupFrames"] = warmup
    return effect
