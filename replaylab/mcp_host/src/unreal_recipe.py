"""Schema for a project-local, editable Unreal restoration."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints, model_validator


Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")]
OperationId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
Vec3 = tuple[FiniteFloat, FiniteFloat, FiniteFloat]
Vec4 = tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]
Size = tuple[Annotated[int, Field(ge=16, le=2048)], Annotated[int, Field(ge=16, le=2048)]]
Value = FiniteFloat | Vec3 | Name
ValueType = Literal["scalar", "vector2", "vector3", "texture2d"]
INPUTS = {"PixelNormalWS": "vector3", "VertexNormalWS": "vector3", "CameraVectorWS": "vector3",
          "WorldPosition": "vector3", "UV0": "vector2", "ScreenUV": "vector2"}
SCENE_INPUTS = {"PostProcessInput0": "PPI_POST_PROCESS_INPUT0", "SceneDepth": "PPI_SCENE_DEPTH",
                "SceneWorldNormal": "PPI_WORLD_NORMAL", "CustomDepth": "PPI_CUSTOM_DEPTH",
                "CustomStencil": "PPI_CUSTOM_STENCIL"}
PINS = {
    "unlit": {"MP_EMISSIVE_COLOR"},
    "render-target": {"MP_EMISSIVE_COLOR"},
    "post-process": {"MP_EMISSIVE_COLOR"},
    "default-lit": {"MP_BASE_COLOR", "MP_EMISSIVE_COLOR", "MP_METALLIC", "MP_ROUGHNESS",
                    "MP_SPECULAR", "MP_NORMAL", "MP_AMBIENT_OCCLUSION"},
    "substrate-toon": {"Base Color", "Emissive Color", "Metallic", "Roughness", "Specular",
                       "Normal", "Tangent", "Anisotropy", "Pattern UVs"},
}
NATIVE_OPERATORS = {"dot": ("DotProduct", ("A", "B")),
                    "multiply": ("Multiply", ("A", "B")),
                    "add": ("Add", ("A", "B")),
                    "lerp": ("LinearInterpolate", ("A", "B", "Alpha")),
                    "saturate": ("Saturate", ("Input",)),
                    "normalize": ("Normalize", ("VectorInput",))}
MATTE_INPUTS = {
    "default-lit": {"MP_METALLIC": 0.0, "MP_SPECULAR": 0.0, "MP_ROUGHNESS": 1.0},
    "substrate-toon": {"Metallic": 0.0, "Specular": 0.0, "Roughness": 1.0},
}
LIGHT_CLASSES = {"directional": "DirectionalLight", "point": "PointLight",
                 "spot": "SpotLight", "rect": "RectLight"}
LIGHT_SETTINGS = {"castShadows": "cast_shadows", "indirectIntensity": "indirect_lighting_intensity",
                  "specularScale": "specular_scale", "diffuseScale": "diffuse_scale",
                  "sourceAngle": "light_source_angle", "sourceRadius": "source_radius",
                  "attenuationRadius": "attenuation_radius", "innerCone": "inner_cone_angle",
                  "outerCone": "outer_cone_angle", "sourceWidth": "source_width", "sourceHeight": "source_height"}
NonNegative = Annotated[FiniteFloat, Field(ge=0)]
PRESENTATION = {"bloom_intensity", "auto_exposure_bias", "auto_exposure_min_brightness",
                "auto_exposure_max_brightness", "motion_blur_amount", "tone_curve_amount",
                "vignette_intensity", "lumen_scene_lighting_quality", "lumen_final_gather_quality",
                "indirect_lighting_intensity", "bloom_threshold", "film_slope", "film_toe",
                "film_shoulder", "film_black_clip", "film_white_clip", "white_temp", "white_tint",
                "lumen_diffuse_color_boost"}
COLOR_GRADING = {field + region for field in ("color_saturation", "color_contrast", "color_gamma",
                                                "color_gain", "color_offset")
                 for region in ("", "_shadows", "_midtones", "_highlights")}


def valid_post_process(values):
    return all((name in COLOR_GRADING if isinstance(value, tuple) else name in PRESENTATION)
               for name, value in values.items())


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Parameter(Record):
    type: Literal["scalar", "vector3", "texture2d"]
    comment: Note
    value: Value | None = None
    texture: Name | None = None

    @model_validator(mode="after")
    def validate_binding(self):
        if self.type == "texture2d":
            if self.value is not None or self.texture is None:
                raise ValueError("Texture parameter needs a declared texture id")
        elif self.texture is not None or not self.accepts(self.value):
            raise ValueError("Material parameter value does not match its type")
        return self

    def accepts(self, value):
        return isinstance(value, {"scalar": (int, float), "vector3": tuple, "texture2d": str}[self.type])


class Input(Record):
    name: Name
    source: Name
    type: ValueType
    comment: Note


class Function(Record):
    id: Name
    comment: Note
    code: Annotated[str, StringConstraints(min_length=1, max_length=12000)] | None = None
    operator: Literal["dot", "multiply", "add", "lerp", "saturate", "normalize"] | None = None
    inputs: list[Input] = Field(max_length=32)
    outputType: Literal["scalar", "vector2", "vector3"]

    @model_validator(mode="after")
    def validate_code(self):
        if (self.code is None) == (self.operator is None):
            raise ValueError("Select one native material node or a self-contained Custom expression")
        if self.code and "#" in self.code:
            raise ValueError("Custom HLSL must be self-contained without preprocessor directives")
        if self.operator:
            _, pins = NATIVE_OPERATORS[self.operator]
            if len(self.inputs) != len(pins) or any(i.type == "texture2d" for i in self.inputs):
                raise ValueError("Native material node input count/type mismatch")
        if len({i.name.casefold() for i in self.inputs}) != len(self.inputs):
            raise ValueError("Duplicate function input name")
        return self


class Surface(Record):
    id: Name
    template: Literal["unlit", "default-lit", "substrate-toon", "post-process", "render-target"]
    parameters: dict[Name, Parameter] = Field(max_length=96)
    functions: list[Function] = Field(max_length=32)
    outputs: dict[str, Name] = Field(min_length=1, max_length=12)
    instance: dict[Name, Value] = Field(default_factory=dict, max_length=96)
    toonProfile: str | None = None
    nativeSpecular: Literal["authored", "matte-diffuse"] = "authored"
    location: Annotated[str, StringConstraints(pattern=r"^BL_[A-Z0-9_]+$", max_length=80)] | None = None

    @model_validator(mode="after")
    def validate_graph(self):
        types = dict(INPUTS)
        if self.template == "post-process":
            types.update(dict.fromkeys(SCENE_INPUTS, "vector3"))
        for name, parameter in self.parameters.items():
            if name.casefold() in {n.casefold() for n in types}:
                raise ValueError("Duplicate or reserved parameter: " + name)
            types[name] = parameter.type
        for stage in self.functions:
            if stage.id.casefold() in {n.casefold() for n in types}:
                raise ValueError("Duplicate or reserved graph id: " + stage.id)
            for item in stage.inputs:
                source_type = types.get(item.source)
                if source_type != item.type:
                    raise ValueError(f"{stage.id}.{item.name} requires {item.type} from {item.source}, got {source_type}")
            types[stage.id] = stage.outputType
        if self.nativeSpecular == "matte-diffuse":
            pins = MATTE_INPUTS.get(self.template)
            if not pins or set(self.outputs) & pins.keys():
                raise ValueError("Matte diffuse needs a lit template with no explicit Metallic, Specular or Roughness wires")
        if set(self.outputs) - PINS[self.template]:
            raise ValueError("Output pin is unavailable for " + self.template)
        for pin, source in self.outputs.items():
            if types.get(source) not in {"scalar", "vector2", "vector3"}:
                raise ValueError(f"Output {pin} needs a numeric graph source")
            if source in INPUTS or source in SCENE_INPUTS:
                raise ValueError("Route built-in inputs through a documented material function")
        for name, value in self.instance.items():
            if name not in self.parameters or not self.parameters[name].accepts(value):
                raise ValueError("Invalid instance override " + name)
        if self.toonProfile and (self.template != "substrate-toon" or
                                not re.fullmatch(r"/Game/[A-Za-z0-9_/]+", self.toonProfile)):
            raise ValueError("Toon Profile must be an existing /Game asset path")
        if self.location and self.template != "post-process":
            raise ValueError("Blendable location applies only to a post-process material")
        return self


class Transform(Record):
    location: Vec3 = (0, 0, 0)
    rotation: Vec3 = (0, 0, 0)
    scale: Vec3 = (1, 1, 1)


class Camera(Transform):
    fov: Annotated[FiniteFloat, Field(gt=1, lt=170)] = 60


class Light(Transform):
    intensity: Annotated[FiniteFloat, Field(ge=0)]
    color: Vec3 = (1, 1, 1)


class RigLight(Light):
    id: Name
    kind: Literal["directional", "point", "spot", "rect"]
    castShadows: bool = True
    indirectIntensity: NonNegative | None = None
    specularScale: NonNegative | None = None
    diffuseScale: NonNegative | None = None
    sourceAngle: NonNegative | None = None
    sourceRadius: NonNegative | None = None
    attenuationRadius: NonNegative | None = None
    innerCone: NonNegative | None = None
    outerCone: NonNegative | None = None
    sourceWidth: NonNegative | None = None
    sourceHeight: NonNegative | None = None
    units: Literal["UNITLESS", "LUMENS", "CANDELAS"] | None = None

    @model_validator(mode="after")
    def validate_light(self):
        allowed = {"directional": {"sourceAngle"}, "point": {"attenuationRadius", "sourceRadius", "units"},
                   "spot": {"attenuationRadius", "sourceRadius", "units", "innerCone", "outerCone"},
                   "rect": {"attenuationRadius", "units", "sourceWidth", "sourceHeight"}}
        typed = set().union(*allowed.values())
        if any(getattr(self, field) is not None for field in typed - allowed[self.kind]):
            raise ValueError("A light setting does not apply to " + self.kind)
        if self.innerCone is not None and self.outerCone is not None and self.innerCone > self.outerCone:
            raise ValueError("Spot inner cone exceeds the outer cone")
        return self


class Sky(Record):
    intensity: NonNegative
    color: Vec3 = (1, 1, 1)
    indirectIntensity: NonNegative | None = None
    cubemap: Annotated[str, StringConstraints(pattern=r"^/Game/[A-Za-z0-9_/]+$")] | None = None


class Texture(Record):
    id: Name
    artifact: Annotated[str, StringConstraints(pattern=r"^source/.+\.(png|tga|exr|hdr)$", max_length=256)]
    sRGB: bool
    operationId: OperationId | None = None
    filter: Literal["NEAREST", "BILINEAR", "TRILINEAR", "DEFAULT"] = "DEFAULT"
    addressX: Literal["WRAP", "CLAMP", "MIRROR"] = "WRAP"
    addressY: Literal["WRAP", "CLAMP", "MIRROR"] = "WRAP"


class CapturedMesh(Transform):
    id: Name
    operationId: OperationId
    material: Name


class SceneObject(Transform):
    id: Name
    shape: Literal["Cube", "Sphere", "Plane"]
    material: Name | None = None


class LiveCapture(Camera):
    id: Name
    mode: Literal["scene-color", "scene-depth", "world-normal", "base-color"]
    size: Size = (512, 512)
    orthoWidth: Annotated[FiniteFloat, Field(gt=0)] | None = None
    showOnly: list[Name] = Field(min_length=1, max_length=32)


class Pass(Record):
    id: Name
    mode: Literal["final", "final-hdr", "scene-color", "base-color", "world-normal", "scene-depth", "material", "live"]
    size: Size | None = None
    warmup: int = Field(default=1, ge=1, le=64)
    precision: Literal["float16", "float32"] | None = None
    material: Name | None = None
    source: Name | None = None
    reads: dict[Name, Name] = Field(default_factory=dict, max_length=12)


class Variant(Record):
    id: Name
    camera: Camera | None = None
    surface: Name | None = None
    parameters: dict[Name, FiniteFloat | Vec3] = Field(default_factory=dict, max_length=32)
    lightScales: dict[Name, NonNegative] = Field(default_factory=dict, max_length=24)
    lightSpecularScales: dict[Name, NonNegative] = Field(default_factory=dict, max_length=24)
    postProcess: dict[str, FiniteFloat | Vec4] = Field(default_factory=dict, max_length=20)
    passes: list[Pass] = Field(min_length=1, max_length=16)


class Layer(Record):
    id: Name
    kind: Literal["diffuse", "specular", "rim", "shadow", "ambient", "multi-light", "outline",
                  "post-process", "aa", "appearance", "other"]
    route: Literal["exact", "replacement"]
    captureBasis: Note
    implementation: Note
    adjust: Note
    surface: Name | None = None
    functions: list[Name] = Field(default_factory=list, max_length=16)
    passes: list[Name] = Field(min_length=1, max_length=8)
    sourceEvents: dict[Name, Annotated[int, Field(ge=1)]] = Field(default_factory=dict, max_length=8)


class Recipe(Record):
    surface: Name
    surfaces: list[Surface] = Field(min_length=1, max_length=64)
    textures: list[Texture] = Field(default_factory=list, max_length=256)
    additionalMeshes: list[CapturedMesh] = Field(default_factory=list, max_length=63)
    transform: Transform = Field(default_factory=Transform)
    camera: Camera
    directional: Light | None = None
    lights: list[RigLight] = Field(default_factory=list, max_length=24)
    sky: Sky | None = None
    sceneObjects: list[SceneObject] = Field(default_factory=list, max_length=32)
    liveCaptures: list[LiveCapture] = Field(default_factory=list, max_length=4)
    skyIntensity: Annotated[FiniteFloat, Field(ge=0)] | None = None
    exposureMode: Literal["project", "manual"] = "project"
    globalIllumination: Literal["project", "lumen", "screen-space", "none"] = "project"
    reflections: Literal["project", "lumen", "screen-space", "none"] = "project"
    postProcess: dict[str, FiniteFloat | Vec4] = Field(default_factory=dict, max_length=20)
    size: Size = (512, 512)
    passes: list[Pass] = Field(min_length=1, max_length=24)
    variants: list[Variant] = Field(default_factory=list, max_length=12)
    layers: list[Layer] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_plan(self):
        def unique(items):
            ids = {item.id.casefold() for item in items}
            if len(ids) != len(items):
                raise ValueError("IDs within a restoration group must be distinct")
            return {item.id: item for item in items}
        surfaces = unique(self.surfaces)
        textures = unique([*self.textures, *self.liveCaptures])
        objects = unique(self.sceneObjects)
        meshes = unique(self.additionalMeshes)
        for mesh in meshes.values():
            if mesh.id.casefold() == "captureddraw":
                raise ValueError("CapturedDraw is reserved for the primary mesh")
            if mesh.material not in surfaces or surfaces[mesh.material].template in {"post-process", "render-target"}:
                raise ValueError("Captured mesh needs a surface material: " + mesh.id)
        for target in self.liveCaptures:
            if set(target.showOnly) - objects.keys():
                raise ValueError("Live RT inputs must be explicit scene objects")
            if any(objects[obj].material for obj in target.showOnly):
                raise ValueError("Live captures require independent producers without material feedback")
        for obj in self.sceneObjects:
            if obj.material and (obj.material not in surfaces or
                                 surfaces[obj.material].template in {"post-process", "render-target"}):
                raise ValueError("Scene object needs a surface material")
        variants = unique(self.variants)
        if any(key.casefold() == "baseline" for key in variants):
            raise ValueError("Baseline is reserved for the saved view")
        unique(self.layers)
        passes = unique([*self.passes, *(item for variant in self.variants for item in variant.passes)])
        lights = unique(self.lights)
        if any(key.casefold() in {"key", "environment"} for key in lights):
            raise ValueError("Key and Environment are reserved light ids")
        if self.directional:
            lights["Key"] = self.directional
        if self.sky is not None and self.skyIntensity is not None:
            raise ValueError("Specify one sky configuration")
        if self.sky is not None or self.skyIntensity is not None:
            lights["Environment"] = self.sky
        if self.surface not in surfaces or surfaces[self.surface].template in {"post-process", "render-target"}:
            raise ValueError("Select a surface material for the captured mesh")
        if not valid_post_process(self.postProcess):
            raise ValueError("Unknown post-process setting")
        for variant in self.variants:
            surface = surfaces.get(variant.surface or self.surface)
            if not surface or surface.template in {"post-process", "render-target"}:
                raise ValueError("A diagnostic variant needs a mesh surface material")
            if ((variant.lightScales.keys() | variant.lightSpecularScales.keys()) - lights.keys()
                    or not valid_post_process(variant.postProcess)):
                raise ValueError("A diagnostic variant references an unknown light or post-process setting")
            if "Environment" in variant.lightSpecularScales and "Environment" in lights:
                raise ValueError("Sky Light reflection is calibrated separately from per-light specular")
            if any(name not in surface.parameters or not surface.parameters[name].accepts(value)
                   for name, value in variant.parameters.items()):
                raise ValueError("A diagnostic variant references an unknown or mismatched material parameter")
        for layer in self.layers:
            if (len(set(layer.passes)) != len(layer.passes) or set(layer.passes) - passes.keys()
                    or layer.sourceEvents.keys() - set(layer.passes)
                    or (layer.surface and layer.surface not in surfaces)):
                raise ValueError("A layer references an unknown surface or observation")
            if layer.functions and (not layer.surface or set(layer.functions) -
                                    {stage.id for stage in surfaces[layer.surface].functions}):
                raise ValueError("A layer references an unknown material function")
            modes = {passes[name].mode for name in layer.passes}
            if layer.route == "exact" and modes == {"final"}:
                raise ValueError("An exact layer needs a linear observation")
        if self.layers and not any(layer.kind == "appearance" and layer.route == "replacement" and
                                   any(p.id in layer.passes and p.mode == "final" for p in self.passes)
                                   for layer in self.layers):
            raise ValueError("A layered effect plan needs a baseline appearance comparison")
        for surface in self.surfaces:
            for parameter in surface.parameters.values():
                if parameter.texture and parameter.texture not in textures:
                    raise ValueError("Unknown material texture " + parameter.texture)
            for name, value in surface.instance.items():
                if surface.parameters[name].type == "texture2d" and value not in textures:
                    raise ValueError("Unknown instance texture " + value)
        total_pixels = 0
        for group in [self.passes, *(variant.passes for variant in self.variants)]:
            produced = set()
            for item in group:
                live_target = next((target for target in self.liveCaptures if target.id == item.source), None)
                if item.mode == "live":
                    if not live_target or item.size:
                        raise ValueError("A live readback uses an existing live capture and its size")
                elif item.source:
                    raise ValueError("A source id is only valid for a live RT readback")
                width, height = live_target.size if live_target else item.size or self.size
                total_pixels += width * height
                if set(item.reads.values()) - produced:
                    raise ValueError("An RT dependency has no earlier producer: " + item.id)
                if item.mode == "material":
                    material = surfaces.get(item.material)
                    if not material or material.template != "render-target":
                        raise ValueError("An RT material pass needs a render-target surface")
                    if any(name not in material.parameters or material.parameters[name].type != "texture2d"
                           for name in item.reads):
                        raise ValueError("RT inputs must bind declared texture parameters")
                elif item.material or item.reads:
                    raise ValueError("Scene capture passes do not accept material inputs")
                produced.add(item.id)
        if total_pixels > 16 * 1024 * 1024 or not any(p.mode == "final" for p in self.passes):
            raise ValueError("Provide a final pass and keep total RT readback within 16 million pixels")
        return self
