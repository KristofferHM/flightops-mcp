"""Log redaction: a canary value must never survive to a log line.

This is the test that makes the GDPR claim checkable rather than aspirational.
The canary strings are deliberately absurd so a hit is unambiguous.
"""

from __future__ import annotations

import io
import json
import logging

from flightops_mcp.telemetry import configure_logging, get_logger, redaction_processor

CANARY_TOKEN = "CANARY-TOKEN-abc123"
CANARY_PNR = "CANARY-PNR-999"
CANARY_PAX = "CANARY-PAX-name"
CANARY_BEARER = "Bearer CANARY-BEARER-xyz789"


def test_keyed_secrets_and_pii_are_removed() -> None:
    event = {
        "event": "tool_call",
        "access_token": CANARY_TOKEN,
        "client_secret": CANARY_TOKEN,
        "passenger_name": CANARY_PAX,
        "pnr": CANARY_PNR,
        "tool": "airport_movements",
    }
    cleaned = redaction_processor(None, "info", dict(event))

    rendered = json.dumps(cleaned)
    assert "CANARY" not in rendered
    assert cleaned["tool"] == "airport_movements", "non-sensitive fields must survive"


def test_bearer_tokens_inside_free_text_are_removed() -> None:
    event = {"event": "upstream_request", "headers": {"Authorization": CANARY_BEARER}}
    rendered = json.dumps(redaction_processor(None, "info", dict(event)))
    assert "CANARY-BEARER" not in rendered


def test_a_bearer_string_in_a_plain_message_is_scrubbed() -> None:
    event = {"event": f"calling with Authorization: {CANARY_BEARER}"}
    cleaned = redaction_processor(None, "info", dict(event))
    assert "CANARY-BEARER" not in cleaned["event"]
    assert "[REDACTED]" in cleaned["event"]


def test_api_secrets_are_removed_from_nested_structures() -> None:
    event = {"event": "nested", "payload": {"items": [{"token": CANARY_TOKEN, "keep": 1}]}}
    rendered = json.dumps(redaction_processor(None, "info", dict(event)))
    assert "CANARY" not in rendered
    assert '"keep": 1' in rendered


def test_emitted_log_lines_are_clean() -> None:
    """Attach a handler, log a record full of secrets, and read what shipped."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    configure_logging("INFO")
    root.addHandler(handler)
    try:
        log = get_logger("canary.test")
        log.info(
            "tool_result",
            access_token=CANARY_TOKEN,
            passenger_name=CANARY_PAX,
            pnr=CANARY_PNR,
            authorization=CANARY_BEARER,
            airport="ESGG",
        )
    finally:
        root.removeHandler(handler)

    emitted = stream.getvalue()
    assert "CANARY" not in emitted, emitted
    assert "ESGG" in emitted, "the useful part of the record must still be there"
    json.loads(emitted.strip())  # the output stays machine-readable
