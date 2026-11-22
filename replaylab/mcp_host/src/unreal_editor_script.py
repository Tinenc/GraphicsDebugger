"""Fixed Bread Editor script. Executed only inside the selected Unreal Editor."""
import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import traceback

import unreal as ue


EDIT = ue.MaterialEditingLibrary
ASSETS = ue.EditorAssetLibrary
CAPTURES = {
    "final": ("SCS_FINAL_COLOR_LDR", "RTF_RGBA8", "display-srgb"),
    "final-hdr": ("SCS_FINAL_COLOR_HDR", "RTF_RGBA32F", "final-linear-hdr"),
    "scene-color": ("SCS_SCENE_COLOR_HDR_NO_ALPHA", "RTF_RGBA32F", "scene-linear-hdr"),
    "base-color": ("SCS_BASE_COLOR", "RTF_RGBA32F", "gbuffer-base-color"),
    "world-normal": ("SCS_NORMAL", "RTF_RGBA32F", "scene-capture-normal"),
    "scene-depth": ("SCS_SCENE_DEPTH", "RTF_RGBA32F", "scene-depth-ue-units"),
}
INPUT_TYPES = {
    "scalar": "FUNCTION_INPUT_SCALAR", "vector3": "FUNCTION_INPUT_VECTOR3",
    "vector2": "FUNCTION_INPUT_VECTOR2", "texture2d": "FUNCTION_INPUT_TEXTURE2D",
}
OUTPUT_TYPES = {"scalar": "CMOT_FLOAT1", "vector2": "CMOT_FLOAT2", "vector3": "CMOT_FLOAT3"}


def require(value, message):
    if not value:
        raise RuntimeError(message)
    return value


def preflight_materials(materials, world=None, log_path=None, include_level=True, recompile=True):
    """Finish active material resources before allocating/capturing a diagnostic batch.

    GetStatistics finishes the actual material resource's compilation in the installed
    UE implementation. A missing/zero resource is unavailable, never a success claim.
    Functions are checked through their consuming materials and static instances.
    """
    get_stats = getattr(EDIT, "get_statistics", None)
    if get_stats is None:
        return {"status": "unavailable", "passed": False, "diagnostics": ["MaterialEditingLibrary.get_statistics is unavailable"]}
    candidates = list(materials)
    if world is not None and include_level:
        for actor in ue.get_editor_subsystem(ue.EditorActorSubsystem).get_all_level_actors():
            for mesh in actor.get_components_by_class(ue.MeshComponent):
                candidates.extend(mesh.get_material(i) for i in range(mesh.get_num_materials()))
            if isinstance(actor, ue.PostProcessVolume):
                settings = actor.get_editor_property("settings")
                blends = settings.get_editor_property("weighted_blendables").get_editor_property("array")
                candidates.extend(item.get_editor_property("object") for item in blends)
    candidates = {m.get_path_name(): m for m in candidates if m is not None}
    if not candidates:
        return {"status": "unavailable", "passed": False, "diagnostics": ["No consuming materials were selected"]}
    def flush():
        if world is not None:
            ue.SystemLibrary.execute_console_command(world, "FlushLog")
    flush()
    log = Path(log_path) if log_path else None
    offset = log.stat().st_size if log and log.is_file() else 0
    rows, errors, parents = [], [], set()
    for path, material in sorted(candidates.items()):
        try:
            parent = material
            visited = set()
            while not isinstance(parent, ue.Material):
                name = parent.get_path_name()
                require(name not in visited, "Cyclic material parent chain")
                visited.add(name)
                parent = require(parent.get_editor_property("parent"), "Material instance has no parent")
            if recompile and parent.get_path_name() not in parents:
                EDIT.recompile_material(parent)
                parents.add(parent.get_path_name())
            stats = get_stats(material)
            pixel = stats.num_pixel_shader_instructions
            vertex = stats.num_vertex_shader_instructions
            passed = pixel > 0
            rows.append({"material": path, "parent": parent.get_path_name(), "passed": passed,
                         "pixelInstructions": pixel, "vertexInstructions": vertex})
            if not passed:
                errors.append("No compiled pixel shader resource: " + path)
        except Exception as error:
            rows.append({"material": path, "passed": False})
            errors.append(path + ": " + str(error))
    flush()
    log_checked = bool(log and log.is_file())
    if log_checked:
        with log.open("rb") as stream:
            stream.seek(offset)
            text = stream.read(4 * 1024 * 1024 + 1)
        if len(text) > 4 * 1024 * 1024:
            errors.append("Compile log exceeds preflight diagnostic limit")
        for line in text.decode("utf-8", errors="replace").splitlines():
            if re.search(r"failed to compile material|default material will be used|error \[SM\d", line, re.I):
                errors.append(line.strip())
    # Missing diagnostic access is explicit: statistics alone cannot exclude fallback/stale resources.
    if not log_checked:
        errors.append("Compile log unavailable; fallback diagnostics could not be checked")
    return {"schema": "bread-unreal-material-preflight/v1", "status": "passed" if not errors else "blocked",
            "passed": not errors, "method": "synchronous-resource-statistics-and-current-log",
            "logChecked": log_checked, "materials": rows, "diagnostics": errors[:40],
            "scope": "active editor shader platform and current consuming instances; no other-platform or visual-fidelity claim"}


def prop(obj, name, value):
    obj.set_editor_property(name, value)
    return obj


