"""A Skill the AI writes is either loadable or refused with the reason.

`apply_configuration_change` used to validate only the id and the size, so a
document without front matter was written, reported as "wrote Skill x, enabled
Skill x", and quarantined at the next task with a content-free code: the user saw
nothing, and the AI believed it had installed something.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src import replaylab_configuration as configuration


class WrittenSkillValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tools_path = Path(self.temporary.name) / "agent-tools.json"

    def _apply(self, content: str) -> dict:
        return configuration.apply_configuration_change(
            write_skills=[{"id": "my-skill", "content": content}],
            tools_path=self.tools_path,
        )

    def test_a_document_without_front_matter_is_refused_with_the_rule(self) -> None:
        result = self._apply("# My procedure\n1. Look at the shader.\n")
        rejected = " ".join(result.get("rejected") or [])
        self.assertIn("front matter", rejected)
        self.assertIn("name: my-skill", rejected)
        self.assertFalse((configuration.user_skill_overlay_root(self.tools_path)
                          / "my-skill" / "SKILL.md").exists())

    def test_a_name_that_differs_from_the_id_is_refused(self) -> None:
        result = self._apply("---\nname: other\ndescription: x\n---\nbody\n")
        self.assertIn("name: my-skill", " ".join(result.get("rejected") or []))

    def test_a_loadable_document_is_written(self) -> None:
        result = self._apply(
            "---\nname: my-skill\ndescription: Explains one pass.\n---\n\n# Steps\n1. Look.\n")
        self.assertEqual(result.get("rejected") or [], [])
        document = configuration.user_skill_overlay_root(self.tools_path) / "my-skill" / "SKILL.md"
        self.assertTrue(document.is_file())


class McpServerUpsertTests(unittest.TestCase):
    """The AI can register the same server shapes Settings can, and toggle in place."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tools_path = Path(self.temporary.name) / "agent-tools.json"

    def _servers(self) -> list[dict]:
        import json
        return json.loads(self.tools_path.read_text(encoding="utf-8")).get("mcpServers") or []

    def test_an_https_server_is_registered_and_a_plain_http_one_refused(self) -> None:
        result = configuration.apply_configuration_change(
            upsert_mcp_servers=[
                {"id": "docs", "url": "https://docs.example/mcp", "title": "Docs"},
                {"id": "plain", "url": "http://insecure.example/mcp"},
            ],
            tools_path=self.tools_path,
        )
        self.assertIn("configured MCP server docs", result["changed"])
        self.assertTrue(any("https://" in item for item in result["rejected"]))
        servers = self._servers()
        self.assertEqual([item["id"] for item in servers], ["docs"])
        self.assertEqual(servers[0]["transport"], "http")
        self.assertEqual(servers[0]["url"], "https://docs.example/mcp")

    def test_a_toggle_keeps_the_connection_details(self) -> None:
        configuration.apply_configuration_change(
            upsert_mcp_servers=[{"id": "fs", "command": "npx", "args": ["-y", "server-fs"],
                                 "env": {"ROOT": "D:/x"}}],
            tools_path=self.tools_path,
        )
        result = configuration.apply_configuration_change(
            upsert_mcp_servers=[{"id": "fs", "enabled": False}],
            tools_path=self.tools_path,
        )
        self.assertIn("disabled MCP server fs", result["changed"])
        (server,) = self._servers()
        self.assertFalse(server["enabled"])
        self.assertEqual(server["command"], "npx")
        self.assertEqual(server["args"], ["-y", "server-fs"])
        self.assertEqual(server["env"], {"ROOT": "D:/x"})

    def test_ids_the_agent_loader_would_drop_are_refused_now(self) -> None:
        result = configuration.apply_configuration_change(
            upsert_mcp_servers=[
                {"id": "Bad Id", "command": "x"},
                {"id": "replaylab", "command": "x"},
                {"id": "ok", "command": "x", "transport": "websocket"},
            ],
            tools_path=self.tools_path,
        )
        self.assertEqual(result["changed"], [])
        joined = " ".join(result["rejected"])
        self.assertIn("lower-case", joined)
        self.assertIn("built-in server id", joined)
        self.assertIn("transport must be", joined)
        self.assertFalse(self.tools_path.exists())


class LoaderParityTests(unittest.TestCase):
    """What the host accepts, the agent loader admits — checked against the loader itself."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tools_path = Path(self.temporary.name) / "agent-tools.json"

    def test_every_accepted_mcp_server_loads(self) -> None:
        from replaylab_agent.agent_tools_config import validate_mcp_server

        result = configuration.apply_configuration_change(
            upsert_mcp_servers=[
                {"id": "good", "command": "npx", "args": ["-y", "x"], "env": {"A_B": "1"}},
                {"id": "multi", "command": "npx", "note": "line one\nline two"},
                {"id": "badenv", "command": "npx", "env": {"1BAD": "x"}},
                {"id": "toomany", "command": "npx", "env": {"V%d" % i: "x" for i in range(40)}},
                {"id": "web", "url": "https://x.example/mcp", "title": "Web"},
            ],
            tools_path=self.tools_path,
        )
        import json
        servers = json.loads(self.tools_path.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(sorted(item["id"] for item in servers), ["good", "web"])
        for item in servers:
            validate_mcp_server(item)  # must not raise
        self.assertEqual(len(result["rejected"]), 3)

    def test_every_accepted_skill_loads(self) -> None:
        from replaylab_agent.skills import SkillCatalog

        good = "---\nname: ue-hair\ndescription: Hair passes.\nmetadata:\n  replaylab-category: rendering\n---\n\n# Steps\n1. Look.\n"
        bad_category = good.replace("rendering", "Not Valid")
        bad_version = good.replace("replaylab-category: rendering", "replaylab-version: zero")
        result = configuration.apply_configuration_change(
            write_skills=[
                {"id": "ue-hair", "content": good},
                {"id": "ue_hair", "content": good.replace("ue-hair", "ue_hair")},
                {"id": "ue-hair-b", "content": bad_category.replace("ue-hair", "ue-hair-b")},
                {"id": "ue-hair-c", "content": bad_version.replace("ue-hair", "ue-hair-c")},
            ],
            tools_path=self.tools_path,
        )
        self.assertEqual(result["writtenSkills"], [str(
            configuration.user_skill_overlay_root(self.tools_path) / "ue-hair" / "SKILL.md")])
        self.assertEqual(len(result["rejected"]), 3)
        # The strict loader raises on anything it would not admit.
        catalog = SkillCatalog([configuration.user_skill_overlay_root(self.tools_path)]).load()
        self.assertEqual(sorted(catalog.skills), ["ue-hair"])
