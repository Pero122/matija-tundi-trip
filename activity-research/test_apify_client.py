from __future__ import annotations

from decimal import Decimal
import json
import subprocess
import unittest
from urllib.parse import parse_qs, urlparse

from apify_client import (
    ApifyClient,
    ApifyConfigurationError,
    ApifyCostLimitError,
    ApifyHttpError,
    ApifyProtocolError,
    ApifyRunFailedError,
    ApifyRunTimeoutError,
    ApifyTransportError,
    TransportResponse,
    load_apify_token,
)


def response(payload, *, status=200, headers=None):
    return TransportResponse(
        status_code=status,
        headers=headers or {},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError(f"Unexpected request: {request.method} {request.full_url}")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(request, timeout)
        return value


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class TokenTests(unittest.TestCase):
    def test_keychain_is_checked_first_for_current_user(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="keychain-secret\n", stderr="")

        token = load_apify_token(
            environ={"APIFY_TOKEN": "environment-secret"},
            username="matija",
            keychain_runner=runner,
        )

        self.assertEqual(token, "keychain-secret")
        self.assertEqual(
            calls[0][0],
            [
                "security",
                "find-generic-password",
                "-s",
                "APIFY_TOKEN",
                "-a",
                "matija",
                "-w",
            ],
        )
        self.assertTrue(calls[0][1]["capture_output"])
        self.assertFalse(calls[0][1]["check"])

    def test_environment_is_used_when_keychain_lookup_fails(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 44, stdout="", stderr="ignored")

        self.assertEqual(
            load_apify_token(
                environ={"APIFY_TOKEN": " env-secret "},
                username="matija",
                keychain_runner=runner,
            ),
            "env-secret",
        )

    def test_missing_token_raises_without_exposing_subprocess_output(self):
        def runner(command, **kwargs):
            raise FileNotFoundError("security is unavailable")

        with self.assertRaisesRegex(ApifyConfigurationError, "APIFY_TOKEN") as caught:
            load_apify_token(
                environ={}, username="matija", keychain_runner=runner
            )
        self.assertNotIn("security is unavailable", str(caught.exception))


class RequestTests(unittest.TestCase):
    def test_usage_and_plan_lookup_preserves_exact_payloads(self):
        user = {"id": "u1", "plan": {"id": "FREE", "tier": "FREE"}}
        limits = {
            "monthlyUsageCycle": {"startAt": "a", "endAt": "b"},
            "limits": {"maxMonthlyUsageUsd": 5},
            "current": {"monthlyUsageUsd": 1.25},
        }
        transport = FakeTransport(response({"data": user}), response({"data": limits}))
        client = ApifyClient(token="secret-token", transport=transport)

        result = client.get_current_usage_plan()

        self.assertEqual(result.user, user)
        self.assertEqual(result.limits, limits)
        self.assertEqual(result.plan, user["plan"])
        self.assertEqual(result.current_usage, limits["current"])
        self.assertEqual(result.account_limits, limits["limits"])
        self.assertTrue(transport.requests[0][0].full_url.endswith("/users/me"))
        self.assertTrue(
            transport.requests[1][0].full_url.endswith("/users/me/limits")
        )
        for request, timeout in transport.requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
            self.assertNotIn("secret-token", request.full_url)
            self.assertEqual(timeout, 30.0)

    def test_actor_metadata_lookup_quotes_path_but_keeps_owner_separator(self):
        actor = {"id": "a1", "name": "crawler"}
        transport = FakeTransport(response({"data": actor}))
        client = ApifyClient(token="secret", transport=transport)

        self.assertEqual(client.get_actor("owner~actor name"), actor)
        self.assertTrue(
            transport.requests[0][0].full_url.endswith("/actors/owner~actor%20name")
        )

    def test_http_error_is_structured_rate_limit_aware_and_token_redacted(self):
        transport = FakeTransport(
            response(
                {
                    "error": {
                        "type": "rate-limit-exceeded",
                        "message": "token secret-token exceeded a limit",
                    }
                },
                status=429,
                headers={"retry-after": "12"},
            )
        )
        client = ApifyClient(token="secret-token", transport=transport)

        with self.assertRaises(ApifyHttpError) as caught:
            client.get_actor("actor")

        error = caught.exception
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.error_type, "rate-limit-exceeded")
        self.assertEqual(error.retry_after, "12")
        self.assertNotIn("secret-token", str(error))
        self.assertNotIn("secret-token", error.url)
        self.assertIn("[REDACTED]", error.server_message)

    def test_invalid_success_json_and_bad_transport_are_clear_errors(self):
        invalid_json = TransportResponse(200, {}, b"not-json")
        client = ApifyClient(token="secret", transport=FakeTransport(invalid_json))
        with self.assertRaisesRegex(ApifyProtocolError, "valid UTF-8 JSON"):
            client.get_actor("actor")

        client = ApifyClient(token="secret", transport=lambda request, timeout: object())
        with self.assertRaisesRegex(ApifyTransportError, "TransportResponse"):
            client.get_actor("actor")


