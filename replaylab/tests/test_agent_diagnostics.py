

def test_redaction_replaces_the_whole_value_not_a_fixed_prefix() -> None:
    """A log line must say a secret was there, and never any part of it.

    Keeping the first twelve characters of the *match* left the head of a short
    secret in the log, and for a short enough one left all of it; and an
    `Authorization: Bearer <token>` header had its scheme redacted and its token
    printed.
    """

    from replaylab_agent.agent_diagnostics import redact

    for line, secret, keyword in (
        ("api_key=sk-ant-api03-abcdefghijklmnop", "sk-ant", "api_key"),
        ("token=abcdef", "abcdef", "token"),
        ("password: hunter2000", "hunter2000", "password"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGci",
         "authorization"),
        ("bearer abcdefgh12345678", "abcdefgh", "bearer"),
    ):
        redacted = redact(line)
        assert secret not in redacted, redacted
        assert "[REDACTED]" in redacted
        # The keyword survives, so the line still says what kind of thing was there.
        assert keyword in redacted.lower()

    assert redact("nothing to hide here") == "nothing to hide here"


def test_quoted_credentials_and_url_userinfo_are_redacted_before_public_truncation() -> None:
    from replaylab_agent.agent_diagnostics import public_error, redact

    cases = (
        ('{"api_key": "CANARY_quoted_secret", "status": 401}', 'CANARY_quoted_secret'),
        ("{'authorization': 'Bearer CANARY_header_secret'}", 'CANARY_header_secret'),
        ('{"password": "CANARY with spaces \\" and escape"}', 'CANARY with spaces'),
        ('{"token": "xy"}', 'xy'),
        ('token=xy', 'xy'),
        ('https://CANARY_user:CANARY_password@localhost:1234/v1', 'CANARY'),
        ('https://CANARY_user%3Aencoded@localhost/v1', 'CANARY'),
        ('socks5://CANARY_user:CANARY_password@localhost:1234', 'CANARY'),
    )
    for line, secret in cases:
        redacted = redact(line)
        assert secret not in redacted
        assert "[REDACTED]" in redacted
        assert secret not in public_error(RuntimeError(line), limit=80)
    assert redact('https://user:password@localhost:1234/v1') == 'https://[REDACTED]@localhost:1234/v1'
    ordinary = 'https://localhost:1234/v1?model=example; status=401'
    assert redact(ordinary) == ordinary


def test_log_traceback_redacts_quoted_credentials_and_url_userinfo(tmp_path) -> None:
    from replaylab_agent.agent_diagnostics import close_logging, configure_logging, log_exception

    logger = configure_logging(tmp_path)
    try:
        try:
            raise RuntimeError('{"api_key": "CANARY_trace_secret"}; https://CANARY_user:password@localhost/v1')
        except RuntimeError as error:
            log_exception(logger, "synthetic provider failure", error)
    finally:
        close_logging()
    rendered = (tmp_path / 'agent-core.log').read_text(encoding='utf-8')
    assert 'CANARY' not in rendered
    assert 'localhost/v1' in rendered
    assert '[REDACTED]' in rendered
