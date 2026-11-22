import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("check_changes", Path(__file__).resolve().parents[1] / "check_changes.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ChangedLines(unittest.TestCase):
    def test_insertion_does_not_format_unrelated_legacy_code(self):
        self.assertEqual(module.changed_ranges("old\nunchanged\n", "old\nnew\nunchanged\n"), [(2, 2)])

    def test_new_file_checks_all_lines(self):
        self.assertEqual(module.changed_ranges("", "int main() {\nreturn 0;\n}\n"), [(1, 3)])

    def test_modification_checks_replacement(self):
        self.assertEqual(module.changed_ranges("bad\nother\n", "fixed\nother\n"), [(1, 1)])

    def test_deletion_does_not_hide_new_code(self):
        self.assertEqual(module.changed_ranges("old\nother\n", "other\n"), [])

    def test_expanded_legacy_formatting_does_not_hide_new_format_errors(self):
        source = "legacy bad\nnew bad\n"
        formatted = "legacy fixed\nnew fixed\n"
        self.assertTrue(module.relevant_format_changes(source, formatted, [(2, 2)]))
        self.assertFalse(module.relevant_format_changes("legacy bad\nnew good\n", "legacy fixed\nnew good\n", [(2, 2)]))
