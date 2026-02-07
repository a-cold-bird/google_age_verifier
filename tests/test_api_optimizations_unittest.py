import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import age_verification_api as api  # noqa: E402


class ApiOptimizationTests(unittest.TestCase):
    def setUp(self):
        api.TURNSTILE_SECRET_KEY = None
        api.API_KEY = ""
        with api.tasks_lock:
            api.tasks.clear()
        with api.rate_limit_lock:
            api.rate_limit_buckets.clear()
            api.rate_limit_last_seen.clear()

    def test_append_log_is_capped(self):
        task_id = api.create_task(
            "https://age-verification.privateid.com/path",
            {"device": "pixel_7_pro"},
        )
        for i in range(api.MAX_TASK_LOGS + 25):
            api.append_log(task_id, f"log-{i}")

        task = api.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(len(task["logs"]), api.MAX_TASK_LOGS)
        self.assertTrue(task["logs"][-1].endswith("log-224"))

    def test_verify_batch_rejects_invalid_url(self):
        client = api.app.test_client()
        resp = client.post(
            "/verify/batch",
            json={"urls": ["https://example.com/not-privateid"]},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["success"])

    def test_verify_batch_clamps_max_concurrent(self):
        client = api.app.test_client()
        with patch.object(api.executor, "submit", return_value=None):
            resp = client.post(
                "/verify/batch",
                json={
                    "urls": [
                        "https://age-verification.privateid.com/a",
                        "https://age-verification.privateid.com/b",
                    ],
                    "max_concurrent": 0,
                },
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(len(body["tasks"]), 2)

        batch_tasks = api.get_batch_tasks(body["batch_id"])
        self.assertEqual(len(batch_tasks), 2)
        for task in batch_tasks:
            self.assertEqual(task["config"]["max_concurrent"], 1)

    def test_list_tasks_invalid_limit(self):
        client = api.app.test_client()
        resp = client.get("/tasks?limit=abc")
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["success"])

    def test_prune_removes_stale_terminal_tasks(self):
        old_time = (datetime.now() - timedelta(hours=10)).isoformat()
        task_id = api.create_task(
            "https://age-verification.privateid.com/old",
            {"device": "pixel_7_pro"},
        )
        api.update_task(
            task_id,
            status=api.TaskStatus.COMPLETED,
            completed_at=old_time,
            result={"success": True},
        )

        api.prune_tasks()
        self.assertIsNone(api.get_task(task_id))

    def test_stats_local_request_bypasses_auth(self):
        client = api.app.test_client()
        resp = client.get("/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])

    def test_remote_request_requires_credentials(self):
        client = api.app.test_client()
        resp = client.get(
            "/stats",
            base_url="http://example.com",
            environ_base={"REMOTE_ADDR": "8.8.8.8"},
            headers={"User-Agent": "unittest-client"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.get_json()["success"])

    def test_remote_client_token_can_access_stats(self):
        client = api.app.test_client()
        common_headers = {"User-Agent": "unittest-client"}
        common_environ = {"REMOTE_ADDR": "8.8.8.8"}

        token_resp = client.post(
            "/auth/client-token",
            json={},
            base_url="http://example.com",
            environ_base=common_environ,
            headers=common_headers,
        )
        self.assertEqual(token_resp.status_code, 200)
        token_body = token_resp.get_json()
        self.assertTrue(token_body["success"])
        self.assertTrue(token_body["token"])

        stats_resp = client.get(
            "/stats",
            base_url="http://example.com",
            environ_base=common_environ,
            headers={
                "User-Agent": "unittest-client",
                "X-Client-Token": token_body["token"],
            },
        )
        self.assertEqual(stats_resp.status_code, 200)
        self.assertTrue(stats_resp.get_json()["success"])

    def test_rate_limit_blocks_excessive_remote_calls(self):
        client = api.app.test_client()
        common_headers = {"User-Agent": "unittest-client"}
        common_environ = {"REMOTE_ADDR": "9.9.9.9"}
        old_limit = api.RATE_LIMIT_MAX_REQUESTS
        old_window = api.RATE_LIMIT_WINDOW_SECONDS
        api.RATE_LIMIT_MAX_REQUESTS = 2
        api.RATE_LIMIT_WINDOW_SECONDS = 60

        try:
            for _ in range(2):
                resp = client.post(
                    "/auth/client-token",
                    json={},
                    base_url="http://example.com",
                    environ_base=common_environ,
                    headers=common_headers,
                )
                self.assertEqual(resp.status_code, 200)

            blocked = client.post(
                "/auth/client-token",
                json={},
                base_url="http://example.com",
                environ_base=common_environ,
                headers=common_headers,
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertFalse(blocked.get_json()["success"])
        finally:
            api.RATE_LIMIT_MAX_REQUESTS = old_limit
            api.RATE_LIMIT_WINDOW_SECONDS = old_window

    def test_spoofed_forwarded_for_cannot_bypass_local_bypass(self):
        client = api.app.test_client()
        resp = client.get(
            "/stats",
            base_url="http://example.com",
            environ_base={"REMOTE_ADDR": "8.8.8.8"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.get_json()["success"])

    def test_spoofed_localhost_host_cannot_bypass_local_bypass(self):
        client = api.app.test_client()
        resp = client.get(
            "/stats",
            base_url="http://localhost",
            environ_base={"REMOTE_ADDR": "8.8.8.8"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.get_json()["success"])

    def test_unauthorized_requests_are_rate_limited(self):
        client = api.app.test_client()
        old_limit = api.RATE_LIMIT_MAX_REQUESTS
        old_window = api.RATE_LIMIT_WINDOW_SECONDS
        api.RATE_LIMIT_MAX_REQUESTS = 2
        api.RATE_LIMIT_WINDOW_SECONDS = 60
        try:
            for _ in range(2):
                resp = client.get(
                    "/stats",
                    base_url="http://example.com",
                    environ_base={"REMOTE_ADDR": "8.8.8.8"},
                )
                self.assertEqual(resp.status_code, 401)

            blocked = client.get(
                "/stats",
                base_url="http://example.com",
                environ_base={"REMOTE_ADDR": "8.8.8.8"},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertFalse(blocked.get_json()["success"])
        finally:
            api.RATE_LIMIT_MAX_REQUESTS = old_limit
            api.RATE_LIMIT_WINDOW_SECONDS = old_window


if __name__ == "__main__":
    unittest.main()