def create(folder, name, cls, factory):
    return require(ue.AssetToolsHelpers.get_asset_tools().create_asset(name, folder, cls, factory()),
                   "Could not create " + folder + "/" + name)


def save(asset):
    require(ASSETS.save_loaded_asset(asset, only_if_is_dirty=False),
            "Could not save " + asset.get_path_name())
    return asset


def expr(graph, cls, x, y, description):
    if isinstance(graph, ue.MaterialFunction):
        node = EDIT.create_material_expression_in_function(graph, cls, x, y)
    else:
        node = EDIT.create_material_expression(graph, cls, x, y)
    return prop(require(node, "Could not create expression " + cls.__name__), "desc", description)


def wire(source, dest, pin, output=""):
    require(EDIT.connect_material_expressions(source, output, dest, pin),
            "Material pin connection failed: " + dest.get_name() + "." + pin)


def make_function(root, stage, native_operators):
    function = create(root + "/Materials/Functions", "MF_" + stage["id"],
                      ue.MaterialFunction, ue.MaterialFunctionFactoryNew)
    prop(function, "description", stage["comment"])
    input_nodes = []
    for i, item in enumerate(stage["inputs"]):
        node = expr(function, ue.MaterialExpressionFunctionInput, -420, i * 110, item["comment"])
        prop(node, "input_name", item["name"])
        prop(node, "input_type", getattr(ue.FunctionInputType, INPUT_TYPES[item["type"]]))
        prop(node, "sort_priority", i)
        if item["type"] == "texture2d":
            preview = expr(function, ue.MaterialExpressionTextureObject, -710, i * 110,
                           "Engine texture used only in the function preview")
            prop(preview, "texture", require(ue.load_asset("/Engine/EngineResources/DefaultTexture"),
                                             "The engine preview texture is missing"))
            wire(preview, node, "")
        input_nodes.append(node)
    operator = stage.get("operator")
    if operator:
        cls, pins = native_operators[operator]
        node = expr(function, getattr(ue, "MaterialExpression" + cls), -80, 0, stage["comment"])
    else:
        node = expr(function, ue.MaterialExpressionCustom, -80, 0, stage["comment"])
        prop(node, "description", stage["id"])
        prop(node, "code", stage["code"])
        prop(node, "output_type", getattr(ue.CustomMaterialOutputType, OUTPUT_TYPES[stage["outputType"]]))
        pins = [item["name"] for item in stage["inputs"]]
        custom_inputs = [prop(ue.CustomInput(), "input_name", pin) for pin in pins]
        prop(node, "inputs", custom_inputs)
    for pin, input_node in zip(pins, input_nodes):
        wire(input_node, node, pin)
    output = expr(function, ue.MaterialExpressionFunctionOutput, 280, 0, "Result of " + stage["id"])
    prop(output, "output_name", "Result")
    wire(node, output, "")
    comment = expr(function, ue.MaterialExpressionComment, -470, -85, stage["comment"])
    prop(comment, "text", stage["comment"])
    EDIT.update_material_function(function)
    return save(function)


def parameter_nodes(graph, bindings, textures):
    nodes = {}
    for i, (name, binding) in enumerate(bindings.items()):
        kind = binding["type"]
        description = binding["comment"]
        y = i * 120
        if kind == "scalar":
            node = expr(graph, ue.MaterialExpressionScalarParameter, -850, y, description)
            prop(node, "default_value", binding["value"])
        elif kind == "vector3":
            node = expr(graph, ue.MaterialExpressionVectorParameter, -850, y, description)
            prop(node, "default_value", ue.LinearColor(*binding["value"], 1))
        else:
            node = expr(graph, ue.MaterialExpressionTextureObjectParameter, -850, y, description)
            texture = textures[binding["texture"]]
            prop(node, "texture", texture)
            color = isinstance(texture, ue.Texture2D) and texture.get_editor_property("srgb")
            prop(node, "sampler_type", getattr(ue.MaterialSamplerType,
                  "SAMPLERTYPE_COLOR" if color else "SAMPLERTYPE_LINEAR_COLOR"))
        nodes[name] = prop(node, "parameter_name", name)
    return nodes


