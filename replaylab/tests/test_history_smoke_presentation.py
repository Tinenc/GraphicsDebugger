"""CPU acceptance tests for the owned-process history fixture; no Qt/GPU imports."""
import ast
from pathlib import Path
import unittest


def load_pending_evidence():
    path = Path(__file__).parent / "fixtures" / "qrenderdoc_history_smoke.py"
    source = path.read_text(encoding="utf-8")
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_pending_surface_evidence")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[function.name]


class HistoryPresentationTest(unittest.TestCase):
    def setUp(self):
        self.classify = load_pending_evidence()
        self.current = {"imageSize": [674, 957], "sha256": "actual-current"}
        self.sample = {"imageSize": [674, 722], "sha256": "actual-history",
                       "displayedEvent": None, "displayedResource": None, "blankRGB": False}

    def test_pending_never_allows_previous_historical_pixels(self):
        with self.assertRaises(RuntimeError):
            self.classify(self.sample, self.current)

    def test_real_clear_can_be_visible_without_certifying_a_frame(self):
        self.sample["blankRGB"] = True
        self.assertEqual(self.classify(self.sample, self.current), "gpu-cleared")

    def test_clear_with_premature_event_or_resource_certificate_is_rejected(self):
        self.sample["blankRGB"] = True
        for field, value in (("displayedEvent", 5489), ("displayedResource", "ResourceId::846243")):
            sample = dict(self.sample, **{field: value})
            with self.assertRaises(RuntimeError):
                self.classify(sample, self.current)

    def test_final_display_before_queued_gui_completion_requires_exact_reference(self):
        self.sample.update(self.current)
        self.assertEqual(self.classify(self.sample, self.current), "current-before-gui-completion")
        self.sample["imageSize"] = [674, 722]
        with self.assertRaises(RuntimeError):
            self.classify(self.sample, self.current)


if __name__ == "__main__":
    unittest.main()
