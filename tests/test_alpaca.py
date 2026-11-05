"""Retry behaviour of the stdlib Alpaca client: reads retry, orders never do."""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "aws" / "src"))
from shared.alpaca import Alpaca, AlpacaError


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "error", {}, io.BytesIO(b"detail"))


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.client = Alpaca("key", "secret")
        sleep = patch("shared.alpaca.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def respond(self, *outcomes):
        """Patch urlopen to raise or return each outcome in turn."""
        calls = []

        def urlopen(request, timeout):
            calls.append(request.get_method())
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return Response(outcome)

        p = patch("shared.alpaca.urllib.request.urlopen", side_effect=urlopen)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_read_retries_a_server_error_then_succeeds(self):
        calls = self.respond(http_error(503), b'{"is_open": true}')
        self.assertEqual(self.client.clock(), {"is_open": True})
        self.assertEqual(len(calls), 2)
        self.sleep.assert_called_once_with(1)

    def test_read_retries_a_network_failure(self):
        calls = self.respond(urllib.error.URLError("reset"), b"{}")
        self.assertEqual(self.client.account(), {})
        self.assertEqual(len(calls), 2)

    def test_read_gives_up_after_three_attempts(self):
        calls = self.respond(http_error(503), http_error(503), http_error(503))
        with self.assertRaises(AlpacaError) as ctx:
            self.client.clock()
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(len(calls), 3)

    def test_client_errors_are_not_retried(self):
        calls = self.respond(http_error(404))
        self.assertIsNone(self.client.order_by_client_id("missing"))
        self.assertEqual(len(calls), 1)

    def test_order_submission_is_never_retried(self):
        calls = self.respond(http_error(503), b'{"id": "should-not-be-reached"}')
        with self.assertRaises(AlpacaError):
            self.client.submit_order("A", 1, "buy", client_order_id="bot-1")
        self.assertEqual(calls, ["POST"])
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
