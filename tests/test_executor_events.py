"""The executor's OpenCode event parsing, especially error reporting.

A failed job must surface OpenCode's real error message, not a bare
"unknown error" (which is what the worker reported for a transport/socket
error and made the failure opaque).
"""

import json

from worker.app.executor import _EventAccumulator


def test_transport_error_is_reported_not_masked_as_unknown_error():
    acc = _EventAccumulator()
    line = json.dumps(
        {
            "type": "error",
            "sessionID": "ses_x",
            "error": {
                "type": "unknown",
                "message": (
                    "Transport: The socket connection was closed unexpectedly. "
                    "For more information, pass `verbose: true` in the second "
                    "argument to fetch()"
                ),
            },
        }
    )
    acc.handle(line)
    assert "Transport: The socket connection was closed unexpectedly" in acc.error_text()
    assert acc.error_text() != "unknown error"


def test_named_error_with_data_message_and_ref():
    acc = _EventAccumulator()
    acc.handle(
        json.dumps(
            {
                "type": "error",
                "error": {
                    "name": "ProviderError",
                    "data": {"message": "boom", "ref": "req_1"},
                },
            }
        )
    )
    assert acc.error_text() == "boom (ref req_1)"


def test_error_without_a_message_falls_back_to_type_then_unknown():
    acc = _EventAccumulator()
    acc.handle(json.dumps({"type": "error", "error": {"type": "unknown"}}))
    assert acc.error_text() == "unknown"

    acc2 = _EventAccumulator()
    acc2.handle(json.dumps({"type": "error", "error": {}}))
    assert acc2.error_text() == "unknown error"
