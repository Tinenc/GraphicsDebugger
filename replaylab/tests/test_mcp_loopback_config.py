"""Existing local Unity MCP endpoints remain local and credentials stay out of URLs."""
import pytest

from replaylab_agent.agent_tools_config import validate_mcp_server
from replaylab_agent.errors import ConfigurationError


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/mcp", "http://localhost:8080/mcp", "http://[::1]:8080/mcp", "https://example.test/mcp?project=one"])
def test_loopback_http_and_existing_https_are_accepted(url):
    assert validate_mcp_server({"id": "unity", "transport": "http", "url": url}).url == url


@pytest.mark.parametrize("url", ["http://example.test/mcp", "http://127.0.0.1.example.test/mcp", "http://localhost.example.test/mcp", "http://127.0.0.1:8080/mcp?token=secret", "https://user:secret@example.test/mcp", "http://[::1]:99999/mcp", "http://localhost/mcp#secret", "ws://127.0.0.1:8080/mcp"])
def test_insecure_or_credential_bearing_urls_are_rejected_without_echo(url):
    with pytest.raises(ConfigurationError) as error:
        validate_mcp_server({"id": "unity", "transport": "http", "url": url})
    assert "secret" not in str(error.value)
    assert url not in str(error.value)
