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


def test_step_finish_usage_is_summed_across_steps():
    """Each step reports only its own usage; the job total is the sum.

    Regression: the accumulator used to overwrite ``tokens``/``cost`` on every
    ``step_finish``, so the dashboard showed the last step's 4k input instead of
    the job's real tens-of-thousands total.
    """
    acc = _EventAccumulator()
    acc.handle(
        json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "input": 100,
                        "output": 10,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                    "cost": 0.001,
                },
            }
        )
    )
    acc.handle(
        json.dumps(
            {
                "type": "step-finish",  # both spellings must accumulate
                "part": {
                    "tokens": {
                        "input": 50,
                        "output": 5,
                        "reasoning": 7,
                        "cache": {"read": 900, "write": 3},
                    },
                    "cost": 0.002,
                },
            }
        )
    )
    assert acc.tokens["input"] == 150
    assert acc.tokens["output"] == 15
    assert acc.tokens["reasoning"] == 7
    assert acc.tokens["cache"] == {"read": 900, "write": 3}
    assert acc.tokens["total"] == 150 + 15 + 7 + 900 + 3
    assert abs(acc.cost - 0.003) < 1e-12
