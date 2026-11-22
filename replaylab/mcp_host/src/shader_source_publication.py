"""Keep saved organization drafts separate from replay restoration authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .shader_workspace.io import atomic_write_json


SOURCE_ONLY_PUBLICATION = "source-only-draft"


def source_only_restore_reason(
    knowledge: Mapping[str, Any], project_path: str | Path | None = None,
) -> str | None:
    """Recognize explicit drafts and exact legacy save-only session receipts.

    This is a negative admission check. A missing record never proves numeric
    equivalence, and an ABI cache never overrides an explicit source draft.
    Legacy accepted binaries keep their existing restoration behavior.
    """
    if knowledge.get("publicationMode") == SOURCE_ONLY_PUBLICATION:
        return "source-only-project-draft"
    # Once a legacy saved-only publication is identified, retain that decision
    # independently of the editable workspace/session files. Knowledge remains
    # immutable; a later accepted publication receives a different knowledge ID.
    source_sha = knowledge.get("sourceSha256")
    knowledge_id = knowledge.get("knowledgeId")
    marker_path = None
    marker_identity = {key: knowledge.get(key) for key in (
        "knowledgeId", "sourceSha256", "rawSha256", "stage", "entryPoint")}
    if (project_path and isinstance(knowledge_id, str)
            and re.fullmatch(r"[0-9a-f]{32}", knowledge_id)
            and isinstance(source_sha, str) and re.fullmatch(r"[0-9a-f]{64}", source_sha)):
        project = Path(project_path)
        if project.is_absolute() and project.is_dir() and not project.is_symlink():
            marker_path = project / "derived" / "source-only-publications" / (knowledge_id + ".json")
            try:
                marker_path.resolve().relative_to(project.resolve())
                if any(parent.is_symlink() for parent in (
                        marker_path.parent, marker_path.parent.parent)):
                    return "legacy-organization-publication-needs-review"
                if marker_path.exists():
                    if marker_path.is_symlink() or marker_path.stat().st_size > 16384:
                        return "legacy-organization-publication-needs-review"
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    if (marker.get("schema") == "bread-source-only-publication/v1"
                            and marker.get("identity") == marker_identity):
                        return "legacy-saved-organization-draft"
                    return "legacy-organization-publication-needs-review"
            except (OSError, ValueError, AttributeError):
                return "legacy-organization-publication-needs-review"
    if (knowledge.get("compiledCandidate") is not None
            or knowledge.get("registeredCompiledCandidatePresent") is True):
        return None
    workspace_id = knowledge.get("workspaceId")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
        return None
    root = Path(str(knowledge.get("workspacePath", "")))
    if not root.is_absolute() or root.is_symlink():
        return None

    def read(relative: str) -> dict[str, Any] | None:
        path = root / relative
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                return None
            path.resolve().relative_to(root.resolve())
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    # A new begin/restart can replace the root gate. The saved publication's
    # session is durable history and must still restrict that exact old source.
    sessions = root / "ai" / "organization-sessions"
    if sessions.is_symlink() or not sessions.is_dir():
        return None
    try:
        with os.scandir(sessions) as entries:
            names = [entry.name for entry in entries
                     if re.fullmatch(r"[0-9a-f]{32}", entry.name)
                     and entry.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    if len(names) > 512:
        return "legacy-organization-publication-needs-review"
    for organization_id in names:
        session = read("ai/organization-sessions/%s/session.json" % organization_id)
        if (session and session.get("schema") == "renderdoc-ai-shader-organization-session/v1"
                and session.get("workspaceId") == workspace_id
                and session.get("organizationId") == organization_id
                and session.get("originalRawSha256") == knowledge.get("rawSha256")
                and session.get("stage") == knowledge.get("stage")
                and session.get("entryPoint", "") == knowledge.get("entryPoint", "")
                and session.get("syncedDraftSourceSha256") == source_sha
                and session.get("savedDraftAt")
                and session.get("replayApplied") is False):
            if (session.get("state") == "completed"
                    and session.get("publicationContract") == "validated-source"
                    and (session.get("finalBinding") or {}).get("sourceSha256") == source_sha):
                continue
            if marker_path is not None:
                # Failure to persist must not grant replay authority for this
                # invocation. A readable workspace will be checked again later.
                try:
                    atomic_write_json(marker_path, {
                        "schema": "bread-source-only-publication/v1",
                        "identity": marker_identity, "organizationId": organization_id,
                        "reason": "legacy-saved-organization-draft",
                    })
                except OSError:
                    pass
            return "legacy-saved-organization-draft"
    return None