class CostGuardTests(unittest.TestCase):
    def limits_response(self, cap=10, usage=3):
        return response(
            {
                "data": {
                    "limits": {"maxMonthlyUsageUsd": cap},
                    "current": {"monthlyUsageUsd": usage},
                }
            }
        )

    def test_start_is_async_and_sends_both_hard_caps(self):
        started = {
            "id": "run-1",
            "status": "READY",
            "defaultDatasetId": "dataset-1",
        }
        transport = FakeTransport(
            self.limits_response(cap="20.00", usage="3.25"),
            response({"data": started}),
        )
        client = ApifyClient(token="secret", transport=transport)
        run_input = {"location": "Magyarország", "languages": ["hu", "en"]}

        self.assertEqual(
            client.start_actor(
                "owner~crawler",
                run_input,
                max_items=250,
                max_total_charge_usd="2.50",
            ),
            started,
        )

        self.assertEqual(len(transport.requests), 2)
        request = transport.requests[1][0]
        parsed = urlparse(request.full_url)
        self.assertEqual(request.method, "POST")
        self.assertEqual(parsed.path, "/v2/actors/owner~crawler/runs")
        self.assertEqual(
            parse_qs(parsed.query),
            {"maxItems": ["250"], "maxTotalChargeUsd": ["2.5"]},
        )
        self.assertEqual(json.loads(request.data), run_input)
        self.assertEqual(
            request.get_header("Content-type"), "application/json; charset=utf-8"
        )

    def test_account_monthly_remainder_blocks_run_before_post(self):
        transport = FakeTransport(self.limits_response(cap="5", usage="4.25"))
        client = ApifyClient(token="secret", transport=transport)

        with self.assertRaises(ApifyCostLimitError) as caught:
            client.start_actor(
                "actor", {}, max_items=10, max_total_charge_usd="0.76"
            )

        self.assertEqual(caught.exception.requested_usd, Decimal("0.76"))
        self.assertEqual(caught.exception.remaining_usd, Decimal("0.75"))
        self.assertEqual(len(transport.requests), 1)

    def test_local_monthly_hard_cap_intersects_account_cap(self):
        transport = FakeTransport(self.limits_response(cap="100", usage="3.5"))
        client = ApifyClient(
            token="secret", monthly_hard_cap_usd="4", transport=transport
        )

        with self.assertRaises(ApifyCostLimitError) as caught:
            client.start_actor(
                "actor", {}, max_items=1, max_total_charge_usd="0.51"
            )

        self.assertEqual(caught.exception.monthly_hard_cap_usd, Decimal("4"))
        self.assertEqual(caught.exception.remaining_usd, Decimal("0.5"))
        self.assertEqual(len(transport.requests), 1)

    def test_malformed_limits_fail_closed(self):
        transport = FakeTransport(response({"data": {"limits": {}, "current": {}}}))
        client = ApifyClient(token="secret", transport=transport)

        with self.assertRaisesRegex(ApifyProtocolError, "refusing to run"):
            client.start_actor(
                "actor", {}, max_items=1, max_total_charge_usd="0.01"
            )
        self.assertEqual(len(transport.requests), 1)

    def test_invalid_caps_are_rejected_before_network(self):
        transport = FakeTransport()
        client = ApifyClient(token="secret", transport=transport)

        for max_items in (0, -1, True, 1.5):
            with self.subTest(max_items=max_items):
                with self.assertRaises(ValueError):
                    client.start_actor(
                        "actor",
                        {},
                        max_items=max_items,
                        max_total_charge_usd="1",
                    )
        for charge in (0, -1, True, "NaN", "Infinity"):
            with self.subTest(charge=charge):
                with self.assertRaises((TypeError, ValueError)):
                    client.start_actor(
                        "actor", {}, max_items=1, max_total_charge_usd=charge
                    )
        self.assertEqual(transport.requests, [])


