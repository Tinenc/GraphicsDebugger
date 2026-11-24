import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "lint_ts.py"
REPO_ROOT = MODULE_PATH.parents[2]
REAL_GLOSSARY_PATH = REPO_ROOT / "qrenderdoc" / "Translations" / "glossary.zh_CN.yaml"
REAL_SOURCE_MANIFEST_PATH = REPO_ROOT / "util" / "i18n" / "source_manifest.json"
SPEC = importlib.util.spec_from_file_location("lint_ts", MODULE_PATH)
lint_ts = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = lint_ts
SPEC.loader.exec_module(lint_ts)


GLOSSARY = {
    "locale": "zh_CN",
    "protected_tokens": ["IA", "VS", "HS", "DS", "GS", "RS", "PS", "OM", "CS", "VTX", "FB"],
    "protected_patterns": [],
    "lint_terms": [],
    "forbidden_rules": [
        {
            "source_regex": "(?i)normal",
            "context_regex": "TextureViewer",
            "translation_regex": "正常",
            "message": "wrong graphics sense",
        }
    ],
}


def catalog(message: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE TS>
<TS version="2.1" language="zh_CN" sourcelanguage="en">
<context><name>TextureViewer</name>{message}</context>
</TS>
'''


class CatalogLintTests(unittest.TestCase):
    def run_lint(self, contents: str):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "test.ts"
            path.write_text(contents, encoding="utf-8")
            return lint_ts.lint_catalog(path, GLOSSARY, require_complete=False)[0]

    def test_valid_placeholders_html_token_and_filter(self):
        result = self.run_lint(catalog('''
        <message>
          <source>PS: &lt;b&gt;%1&lt;/b&gt; Captures (*.rdc);;All (*.*)</source>
          <translation>PS：&lt;b&gt;%1&lt;/b&gt; 捕获文件 (*.rdc);;全部 (*.*)</translation>
        </message>'''))
        self.assertEqual([], result.errors)

    def test_placeholder_mismatch_is_rejected(self):
        result = self.run_lint(catalog('''
        <message><source>Open %1 at %2</source><translation>在 %1 打开</translation></message>'''))
        self.assertTrue(any("placeholder mismatch" in error for error in result.errors))

    def test_html_css_and_attributes_are_program_data(self):
        result = self.run_lint(catalog('''
        <message>
          <source>&lt;style&gt;p { white-space: pre-wrap; }&lt;/style&gt;&lt;p style="color:#fff"&gt;Help&lt;/p&gt;</source>
          <translation>&lt;style&gt;p { 空白: 预换行; }&lt;/style&gt;&lt;p style="颜色:#fff"&gt;帮助&lt;/p&gt;</translation>
        </message>'''))
        self.assertTrue(any("HTML attributes" in error for error in result.errors))
        self.assertTrue(any("HTML style block" in error for error in result.errors))

    def test_angle_bracket_placeholder_is_not_misread_as_html(self):
        result = self.run_lint(catalog('''
        <message>
          <source>&lt;Auto-generated&gt;</source>
          <translation>&lt;自动生成&gt;</translation>
        </message>'''))
        self.assertEqual([], result.errors)

    def test_matching_incomplete_html_fragment_is_allowed(self):
        result = self.run_lint(catalog('''
        <message>
          <source>&lt;p&gt;Read &lt;a href="https://example.test"&gt;this</source>
          <translation>&lt;p&gt;阅读&lt;a href="https://example.test"&gt;此内容</translation>
        </message>'''))
        self.assertEqual([], result.errors)
        self.assertTrue(any("incomplete/malformed HTML" in warning for warning in result.warnings))

    def test_inline_code_and_command_options_are_preserved(self):
        result = self.run_lint(catalog('''
        <message>
          <source>Run `renderdoccmd capture` with --wait-for-exit</source>
          <translation>运行 `renderdoccmd 捕获` 并使用 --等待退出</translation>
        </message>'''))
        self.assertTrue(any("backtick code" in error for error in result.errors))
        self.assertTrue(any("command-line option" in error for error in result.errors))

    def test_accelerators_newlines_and_edge_spacing_are_preserved(self):
        result = self.run_lint(catalog('''
        <message>
          <source> &amp;Open&amp;&amp;More&#10;Now </source>
          <translation>打开更多 现在</translation>
        </message>'''))
        self.assertTrue(any("accelerator" in error for error in result.errors))
        self.assertTrue(any("newline" in error for error in result.errors))
        self.assertTrue(any("whitespace" in error for error in result.errors))

    def test_ampersand_conjunctions_can_be_localized_naturally(self):
        result = self.run_lint(catalog('''
        <message><source>Camera Settings &amp;&amp; Controls</source><translation>相机设置与控制</translation></message>
        <message><source>Source&amp;Dest</source><translation>源和目标</translation></message>'''))
        self.assertEqual([], result.errors)

    def test_protected_stage_abbreviation_is_rejected(self):
        result = self.run_lint(catalog('''
        <message><source>IA / DS / RS / PS / OM / CS</source><translation>IA / 域 / 光栅 / PS / 输出合并 / CS</translation></message>'''))
        self.assertTrue(any("protected token 'DS'" in error for error in result.errors))
        self.assertTrue(any("protected token 'RS'" in error for error in result.errors))
        self.assertTrue(any("protected token 'OM'" in error for error in result.errors))

    def test_all_compact_stage_abbreviations_are_protected(self):
        glossary = lint_ts.load_glossary(REAL_GLOSSARY_PATH)
        required = {
            "IA", "VS", "HS", "DS", "GS", "RS", "PS", "OM", "CS",
            "AS", "MS", "FS", "TS", "TCS", "TES", "VTX", "FB", "SO", "XFB",
        }
        self.assertEqual(set(), required - set(glossary["protected_tokens"]))

    def test_contextual_normal_mistranslation_is_rejected(self):
        result = self.run_lint(catalog('''
        <message><source>Normal</source><translation>正常</translation></message>'''))
        self.assertTrue(any("wrong graphics sense" in error for error in result.errors))

    def test_empty_unfinished_translation_is_allowed(self):
        result = self.run_lint(catalog('''
        <message><source>Pending</source><translation type="unfinished"></translation></message>'''))
        self.assertEqual([], result.errors)

    def test_added_line_parser_tracks_new_line_numbers(self):
        diff = '''diff --git a/qrenderdoc/a.cpp b/qrenderdoc/a.cpp
--- a/qrenderdoc/a.cpp
+++ b/qrenderdoc/a.cpp
@@ -3,0 +4,2 @@
+widget->setText(tr("Hello"));
+auto id = lit("identifier");
'''
        self.assertEqual(
            [
                ("qrenderdoc/a.cpp", 4, 'widget->setText(tr("Hello"));'),
                ("qrenderdoc/a.cpp", 5, 'auto id = lit("identifier");'),
            ],
            list(lint_ts.added_diff_lines(diff)),
        )

    def test_full_tree_baseline_rejects_new_literal_and_accepts_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            source_directory = repo / "qrenderdoc" / "Code"
            source_directory.mkdir(parents=True)
            source = source_directory / "NewPanel.cpp"
            baseline = repo / "baseline.json"
            baseline.write_text('{"schema_version": 1, "entries": {}}', encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "root": "qrenderdoc",
                "extensions": (".cpp", ".cc", ".cxx", ".h", ".hpp", ".ui"),
                "excluded_prefixes": ("qrenderdoc/3rdparty/",),
                "included_prefixes": (
                    "qrenderdoc/3rdparty/toolwindowmanager/",
                    "qrenderdoc/3rdparty/flowlayout/",
                ),
            }

            source.write_text('auto title = lit("Visible title");\n', encoding="utf-8")
            rejected = lint_ts.Result()
            lint_ts.lint_source_tree(repo, baseline, rejected, manifest=manifest)
            self.assertTrue(any("NewPanel.cpp:1" in error for error in rejected.errors))

            source.write_text(
                '// i18n-ignore: fixed protocol identifier\nauto id = lit("ProtocolName");\n',
                encoding="utf-8",
            )
            accepted = lint_ts.Result()
            lint_ts.lint_source_tree(repo, baseline, accepted, manifest=manifest)
            self.assertEqual([], accepted.errors)

    def test_source_manifest_auto_discovers_first_party_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            manifest_path = repo / "source_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "root": "qrenderdoc",
                        "extensions": [".cpp", ".cc", ".cxx", ".h", ".hpp", ".ui"],
                        "excluded_prefixes": ["qrenderdoc/3rdparty/"],
                        "included_prefixes": [
                            "qrenderdoc/3rdparty/toolwindowmanager/",
                            "qrenderdoc/3rdparty/flowlayout/",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            files = {
                "qrenderdoc/NewPanel/NewPanel.ui": "<ui/>",
                "qrenderdoc/NewPanel/NewPanel.cxx": "// first-party source",
                "qrenderdoc/3rdparty/vendor/Hidden.cpp": "// excluded vendor source",
                "qrenderdoc/3rdparty/toolwindowmanager/Included.cpp": "// allowlisted vendor source",
                "qrenderdoc/3rdparty/flowlayout/Included.hpp": "// allowlisted vendor header",
                "qrenderdoc/NewPanel/Ignored.qml": "// unsupported extension",
            }
            for relative, contents in files.items():
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")

            manifest = lint_ts.load_source_manifest(manifest_path)
            discovered = {
                path.relative_to(repo).as_posix()
                for path in lint_ts.discover_source_files(repo, manifest)
            }
            self.assertEqual(
                {
                    "qrenderdoc/NewPanel/NewPanel.ui",
                    "qrenderdoc/NewPanel/NewPanel.cxx",
                    "qrenderdoc/3rdparty/toolwindowmanager/Included.cpp",
                    "qrenderdoc/3rdparty/flowlayout/Included.hpp",
                },
                discovered,
            )

    def _compact_flow_fixture(self, directory: str, viewer_text: str, renderer_text: str):
        repo = pathlib.Path(directory)
        viewer_relative = "qrenderdoc/Windows/PipelineState/TestPipelineStateViewer.cpp"
        renderer_relative = "qrenderdoc/Widgets/PipelineFlowChart.cpp"
        for relative, contents in (
            (viewer_relative, viewer_text),
            (renderer_relative, renderer_text),
        ):
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "root": "qrenderdoc",
            "extensions": (".cpp", ".cc", ".cxx", ".h", ".hpp", ".ui"),
            "excluded_prefixes": ("qrenderdoc/3rdparty/",),
            "included_prefixes": (),
        }
        glossary = {
            "protected_tokens": ["IA", "VS", "SO"],
            "compact_pipeline_flows": {
                "renderer": renderer_relative,
                "viewers": [
                    {
                        "path": viewer_relative,
                        "stage_sequences": [["IA", "VS"]],
                        "dynamic_abbreviations": ["SO"],
                    }
                ],
            },
        }
        return repo, glossary, manifest

    def test_current_compact_pipeline_flow_contract(self):
        glossary = lint_ts.load_glossary(REAL_GLOSSARY_PATH)
        manifest = lint_ts.load_source_manifest(REAL_SOURCE_MANIFEST_PATH)
        result = lint_ts.Result()
        lint_ts.lint_compact_pipeline_flows(REPO_ROOT, glossary, manifest, result)
        self.assertEqual([], result.errors)

    def test_compact_pipeline_rejects_translated_or_wrong_abbreviation(self):
        renderer = '''
const QString &s = m_StageAbbrevs[i];
p.drawText(boxrect, s, opts);
'''
        invalid_viewer = '''
ui->pipeFlow->setStages(
    {lit("IA"), tr("Vertex Shader")},
    {tr("Input Assembler"), tr("Vertex Shader")});
ui->pipeFlow->setStageName(1, lit("SO"), tr("Stream Out"));
'''
        with tempfile.TemporaryDirectory() as directory:
            repo, glossary, manifest = self._compact_flow_fixture(
                directory, invalid_viewer, renderer
            )
            result = lint_ts.Result()
            lint_ts.lint_compact_pipeline_flows(repo, glossary, manifest, result)
            self.assertTrue(any("non-literal compact abbreviation" in error for error in result.errors))
            self.assertTrue(any("stage sequence differs" in error for error in result.errors))

    def test_compact_pipeline_rejects_rendering_translated_names(self):
        invalid_renderer = '''
const QString &s = m_StageNames[i];
p.drawText(boxrect, s, opts);
'''
        valid_viewer = '''
ui->pipeFlow->setStages(
    {lit("IA"), lit("VS")},
    {tr("Input Assembler"), tr("Vertex Shader")});
ui->pipeFlow->setStageName(1, lit("SO"), tr("Stream Out"));
'''
        with tempfile.TemporaryDirectory() as directory:
            repo, glossary, manifest = self._compact_flow_fixture(
                directory, valid_viewer, invalid_renderer
            )
            result = lint_ts.Result()
            lint_ts.lint_compact_pipeline_flows(repo, glossary, manifest, result)
            self.assertTrue(any("must be drawn from m_StageAbbrevs" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
