import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


I18N_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = I18N_ROOT.parents[1]
EXTRACTOR_PATH = I18N_ROOT / "extract_replaylab_translations.py"
SYNC_PATH = I18N_ROOT / "sync_replaylab_catalog.py"
MAPPING_PATH = I18N_ROOT / "replaylab_zh_CN.json"
REGISTRY_PATH = REPO_ROOT / "qrenderdoc" / "Translations" / "ReplayLabProjects.cpp"
CATALOG_PATH = REPO_ROOT / "qrenderdoc" / "Translations" / "qrenderdoc_zh_CN.ts"
INTEGRATION_ROOT = REPO_ROOT / "replaylab" / "renderdoc_mcp_bridge"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


extractor = load_module("extract_replaylab_translations", EXTRACTOR_PATH)
catalog_sync = load_module("sync_replaylab_catalog", SYNC_PATH)


class ReplayLabI18nTests(unittest.TestCase):
    def test_raw_python_widget_literal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "project_launcher.py"
            path.write_text(
                'def build():\n    return QtWidgets.QPushButton("New Tool")\n',
                encoding="utf-8",
            )
            messages, errors = extractor.extract(path)
        self.assertEqual(set(), messages)
        self.assertTrue(any("raw user-visible string" in error for error in errors))

    def test_direct_translate_call_is_extractable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "project_launcher.py"
            path.write_text(
                'TRANSLATION_CONTEXT = "ReplayLabProjects"\n'
                'def build():\n'
                '    return QtWidgets.QPushButton(QtCore.QCoreApplication.translate(\n'
                '        TRANSLATION_CONTEXT, "New Tool"))\n',
                encoding="utf-8",
            )
            messages, errors = extractor.extract(path)
        self.assertEqual({("ReplayLabProjects", "New Tool")}, messages)
        self.assertEqual([], errors)

    def test_raw_gate_covers_non_ascii_lowercase_new_calls_and_variables(self):
        samples = (
            'def build():\n    return QtWidgets.QPushButton("新按钮")\n',
            'def build():\n    return QtWidgets.QPushButton("refresh")\n',
            'def build(combo):\n    combo.addItem("New preset")\n',
            'def build():\n    label = "New Tool"\n    return QtWidgets.QPushButton(label)\n',
        )
        for index, source in enumerate(samples):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "project_launcher.py"
                path.write_text(source, encoding="utf-8")
                unused_messages, errors = extractor.extract(path)
            self.assertTrue(
                any("raw user-visible string" in error for error in errors), errors)

    def test_init_project_region_and_context_are_mandatory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "__init__.py"
            path.write_text(
                '# replaylab-project-i18n-begin\n'
                'def new_project_ui():\n'
                '    return QtWidgets.QPushButton("New Tool")\n'
                '# replaylab-project-i18n-end\n',
                encoding="utf-8",
            )
            unused_messages, errors = extractor.extract(path)
            self.assertTrue(any("raw user-visible string" in error for error in errors))

            path.write_text(
                '# replaylab-project-i18n-begin\n'
                'def new_project_ui():\n'
                '    return QtWidgets.QPushButton(QtCore.QCoreApplication.translate(\n'
                '        "WrongContext", "New Tool"))\n'
                '# replaylab-project-i18n-end\n',
                encoding="utf-8",
            )
            unused_messages, errors = extractor.extract(path)
            self.assertTrue(any("translation context must be" in error for error in errors))

    def test_python_numerus_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "project_launcher.py"
            path.write_text(
                'TRANSLATION_CONTEXT = "ReplayLabProjects"\n'
                'def build():\n'
                '    return QtWidgets.QLabel(QtCore.QCoreApplication.translate(\n'
                '        TRANSLATION_CONTEXT, "%n project(s)"))\n',
                encoding="utf-8",
            )
            unused_messages, errors = extractor.extract(path)
        self.assertTrue(any("does not support %n" in error for error in errors))

    def test_translated_source_must_not_contain_cjk(self):
        for text in ("新建项目", "プロジェクト", "프로젝트"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "project_launcher.py"
                path.write_text(
                    'TRANSLATION_CONTEXT = "ReplayLabProjects"\n'
                    'def build():\n'
                    '    return QtWidgets.QLabel(QtCore.QCoreApplication.translate(\n'
                    f'        TRANSLATION_CONTEXT, {text!r}))\n',
                    encoding="utf-8",
                )
                messages, errors = extractor.extract(path)
            self.assertEqual(set(), messages)
            self.assertTrue(any("must not contain CJK text" in error for error in errors))

    def test_check_can_allow_a_registry_superset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "project_launcher.py"
            output = root / "ReplayLabProjects.cpp"
            source.write_text(
                'TRANSLATION_CONTEXT = "ReplayLabProjects"\n'
                'def build():\n'
                '    return QtWidgets.QLabel(QtCore.QCoreApplication.translate(\n'
                '        TRANSLATION_CONTEXT, "Old Tool"))\n',
                encoding="utf-8",
            )
            output.write_text(
                extractor.cpp_registry(
                    {
                        ("ReplayLabProjects", "Old Tool"),
                        ("ReplayLabProjects", "Future Tool"),
                    },
                    [source],
                ),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(EXTRACTOR_PATH),
                "--source", str(source),
                "--output", str(output),
            ]
            strict = subprocess.run(
                [*command, "--check"], capture_output=True, text=True, check=False)
            compatible = subprocess.run(
                [*command, "--check", "--allow-output-superset"],
                capture_output=True,
                text=True,
                check=False,
            )
            invalid_usage = subprocess.run(
                [*command, "--allow-output-superset"],
                capture_output=True,
                text=True,
                check=False,
            )
            output.write_text(
                extractor.cpp_registry(
                    {("ReplayLabProjects", "Future Tool")}, [source]),
                encoding="utf-8",
            )
            missing = subprocess.run(
                [*command, "--check", "--allow-output-superset"],
                capture_output=True,
                text=True,
                check=False,
            )
            output.write_text(
                extractor.cpp_registry(
                    {
                        ("ReplayLabProjects", "Old Tool"),
                        ("ReplayLabProjects", "新工具"),
                    },
                    [source],
                ),
                encoding="utf-8",
            )
            cjk_extra = subprocess.run(
                [*command, "--check", "--allow-output-superset"],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(0, strict.returncode)
        self.assertEqual(0, compatible.returncode, compatible.stderr)
        self.assertNotEqual(0, invalid_usage.returncode)
        self.assertNotEqual(0, missing.returncode)
        self.assertNotEqual(0, cjk_extra.returncode)

    def test_reviewed_mapping_matches_canonical_integration_and_catalog(self):
        self.assertTrue(
            INTEGRATION_ROOT.is_dir(),
            f"canonical ReplayLab built-in integration is unavailable: {INTEGRATION_ROOT}",
        )
        messages = set()
        errors = []
        sources = sorted(
            [INTEGRATION_ROOT / "__init__.py", *INTEGRATION_ROOT.rglob("project_*.py")],
            key=lambda path: str(path).lower(),
        )
        for source in sources:
            extracted, source_errors = extractor.extract(source)
            messages.update(extracted)
            errors.extend(source_errors)
        self.assertEqual([], errors)

        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))["translations"]
        extracted_sources = {
            source for context, source in messages if context == "ReplayLabProjects"
        }
        self.assertEqual(set(mapping), extracted_sources)

        registry_text = REGISTRY_PATH.read_text(encoding="utf-8")
        registry_messages = extractor.parse_cpp_registry_messages(registry_text)
        self.assertEqual(
            {("ReplayLabProjects", source) for source in mapping}, registry_messages)
        self.assertEqual(
            registry_text.replace("\r\n", "\n"),
            extractor.cpp_registry(messages, sources),
        )

        catalog_text = CATALOG_PATH.read_text(encoding="utf-8")
        context_match = catalog_sync.CONTEXT_RE.search(catalog_text)
        self.assertIsNotNone(context_match)
        found = {}
        for message_match in catalog_sync.MESSAGE_RE.finditer(context_match.group(0)):
            import xml.etree.ElementTree as ET
            message = ET.fromstring(message_match.group(0))
            found[catalog_sync.element_text(message.find("source"))] = (
                catalog_sync.element_text(message.find("translation")),
                message.find("translation").get("type"),
            )
        self.assertEqual(set(mapping), set(found))
        for source, translation in mapping.items():
            self.assertEqual((translation, None), found[source])


if __name__ == "__main__":
    unittest.main()