class RunAndDatasetTests(unittest.TestCase):
    def test_wait_polls_until_success_using_injected_time(self):
        transport = FakeTransport(
            response({"data": {"id": "r", "status": "RUNNING"}}),
            response({"data": {"id": "r", "status": "RUNNING"}}),
            response({"data": {"id": "r", "status": "SUCCEEDED"}}),
        )
        fake_time = FakeTime()
        client = ApifyClient(
            token="secret",
            transport=transport,
            sleep=fake_time.sleep,
            clock=fake_time.clock,
        )

        run = client.wait_for_run(
            "r", timeout_seconds=20, poll_interval_seconds=3, require_success=True
        )

        self.assertEqual(run["status"], "SUCCEEDED")
        self.assertEqual(fake_time.sleeps, [3, 3])
        self.assertEqual(len(transport.requests), 3)

    def test_failed_terminal_run_raises_when_success_is_required(self):
        failed = {
            "id": "r",
            "status": "FAILED",
            "statusMessage": "Actor process exited",
        }
        client = ApifyClient(
            token="secret", transport=FakeTransport(response({"data": failed}))
        )

        with self.assertRaises(ApifyRunFailedError) as caught:
            client.wait_for_run("r", require_success=True)
        self.assertEqual(caught.exception.run, failed)

    def test_wait_times_out_without_real_sleep(self):
        transport = FakeTransport(
            response({"data": {"id": "r", "status": "RUNNING"}}),
            response({"data": {"id": "r", "status": "RUNNING"}}),
            response({"data": {"id": "r", "status": "RUNNING"}}),
        )
        fake_time = FakeTime()
        client = ApifyClient(
            token="secret",
            transport=transport,
            sleep=fake_time.sleep,
            clock=fake_time.clock,
        )

        with self.assertRaises(ApifyRunTimeoutError):
            client.wait_for_run("r", timeout_seconds=2, poll_interval_seconds=1)
        self.assertEqual(fake_time.sleeps, [1, 1])

    def test_dataset_items_are_paginated_and_left_unchanged(self):
        first = [
            {"id": 1, "nested": {"text": "árvíztűrő"}, "#meta": {"x": 1}},
            {"id": 2, "reviews": [{"rating": 5}]},
        ]
        second = [{"id": 3, "nullable": None}]
        transport = FakeTransport(
            response(
                first,
                headers={"X-Apify-Pagination-Total": "3"},
            ),
            response(
                second,
                headers={"x-apify-pagination-total": "3"},
            ),
        )
        client = ApifyClient(token="secret", transport=transport)

        items = client.get_dataset_items("dataset", page_size=2)

        self.assertEqual(items, first + second)
        first_query = parse_qs(urlparse(transport.requests[0][0].full_url).query)
        second_query = parse_qs(urlparse(transport.requests[1][0].full_url).query)
        self.assertEqual(first_query["offset"], ["0"])
        self.assertEqual(second_query["offset"], ["2"])
        self.assertEqual(first_query["clean"], ["false"])
        self.assertEqual(first_query["desc"], ["false"])
        self.assertNotIn("fields", first_query)
        self.assertNotIn("unwind", first_query)

    def test_short_page_with_larger_reported_total_keeps_paginating(self):
        first = [{"id": 1}]
        second = [{"id": 2}, {"id": 3}]
        transport = FakeTransport(
            response(first, headers={"X-Apify-Pagination-Total": "3"}),
            response(second, headers={"X-Apify-Pagination-Total": "3"}),
        )
        client = ApifyClient(token="secret", transport=transport)

        pages = list(client.iter_dataset_pages("dataset", page_size=2))

        self.assertEqual(pages, [(0, first, 3), (1, second, 3)])
        offsets = [
            parse_qs(urlparse(request.full_url).query)["offset"][0]
            for request, _timeout in transport.requests
        ]
        self.assertEqual(offsets, ["0", "1"])

    def test_empty_page_before_reported_total_fails_closed(self):
        client = ApifyClient(
            token="secret",
            transport=FakeTransport(
                response([], headers={"X-Apify-Pagination-Total": "2"})
            ),
        )

        with self.assertRaisesRegex(ApifyProtocolError, "before total 2"):
            list(client.iter_dataset_pages("dataset", page_size=2))

    def test_dataset_rejects_non_object_items(self):
        client = ApifyClient(
            token="secret", transport=FakeTransport(response([{"ok": True}, "bad"]))
        )
        with self.assertRaisesRegex(ApifyProtocolError, "not a JSON object"):
            client.get_dataset_items("dataset")

    def test_complete_run_returns_run_dataset_and_exact_items(self):
        limits = {
            "limits": {"maxMonthlyUsageUsd": 10},
            "current": {"monthlyUsageUsd": 0},
        }
        started = {"id": "run-1", "status": "READY"}
        running = {"id": "run-1", "status": "RUNNING"}
        completed = {
            "id": "run-1",
            "status": "SUCCEEDED",
            "defaultDatasetId": "dataset-1",
            "usageTotalUsd": 0.2,
        }
        dataset = {"id": "dataset-1", "itemCount": 1}
        items = [{"title": "Tihany", "raw": {"rating": 4.7}}]
        transport = FakeTransport(
            response({"data": limits}),
            response({"data": started}),
            response({"data": running}),
            response({"data": completed}),
            response({"data": dataset}),
            response(items),
        )
        fake_time = FakeTime()
        client = ApifyClient(
            token="secret",
            transport=transport,
            sleep=fake_time.sleep,
            clock=fake_time.clock,
        )

        result = client.run_actor(
            "actor",
            {"country": "Hungary"},
            max_items=100,
            max_total_charge_usd="1",
            poll_interval_seconds=0.1,
        )

        self.assertEqual(result.run, completed)
        self.assertEqual(result.run_metadata, completed)
        self.assertEqual(result.dataset, dataset)
        self.assertEqual(result.dataset_metadata, dataset)
        self.assertEqual(result.items, items)
        self.assertEqual(len(transport.requests), 6)


if __name__ == "__main__":
    unittest.main()