def make_surface(root, surface, textures, native_operators, scene_inputs, matte_inputs):
    folder = root + "/Materials"
    material = create(folder, "M_" + surface["id"], ue.Material, ue.MaterialFactoryNew)
    model = surface["template"]
    prop(material, "shading_model", getattr(ue.MaterialShadingModel,
         "MSM_UNLIT" if model in {"unlit", "post-process", "render-target"} else "MSM_DEFAULT_LIT"))
    if model == "post-process":
        prop(material, "material_domain", ue.MaterialDomain.MD_POST_PROCESS)
        if surface.get("location"):
            prop(material, "blendable_location", getattr(ue.BlendableLocation, surface["location"]))
    nodes = parameter_nodes(material, surface["parameters"], textures)
    builtins = {
        "PixelNormalWS": "PixelNormalWS", "VertexNormalWS": "VertexNormalWS",
        "CameraVectorWS": "CameraVectorWS", "WorldPosition": "WorldPosition",
        "UV0": "TextureCoordinate", "ScreenUV": "ScreenPosition",
    }
    sources = {i["source"] for stage in surface["functions"] for i in stage["inputs"]}
    for name in sorted(sources & scene_inputs.keys()):
        node = expr(material, ue.MaterialExpressionSceneTexture, -850, (len(nodes) + 1) * 120, name)
        prop(node, "scene_texture_id", getattr(ue.SceneTextureId, scene_inputs[name]))
        nodes[name] = node
    for name in sorted(sources & builtins.keys()):
        node = expr(material, getattr(ue, "MaterialExpression" + builtins[name]), -850,
                    (len(nodes) + 1) * 120, "UE input " + name)
        nodes[name] = node
    for i, stage in enumerate(surface["functions"]):
        function = make_function(root, {**stage, "id": surface["id"] + "_" + stage["id"]}, native_operators)
        node = expr(material, ue.MaterialExpressionMaterialFunctionCall, -360 + (i % 4) * 320,
                    (i // 4) * 400, stage["comment"])
        node.set_material_function(function)
        comment = expr(material, ue.MaterialExpressionComment, -380 + (i % 4) * 320,
                       -75 + (i // 4) * 400, stage["comment"])
        prop(comment, "text", stage["comment"])
        for item in stage["inputs"]:
            source = item["source"]
            wire(nodes[source], node, item["name"], "ViewportUV" if source == "ScreenUV" else "")
        nodes[stage["id"]] = node
    outputs = dict(surface["outputs"])
    if surface["nativeSpecular"] == "matte-diffuse":
        for i, (pin, value) in enumerate(matte_inputs[model].items()):
            key = "$matte" + str(i)
            node = expr(material, ue.MaterialExpressionConstant, 330, 470 + i * 95,
                        "Suppress the native specular lobe while retaining lit diffuse.")
            nodes[key] = prop(node, "r", value)
            outputs[pin] = key
    if model == "substrate-toon":
        toon_cls = getattr(ue, "MaterialExpressionSubstrateToonBSDF", None)
        require(toon_cls, "The selected editor has no Substrate Toon BSDF")
        toon = expr(material, toon_cls, 680, 0, "UE Substrate evaluates scene lights using the Toon Profile.")
        if surface.get("toonProfile"):
            prop(toon, "toon_profile", require(ue.load_asset(surface["toonProfile"]), "Toon Profile not found"))
        for pin, source in outputs.items():
            wire(nodes[source], toon, pin)
        require(EDIT.connect_material_property(toon, "", ue.MaterialProperty.MP_FRONT_MATERIAL),
                "Substrate Front Material connection failed")
    else:
        for pin, source in outputs.items():
            require(EDIT.connect_material_property(nodes[source], "", getattr(ue.MaterialProperty, pin)),
                    "Material output connection failed: " + pin)
    EDIT.recompile_material(material)
    save(material)
    instance = create(folder + "/Instances", "MI_" + surface["id"], ue.MaterialInstanceConstant,
                      ue.MaterialInstanceConstantFactoryNew)
    EDIT.set_material_instance_parent(instance, material)
    overrides = []
    # UE-291403: Editor setters may report false after a successful update.
    for name, value in surface["instance"].items():
        kind = surface["parameters"][name]["type"]
        if kind == "scalar":
            EDIT.set_material_instance_scalar_parameter_value(instance, name, value)
        elif kind == "vector3":
            EDIT.set_material_instance_vector_parameter_value(instance, name, ue.LinearColor(*value, 1))
        else:
            EDIT.set_material_instance_texture_parameter_value(instance, name, textures[value])
        overrides.append((name, kind, value))
    EDIT.update_material_instance(instance)
    for name, kind, value in overrides:
        if kind == "scalar":
            actual = EDIT.get_material_instance_scalar_parameter_value(instance, name)
            matched = math.isclose(actual, value, rel_tol=1e-6, abs_tol=1e-7)
        elif kind == "vector3":
            actual = EDIT.get_material_instance_vector_parameter_value(instance, name)
            matched = all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7)
                          for a, b in zip((actual.r, actual.g, actual.b), value))
        else:
            matched = EDIT.get_material_instance_texture_parameter_value(instance, name) == textures[value]
        require(matched, "Material instance readback mismatch: " + name)
    return save(instance)


def import_asset(path, folder, name):
    task = ue.AssetImportTask()
    for key, value in {"filename": str(path), "destination_path": folder, "destination_name": name,
                       "automated": True, "replace_existing": False, "save": False}.items():
        prop(task, key, value)
    ue.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return task.get_objects()


def captured_mesh_actor(root, path, name, label, transform, material):
    imported = import_asset(path, root + "/Meshes", name)
    require(len(imported) == 1 and isinstance(imported[0], ue.StaticMesh),
            "The glTF import must produce one static mesh: " + name)
    mesh = imported[0]
    actor = spawn(ue.StaticMeshActor, label, transform)
    component = actor.get_component_by_class(ue.StaticMeshComponent)
    require(component.set_static_mesh(mesh), "Could not assign captured mesh: " + name)
    for slot in range(component.get_num_materials()):
        component.set_material(slot, material)
    save(mesh)


def texture_asset(root, item):
    found = [a for a in import_asset(item["path"], root + "/Textures", item["id"])
             if isinstance(a, ue.Texture2D)]
    require(len(found) == 1, "Texture import failed: " + item["id"])
    texture = found[0]
    prop(texture, "srgb", item["sRGB"])
    prop(texture, "compression_settings", ue.TextureCompressionSettings.TC_HDR if item["float"]
         else ue.TextureCompressionSettings.TC_VECTOR_DISPLACEMENTMAP)
    prop(texture, "mip_gen_settings", ue.TextureMipGenSettings.TMGS_NO_MIPMAPS)
    for field, value in item["sampler"].items():
        enum, prefix = (ue.TextureFilter, "TF_") if field == "filter" else (ue.TextureAddress, "TA_")
        prop(texture, field, getattr(enum, prefix + value))
    return save(texture)


def rotation(values):
    return ue.Rotator(roll=values[0], pitch=values[1], yaw=values[2])


def spawn(cls, label, transform=None):
    transform = transform or {}
    actor = require(ue.get_editor_subsystem(ue.EditorActorSubsystem).spawn_actor_from_class(
        cls, ue.Vector(*transform.get("location", [0, 0, 0])),
        rotation(transform.get("rotation", [0, 0, 0]))), "Could not spawn " + label)
    actor.set_actor_label(label)
    if "scale" in transform:
        actor.set_actor_scale3d(ue.Vector(*transform["scale"]))
    return actor


def renderer_cvars():
    return {name: ue.SystemLibrary.get_console_variable_int_value(name)
            for name in ("r.AntiAliasingMethod", "r.DynamicGlobalIlluminationMethod", "r.ReflectionMethod",
                         "r.Shadow.Virtual.Enable", "r.ForwardShading", "r.Substrate",
                         "r.GenerateMeshDistanceFields", "r.RayTracing",
                         "r.RayTracing.SceneCaptures", "r.Lumen.HardwareRayTracing")}


def probe(request):
    cvars = renderer_cvars()
    requirements = {
        "LevelEditorSubsystem": ("new_level", "load_level", "save_current_level"),
        "EditorActorSubsystem": ("spawn_actor_from_class", "destroy_actor", "get_all_level_actors"),
        "MaterialEditingLibrary": ("create_material_expression", "create_material_expression_in_function",
                                   "connect_material_expressions", "connect_material_property", "recompile_material"),
        "RenderingLibrary": ("create_render_target2d", "draw_material_to_render_target", "export_render_target"),
        "SceneCaptureComponent2D": ("capture_scene",),
    }
    missing = [f"{name}.{member}" for name, members in requirements.items() for member in members
               if not hasattr(getattr(ue, name, None), member)]
    toon_node = hasattr(ue, "MaterialExpressionSubstrateToonBSDF")
    lit = not missing and hasattr(ue.MaterialShadingModel, "MSM_DEFAULT_LIT")
    unlit = not missing and hasattr(ue.MaterialShadingModel, "MSM_UNLIT")
    templates = {"default-lit": lit, "unlit": unlit, "post-process": unlit, "render-target": unlit,
                 "substrate-toon": lit and toon_node and cvars["r.Substrate"] != 0}
    forward = bool(cvars["r.ForwardShading"])
    modes = [mode for mode, (source, _, _) in CAPTURES.items()
             if hasattr(ue.SceneCaptureSource, source) and
             (not forward or mode not in {"base-color", "world-normal"})]
    modes += ["material", "live"] if not missing else []

    def methods(enum):
        candidates = ("none",) if forward else ("none", "screen-space", "lumen")
        return ["project", *(name for name in candidates if hasattr(enum, name.replace("-", "_").upper()))]

    return {"editorVersion": ue.SystemLibrary.get_engine_version(), "missingApis": missing,
            "templates": templates, "toonNodePresent": toon_node,
            "toonRequirement": "Requires the installed Toon BSDF, enabled Substrate and a compatible GBuffer",
            "lightTypes": [key for key, cls in request["lightClasses"].items() if hasattr(ue, cls)],
            "lightControls": [key for key, member in {"specularScale": "set_specular_scale",
                              "diffuseScale": "set_diffuse_scale", "indirectIntensity": "set_indirect_lighting_intensity"}.items()
                              if hasattr(ue.LightComponent, member)],
            "sceneCaptureModes": modes,
            "sceneCaptureNotes": {
                "scene-color": "Raw Scene Color bypasses the post-process pipeline and can omit native GI. It is not a complete engine-lit HDR observation.",
                "final-hdr": "Final Color HDR preserves the engine post-process pipeline used by native GI. Match exposure and presentation effects to the intended layer, and verify actual GI response.",
            },
            "rendererCVars": cvars,
            "forwardShading": forward,
            "giMethods": methods(getattr(ue, "DynamicGlobalIlluminationMethod", None)),
            "reflectionMethods": methods(getattr(ue, "ReflectionMethod", None)),
            "floatReadback": {"raw": hasattr(ue.RenderingLibrary, "read_render_target_raw"),
                              "exr": hasattr(ue, "ImageWriteBlueprintLibrary")},
            "materialCompilationPreflight": {"resourceStatistics": hasattr(EDIT, "get_statistics"),
                                              "requiresCurrentCompileLog": True},
            "blendableLocations": [name for name in dir(ue.BlendableLocation) if name.startswith("BL_")]}


def validate_capabilities(recipe, caps):
    require(not caps["missingApis"], "Missing editor APIs: " + ", ".join(caps["missingApis"]))
    require(caps["materialCompilationPreflight"]["resourceStatistics"],
            "Cannot validate compiled material resources before rendering on this Editor")
    for surface in recipe["surfaces"]:
        template = surface["template"]
        if not caps["templates"].get(template):
            detail = caps["toonRequirement"] if template == "substrate-toon" else "Unsupported material template"
            raise RuntimeError(template + ": " + detail)
        if surface.get("location"):
            require(surface["location"] in caps["blendableLocations"], "Unsupported blendable location")
    require(recipe["globalIllumination"] in caps["giMethods"], "Unsupported GI method")
    require(recipe["reflections"] in caps["reflectionMethods"], "Unsupported reflection method")
    for light in recipe["lights"]:
        require(light["kind"] in caps["lightTypes"], "Unsupported light type: " + light["kind"])
        for key in ("diffuseScale", "specularScale", "indirectIntensity"):
            if key in light:
                require(key in caps["lightControls"], "Missing native light control: " + key)
    if any(variant.get("lightSpecularScales") for variant in recipe["variants"]):
        require("specularScale" in caps["lightControls"], "Diagnostic native specular control is unavailable")
    for item in [*recipe["passes"], *recipe["liveCaptures"],
                 *(item for variant in recipe["variants"] for item in variant["passes"])]:
        require(item["mode"] in caps["sceneCaptureModes"], "Unsupported observation: " + item["mode"])


def light_component(actor):
    return actor.get_component_by_class(ue.LightComponentBase)


def configure_lights(recipe, classes, settings):
    if "directional" in recipe:
        item = recipe["directional"]
        light = spawn(ue.DirectionalLight, "Restoration key light", item)
        component = light_component(light)
        prop(component, "mobility", ue.ComponentMobility.MOVABLE)
        component.set_intensity(item["intensity"])
        component.set_light_color(ue.LinearColor(*item["color"], 1), srgb=False)
    for item in recipe["lights"]:
        light = spawn(getattr(ue, classes[item["kind"]]), "Restoration light " + item["id"], item)
        component = light_component(light)
        prop(component, "mobility", ue.ComponentMobility.MOVABLE)
        if "units" in item:
            prop(component, "intensity_units", getattr(ue.LightUnits, item["units"]))
        for key, field in settings.items():
            if key in item:
                prop(component, field, item[key])
        component.set_intensity(item["intensity"])
        component.set_light_color(ue.LinearColor(*item["color"], 1), srgb=False)
    sky = recipe.get("sky")
    if sky is None and "skyIntensity" in recipe:
        sky = {"intensity": recipe["skyIntensity"], "color": [1, 1, 1]}
    if sky is not None:
        actor = spawn(ue.SkyLight, "Restoration sky fill")
        component = light_component(actor)
        prop(component, "mobility", ue.ComponentMobility.MOVABLE)
        component.set_intensity(sky["intensity"])
        component.set_light_color(ue.LinearColor(*sky["color"], 1))
        if "indirectIntensity" in sky:
            prop(component, "indirect_lighting_intensity", sky["indirectIntensity"])
        if "cubemap" in sky:
            cube = require(ue.load_asset(sky["cubemap"]), "Sky cubemap not found")
            require(isinstance(cube, ue.TextureCube), "Sky cubemap must be a TextureCube")
            prop(component, "source_type", ue.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
            component.set_cubemap(cube)
        component.recapture_sky()


def exposure(settings, config, manual=False):
    if manual:
        prop(settings, "override_auto_exposure_method", True)
        prop(settings, "auto_exposure_method", ue.AutoExposureMethod.AEM_MANUAL)
        prop(settings, "override_auto_exposure_apply_physical_camera_exposure", True)
        prop(settings, "auto_exposure_apply_physical_camera_exposure", False)
    for name, value in config.items():
        prop(settings, "override_" + name, True)
        prop(settings, name, ue.Vector4(*value) if isinstance(value, list) else value)
    return settings


def lighting_override(settings, gi, reflections):
    for field, enum, value in (
        ("dynamic_global_illumination_method", ue.DynamicGlobalIlluminationMethod, gi),
        ("reflection_method", ue.ReflectionMethod, reflections),
    ):
        if value != "project":
            prop(settings, "override_" + field, True)
            prop(settings, field, getattr(enum, value.replace("-", "_").upper()))
    return settings


def observation_lighting(actors):
    volume = next((actor for actor in actors if isinstance(actor, ue.PostProcessVolume)
                   and actor.get_actor_label() == "Restoration post processing"
                   and actor.get_editor_property("enabled") and actor.get_editor_property("unbound")), None)
    saved = volume.get_editor_property("settings") if volume else None
    methods = []
    for field, enum, cvar in (
        ("dynamic_global_illumination_method", ue.DynamicGlobalIlluminationMethod, "r.DynamicGlobalIlluminationMethod"),
        ("reflection_method", ue.ReflectionMethod, "r.ReflectionMethod"),
    ):
        if saved and saved.get_editor_property("override_" + field):
            value = saved.get_editor_property(field).value
        else:
            value = ue.SystemLibrary.get_console_variable_int_value(cvar)
        methods.append(next((name for name in ("none", "screen-space", "lumen", "plugin")
                             if getattr(getattr(enum, name.replace("-", "_").upper(), None), "value", None) == value),
                            "project"))
    return tuple(methods)


def save_rt(world, rt, mode, directory, name, width, height, encoding):
    suffix = ".png" if mode == "final" else ".rgba32f"
    path = directory / (name + suffix)
    if mode == "final":
        ue.RenderingLibrary.export_render_target(world, rt, str(directory), path.name)
    else:
        reader = getattr(ue.RenderingLibrary, "read_render_target_raw", None)
        pixels = reader(world, rt, normalize=False) if reader else None
        if pixels is None:
            path = path.with_suffix(".exr")
            options = ue.ImageWriteOptions()
            prop(options, "format", ue.DesiredImageFormat.EXR)
            prop(options, "async_", False)
            prop(options, "overwrite_file", False)
            prop(options, "compression_quality", 1)
            try:
                ue.ImageWriteBlueprintLibrary.export_to_disk(rt, str(path), options)
            except RuntimeError as error:
                raise RuntimeError("EXR readback failed for " + name + ": " + str(error)) from error
            require(path.is_file() and path.stat().st_size > 0, "EXR readback failed: " + name)
            return {"name": name, "mode": mode, "path": str(path), "encoding": encoding,
                    "width": width, "height": height, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        require(len(pixels) == width * height, "Incomplete linear RT readback: " + name)
        data = array.array("f", (value for p in pixels for value in (p.r, p.g, p.b, p.a)))
        with path.open("wb") as stream:
            data.tofile(stream)
    require(path.is_file() and path.stat().st_size > 0, "Missing RT output " + name)
    return {"name": name, "mode": mode, "path": str(path), "encoding": encoding,
            "width": width, "height": height, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def live_capture(root, item, objects):
    rt = create(root + "/Textures/RenderTargets", "RT_" + item["id"],
                ue.TextureRenderTarget2D, ue.TextureRenderTargetFactoryNew)
    prop(rt, "size_x", item["size"][0])
    prop(rt, "size_y", item["size"][1])
    prop(rt, "render_target_format", ue.TextureRenderTargetFormat.RTF_RGBA32F)
    save(rt)
    actor = spawn(ue.SceneCapture2D, "Bread RT " + item["id"], item)
    comp = actor.get_component_by_class(ue.SceneCaptureComponent2D)
    prop(comp, "primitive_render_mode", ue.SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST)
    for name in item["showOnly"]:
        comp.show_only_actor_components(objects[name])
    prop(comp, "texture_target", rt)
    prop(comp, "capture_source", getattr(ue.SceneCaptureSource, CAPTURES[item["mode"]][0]))
    prop(comp, "fov_angle", item["fov"])
    if item.get("orthoWidth"):
        prop(comp, "projection_type", ue.CameraProjectionMode.ORTHOGRAPHIC)
        prop(comp, "ortho_width", item["orthoWidth"])
    comp.capture_scene()
    return rt


def capture(world, camera, post, effects, materials, live, directory, width, height, manual, lighting,
            recompile=True):
    preflight = preflight_materials(materials.values(), world, Path(directory) / "editor.log", True, recompile)
    # Persist the failure before stopping; no image from this batch can be accepted as a fallback render.
    report_path = Path(directory, "material-preflight-" + effects[0]["id"] + ".json")
    report_path.write_text(json.dumps(preflight), encoding="utf-8")
    require(preflight["passed"], "Material compilation preflight blocked capture: " + json.dumps(preflight))
    view = spawn(ue.SceneCapture2D, "Bread diagnostic view", camera)
    comp = view.get_component_by_class(ue.SceneCaptureComponent2D)
    prop(comp, "capture_every_frame", False)
    prop(comp, "capture_on_movement", False)
    prop(comp, "always_persist_rendering_state", True)
    prop(comp, "fov_angle", camera["fov"])
    prop(comp, "post_process_blend_weight", 1.0)
    capture_ray_tracing = None
    if hasattr(comp, "use_ray_tracing_if_enabled"):
        # Stock SceneCapture defaults to no ray tracing independently of the main view.
        prop(comp, "use_ray_tracing_if_enabled",
             bool(ue.SystemLibrary.get_console_variable_int_value("r.RayTracing")))
        capture_ray_tracing = comp.get_editor_property("use_ray_tracing_if_enabled")
    settings = exposure(comp.get_editor_property("post_process_settings"), post, manual)
    # SceneCapture defaults to no Lumen even when the main view uses it.
    prop(comp, "post_process_settings", lighting_override(settings, *lighting))
    targets = {}
    images = []
    try:
        for item in effects:
            mode = item["mode"]
            name = item["id"]
            w, h = item.get("size", [width, height])
            if mode == "live":
                capture_mode, rt = live[item["source"]]
                _, fmt, encoding = CAPTURES[capture_mode]
                w, h = rt.get_editor_property("size_x"), rt.get_editor_property("size_y")
                targets[name] = rt
                images.append({**save_rt(world, rt, mode, directory, name, w, h, encoding),
                               "renderTargetFormat": fmt, "captureSource": CAPTURES[capture_mode][0]})
                continue
            source, fmt, encoding = CAPTURES.get(mode, ("", "RTF_RGBA32F", "material-linear-output"))
            if mode != "final" and item.get("precision", "float16" if mode == "material" else "float32") == "float16":
                fmt = "RTF_RGBA16F"
            rt = require(ue.RenderingLibrary.create_render_target2d(
                world, w, h, getattr(ue.TextureRenderTargetFormat, fmt)), "Cannot allocate RT")
            targets[name] = rt
            if mode == "material":
                instance = ue.MaterialLibrary.create_dynamic_material_instance(world, materials[item["material"]])
                for parameter, dependency in item["reads"].items():
                    instance.set_texture_parameter_value(parameter, targets[dependency])
                ue.RenderingLibrary.draw_material_to_render_target(world, rt, instance)
            else:
                prop(comp, "capture_source", getattr(ue.SceneCaptureSource, source))
                prop(comp, "texture_target", rt)
                for _ in range(item.get("warmup", 1)):
                    comp.capture_scene()
            images.append({**save_rt(world, rt, mode, directory, name, w, h, encoding),
                           "renderTargetFormat": fmt, "captureSource": source or "material-draw",
                           "captureRayTracingIfEnabled": capture_ray_tracing if source else None})
    finally:
        ue.get_editor_subsystem(ue.EditorActorSubsystem).destroy_actor(view)
    for image in images:
        image["materialPreflight"] = {"passed": True, "path": str(report_path),
                                      "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest()}
    return images


def capture_observations(world, camera, recipe, materials, live, directory, initial):
    post = recipe["postProcess"] if initial else {}
    manual = initial and recipe["exposureMode"] == "manual"
    dimensions = recipe["size"]
    actors = ue.get_editor_subsystem(ue.EditorActorSubsystem).get_all_level_actors()
    lighting = observation_lighting(actors)
    images = capture(world, camera, post, recipe["passes"], materials, live, directory, *dimensions, manual, lighting)
    for image in images:
        image["variant"] = "baseline"
        image["captureLighting"] = {"globalIllumination": lighting[0], "reflections": lighting[1]}
    if not recipe["variants"]:
        return images

    by_label = {actor.get_actor_label(): actor for actor in actors}
    lights = {"Key": "Restoration key light", "Environment": "Restoration sky fill"}
    lights.update({item["id"]: "Restoration light " + item["id"] for item in recipe["lights"]})
    for variant in recipe["variants"]:
        original_light_controls = []
        original_materials = []
        try:
            for light, scale in variant["lightScales"].items():
                actor = require(by_label.get(lights[light]), "Diagnostic light is missing: " + light)
                comp = light_component(actor)
                value = comp.get_editor_property("intensity")
                original_light_controls.append((comp.set_intensity, value))
                comp.set_intensity(value * scale)
            for light, scale in variant.get("lightSpecularScales", {}).items():
                actor = require(by_label.get(lights[light]), "Diagnostic light is missing: " + light)
                comp = light_component(actor)
                set_specular = getattr(comp, "set_specular_scale", None)
                require(set_specular, "This light has no native specular control: " + light)
                original_light_controls.append((set_specular, comp.get_editor_property("specular_scale")))
                set_specular(scale)
            chosen = variant.get("surface")
            if chosen or variant["parameters"]:
                primary = require(by_label.get("Captured draw"), "Could not find the saved captured mesh actor")
                mesh = require(primary.get_component_by_class(ue.StaticMeshComponent),
                               "The saved captured actor has no static mesh component")
                original_materials = [mesh.get_material(i) for i in range(mesh.get_num_materials())]
                parent = materials[chosen] if chosen else original_materials[0]
                dynamic = ue.MaterialLibrary.create_dynamic_material_instance(world, parent)
                for name, value in variant["parameters"].items():
                    if isinstance(value, list):
                        dynamic.set_vector_parameter_value(name, ue.LinearColor(*value, 1))
                    else:
                        dynamic.set_scalar_parameter_value(name, value)
                for index in range(len(original_materials)):
                    mesh.set_material(index, dynamic)
            for name in live:
                actor = by_label["Bread RT " + name]
                actor.get_component_by_class(ue.SceneCaptureComponent2D).capture_scene()
            view = variant.get("camera", camera)
            case = capture(world, view, {**post, **variant["postProcess"]}, variant["passes"], materials,
                           live, directory, *dimensions, manual, lighting, False)
            for image in case:
                image["variant"] = variant["id"]
                image["captureLighting"] = {"globalIllumination": lighting[0], "reflections": lighting[1]}
            images.extend(case)
        finally:
            for setter, value in reversed(original_light_controls):
                setter(value)
            for index, material in enumerate(original_materials):
                mesh.set_material(index, material)
    return images


def observe_level(request):
    target = request["observe"]
    recipe = request["recipe"]
    levels = ue.get_editor_subsystem(ue.LevelEditorSubsystem)
    require(levels.load_level(target["level"]), "Could not reopen the saved restoration level")
    world = ue.get_editor_subsystem(ue.UnrealEditorSubsystem).get_editor_world()
    actors = ue.get_editor_subsystem(ue.EditorActorSubsystem).get_all_level_actors()
    cameras = [a for a in actors if isinstance(a, ue.CameraActor) and a.get_actor_label() == "Restoration camera"]
    require(len(cameras) == 1, "The saved restoration camera is missing or ambiguous")
    camera = cameras[0]
    pos, rot = camera.get_actor_location(), camera.get_actor_rotation()
    transform = {"location": [pos.x, pos.y, pos.z], "rotation": [rot.roll, rot.pitch, rot.yaw],
                 "fov": camera.get_component_by_class(ue.CameraComponent).field_of_view}
    captures = {actor.get_actor_label(): actor for actor in actors if isinstance(actor, ue.SceneCapture2D)}
    live = {}
    for item in recipe["liveCaptures"]:
        actor = require(captures.get("Bread RT " + item["id"]), "Saved RT producer is missing")
        component = actor.get_component_by_class(ue.SceneCaptureComponent2D)
        component.capture_scene()
        live[item["id"]] = (item["mode"], component.get_editor_property("texture_target"))
    materials = {key: require(ue.load_asset(value), "Saved material instance missing: " + value)
                 for key, value in target["materials"].items()}
    images = capture_observations(world, transform, recipe, materials, live, Path(request["evidence"]), False)
    return {**target, "assets": sorted(ASSETS.list_assets(request["assetRoot"], recursive=True, include_folder=False)),
            "images": images, "editorVersion": ue.SystemLibrary.get_engine_version()}


def main(request):
    path = Path(request["project"])
    require(os.path.samefile(path, ue.Paths.convert_relative_path_to_full(ue.Paths.get_project_file_path())),
            "Unreal Editor opened a different project")
    require(ue.SystemLibrary.get_engine_version().split("-")[0] == request["engine"],
            "Unreal Editor version differs from the request")
    if "editorPython" in request:
        scope = {"__name__": "__main__", "unreal": ue, "result": None}
        exec(compile(request["editorPython"], "<Bread Editor MCP>", "exec"), scope)
        encoded = json.dumps(scope["result"], allow_nan=False)
        require(len(encoded.encode("utf-8")) <= 65536, "Editor tool result exceeds 64 KiB")
        return {"status": "executed", "result": scope["result"]}
    capabilities = probe(request)
    if request.get("probe"):
        return {"status": "probed", "renderCapabilities": capabilities}
    recipe = request["recipe"]
    validate_capabilities(recipe, capabilities)
    if "observe" in request:
        return {**observe_level(request), "renderCapabilities": capabilities}
    root = request["assetRoot"]
    map_path = root + "/Maps/Restoration"
    require(not ASSETS.does_directory_exist(root), "The restoration asset namespace already exists")
    levels = ue.get_editor_subsystem(ue.LevelEditorSubsystem)
    require(levels.new_level(map_path), "Could not create preview level " + map_path)
    world = ue.get_editor_subsystem(ue.UnrealEditorSubsystem).get_editor_world()
    objects = {}
    for item in recipe.get("sceneObjects", []):
        obj = spawn(ue.StaticMeshActor, item["id"], item)
        shape = require(ue.load_asset("/Engine/BasicShapes/" + item["shape"]), "UE primitive is missing")
        obj.get_component_by_class(ue.StaticMeshComponent).set_static_mesh(shape)
        objects[item["id"]] = obj
    textures = {item["id"]: texture_asset(root, item) for item in request["textures"]}
    live = {item["id"]: (item["mode"], live_capture(root, item, objects))
            for item in recipe.get("liveCaptures", [])}
    textures.update({name: rt for name, (_, rt) in live.items()})
    materials = {s["id"]: make_surface(root, s, textures, request["nativeOperators"], request["sceneInputs"], request["matteInputs"])
                 for s in recipe["surfaces"]}
    for item in recipe.get("sceneObjects", []):
        if item.get("material"):
            comp = objects[item["id"]].get_component_by_class(ue.StaticMeshComponent)
            comp.set_material(0, materials[item["material"]])
    captured_mesh_actor(root, request["mesh"], "SM_CapturedDraw", "Captured draw",
                        recipe.get("transform"), materials[recipe["surface"]])
    for item in recipe.get("additionalMeshes", []):
        captured_mesh_actor(root, request["additionalMeshes"][item["id"]], "SM_" + item["id"],
                            "Captured draw " + item["id"], item, materials[item["material"]])
    camera = spawn(ue.CameraActor, "Restoration camera", recipe["camera"])
    prop(camera.get_component_by_class(ue.CameraComponent), "field_of_view", recipe["camera"]["fov"])
    pp = spawn(ue.PostProcessVolume, "Restoration post processing")
    prop(pp, "unbound", True)
    manual = recipe.get("exposureMode") == "manual"
    settings = exposure(pp.get_editor_property("settings"), recipe["postProcess"], manual)
    prop(pp, "settings", lighting_override(settings, recipe["globalIllumination"], recipe["reflections"]))
    for s in recipe["surfaces"]:
        if s["template"] == "post-process":
            pp.add_or_update_blendable(materials[s["id"]], 1.0)
    configure_lights(recipe, request["lightClasses"], request["lightSettings"])
    editor = ue.get_editor_subsystem(ue.UnrealEditorSubsystem)
    editor.set_level_viewport_camera_info(camera.get_actor_location(), camera.get_actor_rotation())
    require(levels.save_current_level(), "Could not save preview level")
    images = capture_observations(world, recipe["camera"], recipe, materials, live, Path(request["evidence"]), True)
    assets = sorted(ASSETS.list_assets(root, recursive=True, include_folder=False))
    return {"level": map_path, "assets": assets, "images": images, "editorVersion": ue.SystemLibrary.get_engine_version(),
            "materials": {key: value.get_path_name() for key, value in materials.items()},
            "renderCapabilities": capabilities}


if __name__ == "__main__":
    request_path = Path(os.environ["BREAD_UNREAL_REQUEST"])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        result = {"status": "rendered", **main(request)}
        result["observedRendererCVars"] = renderer_cvars()
    except Exception:
        result = {"status": "editor-error", "diagnostic": traceback.format_exc()}
        ue.log_error(result["diagnostic"])
    result["nonce"] = request["nonce"]
    request_path.with_name("receipt.json").write_text(json.dumps(result), encoding="utf-8")
