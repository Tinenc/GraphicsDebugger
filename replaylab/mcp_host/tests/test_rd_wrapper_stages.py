from __future__ import annotations

import types
import unittest

from src.rd_wrapper import CaptureSession


class RenderDocStageResolutionTests(unittest.TestCase):
    def test_task_mesh_and_ray_names_resolve_without_fragment_fallback(self) -> None:
        stage = types.SimpleNamespace(
            Vertex=object(),
            Fragment=object(),
            Task=object(),
            Mesh=object(),
            RayGen=object(),
            ClosestHit=object(),
        )
        session = CaptureSession.__new__(CaptureSession)
        session.rd = types.SimpleNamespace(ShaderStage=stage)

        self.assertIs(session._shader_stage("amplification"), stage.Task)
        self.assertIs(session._shader_stage("ms"), stage.Mesh)
        self.assertIs(session._shader_stage("raygen"), stage.RayGen)
        self.assertIs(session._shader_stage("ray generation"), stage.RayGen)
        self.assertEqual(session._shader_stage_name("amplification"), "task")
        self.assertEqual(session._shader_stage_name("raygeneration"), "raygen")
        self.assertIs(session._shader_stage("closesthit"), stage.ClosestHit)
        with self.assertRaisesRegex(ValueError, "unsupported shader stage"):
            session._shader_stage("not-a-stage")
        with self.assertRaisesRegex(ValueError, "unsupported shader stage"):
            session._shader_stage("miss")


if __name__ == "__main__":
    unittest.main()
