from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.shader_source_publication import source_only_restore_reason


class SourcePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.workspace = Path(self.temp.name) / "workspace"
        self.org = "c" * 32
        self.session = self.workspace / "ai" / "organization-sessions" / self.org / "session.json"
        self.session.parent.mkdir(parents=True)
        self.knowledge = {
            "knowledgeId": "a" * 32, "workspaceId": "b" * 32,
            "workspacePath": str(self.workspace), "sourceSha256": "1" * 64,
            "rawSha256": "2" * 64, "stage": "fragment", "entryPoint": "MainPS",
        }
        self.saved = {
            "schema": "renderdoc-ai-shader-organization-session/v1",
            "organizationId": self.org, "workspaceId": "b" * 32,
            "originalRawSha256": "2" * 64, "stage": "fragment", "entryPoint": "MainPS",
            "syncedDraftSourceSha256": "1" * 64, "savedDraftAt": "2026-10-19T07:50:22Z",
            "replayApplied": False, "state": "drafting",
        }
        self.write_session()

    def write_session(self):
        self.session.write_text(json.dumps(self.saved), encoding="utf-8")

    def test_explicit_source_only_blocks_even_an_abi_cache(self):
        self.knowledge.update(publicationMode="source-only-draft", compiledCandidate={"rawSha256": "3" * 64})
        self.assertEqual(source_only_restore_reason(self.knowledge), "source-only-project-draft")

    def test_legacy_saved_source_survives_restart_and_session_removal(self):
        gate = self.workspace / "ai" / "organize-code-gate.json"
        gate.write_text(json.dumps({"organizationId": "d" * 32, "complete": False}), encoding="utf-8")
        self.assertEqual(source_only_restore_reason(self.knowledge, self.project), "legacy-saved-organization-draft")
        marker = self.project / "derived" / "source-only-publications" / ("a" * 32 + ".json")
        self.assertTrue(marker.is_file())
        self.session.unlink()
        self.knowledge["workspacePath"] = str(self.workspace / "deleted-workspace")
        self.knowledge["compiledCandidate"] = {"rawSha256": "3" * 64}
        self.assertEqual(source_only_restore_reason(self.knowledge, self.project), "legacy-saved-organization-draft")

    def test_accepted_publication_receives_a_different_identity(self):
        self.assertEqual(source_only_restore_reason(self.knowledge, self.project), "legacy-saved-organization-draft")
        accepted = dict(self.knowledge, knowledgeId="d" * 32, compiledCandidate={"rawSha256": "3" * 64})
        self.assertIsNone(source_only_restore_reason(accepted, self.project))

    def test_completed_exact_validated_source_is_not_a_failed_draft(self):
        self.saved.update(state="completed", publicationContract="validated-source",
                          finalBinding={"sourceSha256": self.knowledge["sourceSha256"]})
        self.write_session()
        self.assertIsNone(source_only_restore_reason(self.knowledge, self.project))
        self.saved["finalBinding"]["sourceSha256"] = "4" * 64
        self.write_session()
        self.assertEqual(source_only_restore_reason(self.knowledge, self.project), "legacy-saved-organization-draft")

    def test_other_source_or_shader_session_does_not_reject_known_source(self):
        for field, value in (("syncedDraftSourceSha256", "4" * 64), ("originalRawSha256", "4" * 64),
                             ("workspaceId", "d" * 32), ("stage", "vertex"), ("entryPoint", "other")):
            with self.subTest(field=field):
                original = self.saved[field]
                self.saved[field] = value
                self.write_session()
                self.assertIsNone(source_only_restore_reason(self.knowledge, self.project))
                self.saved[field] = original

    def test_historical_registered_binary_is_not_reclassified_by_old_draft(self):
        for marker in ({"compiledCandidate": {"rawSha256": "3" * 64}},
                       {"registeredCompiledCandidatePresent": True}):
            with self.subTest(marker=marker):
                self.assertIsNone(source_only_restore_reason(dict(self.knowledge, **marker), self.project))
