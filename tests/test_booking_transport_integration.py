"""Actual runner + signing + requests preparation, with no external traffic."""

import json
import queue
import threading
import time
import unittest
from datetime import datetime
from types import SimpleNamespace

import requests

from seathunter.api.client import ApiClient
from seathunter.scheduler.booking_runner import BookingRunner


class BlockedTransport(requests.adapters.BaseAdapter):
    def __init__(self):
        self.calls = queue.Queue()
        self.release = threading.Event()

    def send(self, request, **kwargs):
        self.calls.put((time.monotonic(), request))
        if not self.release.wait(5):
            raise requests.ReadTimeout("test never released response")
        response = requests.Response()
        response.request = request
        response.status_code = 200
        response._content = json.dumps({"CODE": "ok", "MESSAGE": "created"}).encode()
        return response

    def close(self):
        pass


class BookingTransportIntegrationTests(unittest.TestCase):
    def test_four_requests_share_global_cadence_while_first_response_is_blocked(self):
        transport = BlockedTransport()
        session = requests.Session()
        self.addCleanup(session.close)
        session.trust_env = False
        session.params = {"LAB_JSON": "1"}
        session.cookies.set("session", "offline-session")
        session.mount("https://", transport)
        manager = SimpleNamespace(session=session, uid="123", base_url="https://test.invalid")

        class OfflineClient(ApiClient):
            def fork(self):
                client = super().fork()
                # Replace only transport, retaining the real forked session.
                client.session.mount("https://", transport)
                return client

        runner = BookingRunner(OfflineClient(manager), manager, interval=0.1,
                               max_try_times=20, max_inflight=4, retry_jitter_ratio=0)
        plan = SimpleNamespace(
            id="test", room_name="room", begin_time="10:00:00", duration_hours=11,
            seats=[SimpleNamespace(seat_id="62561", seat_num="421")],
        )
        results = []
        errors = []

        def book():
            try:
                results.extend(runner.run_booking([plan], datetime(2026, 9, 7)))
            except Exception as exc:
                errors.append(exc)

        runner.prepare()
        self.assertTrue(transport.calls.empty(), "preparing workers must not send requests")
        worker = threading.Thread(target=book)
        try:
            worker.start()
            calls = [transport.calls.get(timeout=2) for _ in range(4)]
            with self.assertRaises(queue.Empty):
                transport.calls.get(timeout=0.15)
            self.assertFalse(transport.release.is_set())
            for (a, _), (b, _) in zip(calls, calls[1:]):
                # Transport entry follows admission plus signing/encoding.
                self.assertGreaterEqual(b - a, 0.09)
            self.assertLess(calls[-1][0] - calls[0][0], 1.5)
            for _, request in calls:
                self.assertEqual(request.method, "POST")
                self.assertIn("session=offline-session", request.headers["Cookie"])
                self.assertEqual(int(request.headers["Content-Length"]), len(request.body.encode()))
            transport.release.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 4)
            self.assertTrue(all(result.created_by_this_run for result in results))
            self.assertTrue(transport.calls.empty(), "no fifth request after success")
        finally:
            runner.cancel()
            transport.release.set()
            worker.join(3)
            runner.close()


if __name__ == "__main__":
    unittest.main()
