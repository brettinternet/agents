from __future__ import annotations

import unittest

from agents.execution import (
    ExecutionBusy,
    ExecutionConflict,
    ExecutionNotFound,
    ExecutionProtocolError,
    ExecutionTimeout,
    ExecutionUnauthorized,
    ExecutionUnavailable,
)


class ExecutionErrorTests(unittest.TestCase):
    def test_error_taxonomy_preserves_provider_details(self) -> None:
        cases = (
            (ExecutionUnavailable("offline", "down"), True, False),
            (ExecutionTimeout("timeout", "late"), True, False),
            (ExecutionTimeout("timeout", "late", outcome_unknown=True), True, True),
            (ExecutionBusy("blocked", "busy"), True, False),
            (ExecutionNotFound("missing", "gone"), False, False),
            (ExecutionUnauthorized("denied", "no"), False, False),
            (ExecutionProtocolError("shape", "bad"), False, False),
            (ExecutionConflict("duplicate", "many", outcome_unknown=True), False, True),
        )
        for error, retryable, outcome_unknown in cases:
            with self.subTest(type=type(error).__name__):
                self.assertEqual(error.code, str(error).split(":", 1)[0])
                self.assertEqual(error.retryable, retryable)
                self.assertEqual(error.outcome_unknown, outcome_unknown)


if __name__ == "__main__":
    unittest.main()
