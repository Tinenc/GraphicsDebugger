from __future__ import annotations

import base64
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from src.asset_catalog import AssetCatalogService
from src.shader_asset_retrieval import retrieve, texture_identity_evidence
from src.symbols.imaging import (
    compare_hashes, decode_block_compressed, decode_dds, decode_png, dhash_variants, fingerprint,
    fingerprint_from_thumbnail, gray_thumbnail, hamming,
)
from src.symbols.engines.unreal_texture import parse_platform_data, texture_fingerprint
from tests.test_shader_asset_retrieval import NAMES, build_package, crow_material, uexp_blob


def gradient_rgba(width: int, height: int) -> bytearray:
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes((x * 255 // max(1, width - 1), y * 255 // max(1, height - 1), (x * y) % 256, 255))
    return out


def mirror_x(rgba: bytearray, width: int, height: int) -> bytearray:
    out = bytearray()
    for y in range(height):
        row = rgba[y * width * 4:(y + 1) * width * 4]
        for x in reversed(range(width)):
            out += row[x * 4:x * 4 + 4]
    return out


def png_bytes(rgba: bytes, width: int, height: int) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgba[y * width * 4:(y + 1) * width * 4]) for y in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def texture_uexp(rgba: bytes, width: int, height: int) -> bytes:
    """A cooked Texture2D export in the UE 5.3+ data-resource mip style."""

    fmt = b"PF_B8G8R8A8\x00"
    bgra = bytearray()
    for i in range(width * height):
        r, g, b, a = rgba[i * 4:i * 4 + 4]
        bgra += bytes((b, g, r, a))
    return (b"\x00" * 24 + struct.pack("<iiI", width, height, 1) + struct.pack("<i", len(fmt)) + fmt
            + struct.pack("<ii", 0, 1) + struct.pack("<i", 0) + bytes(bgra) + struct.pack("<iii", width, height, 1)
            + b"\xc1\x83\x2a\x9e")


def texture_package(name: str) -> bytes:
    imports = [("/Script/CoreUObject", "Package", 0, "/Script/Engine"), ("/Script/CoreUObject", "Class", -1, "Texture2D")]
    return build_package(NAMES, imports, name, -2)


class ImagingTests(unittest.TestCase):
    def test_png_inflated_payload_cannot_exceed_declared_dimensions(self) -> None:
        data = bytearray(png_bytes(bytes((1, 2, 3, 255)) * 1024, 32, 32))
        # Advertise one pixel without changing the much larger IDAT payload.
        struct.pack_into(">II", data, 16, 1, 1)
        with self.assertRaisesRegex(ValueError, "exceeds its declared dimensions"):
            decode_png(bytes(data))

    def test_bc1_and_dds_decode(self) -> None:
        # BC1 block: c0 = pure red (0xF800), c1 = pure blue (0x001F), all indices 0 -> red
        block = struct.pack("<HHI", 0xF800, 0x001F, 0)
        rgba = decode_block_compressed(block * 4, 8, 8, "BC1")
        self.assertEqual(tuple(rgba[:4]), (255, 0, 0, 255))
        header = bytearray(128)
        header[:4] = b"DDS "
        struct.pack_into("<II", header, 12, 8, 8)
        header[84:88] = b"DXT1"
        out, width, height, fmt = decode_dds(bytes(header) + block * 4)
        self.assertEqual((width, height, fmt, tuple(out[-4:])), (8, 8, "BC1", (255, 0, 0, 255)))

    def test_png_roundtrip_and_dihedral_hashes(self) -> None:
        rgba = gradient_rgba(32, 32)
        decoded, width, height = decode_png(png_bytes(rgba, 32, 32))
        self.assertEqual((width, height), (32, 32))
        self.assertEqual(bytes(decoded), bytes(rgba))
        original = fingerprint(rgba, 32, 32)
        mirrored = fingerprint(mirror_x(rgba, 32, 32), 32, 32)
        self.assertGreater(hamming(original["dhash"], mirrored["dhash"]), 6)  # plain hash differs
        best = compare_hashes(mirrored["dhash"], original["dhashVariants"])
        self.assertEqual((best["distance"], best["verdict"]), (0, "same-image"))
        self.assertEqual(best["transform"], "mirror-x")
        # resized copy still matches
        small = fingerprint(gradient_rgba(16, 16), 16, 16)
        self.assertLessEqual(compare_hashes(small["dhash"], original["dhashVariants"])["distance"], 6)
        again = fingerprint_from_thumbnail(original["thumbnail"])
        self.assertEqual(again["dhash"], original["dhash"])
        flat = fingerprint(bytes((7, 7, 7, 255)) * 64, 8, 8)
        self.assertTrue(flat["flat"])

    def test_unreal_texture_platform_data(self) -> None:
        rgba = gradient_rgba(16, 16)
        data = texture_uexp(rgba, 16, 16)
        platform = parse_platform_data(data)
        self.assertEqual((platform["pixelFormat"], platform["sizeX"], platform["style"]), ("PF_B8G8R8A8", 16, "resource"))
        self.assertTrue(platform["mips"][0]["inline"])
        result = texture_fingerprint(data)
        self.assertTrue(result["available"], result)
        self.assertEqual(result["mipUsed"]["sizeX"], 16)
        self.assertEqual(result["dhash"], fingerprint(rgba, 16, 16)["dhash"])
        self.assertFalse(texture_fingerprint(b"\x00" * 64)["available"])


class TextureIdentityRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name) / "unpacked"
        materials = root / "Game" / "Content" / "Art" / "Materials"
        textures = root / "Game" / "Content" / "Art" / "Textures"
        materials.mkdir(parents=True)
        textures.mkdir(parents=True)
        (root / "Game" / "Game.uproject").write_text("{}", "utf-8")
        (materials / "M_CrowFeather.uasset").write_bytes(crow_material())
        (materials / "M_CrowFeather.uexp").write_bytes(uexp_blob(["FDepthOnlyPS"]))
        self.body = gradient_rgba(32, 32)
        (textures / "T_Crow_Body_D.uasset").write_bytes(texture_package("T_Crow_Body_D"))
        (textures / "T_Crow_Body_D.uexp").write_bytes(texture_uexp(self.body, 32, 32))
        other = bytearray()
        for y in range(32):
            for x in range(32):
                on = ((x // 4) + (y // 4)) % 2 == 0
                other += bytes((250 if on else 20, 40 if on else 200, 90, 255))
        (textures / "T_Crow_Body_N.uasset").write_bytes(texture_package("T_Crow_Body_N"))
        (textures / "T_Crow_Body_N.uexp").write_bytes(texture_uexp(bytes(other), 32, 32))
        (textures / "T_Loose.png").write_bytes(png_bytes(self.body, 32, 32))
        self.catalog = AssetCatalogService(Path(self.temporary.name) / "catalog.sqlite3")
        self.scan = self.catalog.scan(root, build_id="crow", depth="headers")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_header_scan_fingerprints_textures_and_matches_mirrored_capture(self) -> None:
        status = self.catalog.coverage()
        self.assertGreaterEqual(status["thumbnailRows"], 3)  # two cooked textures + the loose PNG
        captured = fingerprint(mirror_x(self.body, 32, 32), 32, 32)
        search = self.catalog.search_thumbnails(captured["dhash"], mean_color=captured["meanColor"])
        paths = [item["relativePath"] for item in search["candidates"]]
        self.assertIn("Game/Content/Art/Textures/T_Crow_Body_D.uexp", paths)
        self.assertIn("Game/Content/Art/Textures/T_Loose.png", paths)
        self.assertEqual(search["candidates"][0]["verdict"], "same-image")
        self.assertEqual(search["candidates"][0]["transform"], "mirror-x")
        evidence = texture_identity_evidence(self.catalog, [
            {"resourceId": 7, "name": "Texture2D 7", "available": True, "thumbnail": captured["thumbnail"], "meanColor": captured["meanColor"]},
            {"resourceId": 8, "name": "flat", "available": True, "thumbnail": base64.b64encode(bytes([9]) * 256).decode(), "meanColor": [9, 9, 9]},
            {"resourceId": 9, "name": "broken", "available": False, "reason": "SaveTexture failed"},
        ])
        material = evidence["byMaterial"]["Game/Content/Art/Materials/M_CrowFeather.uasset"]
        self.assertEqual(material["sameImage"], 1)
        self.assertIn("uniform texture", evidence["perTexture"][1]["reason"])
        self.assertEqual(evidence["perTexture"][2]["reason"], "SaveTexture failed")
        # retrieval with NO names, only pixels, still finds the material
        result = retrieve(self.catalog, {"stage": "fragment", "boundTextureNames": [],
                                         "boundTextureFingerprints": [{"resourceId": 7, "name": "Texture2D 7", "available": True,
                                                                       "thumbnail": captured["thumbnail"], "meanColor": captured["meanColor"]}]}, top_k=3)
        self.assertTrue(result["candidates"], result)
        leader = result["candidates"][0]
        self.assertEqual(leader["relativePath"], "Game/Content/Art/Materials/M_CrowFeather.uasset")
        self.assertEqual(leader["facets"]["textureIdentity"]["sameImage"], 1)
        self.assertGreaterEqual(leader["confidence"], 0.6)
        self.assertIn("matched by content", leader["why"])
        self.assertIn("strong perceptual matches", leader["why"])
        self.assertFalse(material["physicalIdentityVerified"])


if __name__ == "__main__":
    unittest.main()
