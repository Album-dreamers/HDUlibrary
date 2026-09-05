"""Exercise actual requests preparation using an offline HTTP adapter."""

import base64
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import patch

import requests

from seathunter.api.client import ApiClient, retry_after_seconds


class OfflineAdapter(requests.adapters.BaseAdapter):
    def __init__(self, status=200, body=None, headers=None, error=None):
        self.status = status
        self.body = {"CODE": "ok", "MESSAGE": "created"} if body is None else body
        self.headers = headers or {}
        self.error = error
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        if self.error:
            raise self.error
        response = requests.Response()
        response.request = request
        response.status_code = self.status
        response.headers.update(self.headers)
        response._content = (json.dumps(self.body) if not isinstance(self.body, str)
                             else self.body).encode()
        return response

    def close(self):
        pass


class ApiTransportTests(unittest.TestCase):
    def setUp(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.params = {"LAB_JSON": "1"}
        self.session.cookies.set("session", "offline-cookie")
        self.client = ApiClient(SimpleNamespace(
            session=self.session, base_url="https://library.example.invalid",
        ))
        self.addCleanup(self.session.close)

    def call(self, adapter, **kwargs):
        self.session.mount("https://", adapter)
        return self.client.book_seat(datetime(2026, 9, 7, 10), 11, ["62561"], ["123"], **kwargs)

    def test_body_length_and_signature_match_without_mutating_session_headers(self):
        adapter = OfflineAdapter()
        self.session.headers["Api-Token"] = "original"
        self.session.headers["Content-Length"] = "114"
        original_headers = dict(self.session.headers)
        result = self.call(adapter)
        self.assertEqual(result["CODE"], "ok")
        request, options = adapter.calls[0]
        data = parse_qs(request.body)
        signature = (
            "post&/Seat/Index/bookSeats?LAB_JSON=1"
            f"&api_time{data['api_time'][0]}&beginTime{data['beginTime'][0]}"
            f"&duration{data['duration'][0]}&is_recommend0"
            f"&seatBookers[0]{data['seatBookers[0]'][0]}&seats[0]{data['seats[0]'][0]}"
        )
        expected = base64.b64encode(hashlib.md5(signature.encode()).hexdigest().encode()).decode()
        self.assertEqual(request.headers["Api-Token"], expected)
        self.assertEqual(int(request.headers["Content-Length"]), len(request.body.encode()))
        self.assertEqual(dict(self.session.headers), original_headers)
        self.assertIn("session=offline-cookie", request.headers["Cookie"])
        self.assertEqual(options["timeout"], (3.05, 30))

    def test_rejected_admission_never_generates_token_or_sends_post(self):
        adapter = OfflineAdapter()
        with patch("seathunter.api.client.generate_booking_data") as sign:
            self.assertEqual(self.call(adapter, before_send=lambda: False)["CODE"], "not_sent")
        sign.assert_not_called()
        self.assertEqual(adapter.calls, [])

    def test_signing_happens_after_waiting_for_admission(self):
        events = []
        from seathunter.api.token import generate_booking_data

        def sign(*args):
            events.append("sign")
            return generate_booking_data(*args)

        def admit():
            events.append("admit")
            return True

        with patch("seathunter.api.client.generate_booking_data", side_effect=sign):
            self.call(OfflineAdapter(), before_send=admit)
        self.assertEqual(events, ["admit", "sign"])

    def test_cancellation_during_payload_preparation_sends_nothing(self):
        adapter = OfflineAdapter()
        allowed = [True]

        def sign(*args):
            allowed[0] = False
            return {"data": "unused"}, "unused"

        with patch("seathunter.api.client.generate_booking_data", side_effect=sign):
            result = self.call(adapter, before_send=lambda: True, can_send=lambda: allowed[0])
        self.assertEqual(result["CODE"], "not_sent")
        self.assertEqual(adapter.calls, [])

    def test_fork_isolates_mutable_state_and_connection_pool(self):
        fork = self.client.fork()
        self.addCleanup(fork.close)
        self.assertIsNot(fork.session, self.session)
        self.assertIsNot(fork.session.adapters["https://"], self.session.adapters["https://"])
        fork.session.headers["X-Test"] = "worker"
        fork.session.cookies.set("session", "changed")
        fork.session.params["LAB_JSON"] = "changed"
        self.assertNotIn("X-Test", self.session.headers)
        self.assertEqual(self.session.cookies.get("session"), "offline-cookie")
        self.assertEqual(self.session.params["LAB_JSON"], "1")
        self.assertFalse(fork.session.trust_env)

    def test_http_429_preserves_retry_after_even_with_html_body(self):
        result = self.call(OfflineAdapter(429, "<html>busy</html>", {"Retry-After": "3"}))
        self.assertEqual(result["CODE"], "429")
        self.assertEqual(result["_retry_after_seconds"], 3)

    def test_post_redirect_is_not_followed_or_claimed_as_success(self):
        adapter = OfflineAdapter(307, headers={"Location": "https://library.example.invalid/other"})
        self.assertEqual(self.call(adapter)["CODE"], "307")
        self.assertEqual(len(adapter.calls), 1)

    def test_non_json_or_http_failure_is_not_success(self):
        for adapter in (OfflineAdapter(200, "html"), OfflineAdapter(503), OfflineAdapter(200, [1])):
            self.assertNotEqual(self.call(adapter)["CODE"], "ok")

    def test_timeout_does_not_create_an_automatic_transport_retry(self):
        adapter = OfflineAdapter(error=requests.ReadTimeout("timed out"))
        self.assertEqual(self.call(adapter)["CODE"], "error")
        self.assertEqual(len(adapter.calls), 1)

    def test_retry_after_parsing(self):
        self.assertEqual(retry_after_seconds("3"), 3)
        self.assertEqual(retry_after_seconds("-1"), 0)
        for value in (None, "invalid", "nan", "inf"):
            self.assertIsNone(retry_after_seconds(value))
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
        self.assertTrue(8 <= retry_after_seconds(future) <= 10)


if __name__ == "__main__":
    unittest.main()
