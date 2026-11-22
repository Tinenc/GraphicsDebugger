from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

import pytest

from replaylab_agent.acp_session import ACPMainAgentSession
from replaylab_agent.connectors.acp import ACPConnector, _connection_models


class Category(Enum):
    MODEL = "model"


def test_connection_models_reads_stable_grouped_config_options() -> None:
    session = {
        "configOptions": [
            {
                "id": "models",
                "category": Category.MODEL,
                "options": [
                    {
                        "name": "Recommended",
                        "options": [
                            {"value": "fast-model"},
                            {"value": "reasoning-model"},
                        ],
                    },
                    {"value": "fast-model"},
                ],
            }
        ]
    }

    assert _connection_models(session) == ["fast-model", "reasoning-model"]


def test_connection_models_reads_legacy_available_models_and_bounds_ids() -> None:
    session = SimpleNamespace(
        models=SimpleNamespace(
            available_models=[
                SimpleNamespace(model_id="legacy-model"),
                SimpleNamespace(model_id="x" * 513),
            ]
        )
    )

    assert _connection_models(session) == ["legacy-model"]


@pytest.mark.asyncio
async def test_catalogued_acp_authentication_method_is_sent_before_session_use(
    tmp_path,
) -> None:
    calls: list[str] = []

    class Connection:
        async def authenticate(self, *, method_id: str):
            calls.append(method_id)

    connector = ACPConnector(
        ["cursor-agent", "acp"],
        cwd=tmp_path,
        timeout=5,
        authentication_method="cursor_login",
    )
    await connector._authenticate(Connection())

    assert calls == ["cursor_login"]


@pytest.mark.asyncio
async def test_cursor_preauth_does_not_reenter_blocking_acp_login(tmp_path) -> None:
    calls: list[str] = []

    class Connection:
        async def authenticate(self, *, method_id: str):
            calls.append(method_id)

    connector = ACPConnector(
        ["cursor-agent", "acp"],
        cwd=tmp_path,
        timeout=5,
        authentication_method="cursor_login",
        provider_id="cursor-acp",
    )
    await connector._authenticate(Connection())

    assert calls == []


@pytest.mark.asyncio
async def test_model_selection_accepts_dict_and_plural_model_option(tmp_path) -> None:
    calls: list[dict] = []

    class Connection:
        async def set_config_option(self, **kwargs):
            calls.append(kwargs)

    session = ACPMainAgentSession(
        command=["agent"], cwd=tmp_path, env=None, allowed_tools=frozenset(), timeout=5
    )
    await session._select_model(
        Connection(),
        {
            "configOptions": [
                {
                    "id": "models",
                    "options": [{"value": "model-a"}],
                }
            ]
        },
        "session-1",
        "model-a",
    )

    assert calls == [
        {"config_id": "models", "session_id": "session-1", "value": "model-a"}
    ]
