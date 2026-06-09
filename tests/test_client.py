from __future__ import annotations

from http.client import RemoteDisconnected
from unittest import TestCase
from unittest.mock import patch

from nba_play_recap.client import NbaStatsClient, NbaStatsError


class FakeResponse:
    headers = {"Content-Encoding": ""}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def read(self) -> bytes:
        return b'{"ok": true}'


class NbaStatsClientTests(TestCase):
    def test_get_json_from_url_retries_remote_disconnect(self) -> None:
        attempts = [RemoteDisconnected("closed"), FakeResponse()]
        client = NbaStatsClient(request_retries=2, retry_backoff_seconds=0)

        with patch("nba_play_recap.client.urlopen", side_effect=attempts) as urlopen_mock:
            payload = client.get_json_from_url("https://example.test/data")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_get_json_from_url_reports_remote_disconnect_after_retries(self) -> None:
        client = NbaStatsClient(request_retries=2, retry_backoff_seconds=0)

        with patch(
            "nba_play_recap.client.urlopen",
            side_effect=RemoteDisconnected("closed"),
        ):
            with self.assertRaises(NbaStatsError) as raised:
                client.get_json_from_url("https://example.test/data")

        self.assertIn("failed after 2 attempts", str(raised.exception))

