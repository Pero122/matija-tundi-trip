from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from apify_client import ApifyHttpError, UsagePlan
from scrape_hungary import (
    PipelineBudgetError,
    PlannedRun,
    build_detail_plan,
    build_discovery_plans,
    continuation_decision,
    deep_merge,
    execute_plan,
    execute_pipeline,
    import_dataset,
    ingest_completed_dataset,
    ingest_items,
    load_config,
    resume_runs,
    select_getyourguide_detail_urls,
    plan_fingerprint,
    rebuild_normalized_projections,
    replay_stored_payloads,
    record_getyourguide_detail_attempts,
    summarize_run,
    verify_live_budget,
)
from store import ResearchStore, canonical_json


CONFIG_PATH = HERE / "sources.json"


def usage(*, cap: str, used: str) -> UsagePlan:
    return UsagePlan(
        user={"plan": {"id": "test"}},
        limits={
            "limits": {"maxMonthlyUsageUsd": cap},
            "current": {"monthlyUsageUsd": used},
        },
    )


class NoStartClient:
    def __init__(self, current_usage: UsagePlan):
        self.current_usage = current_usage
        self.started = 0

    def get_current_usage_plan(self) -> UsagePlan:
        return self.current_usage

    def start_actor(self, *args, **kwargs):
        self.started += 1
        raise AssertionError("an actor must not start when the combined budget fails")


class DatasetClient:
    def __init__(self, items):
        self.items = list(items)
        self.dataset_calls = 0

    def get_dataset(self, dataset_id):
        self.dataset_calls += 1
        return {"id": dataset_id, "itemCount": len(self.items)}

    def get_dataset_items(self, dataset_id):
        self.dataset_calls += 1
        return deepcopy(self.items)

    def iter_dataset_pages(self, dataset_id, *, page_size=1000, start_offset=0):
        self.dataset_calls += 1
        for offset in range(start_offset, len(self.items), page_size):
            yield offset, deepcopy(self.items[offset : offset + page_size]), len(self.items)


class ResumeClient(DatasetClient):
    def __init__(self, items):
        super().__init__(items)
        self.started = 0

    def get_run(self, actor_run_id):
        return {
            "id": actor_run_id,
            "status": "SUCCEEDED",
            "defaultDatasetId": "resume-dataset",
            "usageTotalUsd": 0.02,
        }

    def start_actor(self, *args, **kwargs):
        self.started += 1
        raise AssertionError("resume must never start another actor")


class FailedRunClient:
    def __init__(self, current_usage):
        self.current_usage = current_usage
        self.started = []

    def get_current_usage_plan(self):
        return self.current_usage

    def start_actor(self, actor_id, run_input, **caps):
        run_id = f"failed-{len(self.started) + 1}"
        self.started.append((actor_id, deepcopy(run_input), deepcopy(caps)))
        return {"id": run_id, "status": "RUNNING"}

    def wait_for_run(self, actor_run_id, **kwargs):
        return {"id": actor_run_id, "status": "FAILED", "statusMessage": "test failure"}


class PartialPageClient(DatasetClient):
    def iter_dataset_pages(self, dataset_id, *, page_size=1000, start_offset=0):
        if start_offset == 0:
            yield 0, deepcopy(self.items[:1]), len(self.items)
        raise OSError("second page failed")


class EmptyDetailClient(DatasetClient):
    def __init__(self):
        super().__init__([])
        self.started = 0

    def get_current_usage_plan(self):
        return usage(cap="5", used="0")

    def start_actor(self, actor_id, run_input, **caps):
        self.started += 1
        return {
            "id": f"empty-detail-run-{self.started}",
            "status": "RUNNING",
            "defaultDatasetId": "empty-detail-dataset",
        }

    def wait_for_run(self, actor_run_id, **kwargs):
        return {
            "id": actor_run_id,
            "status": "SUCCEEDED",
            "defaultDatasetId": "empty-detail-dataset",
        }


class RejectedStartClient:
    def start_actor(self, actor_id, run_input, **caps):
        raise ApifyHttpError(
            status_code=400,
            method="POST",
            url="https://api.apify.test/runs",
            error_type="invalid-input",
            server_message="rejected before start",
        )


class ScrapeHungaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "research.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_deep_merge_preserves_nested_base_without_mutation(self):
        base = {"proxy": {"enabled": False, "groups": ["A"]}, "language": "en"}
        overlay = {"proxy": {"enabled": True}, "query": "Hungary"}
        result = deep_merge(base, overlay)
        self.assertEqual(
            result,
            {
                "proxy": {"enabled": True, "groups": ["A"]},
                "language": "en",
                "query": "Hungary",
            },
        )
        self.assertFalse(base["proxy"]["enabled"])

    def test_hybrid_plan_expands_each_tripadvisor_query_into_its_own_cap(self):
        config = load_config(CONFIG_PATH)
        plans = build_discovery_plans(config)
        phase = next(
            phase
            for phase in config["actors"]["tripadvisor-discovery"]["phases"]
            if phase["label"] == "destination-and-theme-fanout"
        )
        fanout = [
            plan
            for plan in plans
            if plan.phase_label == "destination-and-theme-fanout"
        ]
        self.assertEqual(len(fanout), len(phase["queries"]))
        self.assertEqual({plan.query for plan in fanout}, set(phase["queries"]))
        for plan in fanout:
            self.assertEqual(plan.run_input["query"], plan.query)
            self.assertEqual(plan.run_input["maxItemsPerQuery"], phase["perQueryMaxItems"])
            self.assertEqual(plan.max_items, phase["perQueryMaxItems"])
            self.assertEqual(
                plan.max_total_charge_usd,
                Decimal(str(phase["perQueryMaxTotalChargeUsd"])),
            )
            self.assertTrue(plan.run_input["includeAttractions"])
            self.assertFalse(plan.run_input["includeHotels"])

    def test_tripadvisor_country_search_uses_supported_query_not_offset_url(self):
        config = load_config(CONFIG_PATH)
        plans = [
            plan
            for plan in build_discovery_plans(config)
            if plan.actor_key == "tripadvisor-discovery"
            and plan.phase_label == "country-hungary"
        ]
        self.assertEqual(len(plans), 1)
        country = plans[0]
        self.assertEqual(country.query, "Hungary")
        self.assertEqual(country.run_input["query"], "Hungary")
        self.assertNotIn("startUrls", country.run_input)
        self.assertEqual(country.run_input["maxItemsPerQuery"], 30)
        self.assertEqual(country.max_items, 30)
        self.assertEqual(country.max_total_charge_usd, Decimal("0.151"))
        continuation_labels = {
            plan.phase_label
            for plan in build_discovery_plans(config)
            if plan.actor_key == "tripadvisor-discovery"
            and plan.phase_label.startswith("country-hungary-page-")
        }
        self.assertEqual(continuation_labels, set())

    def test_getyourguide_destinations_are_balanced_across_bounded_batches(self):
        config = load_config(CONFIG_PATH)
        phase = config["actors"]["getyourguide-discovery"]["phases"][1]
        plans = [
            plan
            for plan in build_discovery_plans(config)
            if plan.actor_key == "getyourguide-discovery"
            and plan.phase_label.startswith("destination-fanout-batch-")
        ]
        self.assertEqual(len(plans), 14)
        flattened = [
            item
            for plan in plans
            for item in plan.run_input["cityUrls"]
        ]
        self.assertEqual(flattened, phase["input"]["cityUrls"])
        self.assertTrue(all(1 <= len(plan.run_input["cityUrls"]) <= 2 for plan in plans))
        self.assertTrue(all(plan.run_input["maxItemsPerCity"] == 16 for plan in plans))
        self.assertTrue(all(plan.max_items == 32 for plan in plans))
        self.assertTrue(
            all(plan.max_total_charge_usd == Decimal("0.065") for plan in plans)
        )

    def test_detail_plan_uses_ranked_urls_as_request_objects(self):
        config = load_config(CONFIG_PATH)
        plan = build_detail_plan(config, ["https://example.test/a", "https://example.test/a", "https://example.test/b"])
        self.assertIsNotNone(plan)
        self.assertEqual(
            plan.run_input["activityUrls"],
            [{"url": "https://example.test/a"}, {"url": "https://example.test/b"}],
        )
        self.assertEqual(plan.run_input["maxActivities"], 2)
        self.assertEqual(plan.max_items, 2)
        self.assertTrue(plan.run_input["includeDetails"])

    def test_full_hard_cap_is_checked_against_local_and_live_allowance(self):
        config = load_config(CONFIG_PATH)
        result = verify_live_budget(config, usage(cap="5", used="0.44"))
        self.assertEqual(result["planned"], Decimal("3.219"))
        self.assertEqual(result["remaining"], Decimal("4.56"))
        with self.assertRaises(PipelineBudgetError):
            verify_live_budget(config, usage(cap="5", used="1.8"))

    def test_no_actor_starts_when_combined_live_budget_is_too_small(self):
        config = load_config(CONFIG_PATH)
        client = NoStartClient(usage(cap="5", used="1.8"))
        with ResearchStore(self.db_path) as store:
            with self.assertRaises(PipelineBudgetError):
                execute_pipeline(config, store, client, print_fn=lambda _line: None)
        self.assertEqual(client.started, 0)

    def test_paid_actor_is_not_started_when_durable_plan_claim_fails(self):
        config = load_config(CONFIG_PATH)
        plan = next(
            plan
            for plan in build_discovery_plans(config)
            if plan.actor_key == "getyourguide-discovery"
        )
        client = NoStartClient(usage(cap="5", used="0"))

        class FailingClaimStore:
            def claim_plan_run(self, *args, **kwargs):
                raise sqlite3.OperationalError("test storage failure")

        with self.assertRaises(sqlite3.OperationalError):
            execute_plan(
                FailingClaimStore(),
                client,
                plan,
                timeout_seconds=1,
                poll_interval_seconds=0.01,
                print_fn=lambda _line: None,
            )
        self.assertEqual(client.started, 0)

    def test_exact_active_plan_resumes_without_starting_duplicate_actor(self):
        config = load_config(CONFIG_PATH)
        plan = next(
            plan
            for plan in build_discovery_plans(config)
            if plan.actor_key == "getyourguide-discovery"
        )
        payload = {
            "activityId": "resume-exact-1",
            "name": "Tihany peninsula experience",
            "url": "https://www.getyourguide.com/tihany-l1/example-t1/",
            "rating": 4.8,
            "reviewCount": 200,
            "city": "Tihany",
            "country": "Hungary",
        }
        client = ResumeClient([payload])
        with ResearchStore(self.db_path) as store:
            local_id, claimed = store.claim_plan_run(
                plan.source,
                plan_fingerprint(plan),
                input_data=plan.run_input,
                metadata={},
            )
            self.assertTrue(claimed)
            store.attach_actor_run(local_id, "completed-remote-run")
            ok = execute_plan(
                store,
                client,
                plan,
                timeout_seconds=1,
                poll_interval_seconds=0.01,
                print_fn=lambda _line: None,
            )
            row = store.connection.execute(
                "SELECT status, actor_run_id FROM scrape_runs WHERE id = ?", (local_id,)
            ).fetchone()
            self.assertTrue(ok)
            self.assertEqual((row["status"], row["actor_run_id"]), ("complete", "completed-remote-run"))
            self.assertEqual(store.stats()["listings"], 1)
        self.assertEqual(client.started, 0)

    def test_unresolved_pending_claim_refuses_uncertain_duplicate_charge(self):
        config = load_config(CONFIG_PATH)
        plan = next(
            plan
            for plan in build_discovery_plans(config)
            if plan.actor_key == "getyourguide-discovery"
        )
        client = NoStartClient(usage(cap="5", used="0"))
        with ResearchStore(self.db_path) as store:
            store.claim_plan_run(
                plan.source,
                plan_fingerprint(plan),
                input_data=plan.run_input,
                metadata={},
            )
            with self.assertRaisesRegex(RuntimeError, "unresolved pending claim"):
                execute_plan(
                    store,
                    client,
                    plan,
                    timeout_seconds=1,
                    poll_interval_seconds=0.01,
                    print_fn=lambda _line: None,
                )
        self.assertEqual(client.started, 0)

    def test_definitive_start_rejection_releases_pending_claim_as_failed(self):
        config = load_config(CONFIG_PATH)
        plan = next(
            plan
            for plan in build_discovery_plans(config)
            if plan.actor_key == "getyourguide-discovery"
        )
        with ResearchStore(self.db_path) as store:
            with self.assertRaises(ApifyHttpError):
                execute_plan(
                    store,
                    RejectedStartClient(),
                    plan,
                    timeout_seconds=1,
                    poll_interval_seconds=0.01,
                    print_fn=lambda _line: None,
                )
            row = store.connection.execute(
                "SELECT status, actor_run_id FROM scrape_runs"
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertTrue(row["actor_run_id"].startswith("pending:"))
            _new_id, claimed = store.claim_plan_run(
                plan.source,
                plan_fingerprint(plan),
                input_data=plan.run_input,
                metadata={},
            )
            self.assertTrue(claimed)

    def test_provider_filter_reserves_only_getyourguide_and_never_starts_tripadvisor(self):
        config = load_config(CONFIG_PATH)
        # $2.10 fits GYG discovery + details ($2.06) but not the full plan.
        client = FailedRunClient(usage(cap="5", used="2.90"))
        with ResearchStore(self.db_path) as store:
            ok = execute_pipeline(
                config,
                store,
                client,
                only_provider="getyourguide",
                print_fn=lambda _line: None,
            )
        self.assertFalse(ok)
        self.assertEqual(len(client.started), 1)
        self.assertTrue(all("getyourguide" in actor_id for actor_id, _input, _caps in client.started))

    def test_completed_exact_paid_inputs_are_skipped_by_default(self):
        config = load_config(CONFIG_PATH)
        tripadvisor_plans = [
            plan for plan in build_discovery_plans(config) if plan.source == "tripadvisor"
        ]
        client = NoStartClient(usage(cap="0", used="0"))
        with ResearchStore(self.db_path) as store:
            for index, plan in enumerate(tripadvisor_plans):
                cached_input = deepcopy(plan.run_input)
                cached_max_items = plan.max_items
                cached_charge = plan.max_total_charge_usd
                if plan.phase_label == "country-hungary":
                    cached_input["maxItemsPerQuery"] = 300
                    cached_max_items = 300
                    cached_charge = Decimal("1.51")
                metadata = {
                    "actor_config_key": plan.actor_key,
                    "actor_id": plan.actor_id,
                    "phase_label": plan.phase_label,
                    "query": plan.query,
                    "hard_caps": {
                        "max_items": cached_max_items,
                        "max_total_charge_usd": str(cached_charge),
                    },
                }
                run_id = store.begin_run(
                    "tripadvisor",
                    actor_run_id=f"cached-{index}",
                    dataset_id=f"dataset-{index}",
                    input_data=cached_input,
                    metadata=metadata,
                )
                store.finish_run(run_id, status="complete")
            ok = execute_pipeline(
                config,
                store,
                client,
                only_provider="tripadvisor",
                print_fn=lambda _line: None,
            )
        self.assertTrue(ok)
        self.assertEqual(client.started, 0)

    def test_sentinel_payload_is_stored_exactly_before_being_recorded_failed(self):
        payload = {
            "error": "Access denied",
            "statusCode": 403,
            "diagnostics": {"attempt": 2, "headers": ["x", "y"]},
        }
        with ResearchStore(self.db_path) as store:
            run_id = store.begin_run(
                "tripadvisor",
                actor_run_id="sentinel-run",
                dataset_id="sentinel-dataset",
            )
            summary = ingest_items(
                store,
                run_id=run_id,
                source="tripadvisor",
                actor_id="maxcopell~tripadvisor",
                items=[payload],
                query_label="country-hungary",
                query=None,
                actor_run_id="sentinel-run",
                dataset_id="sentinel-dataset",
            )
            self.assertEqual(
                (summary.total, summary.stored, summary.failed, summary.sentinels),
                (1, 0, 1, 1),
            )
            raw = store.connection.execute(
                "SELECT canonical_json FROM raw_payloads"
            ).fetchone()["canonical_json"]
            row = store.connection.execute(
                "SELECT status, raw_payload_id, result_rank, metadata_json FROM scrape_run_items"
            ).fetchone()
            self.assertEqual(raw, canonical_json(payload))
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["raw_payload_id"])
            self.assertEqual(row["result_rank"], 1)
            self.assertEqual(json.loads(row["metadata_json"])["classification"], "provider-sentinel")

    def test_exact_raw_persistence_is_deduplicated_and_private(self):
        payload = {"b": [2, 1], "a": "ő"}
        with ResearchStore(self.db_path) as store:
            run_id = store.begin_run("getyourguide", actor_run_id="raw-run")
            first = store.record_unparsed_item(run_id, payload, item_index=0)
            second = store.record_unparsed_item(run_id, payload, item_index=1)
            self.assertEqual(first["raw_payload_id"], second["raw_payload_id"])
            row = store.connection.execute(
                "SELECT canonical_json, is_private FROM raw_payloads WHERE id = ?",
                (first["raw_payload_id"],),
            ).fetchone()
            self.assertEqual(row["canonical_json"], canonical_json(payload))
            self.assertEqual(row["is_private"], 1)

    def test_local_replay_normalizes_existing_raw_rows_without_fetching(self):
        config = load_config(CONFIG_PATH)
        observed_at = "2026-07-20T08:15:00+00:00"
        payload = {
            "activityId": "replay-local-1",
            "activityTitle": "Balaton memorable boat",
            "activityUrl": "https://www.getyourguide.com/tihany-l1/boat-t1/",
            "rating": 4.8,
            "ratingCount": 321,
            "sourceCityUrl": "https://www.getyourguide.com/tihany-l1/",
            "thumbnailUrls": ["https://images.example.test/boat.webp"],
        }
        metadata = {
            "actor_config_key": "getyourguide-discovery",
            "actor_id": "piotrv1001~getyourguide-listings-scraper",
            "phase_label": "country-hungary",
        }
        with ResearchStore(self.db_path) as store:
            run_id = store.begin_run(
                "getyourguide",
                actor_run_id="local-replay-run",
                dataset_id="local-replay-dataset",
                metadata=metadata,
            )
            store.record_unparsed_item(
                run_id,
                payload,
                item_index=0,
                query_label="country-hungary",
                fetched_at=observed_at,
            )
            completed_at = "2026-07-20T08:16:00+00:00"
            store.finish_run(
                run_id, status="complete", completed_at=completed_at
            )
            before_raw = store.stats()["raw_payloads"]
            result = replay_stored_payloads(
                config, store, run_ids=[run_id], print_fn=lambda _line: None
            )
            listing = store.connection.execute(
                "SELECT id, kind, review_count, last_seen_at FROM listings "
                "WHERE external_id = 'replay-local-1'"
            ).fetchone()
            snapshot = store.connection.execute(
                "SELECT kind, scraped_at FROM listing_snapshots WHERE listing_id = ?",
                (listing["id"],),
            ).fetchone()
            self.assertEqual(result, {
                "runs": 1,
                "items": 1,
                "stored": 1,
                "failed": 0,
                "sentinels": 0,
            })
            self.assertEqual(listing["kind"], "experience")
            self.assertEqual(listing["review_count"], 321)
            self.assertEqual(listing["last_seen_at"], observed_at)
            self.assertEqual(snapshot["kind"], "experience")
            self.assertEqual(snapshot["scraped_at"], observed_at)
            self.assertEqual(store.stats()["media"], 1)
            self.assertEqual(store.stats()["raw_payloads"], before_raw)
            self.assertEqual(
                store.connection.execute(
                    "SELECT completed_at FROM scrape_runs WHERE id = ?", (run_id,)
                ).fetchone()[0],
                completed_at,
            )

            # A v4 migration initializes this dimension as unknown.  Replay
            # must backfill kind even if a prior bad replay left the mutable
            # projection timestamp newer than the original observation.
            future_at = "2026-07-30T00:00:00+00:00"
            store.connection.execute(
                "UPDATE listings SET kind = 'unknown', last_seen_at = ? WHERE id = ?",
                (future_at, listing["id"]),
            )
            store.connection.execute(
                "UPDATE listing_snapshots SET kind = 'unknown' WHERE listing_id = ?",
                (listing["id"],),
            )
            store.connection.commit()
            replay_stored_payloads(
                config, store, run_ids=[run_id], print_fn=lambda _line: None
            )
            backfilled_listing = store.connection.execute(
                "SELECT kind, last_seen_at FROM listings WHERE id = ?",
                (listing["id"],),
            ).fetchone()
            backfilled_snapshot = store.connection.execute(
                "SELECT kind, scraped_at FROM listing_snapshots WHERE listing_id = ?",
                (listing["id"],),
            ).fetchone()
            self.assertEqual(backfilled_listing["kind"], "experience")
            self.assertEqual(backfilled_listing["last_seen_at"], future_at)
            self.assertEqual(backfilled_snapshot["kind"], "experience")
            self.assertEqual(backfilled_snapshot["scraped_at"], observed_at)

    def test_replay_uses_each_duplicate_raw_occurrence_observed_at(self):
        config = load_config(CONFIG_PATH)
        payload = {
            "activityId": "same-json-chronology",
            "name": "Eger chronology tour",
            "url": "https://www.getyourguide.com/eger-l1/tour-t700001/",
            "location": "Eger",
            "rating": 4.7,
            "reviewCount": 80,
        }
        observations = (
            "2026-07-20T08:00:00+00:00",
            "2026-07-22T18:45:00+00:00",
        )
        metadata = {
            "actor_config_key": "getyourguide-discovery",
            "actor_id": "piotrv1001~getyourguide-listings-scraper",
            "phase_label": "country-hungary",
        }
        with ResearchStore(self.db_path) as store:
            run_ids = []
            raw_ids = []
            for index, observed_at in enumerate(observations):
                run_id = store.begin_run(
                    "getyourguide",
                    actor_run_id=f"same-json-{index}",
                    dataset_id=f"same-json-dataset-{index}",
                    metadata=metadata,
                )
                recorded = store.record_unparsed_item(
                    run_id,
                    payload,
                    item_index=0,
                    query_label="country-hungary",
                    fetched_at=observed_at,
                )
                store.finish_run(run_id, status="complete")
                run_ids.append(run_id)
                raw_ids.append(recorded["raw_payload_id"])

            self.assertEqual(raw_ids[0], raw_ids[1])
            self.assertEqual(
                observations[0],
                store.connection.execute(
                    "SELECT fetched_at FROM raw_payloads WHERE id = ?", (raw_ids[0],)
                ).fetchone()[0],
            )
            self.assertEqual(
                list(observations),
                [
                    row[0]
                    for row in store.connection.execute(
                        "SELECT observed_at FROM scrape_run_items ORDER BY run_id"
                    )
                ],
            )

            replay_stored_payloads(
                config, store, run_ids=run_ids, print_fn=lambda _line: None
            )
            listing = store.connection.execute(
                "SELECT id, last_seen_at FROM listings "
                "WHERE external_id = 'same-json-chronology'"
            ).fetchone()
            self.assertEqual(observations[1], listing["last_seen_at"])
            self.assertEqual(
                observations[1],
                store.connection.execute(
                    "SELECT scraped_at FROM listing_snapshots WHERE listing_id = ?",
                    (listing["id"],),
                ).fetchone()[0],
            )

            replay_stored_payloads(
                config, store, run_ids=[run_ids[0]], print_fn=lambda _line: None
            )
            self.assertEqual(
                observations[1],
                store.connection.execute(
                    "SELECT last_seen_at FROM listings WHERE id = ?", (listing["id"],)
                ).fetchone()[0],
            )
            self.assertEqual(
                observations[1],
                store.connection.execute(
                    "SELECT scraped_at FROM listing_snapshots WHERE listing_id = ?",
                    (listing["id"],),
                ).fetchone()[0],
            )

    def test_projection_rebuild_cleans_stale_rows_and_preserves_exact_attempts(self):
        config = load_config(CONFIG_PATH)
        observed_at = "2026-07-20T08:15:00+00:00"
        payload = {
            "activityId": "700002",
            "name": "Eger retained-evidence experience",
            "url": "https://www.getyourguide.com/eger-l1/tour-t700002/",
            "location": "Eger",
            "rating": 4.8,
            "reviewCount": 120,
        }
        metadata = {
            "actor_config_key": "getyourguide-discovery",
            "actor_id": "piotrv1001~getyourguide-listings-scraper",
            "phase_label": "country-hungary",
        }
        with ResearchStore(self.db_path) as store:
            discovery = store.begin_run(
                "getyourguide",
                actor_run_id="rebuild-discovery",
                dataset_id="rebuild-dataset",
                metadata=metadata,
                started_at="2026-07-20T08:00:00+00:00",
            )
            summary = ingest_items(
                store,
                run_id=discovery,
                source="getyourguide",
                actor_id=metadata["actor_id"],
                items=[payload],
                query_label=metadata["phase_label"],
                query="Hungary",
                actor_run_id="rebuild-discovery",
                dataset_id="rebuild-dataset",
                fetched_at=observed_at,
            )
            self.assertEqual(1, summary.stored)
            store.finish_run(
                discovery,
                status="complete",
                stats={"original": True},
                completed_at="2026-07-20T08:20:00+00:00",
            )
            listing = store.connection.execute(
                "SELECT id, latest_raw_payload_id, url FROM listings "
                "WHERE external_id = '700002'"
            ).fetchone()
            store.mark_enrichment(
                int(listing["id"]),
                kind="getyourguide-detail",
                version="exact-v1",
                raw_payload_id=int(listing["latest_raw_payload_id"]),
                enriched_at="2026-07-20T08:16:00+00:00",
            )
            attempt_specs = (
                ("not-returned", "2026-07-21T09:00:00+00:00", None),
                ("failed", "2026-07-22T10:00:00+00:00", "actor timeout"),
                ("succeeded", "2026-07-23T11:00:00+00:00", None),
            )
            for index, (status, attempted_at, error) in enumerate(attempt_specs):
                attempt_run = store.begin_run(
                    "getyourguide",
                    actor_run_id=f"rebuild-attempt-{index}",
                    started_at=attempted_at,
                )
                store.record_enrichment_attempt(
                    int(listing["id"]),
                    kind="getyourguide-detail",
                    version="exact-attempt-v1",
                    run_id=attempt_run,
                    status=status,
                    requested_url=str(listing["url"]),
                    attempted_at=attempted_at,
                    error=error,
                )
                store.finish_run(
                    attempt_run, status="complete", completed_at=attempted_at
                )
            store.connection.execute(
                """
                INSERT INTO reviews(
                    listing_id, raw_payload_id, source, external_id, rating,
                    body, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'getyourguide', 'stale-control-review', 3,
                          'See more reviews', ?, ?)
                """,
                (
                    listing["id"],
                    listing["latest_raw_payload_id"],
                    observed_at,
                    observed_at,
                ),
            )
            store.connection.execute(
                "INSERT INTO categories(slug, name, created_at) "
                "VALUES ('stale-category', 'Stale category', ?)",
                (observed_at,),
            )
            store.connection.commit()
            attempts_before = [
                tuple(row)
                for row in store.connection.execute(
                    """
                    SELECT listing.source, listing.external_id,
                           attempt.enrichment_kind, attempt.enrichment_version,
                           attempt.run_id, attempt.status, attempt.requested_url,
                           attempt.attempted_at, attempt.error
                    FROM listing_enrichment_attempts AS attempt
                    JOIN listings AS listing ON listing.id = attempt.listing_id
                    ORDER BY attempt.run_id
                    """
                )
            ]
            run_before = tuple(
                store.connection.execute(
                    "SELECT status, started_at, completed_at, stats_json "
                    "FROM scrape_runs WHERE id = ?",
                    (discovery,),
                ).fetchone()
            )

            result = rebuild_normalized_projections(
                config, store, print_fn=lambda _line: None
            )

            attempts_after = [
                tuple(row)
                for row in store.connection.execute(
                    """
                    SELECT listing.source, listing.external_id,
                           attempt.enrichment_kind, attempt.enrichment_version,
                           attempt.run_id, attempt.status, attempt.requested_url,
                           attempt.attempted_at, attempt.error
                    FROM listing_enrichment_attempts AS attempt
                    JOIN listings AS listing ON listing.id = attempt.listing_id
                    ORDER BY attempt.run_id
                    """
                )
            ]
            self.assertEqual(3, result["attempts_preserved"])
            self.assertEqual(attempts_before, attempts_after)
            self.assertEqual(
                run_before,
                tuple(
                    store.connection.execute(
                        "SELECT status, started_at, completed_at, stats_json "
                        "FROM scrape_runs WHERE id = ?",
                        (discovery,),
                    ).fetchone()
                ),
            )
            self.assertEqual(
                observed_at,
                store.connection.execute(
                    "SELECT observed_at FROM scrape_run_items WHERE run_id = ?",
                    (discovery,),
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                store.connection.execute(
                    "SELECT COUNT(*) FROM reviews "
                    "WHERE body = 'See more reviews' OR body LIKE 'Product ID:%'"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                store.connection.execute(
                    "SELECT COUNT(*) FROM categories WHERE slug = 'stale-category'"
                ).fetchone()[0],
            )
            self.assertEqual([], store.connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_replaying_older_run_preserves_newer_current_projections(self):
        config = load_config(CONFIG_PATH)
        old_at = "2026-07-20T08:00:00+00:00"
        new_at = "2026-07-21T09:00:00+00:00"
        url = "https://www.getyourguide.com/eger-l1/event-time-t777/"
        old_payload = {
            "activityId": "777",
            "activityTitle": "Old Eger activity title",
            "activityUrl": url,
            "rating": 4.1,
            "ratingCount": 10,
            "city": "Eger",
            "country": "Hungary",
            "images": [
                {
                    "id": "hero",
                    "url": "https://images.example.test/old.webp",
                    "caption": "Old caption",
                }
            ],
            "options": [
                {
                    "id": "adult",
                    "name": "Adult ticket",
                    "description": "Old package description",
                    "price": 10,
                    "currency": "EUR",
                }
            ],
            "sampleReviews": [
                {
                    "id": "review-777",
                    "rating": 3,
                    "text": "Old review body",
                }
            ],
        }
        new_payload = json.loads(json.dumps(old_payload))
        new_payload.update(
            {
                "activityTitle": "Current Eger activity title",
                "rating": 4.9,
                "ratingCount": 900,
            }
        )
        new_payload["images"][0].update(
            {
                "url": "https://images.example.test/current.webp",
                "caption": "Current caption",
            }
        )
        new_payload["options"][0].update(
            {"description": "Current package description", "price": 25}
        )
        new_payload["sampleReviews"][0].update(
            {"rating": 5, "text": "Current review body"}
        )
        metadata = {
            "actor_config_key": "getyourguide-details",
            "actor_id": "piotrv1001~getyourguide-listings-scraper",
            "phase_label": "top-outside-budapest-details",
        }

        with ResearchStore(self.db_path) as store:
            old_run = store.begin_run(
                "getyourguide",
                actor_run_id="event-time-old",
                dataset_id="event-time-old-dataset",
                metadata=metadata,
            )
            old_summary = ingest_items(
                store,
                run_id=old_run,
                source="getyourguide",
                actor_id=metadata["actor_id"],
                items=[old_payload],
                query_label=metadata["phase_label"],
                query=None,
                actor_run_id="event-time-old",
                dataset_id="event-time-old-dataset",
                enrichment_kind="getyourguide-detail",
                enrichment_version="piotrv1001-v1",
                fetched_at=old_at,
            )
            store.finish_run(old_run, status="complete")

            new_run = store.begin_run(
                "getyourguide",
                actor_run_id="event-time-new",
                dataset_id="event-time-new-dataset",
                metadata=metadata,
            )
            new_summary = ingest_items(
                store,
                run_id=new_run,
                source="getyourguide",
                actor_id=metadata["actor_id"],
                items=[new_payload],
                query_label=metadata["phase_label"],
                query=None,
                actor_run_id="event-time-new",
                dataset_id="event-time-new-dataset",
                enrichment_kind="getyourguide-detail",
                enrichment_version="piotrv1001-v1",
                fetched_at=new_at,
            )
            store.finish_run(new_run, status="complete")
            self.assertEqual((old_summary.stored, new_summary.stored), (1, 1))
            newer_raw_id = store.connection.execute(
                "SELECT raw_payload_id FROM scrape_run_items WHERE run_id = ?",
                (new_run,),
            ).fetchone()[0]

            replay_stored_payloads(
                config, store, run_ids=[old_run], print_fn=lambda _line: None
            )

            listing = store.connection.execute(
                "SELECT * FROM listings WHERE source = 'getyourguide' "
                "AND external_id = '777'"
            ).fetchone()
            place = store.connection.execute(
                "SELECT * FROM places WHERE id = ?", (listing["place_id"],)
            ).fetchone()
            media = store.connection.execute(
                "SELECT * FROM media WHERE listing_id = ? AND external_id = 'hero'",
                (listing["id"],),
            ).fetchone()
            package = store.connection.execute(
                "SELECT * FROM packages WHERE listing_id = ? AND external_id = 'adult'",
                (listing["id"],),
            ).fetchone()
            review = store.connection.execute(
                "SELECT * FROM reviews WHERE source = 'getyourguide' "
                "AND external_id = 'review-777'"
            ).fetchone()
            enrichment = store.connection.execute(
                "SELECT * FROM listing_enrichments WHERE listing_id = ? "
                "AND enrichment_kind = 'getyourguide-detail'",
                (listing["id"],),
            ).fetchone()
            old_snapshot = store.connection.execute(
                "SELECT scraped_at FROM listing_snapshots "
                "WHERE listing_id = ? AND raw_payload_id <> ?",
                (listing["id"], newer_raw_id),
            ).fetchone()[0]

            self.assertEqual(listing["title"], "Current Eger activity title")
            self.assertEqual(listing["rating"], 4.9)
            self.assertEqual(listing["review_count"], 900)
            self.assertEqual(listing["latest_raw_payload_id"], newer_raw_id)
            self.assertEqual(listing["last_seen_at"], new_at)
            self.assertEqual(place["updated_at"], new_at)
            self.assertEqual(media["url"], "https://images.example.test/current.webp")
            self.assertEqual(media["caption"], "Current caption")
            self.assertEqual(media["last_seen_at"], new_at)
            self.assertEqual(package["description"], "Current package description")
            self.assertEqual(package["price"], 25)
            self.assertEqual(package["last_seen_at"], new_at)
            self.assertEqual(review["body"], "Current review body")
            self.assertEqual(review["rating"], 5)
            self.assertEqual(review["raw_payload_id"], newer_raw_id)
            self.assertEqual(review["last_seen_at"], new_at)
            self.assertEqual(enrichment["raw_payload_id"], newer_raw_id)
            self.assertEqual(enrichment["enriched_at"], new_at)
            self.assertEqual(old_snapshot, old_at)

    def test_full_replay_keeps_rich_detail_geography_over_later_shallow_collection(self):
        config = load_config(CONFIG_PATH)
        shallow = {
            "activityId": "832031",
            "activityTitle": "Hidden-city discovery experience",
            "activityUrl": "https://www.getyourguide.com/budapest-l29/hidden-t832031/",
            "country": "Hungary",
            "city": "Badacsonytomaj",
        }
        rich_budapest = {
            **shallow,
            "description": "Discover hidden places in and around Budapest with a local guide.",
            "city": "Budapest",
        }
        later_shallow = {**shallow, "rating": 4.8}
        discovery_metadata = {
            "actor_config_key": "getyourguide-discovery",
            "actor_id": "piotrv1001~getyourguide-listings-scraper",
            "phase_label": "country-hungary",
        }
        detail_metadata = {
            "actor_config_key": "getyourguide-details",
            "actor_id": "piotrv1001~getyourguide-listings-scraper",
            "phase_label": "top-outside-budapest-details",
        }

        with ResearchStore(self.db_path) as store:
            run_ids = []
            for actor_run_id, payload, metadata, observed_at in (
                ("run21", shallow, discovery_metadata, "2026-07-20T08:00:00+00:00"),
                ("run22", rich_budapest, detail_metadata, "2026-07-21T08:00:00+00:00"),
                ("run34", later_shallow, discovery_metadata, "2026-07-22T08:00:00+00:00"),
            ):
                run_id = store.begin_run(
                    "getyourguide",
                    actor_run_id=actor_run_id,
                    dataset_id=f"dataset-{actor_run_id}",
                    metadata=metadata,
                )
                store.record_unparsed_item(
                    run_id,
                    payload,
                    item_index=0,
                    query_label=metadata["phase_label"],
                    fetched_at=observed_at,
                )
                store.finish_run(run_id, status="complete")
                run_ids.append(run_id)

            result = replay_stored_payloads(
                config, store, run_ids=run_ids, print_fn=lambda _line: None
            )
            listing = store.connection.execute(
                "SELECT * FROM listings WHERE external_id = '832031'"
            ).fetchone()
            place = store.connection.execute(
                "SELECT * FROM places WHERE id = ?", (listing["place_id"],)
            ).fetchone()
            snapshots = store.connection.execute(
                "SELECT location_scope FROM listing_snapshots "
                "WHERE listing_id = ? ORDER BY scraped_at",
                (listing["id"],),
            ).fetchall()
            self.assertEqual(3, result["stored"])
            self.assertEqual("experience", listing["kind"])
            self.assertEqual("budapest", listing["location_scope"])
            self.assertEqual("Budapest", place["locality"])
            self.assertEqual("2026-07-22T08:00:00+00:00", listing["last_seen_at"])
            self.assertEqual(
                ["outside-budapest", "budapest", "outside-budapest"],
                [row["location_scope"] for row in snapshots],
            )

            rich_eger = {
                **shallow,
                "description": "Explore the old town and castle in Eger with a local guide.",
                "city": "Eger",
            }
            correction_run = store.begin_run(
                "getyourguide",
                actor_run_id="run35",
                dataset_id="dataset-run35",
                metadata=detail_metadata,
            )
            store.record_unparsed_item(
                correction_run,
                rich_eger,
                item_index=0,
                query_label=detail_metadata["phase_label"],
                fetched_at="2026-07-23T08:00:00+00:00",
            )
            store.finish_run(correction_run, status="complete")
            replay_stored_payloads(
                config,
                store,
                run_ids=[correction_run],
                print_fn=lambda _line: None,
            )
            corrected = store.connection.execute(
                "SELECT * FROM listings WHERE external_id = '832031'"
            ).fetchone()
            corrected_place = store.connection.execute(
                "SELECT * FROM places WHERE id = ?", (corrected["place_id"],)
            ).fetchone()
            self.assertEqual("outside-budapest", corrected["location_scope"])
            self.assertEqual("Eger", corrected_place["locality"])

    def test_local_replay_keeps_incomplete_running_run_resumable(self):
        config = load_config(CONFIG_PATH)
        payload = {
            "activityId": "running-replay-1",
            "activityTitle": "Running replay activity",
            "activityUrl": "https://www.getyourguide.com/eger-l1/running-t1/",
            "sourceCityUrl": "https://www.getyourguide.com/eger-l1/",
        }
        metadata = {
            "actor_config_key": "getyourguide-discovery",
            "actor_id": "piotrv1001~getyourguide-listings-scraper",
            "phase_label": "country-hungary",
            "dataset": {"itemCount": 10},
        }
        with ResearchStore(self.db_path) as store:
            run_id = store.begin_run(
                "getyourguide",
                actor_run_id="still-running-remote",
                dataset_id="partial-dataset",
                metadata=metadata,
            )
            store.record_unparsed_item(run_id, payload, item_index=0)
            replay_stored_payloads(
                config, store, run_ids=[run_id], print_fn=lambda _line: None
            )
            row = store.connection.execute(
                "SELECT status, next_offset, completed_at FROM scrape_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            self.assertEqual((row["status"], row["next_offset"]), ("running", 1))
            self.assertIsNone(row["completed_at"])

    def test_local_replay_rebuilds_headless_getyourguide_projection(self):
        config = load_config(CONFIG_PATH)
        url = "https://www.getyourguide.com/eger-l1/headless-t987/"
        observed_at = "2026-07-20T09:30:00+00:00"
        with ResearchStore(self.db_path) as store:
            discovery = store.begin_run("getyourguide", actor_run_id="discover-987")
            ingest_items(
                store,
                run_id=discovery,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=[
                    {
                        "activityId": "987",
                        "name": "Headless Eger activity",
                        "url": url,
                        "city": "Eger",
                        "country": "Hungary",
                    }
                ],
                query_label="discovery",
                query=None,
                actor_run_id="discover-987",
                dataset_id="discover-987-dataset",
            )
            headless = store.begin_run(
                "getyourguide",
                actor_run_id="headless:getyourguide:test-987",
                metadata={
                    "actor_config_key": "getyourguide-headless-details",
                    "actor_id": "repository-camoufox",
                    "phase_label": "outside-budapest-headless-details",
                },
            )
            evidence = {
                "transport": "camoufox-rendered-page",
                "source": "getyourguide",
                "sourceUrl": url,
                "externalId": "987",
                "visibleText": (
                    "Customer reviews\nFull description\nA memorable Eger activity.\n"
                    + ("useful rendered context " * 30)
                    + "\nProduct ID: 987\n"
                ),
                "renderedHtml": (
                    '<html><head><link rel="canonical" href="'
                    + url
                    + '"></head><body><script>{"route":{"name":"Activity",'
                    + '"path":"/eger-l1/headless-t987/"}}</script>'
                    + ("x" * 1_100)
                    + "</body></html>"
                ),
            }
            store.record_unparsed_item(
                headless,
                evidence,
                item_index=0,
                query_label="outside-budapest-headless-details",
                fetched_at=observed_at,
            )
            store.finish_run(headless, status="partial")
            result = replay_stored_payloads(
                config, store, run_ids=[headless], print_fn=lambda _line: None
            )
            row = store.connection.execute(
                "SELECT status FROM scrape_runs WHERE id = ?", (headless,)
            ).fetchone()
            enrichment = store.connection.execute(
                """
                SELECT COUNT(*) FROM listing_enrichments
                WHERE enrichment_kind = 'getyourguide-detail'
                  AND enrichment_version = 'getyourguide-browser-render-v1'
                """
            ).fetchone()[0]
            attempt = store.connection.execute(
                """
                SELECT status, requested_url, attempted_at
                FROM listing_enrichment_attempts
                WHERE enrichment_kind = 'getyourguide-detail'
                  AND enrichment_version = 'getyourguide-browser-render-v1'
                  AND run_id = ?
                """,
                (headless,),
            ).fetchone()
            self.assertEqual(result["stored"], 1)
            self.assertEqual(row["status"], "complete")
            self.assertEqual(enrichment, 1)
            self.assertEqual(
                (attempt["status"], attempt["requested_url"], attempt["attempted_at"]),
                ("succeeded", url, observed_at),
            )

    def test_dataset_import_is_idempotent_and_normalizes_searchable_fields(self):
        config = load_config(CONFIG_PATH)
        payload = {
            "activityId": "12345",
            "name": "Tihany memorable lake experience",
            "url": "https://www.getyourguide.com/tihany-l105767/example-t12345/",
            "description": "A distinctive peninsula experience.",
            "rating": 4.8,
            "reviewCount": 620,
            "price": 33.5,
            "currency": "EUR",
            "duration": "3 hours",
            "location": "Tihany",
            "country": "Hungary",
            "latitude": 46.9137,
            "longitude": 17.8897,
            "sampleReviews": [
                {
                    "id": "r1",
                    "author": "Private Person",
                    "rating": 5,
                    "body": "Rare and memorable.",
                    "date": "2026-07-01",
                }
            ],
            "options": [
                {"id": "basic", "name": "Standard", "price": 33.5, "currency": "EUR"}
            ],
        }
        output = io.StringIO()
        client = DatasetClient([payload])
        with ResearchStore(self.db_path) as store:
            for _ in range(2):
                summary = import_dataset(
                    config,
                    store,
                    client,
                    actor_key="getyourguide-discovery",
                    dataset_id="existing-dataset",
                    print_fn=lambda line: output.write(line + "\n"),
                )
                self.assertEqual((summary.stored, summary.failed), (1, 0))
            listing = store.connection.execute(
                """
                SELECT l.rating, l.review_count, l.price_from, l.currency,
                       l.duration_text, l.location_scope, p.locality,
                       p.country_code, p.latitude, p.longitude
                FROM listings AS l JOIN places AS p ON p.id = l.place_id
                """
            ).fetchone()
            counts = {
                table: store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("raw_payloads", "scrape_runs", "scrape_run_items", "listings", "reviews", "packages")
            }
            review_columns = {
                row[1] for row in store.connection.execute("PRAGMA table_info(reviews)").fetchall()
            }
            self.assertEqual(listing["rating"], 4.8)
            self.assertEqual(listing["review_count"], 620)
            self.assertEqual(listing["price_from"], 33.5)
            self.assertEqual(listing["currency"], "EUR")
            self.assertEqual(listing["duration_text"], "3 hours")
            self.assertEqual(listing["location_scope"], "outside-budapest")
            self.assertEqual(listing["locality"], "Tihany")
            self.assertEqual(listing["country_code"], "HU")
            self.assertAlmostEqual(listing["latitude"], 46.9137)
            self.assertAlmostEqual(listing["longitude"], 17.8897)
            self.assertEqual(counts, {
                "raw_payloads": 1,
                "scrape_runs": 1,
                "scrape_run_items": 1,
                "listings": 1,
                "reviews": 1,
                "packages": 1,
            })
            self.assertNotIn("author", review_columns)
            self.assertNotIn("Private Person", output.getvalue())

    def test_completed_dataset_records_and_prints_global_net_new_yield(self):
        item = {
            "activityId": "telemetry-1",
            "name": "Eger telemetry experience",
            "url": "https://www.getyourguide.com/eger-l1/telemetry-t1/",
            "rating": 4.8,
            "reviewCount": 500,
            "city": "Eger",
            "country": "Hungary",
        }
        plan = PlannedRun(
            actor_key="getyourguide-discovery",
            actor_id="piotrv1001~getyourguide-listings-scraper",
            source="getyourguide",
            phase_label="telemetry-duplicate-page",
            run_input={"searchUrl": "https://www.getyourguide.com/hungary-l169024/"},
            max_items=1,
            max_total_charge_usd=Decimal("0.01"),
        )
        output: list[str] = []
        with ResearchStore(self.db_path) as store:
            seed = store.begin_run("getyourguide", actor_run_id="telemetry-seed")
            ingest_items(
                store,
                run_id=seed,
                source="getyourguide",
                actor_id=plan.actor_id,
                items=[item],
                query_label="telemetry-seed",
                query=None,
                actor_run_id="telemetry-seed",
                dataset_id="telemetry-seed-dataset",
            )
            store.finish_run(seed, status="complete")

            summary = ingest_completed_dataset(
                store,
                DatasetClient([item]),
                plan=plan,
                actor_run={
                    "id": "telemetry-duplicate",
                    "status": "SUCCEEDED",
                    "defaultDatasetId": "telemetry-duplicate-dataset",
                },
                print_fn=output.append,
            )
            stats = json.loads(
                store.connection.execute(
                    "SELECT stats_json FROM scrape_runs WHERE actor_run_id = ?",
                    ("telemetry-duplicate",),
                ).fetchone()[0]
            )

        self.assertEqual(summary.unique_yield, 1)
        self.assertEqual(summary.new_listing_yield, 0)
        self.assertEqual(stats["unique_yield"], 1)
        self.assertEqual(stats["new_listing_yield"], 0)
        self.assertTrue(
            any("1 unique in run, 0 globally new" in line for line in output),
            output,
        )

    def test_streamed_dataset_keeps_committed_pages_and_resume_offset_on_later_failure(self):
        config = load_config(CONFIG_PATH)
        plan = next(
            plan
            for plan in build_discovery_plans(config)
            if plan.actor_key == "getyourguide-discovery"
        )
        items = [
            {
                "activityId": f"stream-{index}",
                "name": f"Streamed activity {index}",
                "url": f"https://www.getyourguide.com/eger-l1/stream-t{index}/",
                "city": "Eger",
                "country": "Hungary",
            }
            for index in range(2)
        ]
        client = PartialPageClient(items)
        actor_run = {
            "id": "stream-run",
            "status": "SUCCEEDED",
            "defaultDatasetId": "stream-dataset",
        }
        with ResearchStore(self.db_path) as store:
            local_id = store.begin_run(
                "getyourguide",
                actor_run_id="stream-run",
                input_data=plan.run_input,
                metadata={},
            )
            with self.assertRaisesRegex(OSError, "second page failed"):
                ingest_completed_dataset(
                    store,
                    client,
                    plan=plan,
                    actor_run=actor_run,
                    local_run_id=local_id,
                    print_fn=lambda _line: None,
                )
            row = store.connection.execute(
                "SELECT status, next_offset FROM scrape_runs WHERE id = ?", (local_id,)
            ).fetchone()
            self.assertEqual((row["status"], row["next_offset"]), ("running", 1))
            self.assertEqual(store.stats()["raw_payloads"], 1)
            self.assertEqual(store.stats()["run_items"], 1)
            self.assertEqual(store.stats()["listings"], 1)

    def test_resume_imports_successful_actor_dataset_without_starting_actor(self):
        config = load_config(CONFIG_PATH)
        payload = {
            "activityId": "resume-1",
            "name": "Eger unusual experience",
            "url": "https://www.getyourguide.com/eger-l1573/example-t1/",
            "rating": 4.7,
            "reviewCount": 300,
            "location": "Eger",
            "country": "Hungary",
        }
        metadata = {
            "actor_config_key": "getyourguide-discovery",
            "actor_id": "piotrv1001~getyourguide-listings-scraper",
            "phase_label": "country-hungary",
            "query": None,
            "hard_caps": {"max_items": 250, "max_total_charge_usd": "0.51"},
        }
        client = ResumeClient([payload])
        with ResearchStore(self.db_path) as store:
            local_run_id = store.begin_run(
                "getyourguide",
                actor_run_id="completed-remote-run",
                input_data={"mode": "by-city"},
                metadata=metadata,
            )
            ok = resume_runs(
                config,
                store,
                client,
                actor_run_ids=["completed-remote-run"],
                print_fn=lambda _line: None,
            )
            row = store.connection.execute(
                "SELECT status, dataset_id FROM scrape_runs WHERE id = ?", (local_run_id,)
            ).fetchone()
            self.assertTrue(ok)
            self.assertEqual(row["status"], "complete")
            self.assertEqual(row["dataset_id"], "resume-dataset")
            self.assertEqual(store.stats()["listings"], 1)
            self.assertEqual(client.started, 0)

    def test_general_resume_ignores_partial_local_headless_runs(self):
        config = load_config(CONFIG_PATH)
        client = ResumeClient([])
        with ResearchStore(self.db_path) as store:
            run_id = store.begin_run(
                "tripadvisor",
                actor_run_id="headless:tripadvisor:test",
                metadata={
                    "actor_config_key": "tripadvisor-headless-details",
                    "actor_id": "repository-camoufox",
                },
            )
            store.finish_run(run_id, status="partial")
            self.assertTrue(
                resume_runs(config, store, client, print_fn=lambda _line: None)
            )
        self.assertEqual(client.started, 0)

    def test_tail_quality_metrics_follow_configured_bayesian_thresholds(self):
        items = [
            {
                "activityId": f"strong-{index}",
                "name": f"Strong memorable activity {index}",
                "url": f"https://www.getyourguide.com/eger-l1/strong-t{index}/",
                "rating": 4.8,
                "reviewCount": 500,
                "city": "Eger",
                "country": "Hungary",
            }
            for index in range(3)
        ]
        with ResearchStore(self.db_path) as store:
            run_id = store.begin_run("getyourguide", actor_run_id="tail-run")
            summary = ingest_items(
                store,
                run_id=run_id,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=items,
                query_label="tail",
                query=None,
                actor_run_id="tail-run",
                dataset_id="tail-dataset",
                ranking={
                    "priorRating": 4.0,
                    "priorReviews": 50,
                    "tailWindow": 2,
                    "strongTailMinReviews": 50,
                    "strongTailMinBayesianRating": 4.3,
                    "strongTailMinCount": 2,
                },
            )
            self.assertEqual(summary.unique_yield, 3)
            self.assertEqual(summary.tail_count, 2)
            self.assertEqual(summary.strong_tail_count, 2)
            self.assertTrue(summary.tail_quality_signal)

    def test_historical_tail_uses_observed_snapshot_not_later_listing_value(self):
        policy = {
            "tailWindow": 1,
            "strongTailMinReviews": 50,
            "strongTailMinBayesianRating": 4.3,
            "strongTailMinCount": 1,
        }
        with ResearchStore(self.db_path) as store:
            first_run = store.begin_run("getyourguide", actor_run_id="tail-observed-1")
            first_summary = ingest_items(
                store,
                run_id=first_run,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=[
                    {
                        "activityId": "tail-mutable",
                        "name": "Mutable activity",
                        "url": "https://www.getyourguide.com/eger-l1/mutable-t1/",
                        "rating": 4.0,
                        "reviewCount": 5,
                        "city": "Eger",
                        "country": "Hungary",
                    }
                ],
                query_label="first",
                query=None,
                actor_run_id="tail-observed-1",
                dataset_id="tail-observed-d1",
                ranking=policy,
            )
            second_run = store.begin_run("getyourguide", actor_run_id="tail-observed-2")
            ingest_items(
                store,
                run_id=second_run,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=[
                    {
                        "activityId": "tail-mutable",
                        "name": "Mutable activity",
                        "url": "https://www.getyourguide.com/eger-l1/mutable-t1/",
                        "rating": 5.0,
                        "reviewCount": 500,
                        "city": "Eger",
                        "country": "Hungary",
                    }
                ],
                query_label="second",
                query=None,
                actor_run_id="tail-observed-2",
                dataset_id="tail-observed-d2",
                ranking=policy,
            )
            historical = summarize_run(store, first_run, ranking=policy)
            self.assertEqual(historical.strong_tail_count, 0)
            self.assertFalse(historical.tail_quality_signal)

    def test_adaptive_pages_require_strong_tail_and_net_new_listings(self):
        page_2 = PlannedRun(
            actor_key="tripadvisor-discovery",
            actor_id="maxcopell~tripadvisor",
            source="tripadvisor",
            phase_label="country-hungary-page-2",
            run_input={"query": "synthetic-page-2"},
            max_items=30,
            max_total_charge_usd=Decimal("0.151"),
            requires_tail_signal_from="country-hungary",
            min_prior_new_listings=5,
        )
        page_3 = PlannedRun(
            actor_key="tripadvisor-discovery",
            actor_id="maxcopell~tripadvisor",
            source="tripadvisor",
            phase_label="country-hungary-page-3",
            run_input={"query": "synthetic-page-3"},
            max_items=30,
            max_total_charge_usd=Decimal("0.151"),
            requires_tail_signal_from="country-hungary-page-2",
            min_prior_new_listings=5,
        )
        items = [
            {
                "locationId": f"adaptive-{index}",
                "name": f"Strong Hungary activity {index}",
                "webUrl": (
                    "https://www.tripadvisor.com/Attraction_Review-"
                    f"g274891-d{9000 + index}-Reviews-Example-Tihany.html"
                ),
                "rating": 4.8,
                "numberOfReviews": 500,
                "addressObj": {"city": "Tihany", "country": "Hungary"},
            }
            for index in range(5)
        ]
        with ResearchStore(self.db_path) as store:
            should_run, _reason = continuation_decision(store, page_2)
            self.assertFalse(should_run)

            first = store.begin_run(
                "tripadvisor",
                actor_run_id="adaptive-page-1",
                metadata={"phase_label": "country-hungary"},
            )
            first_summary = ingest_items(
                store,
                run_id=first,
                source="tripadvisor",
                actor_id="maxcopell~tripadvisor",
                items=items,
                query_label="country-hungary",
                query=None,
                actor_run_id="adaptive-page-1",
                dataset_id="adaptive-dataset-1",
            )
            self.assertEqual(first_summary.unique_yield, 5)
            self.assertEqual(first_summary.new_listing_yield, 5)
            store.finish_run(first, status="complete", stats={"tail_quality_signal": True})
            should_run, reason = continuation_decision(store, page_2)
            self.assertTrue(should_run)
            self.assertIn("5 new listings", reason)

            duplicate_page = store.begin_run(
                "tripadvisor",
                actor_run_id="adaptive-page-2",
                metadata={"phase_label": "country-hungary-page-2"},
            )
            duplicate_summary = ingest_items(
                store,
                run_id=duplicate_page,
                source="tripadvisor",
                actor_id="maxcopell~tripadvisor",
                items=items,
                query_label="country-hungary-page-2",
                query=None,
                actor_run_id="adaptive-page-2",
                dataset_id="adaptive-dataset-2",
            )
            self.assertEqual(duplicate_summary.unique_yield, 5)
            self.assertEqual(duplicate_summary.new_listing_yield, 0)
            persisted_duplicate = summarize_run(store, duplicate_page)
            self.assertEqual(persisted_duplicate.unique_yield, 5)
            self.assertEqual(persisted_duplicate.new_listing_yield, 0)
            store.finish_run(
                duplicate_page,
                status="complete",
                stats={"tail_quality_signal": True},
            )
            should_run, reason = continuation_decision(store, page_3)
            self.assertFalse(should_run)
            self.assertIn("only 0 new listings", reason)

    def test_detail_selection_uses_quality_and_excludes_budapest(self):
        items = [
            {
                "activityId": "outside-strong",
                "name": "Lake Balaton special boat",
                "url": "https://www.getyourguide.com/balaton-l1/strong-t1/",
                "rating": 4.8,
                "reviewCount": 900,
                "city": "Tihany",
                "country": "Hungary",
            },
            {
                "activityId": "outside-weak",
                "name": "Eger walk",
                "url": "https://www.getyourguide.com/eger-l2/weak-t2/",
                "rating": 4.9,
                "reviewCount": 2,
                "city": "Eger",
                "country": "Hungary",
            },
            {
                "activityId": "budapest",
                "name": "Budapest bestseller",
                "url": "https://www.getyourguide.com/budapest-l3/city-t3/",
                "rating": 5.0,
                "reviewCount": 50000,
                "city": "Budapest",
                "country": "Hungary",
            },
        ]
        with ResearchStore(self.db_path) as store:
            run_id = store.begin_run("getyourguide", actor_run_id="rank-run")
            summary = ingest_items(
                store,
                run_id=run_id,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=items,
                query_label="country-hungary",
                query=None,
                actor_run_id="rank-run",
                dataset_id="rank-dataset",
            )
            self.assertEqual(summary.stored, 3)
        urls = select_getyourguide_detail_urls(self.db_path, limit=2)
        self.assertEqual(
            urls,
            [
                "https://www.getyourguide.com/balaton-l1/strong-t1/",
                "https://www.getyourguide.com/eger-l2/weak-t2/",
            ],
        )
        self.assertTrue(all("budapest" not in url for url in urls))

        with ResearchStore(self.db_path) as store:
            row = store.connection.execute(
                """
                SELECT id, latest_raw_payload_id
                FROM listings
                WHERE external_id = 'outside-strong'
                """
            ).fetchone()
            store.mark_enrichment(
                int(row["id"]),
                kind="getyourguide-detail",
                version="getyourguide-browser-render-v1",
                raw_payload_id=int(row["latest_raw_payload_id"]),
            )
        self.assertEqual(
            select_getyourguide_detail_urls(self.db_path, limit=2),
            ["https://www.getyourguide.com/eger-l2/weak-t2/"],
        )

    def test_detail_selection_filters_in_sql_beyond_ten_thousand_rows(self):
        target_url = "https://www.getyourguide.com/eger-l2/target-t20001/"
        seen_at = "2026-07-23T00:00:00+00:00"
        with ResearchStore(self.db_path) as store:
            connection = store.connection
            connection.execute(
                """
                INSERT INTO raw_payloads(
                    source, sha256, canonical_json, fetched_at, created_at
                ) VALUES ('getyourguide', ?, '{}', ?, ?)
                """,
                ("c" * 64, seen_at, seen_at),
            )
            raw_id = int(connection.execute(
                "SELECT id FROM raw_payloads WHERE sha256 = ?", ("c" * 64,)
            ).fetchone()[0])
            connection.execute(
                """
                INSERT INTO places(
                    place_key, canonical_name, normalized_name, country_code,
                    locality, location_scope, created_at, updated_at
                ) VALUES (
                    'bulk-eger', 'Eger', 'eger', 'HU', 'Eger',
                    'outside-budapest', ?, ?
                )
                """,
                (seen_at, seen_at),
            )
            place_id = int(connection.execute(
                "SELECT id FROM places WHERE place_key = 'bulk-eger'"
            ).fetchone()[0])
            rows = [
                (
                    place_id,
                    f"bulk-{index}",
                    f"https://www.getyourguide.com/eger-l2/bulk-t{index}/",
                    f"High-ranked enriched {index:05d}",
                    raw_id,
                    seen_at,
                    seen_at,
                )
                for index in range(1, 10_006)
            ]
            rows.append(
                (
                    place_id,
                    "20001",
                    target_url,
                    "Lowest-ranked missing detail",
                    raw_id,
                    seen_at,
                    seen_at,
                )
            )
            connection.executemany(
                """
                INSERT INTO listings(
                    place_id, source, external_id, url, title, kind, rating,
                    review_count, location_scope, latest_raw_payload_id,
                    first_seen_at, last_seen_at
                ) VALUES (
                    ?, 'getyourguide', ?, ?, ?, 'experience', 5.0, 1000,
                    'outside-budapest', ?, ?, ?
                )
                """,
                rows,
            )
            top_ids = connection.execute(
                """
                SELECT id FROM listings
                WHERE source = 'getyourguide' AND external_id LIKE 'bulk-%'
                """
            ).fetchall()
            connection.executemany(
                """
                INSERT INTO listing_enrichments(
                    listing_id, enrichment_kind, enrichment_version,
                    raw_payload_id, enriched_at
                ) VALUES (?, 'getyourguide-detail', 'already-done', ?, ?)
                """,
                [(int(row[0]), raw_id, seen_at) for row in top_ids],
            )
            connection.commit()

        self.assertEqual(
            [target_url], select_getyourguide_detail_urls(self.db_path, limit=1)
        )

    def test_detail_urls_stop_retrying_after_two_not_returned_datasets(self):
        config = load_config(CONFIG_PATH)
        url = "https://www.getyourguide.com/eger-l2/rare-t222/"
        payload = {
            "activityId": "222",
            "name": "Rare Eger activity",
            "url": url,
            "rating": 4.8,
            "ratingCount": 100,
            "location": "Eger",
        }
        plan = build_detail_plan(config, [url])
        self.assertIsNotNone(plan)
        with ResearchStore(self.db_path) as store:
            discovery = store.begin_run("getyourguide", actor_run_id="discover-222")
            ingest_items(
                store,
                run_id=discovery,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=[payload],
                query_label="test",
                query=None,
                actor_run_id="discover-222",
                dataset_id="discover-dataset",
            )
            for attempt in range(2):
                run_id = store.begin_run(
                    "getyourguide", actor_run_id=f"missing-detail-{attempt}"
                )
                counts = record_getyourguide_detail_attempts(
                    store,
                    run_id=run_id,
                    plan=plan,
                )
                self.assertEqual(counts["not-returned"], 1)
                store.finish_run(run_id, status="complete")

        self.assertEqual(
            select_getyourguide_detail_urls(
                self.db_path, limit=10, max_not_returned_attempts=2
            ),
            [],
        )
        self.assertEqual(
            select_getyourguide_detail_urls(
                self.db_path, limit=10, max_not_returned_attempts=3
            ),
            [url],
        )

    def test_identical_empty_detail_dataset_is_retried_until_attempt_cap(self):
        config = load_config(CONFIG_PATH)
        url = "https://www.getyourguide.com/eger-l2/retry-t333/"
        plan = build_detail_plan(config, [url])
        self.assertIsNotNone(plan)
        client = EmptyDetailClient()
        with ResearchStore(self.db_path) as store:
            discovery = store.begin_run("getyourguide", actor_run_id="discover-333")
            ingest_items(
                store,
                run_id=discovery,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=[
                    {
                        "activityId": "333",
                        "name": "Retryable Eger activity",
                        "url": url,
                        "rating": 4.8,
                        "ratingCount": 100,
                        "location": "Eger",
                    }
                ],
                query_label="test",
                query=None,
                actor_run_id="discover-333",
                dataset_id="discover-dataset",
            )
            previous = store.begin_run(
                "getyourguide",
                actor_run_id="first-empty-detail",
                dataset_id="first-empty-dataset",
                input_data=plan.run_input,
                metadata={
                    "actor_config_key": plan.actor_key,
                    "actor_id": plan.actor_id,
                    "phase_label": plan.phase_label,
                    "hard_caps": {
                        "max_items": plan.max_items,
                        "max_total_charge_usd": str(plan.max_total_charge_usd),
                    },
                },
                plan_fingerprint=plan_fingerprint(plan),
            )
            record_getyourguide_detail_attempts(store, run_id=previous, plan=plan)
            store.finish_run(previous, status="complete")

            self.assertTrue(
                execute_pipeline(
                    config,
                    store,
                    client,
                    only_actor_key="getyourguide-details",
                    print_fn=lambda _line: None,
                )
            )
            attempts = store.connection.execute(
                """
                SELECT COUNT(*) FROM listing_enrichment_attempts
                WHERE enrichment_kind = 'getyourguide-detail'
                  AND enrichment_version = 'piotrv1001-v1'
                  AND status = 'not-returned'
                """
            ).fetchone()[0]
            self.assertEqual(attempts, 2)
        self.assertEqual(client.started, 1)
        self.assertEqual(
            select_getyourguide_detail_urls(
                self.db_path, limit=10, max_not_returned_attempts=2
            ),
            [],
        )

    def test_returned_but_unparseable_detail_is_failed_not_not_returned(self):
        config = load_config(CONFIG_PATH)
        url = "https://www.getyourguide.com/eger-l2/broken-t444/"
        plan = build_detail_plan(config, [url])
        self.assertIsNotNone(plan)
        with ResearchStore(self.db_path) as store:
            discovery = store.begin_run("getyourguide", actor_run_id="discover-444")
            ingest_items(
                store,
                run_id=discovery,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=[
                    {
                        "activityId": "444",
                        "name": "Broken detail activity",
                        "url": url,
                        "location": "Eger",
                    }
                ],
                query_label="discovery",
                query=None,
                actor_run_id="discover-444",
                dataset_id="discovery-444",
            )
            detail_run = store.begin_run("getyourguide", actor_run_id="broken-detail")
            summary = ingest_items(
                store,
                run_id=detail_run,
                source="getyourguide",
                actor_id="piotrv1001~getyourguide-listings-scraper",
                items=[{"activityUrl": url}],
                query_label="details",
                query=None,
                actor_run_id="broken-detail",
                dataset_id="broken-detail-dataset",
            )
            self.assertEqual(summary.failed, 1)
            counts = record_getyourguide_detail_attempts(
                store, run_id=detail_run, plan=plan
            )
            self.assertEqual(counts, {"succeeded": 0, "not-returned": 0, "failed": 1})


if __name__ == "__main__":
    unittest.main()
