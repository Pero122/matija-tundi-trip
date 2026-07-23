#!/usr/bin/env python3

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from query import database_stats, query_listings, query_reviews, query_runs  # noqa: E402
from normalizers import GETYOURGUIDE_ACTOR, normalize_item  # noqa: E402
from store import ResearchStore, SCHEMA_PATH, canonical_json, payload_sha256  # noqa: E402


def tapolca_payload() -> dict:
    return {
        "locationId": "ta-101",
        "name": "Tapolca Lake Cave Boat Experience",
        "description": "Row a small boat through a rare limestone cave beneath the town.",
        "webUrl": "https://example.test/tapolca",
        "rating": 4.6,
        "numberOfReviews": "19,379 reviews",
        "price": {"amount": 7700, "currency": "HUF"},
        "duration": "1.5 hours",
        "location": {
            "name": "Tapolca Lake Cave",
            "city": "Tapolca",
            "country": "Hungary",
        },
        "latitude": 46.8814,
        "longitude": 17.4414,
        "categories": [{"name": "Caves"}, {"name": "Boat Tours"}],
        "photos": [
            {
                "id": "photo-1",
                "url": "https://images.example.test/tapolca.webp",
                "caption": "Boat in the cave",
            }
        ],
        "options": [
            {
                "id": "adult",
                "name": "Adult admission",
                "price": {"amount": 7700, "currency": "HUF"},
            },
            {
                "id": "student",
                "name": "Student admission",
                "price": {"amount": 5500, "currency": "HUF"},
            },
        ],
        "reviews": [
            {
                "reviewId": "review-1",
                "rating": 5,
                "title": "Unexpectedly memorable",
                "text": "The magical underground boat ride was unlike a normal cave walk.",
                "languageCode": "en",
                "publishedDate": "2026-07-10",
                "helpfulVotes": 8,
                "username": "Private Reviewer",
                "user": {"id": "private-user-id", "avatar": "private-avatar.jpg"},
            }
        ],
    }


class ResearchStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "research.sqlite3"
        self.store = ResearchStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def scalar(self, sql: str, params: tuple = ()) -> int | float | str | None:
        return self.store.connection.execute(sql, params).fetchone()[0]

    def test_idempotent_ingest_retains_raw_payload_snapshots_and_query_provenance(self) -> None:
        payload = tapolca_payload()
        run_id = self.store.begin_run(
            "tripadvisor",
            actor_run_id="actor-run-42",
            dataset_id="dataset-9",
            input_data={"destinations": ["Hungary"], "maxItems": 500},
            metadata={"actor": "tripadvisor-scraper", "build": "1.2.3"},
            started_at="2026-07-23T00:00:00+00:00",
        )
        first = self.store.ingest_item(
            run_id,
            payload,
            query_label="top rated Hungary",
            destination="Hungary",
            result_rank=1,
            item_metadata={"page": 1, "position": 1},
            fetched_at="2026-07-23T00:01:00+00:00",
        )
        second = self.store.ingest_item(
            run_id,
            payload,
            query_label="caves outside Budapest",
            destination="Tapolca",
            result_rank=3,
            item_metadata={"page": 1, "position": 3},
            fetched_at="2026-07-23T00:02:00+00:00",
        )

        self.assertEqual(first["listing_id"], second["listing_id"])
        self.assertEqual(first["raw_payload_id"], second["raw_payload_id"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM raw_payloads"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM listings"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM listing_snapshots"))
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM scrape_run_items"))

        raw = self.store.connection.execute("SELECT * FROM raw_payloads").fetchone()
        self.assertEqual(canonical_json(payload), raw["canonical_json"])
        self.assertEqual(payload_sha256(payload), raw["sha256"])
        self.assertEqual(1, raw["is_private"])

        occurrences = self.store.connection.execute(
            """
            SELECT query_label, destination, result_rank, metadata_json, observed_at
            FROM scrape_run_items ORDER BY item_index
            """
        ).fetchall()
        self.assertEqual("top rated Hungary", occurrences[0]["query_label"])
        self.assertEqual("caves outside Budapest", occurrences[1]["query_label"])
        self.assertEqual("2026-07-23T00:01:00+00:00", occurrences[0]["observed_at"])
        self.assertEqual("2026-07-23T00:02:00+00:00", occurrences[1]["observed_at"])
        self.assertEqual({"page": 1, "position": 3}, json.loads(occurrences[1]["metadata_json"]))

        changed = tapolca_payload()
        changed["rating"] = 4.7
        changed["numberOfReviews"] = 20_001
        updated = self.store.ingest_item(
            run_id,
            changed,
            query_label="top rated Hungary",
            destination="Hungary",
            result_rank=1,
            fetched_at="2026-07-23T01:00:00+00:00",
        )
        self.assertEqual(first["listing_id"], updated["listing_id"])
        self.assertNotEqual(first["raw_payload_id"], updated["raw_payload_id"])
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM raw_payloads"))
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM listing_snapshots"))
        listing = self.store.connection.execute("SELECT * FROM listings").fetchone()
        self.assertEqual(4.7, listing["rating"])
        self.assertEqual(20_001, listing["review_count"])
        self.assertEqual("outside-budapest", listing["location_scope"])

        self.store.finish_run(
            run_id,
            stats={"items": 3, "requests": 2},
            metadata={"actor": "tripadvisor-scraper", "exitCode": 0},
            completed_at="2026-07-23T01:01:00+00:00",
        )
        run = query_runs(self.db_path)[0]
        self.assertEqual("complete", run["status"])
        self.assertEqual("actor-run-42", run["actor_run_id"])
        self.assertEqual("dataset-9", run["dataset_id"])
        self.assertEqual(3, run["item_count"])
        self.assertEqual({"items": 3, "requests": 2}, json.loads(run["stats_json"]))

    def test_custom_existing_parent_permissions_are_not_changed(self) -> None:
        shared_parent = Path(self.temp_dir.name) / "shared-parent"
        shared_parent.mkdir(mode=0o755)
        shared_parent.chmod(0o755)

        custom_store = ResearchStore(shared_parent / "custom.sqlite3")
        custom_store.close()

        self.assertEqual(shared_parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual((shared_parent / "custom.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_new_custom_parent_is_created_private(self) -> None:
        private_parent = Path(self.temp_dir.name) / "new-private-parent"
        custom_store = ResearchStore(private_parent / "custom.sqlite3")
        custom_store.close()

        self.assertEqual(private_parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((private_parent / "custom.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_normalized_tables_search_filters_and_review_privacy(self) -> None:
        payload = tapolca_payload()
        payload["kind"] = "Attraction"
        stored = self.store.ingest_listing("tripadvisor", payload)

        columns = {
            row["name"]
            for row in self.store.connection.execute("PRAGMA table_info(reviews)").fetchall()
        }
        self.assertTrue({"title", "body", "language", "review_date"} <= columns)
        self.assertFalse(columns & {"author", "reviewer", "username", "user_id", "profile_url", "avatar"})
        normalized_review = dict(self.store.connection.execute("SELECT * FROM reviews").fetchone())
        self.assertNotIn("Private Reviewer", json.dumps(normalized_review))
        raw_json = self.scalar("SELECT canonical_json FROM raw_payloads")
        self.assertIn("Private Reviewer", raw_json)
        self.assertIn("private-user-id", raw_json)

        listings = query_listings(
            self.db_path,
            search="rare limestone",
            sources=["tripadvisor"],
            kinds=["ATTRACTION"],
            scopes=["outside-budapest"],
            category="caves",
            min_rating=4.5,
            min_reviews=10_000,
            sort="quality",
        )
        self.assertEqual([stored["listing_id"]], [row["id"] for row in listings])
        self.assertEqual("attraction", listings[0]["kind"])
        self.assertEqual([], query_listings(self.db_path, kinds="experience"))
        self.assertEqual(2, listings[0]["active_packages"])
        self.assertEqual(1, listings[0]["active_media"])
        self.assertEqual(1, listings[0]["stored_reviews"])

        reviews = query_reviews(
            self.db_path,
            search="magical underground boat",
            scopes="outside-budapest",
            language="en",
            min_rating=5,
            sort="relevance",
        )
        self.assertEqual(1, len(reviews))
        self.assertEqual("review-1", reviews[0]["external_id"])

        standalone = {
            "reviewId": "review-2",
            "rating": 4,
            "text": "The little boat is distinctive.",
            "username": "Another Secret Name",
        }
        first_review_id = self.store.ingest_review("tripadvisor", "ta-101", standalone)
        second_review_id = self.store.ingest_review("tripadvisor", "ta-101", standalone)
        self.assertEqual(first_review_id, second_review_id)
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM reviews"))

        counts = database_stats(self.db_path)
        self.assertEqual(1, counts["listings"])
        self.assertEqual(2, counts["reviews"])
        self.assertEqual(2, counts["packages"])

    def test_bayesian_ranking_rewards_strong_evidence(self) -> None:
        def add(external_id: str, title: str, rating: float, reviews: int) -> None:
            payload = {
                "id": external_id,
                "name": title,
                "description": f"{title} description",
                "rating": rating,
                "reviewCount": reviews,
                "location": {"name": title, "city": "Eger", "country": "Hungary"},
            }
            self.store.ingest_listing("getyourguide", payload)

        add("tiny-perfect", "Tiny Perfect Score", 5.0, 1)
        add("proven-great", "Proven Great Experience", 4.8, 1_000)
        add("baseline", "Ordinary Baseline", 3.0, 1_000)

        rows = query_listings(
            self.db_path,
            sources="getyourguide",
            scopes="outside-budapest",
            sort="quality",
        )
        by_id = {row["external_id"]: row for row in rows}
        self.assertGreater(
            by_id["proven-great"]["bayesian_rating"],
            by_id["tiny-perfect"]["bayesian_rating"],
        )
        self.assertEqual("proven-great", rows[0]["external_id"])

    def test_provider_normalizer_shape_keeps_price_duration_place_and_packages(self) -> None:
        payload = {
            "activityId": "gyg-77",
            "title": "Eger Castle Night Adventure",
            "description": "An unusual guided night experience in Eger.",
            "url": "https://www.getyourguide.test/eger-t77/",
            "rating": 4.8,
            "reviewsCount": 712,
            "priceFrom": "€42.50",
            "currency": "EUR",
            "duration": "3 hours",
            "countryCode": "HU",
            "city": "Eger",
            "lat": 47.904,
            "lon": 20.379,
            "options": [
                {
                    "id": "night",
                    "name": "Night tour",
                    "price": 42.5,
                    "currency": "EUR",
                    "duration": "3 hours",
                    "availability": "Thursday–Sunday",
                    "url": "https://book.example.test/night",
                    "provider": "Example Operator",
                    "category": "Night Tours",
                }
            ],
        }
        normalized = normalize_item(GETYOURGUIDE_ACTOR, payload, rank=4)
        self.assertIsNotNone(normalized)
        self.store.ingest_listing("getyourguide", payload, normalized=normalized)

        listing = self.store.connection.execute("SELECT * FROM listings").fetchone()
        place = self.store.connection.execute("SELECT * FROM places").fetchone()
        package = self.store.connection.execute("SELECT * FROM packages").fetchone()
        self.assertEqual("getyourguide", listing["source"])
        self.assertEqual("experience", listing["kind"])
        self.assertEqual(42.5, listing["price_from"])
        self.assertEqual("3 hours", listing["duration_text"])
        self.assertEqual("outside-budapest", listing["location_scope"])
        self.assertEqual("HU", place["country_code"])
        self.assertEqual("Eger", place["locality"])
        self.assertEqual(47.904, place["latitude"])
        self.assertEqual(20.379, place["longitude"])
        self.assertEqual("3 hours", package["duration_text"])
        self.assertEqual("Thursday–Sunday", package["availability_text"])
        self.assertEqual("https://book.example.test/night", package["url"])
        self.assertEqual("Example Operator", package["provider"])
        self.assertEqual("Night Tours", package["category"])
        self.assertEqual(0, package["sort_order"])

    def test_provider_normalizer_is_authoritative_for_alias_only_payload(self) -> None:
        payload = {
            "activityId": "alias-88",
            "activityTitle": "Tihany sunset paddle",
            "activityUrl": "https://www.getyourguide.test/tihany-t88/",
            "rating": 4.9,
            "location": "Tihany",
            "sourceCityUrl": "https://www.getyourguide.com/tihany-l105767/",
        }
        normalized = normalize_item(GETYOURGUIDE_ACTOR, payload)

        stored = self.store.ingest_listing(
            "getyourguide", payload, normalized=normalized
        )

        listing = self.store.connection.execute(
            "SELECT * FROM listings WHERE id = ?", (stored["listing_id"],)
        ).fetchone()
        self.assertEqual("Tihany sunset paddle", listing["title"])
        self.assertEqual("outside-budapest", listing["location_scope"])

    def test_sparse_unknown_kind_does_not_erase_current_kind_but_remains_in_snapshot(self) -> None:
        rich = {
            **tapolca_payload(),
            "kind": "Attraction",
        }
        self.store.ingest_listing(
            "tripadvisor", rich, fetched_at="2026-07-20T08:00:00+00:00"
        )
        sparse = {
            "locationId": "ta-101",
            "name": "Tapolca Lake Cave Boat Experience",
            "rating": 4.7,
            "location": {"city": "Tapolca", "country": "Hungary"},
        }
        self.store.ingest_listing(
            "tripadvisor", sparse, fetched_at="2026-07-21T08:00:00+00:00"
        )

        listing = self.store.connection.execute("SELECT * FROM listings").fetchone()
        snapshots = self.store.connection.execute(
            "SELECT kind FROM listing_snapshots ORDER BY scraped_at"
        ).fetchall()
        self.assertEqual("attraction", listing["kind"])
        self.assertEqual(["attraction", "unknown"], [row["kind"] for row in snapshots])

    def test_sparse_rediscovery_preserves_rich_children_and_origin_evidence(self) -> None:
        detail = {
            "activityId": "rich-99",
            "title": "From Budapest: Lake Balaton private adventure",
            "url": "https://www.getyourguide.test/balaton-t99/",
            "startsInBudapest": True,
            "countryCode": "HU",
            "city": "Tihany",
            "categories": ["Adventure"],
            "images": [{"url": "https://images.test/detail.webp"}],
            "options": [{"id": "private", "name": "Private tour", "price": 99}],
            "sampleReviews": [{"id": "r99", "rating": 5, "body": "Memorable."}],
        }
        self.store.ingest_listing(
            "getyourguide",
            detail,
            normalized=normalize_item(GETYOURGUIDE_ACTOR, detail),
        )
        shallow = {
            "activityId": "rich-99",
            "activityTitle": "Lake Balaton private adventure",
            "activityUrl": "https://www.getyourguide.test/balaton-t99/",
            "startsInBudapest": False,
            "rating": 4.8,
            "sourceCityUrl": "https://www.getyourguide.com/lake-balaton-l1565/",
        }
        self.store.ingest_listing(
            "getyourguide",
            shallow,
            normalized=normalize_item(GETYOURGUIDE_ACTOR, shallow),
        )

        listing = self.store.connection.execute("SELECT * FROM listings").fetchone()
        self.assertEqual(1, listing["starts_in_budapest"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM listing_categories"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM media WHERE active = 1"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM packages WHERE active = 1"))
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM reviews"))

    def test_shallow_rediscovery_cannot_erase_rich_geography_but_rich_can_correct_it(self) -> None:
        shallow = {
            "activityId": "832031",
            "activityTitle": "Hidden-city discovery experience",
            "activityUrl": "https://www.getyourguide.test/budapest-t832031/",
            "country": "Hungary",
            "city": "Badacsonytomaj",
        }
        rich_budapest = {
            **shallow,
            "description": "Discover hidden places in and around Budapest with a local guide.",
            "city": "Budapest",
        }
        rich_eger = {
            **shallow,
            "description": "Explore the old town and castle in Eger with a local guide.",
            "city": "Eger",
        }
        later_shallow = {**shallow, "rating": 4.8}

        first = self.store.ingest_listing(
            "getyourguide",
            shallow,
            normalized=normalize_item(GETYOURGUIDE_ACTOR, shallow),
            fetched_at="2026-07-20T08:00:00+00:00",
        )
        second = self.store.ingest_listing(
            "getyourguide",
            rich_budapest,
            normalized=normalize_item(GETYOURGUIDE_ACTOR, rich_budapest),
            fetched_at="2026-07-21T08:00:00+00:00",
        )
        third = self.store.ingest_listing(
            "getyourguide",
            later_shallow,
            normalized=normalize_item(GETYOURGUIDE_ACTOR, later_shallow),
            fetched_at="2026-07-22T08:00:00+00:00",
        )

        listing = self.store.connection.execute(
            "SELECT * FROM listings WHERE external_id = '832031'"
        ).fetchone()
        place = self.store.connection.execute(
            "SELECT * FROM places WHERE id = ?", (listing["place_id"],)
        ).fetchone()
        snapshots = self.store.connection.execute(
            "SELECT raw_payload_id, location_scope FROM listing_snapshots "
            "WHERE listing_id = ? ORDER BY scraped_at",
            (listing["id"],),
        ).fetchall()
        self.assertEqual("budapest", listing["location_scope"])
        self.assertEqual("Budapest", place["locality"])
        self.assertEqual(
            ["outside-budapest", "budapest", "outside-budapest"],
            [row["location_scope"] for row in snapshots],
        )
        self.assertEqual(third["raw_payload_id"], listing["latest_raw_payload_id"])
        self.assertNotEqual(first["raw_payload_id"], second["raw_payload_id"])

        self.store.ingest_listing(
            "getyourguide",
            rich_eger,
            normalized=normalize_item(GETYOURGUIDE_ACTOR, rich_eger),
            fetched_at="2026-07-23T08:00:00+00:00",
        )
        corrected = self.store.connection.execute(
            "SELECT * FROM listings WHERE external_id = '832031'"
        ).fetchone()
        corrected_place = self.store.connection.execute(
            "SELECT * FROM places WHERE id = ?", (corrected["place_id"],)
        ).fetchone()
        self.assertEqual("outside-budapest", corrected["location_scope"])
        self.assertEqual("Eger", corrected_place["locality"])

    def test_price_refresh_updates_one_stable_package_instead_of_duplicating(self) -> None:
        base = {
            "locationId": "package-price-1",
            "name": "Memorable boat option",
            "webUrl": "https://www.tripadvisor.com/Attraction_Review-g1-d1-Reviews-X.html",
            "packages": [
                {
                    "name": "Adult ticket",
                    "price": 10,
                    "currency": "EUR",
                    "provider": "Example Operator",
                    "category": "Admission",
                    "url": "https://book.example.test/adult",
                    "duration": "2 hours",
                }
            ],
        }
        self.store.ingest_listing("tripadvisor", base)
        refreshed = json.loads(json.dumps(base))
        refreshed["packages"][0]["price"] = 14
        self.store.ingest_listing("tripadvisor", refreshed)

        rows = self.store.connection.execute(
            "SELECT price, active FROM packages ORDER BY id"
        ).fetchall()
        self.assertEqual([(row["price"], row["active"]) for row in rows], [(14.0, 1)])

    def test_same_thin_place_in_different_scopes_gets_distinct_place_identity(self) -> None:
        for external_id, scope in (("scope-bp", "budapest"), ("scope-out", "outside-budapest")):
            payload = {
                "external_id": external_id,
                "title": f"Shared place {external_id}",
                "url": f"https://example.test/{external_id}",
            }
            normalized = {
                "source": "getyourguide",
                "external_id": external_id,
                "title": payload["title"],
                "url": payload["url"],
                "location_scope": scope,
                "starts_in_budapest": scope == "outside-budapest",
                "place": {
                    "canonical_name": "Shared thin place",
                    "country_code": "HU",
                    "locality": "Budapest",
                    "location_scope": scope,
                },
                "categories": [],
                "media": [],
                "packages": [],
                "reviews": [],
            }
            self.store.ingest_listing("getyourguide", payload, normalized=normalized)

        rows = self.store.connection.execute(
            """
            SELECT l.location_scope AS listing_scope,
                   p.location_scope AS place_scope, l.place_id
            FROM listings AS l JOIN places AS p ON p.id = l.place_id
            ORDER BY l.external_id
            """
        ).fetchall()
        self.assertEqual(len({row["place_id"] for row in rows}), 2)
        self.assertTrue(
            all(row["listing_scope"] == row["place_scope"] for row in rows)
        )

    def test_detail_without_geography_preserves_structured_discovery_place(self) -> None:
        discovery = {
            "activityId": "geo-77",
            "name": "Eger memorable activity",
            "url": "https://www.getyourguide.com/eger-l1/activity-t77/",
            "location": "Eger",
            "sourceCityUrl": "https://www.getyourguide.com/eger-l1/",
        }
        self.store.ingest_listing(
            "getyourguide",
            discovery,
            normalized=normalize_item(GETYOURGUIDE_ACTOR, discovery),
        )
        before = self.store.connection.execute(
            "SELECT place_id FROM listings WHERE external_id = 'geo-77'"
        ).fetchone()["place_id"]
        detail = {
            "activityId": "geo-77",
            "name": "Eger memorable activity",
            "url": "https://www.getyourguide.com/eger-l1/activity-t77/",
            "description": "A longer researched description without location fields.",
            "sampleReviews": [{"rating": 5, "body": "Worth doing."}],
        }
        self.store.ingest_listing(
            "getyourguide",
            detail,
            normalized=normalize_item(GETYOURGUIDE_ACTOR, detail),
        )

        listing = self.store.connection.execute(
            "SELECT place_id, description FROM listings WHERE external_id = 'geo-77'"
        ).fetchone()
        place = self.store.connection.execute(
            "SELECT country_code, locality FROM places WHERE id = ?", (listing["place_id"],)
        ).fetchone()
        self.assertEqual(before, listing["place_id"])
        self.assertEqual((place["country_code"], place["locality"]), ("HU", "Eger"))
        self.assertIn("longer researched", listing["description"])

    def test_replay_updates_snapshot_fields_for_same_raw_payload(self) -> None:
        payload = {
            "activityId": "snapshot-1",
            "name": "Tihany activity",
            "url": "https://www.getyourguide.com/tihany-l1/activity-t1/",
            "rating": 4.8,
            "ratingCount": 321,
            "location": "Tihany",
        }
        normalized = normalize_item(GETYOURGUIDE_ACTOR, payload)
        stale = dict(normalized)
        stale["review_count"] = None
        first = self.store.ingest_listing(
            "getyourguide", payload, normalized=stale
        )
        second = self.store.ingest_listing(
            "getyourguide", payload, normalized=normalized
        )

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM listing_snapshots"))
        self.assertEqual(321, self.scalar("SELECT review_count FROM listing_snapshots"))

    def test_paid_plan_claim_is_cross_connection_and_rerunnable_after_finish(self) -> None:
        first_id, first_created = self.store.claim_plan_run(
            "tripadvisor",
            "fingerprint-1",
            input_data={"query": "Eger"},
            metadata={"phase": "fanout"},
        )
        with ResearchStore(self.db_path) as peer:
            peer_id, peer_created = peer.claim_plan_run(
                "tripadvisor",
                "fingerprint-1",
                input_data={"query": "Eger"},
                metadata={"phase": "fanout"},
            )
        self.assertTrue(first_created)
        self.assertFalse(peer_created)
        self.assertEqual(first_id, peer_id)

        self.store.attach_actor_run(first_id, "remote-run-1", dataset_id="dataset-1")
        self.store.finish_run(first_id, status="complete")
        second_id, second_created = self.store.claim_plan_run(
            "tripadvisor",
            "fingerprint-1",
            input_data={"query": "Eger"},
            metadata={"phase": "fanout"},
        )
        self.assertTrue(second_created)
        self.assertNotEqual(first_id, second_id)

    def test_v1_database_migrates_in_place_without_losing_paid_run(self) -> None:
        self.store.close()
        self.db_path.unlink()
        v1_schema = SCHEMA_PATH.read_text(encoding="utf-8")
        v1_schema = v1_schema.replace(
            "    plan_fingerprint TEXT,\n"
            "    next_offset INTEGER NOT NULL DEFAULT 0 CHECK (next_offset >= 0),\n",
            "",
        )
        v1_schema = v1_schema.replace(
            "    url TEXT,\n    provider TEXT,\n    category TEXT,\n",
            "",
        )
        v1_schema = v1_schema.replace(
            "    kind TEXT NOT NULL DEFAULT 'unknown' CHECK (length(trim(kind)) > 0),\n",
            "",
        )
        enrichment_start = v1_schema.index("CREATE TABLE IF NOT EXISTS listing_enrichments")
        enrichment_end = v1_schema.index("CREATE TABLE IF NOT EXISTS scrape_run_items")
        v1_schema = v1_schema[:enrichment_start] + v1_schema[enrichment_end:]
        v1_schema = v1_schema.replace(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active_plan\n"
            "    ON scrape_runs(plan_fingerprint)\n"
            "    WHERE status = 'running' AND plan_fingerprint IS NOT NULL;\n",
            "",
        )
        v1_schema = v1_schema.replace(
            "CREATE INDEX IF NOT EXISTS idx_enrichments_kind_time\n"
            "    ON listing_enrichments(enrichment_kind, enriched_at DESC);\n",
            "",
        ).replace(
            "CREATE INDEX IF NOT EXISTS idx_listings_kind_scope_quality\n"
            "    ON listings(kind, location_scope, active, rating DESC, review_count DESC);\n",
            "",
        ).replace(
            "CREATE INDEX IF NOT EXISTS idx_enrichment_attempts_lookup\n"
            "    ON listing_enrichment_attempts(\n"
            "        enrichment_kind, enrichment_version, status, listing_id\n"
            "    );\n",
            "",
        ).replace("PRAGMA user_version = 6;", "PRAGMA user_version = 1;")
        connection = sqlite3.connect(self.db_path)
        connection.executescript(v1_schema)
        connection.execute(
            """
            INSERT INTO scrape_runs(source, actor_run_id, status, started_at)
            VALUES ('tripadvisor', 'paid-v1-run', 'complete', '2026-07-23T00:00:00Z')
            """
        )
        connection.commit()
        connection.close()

        self.store = ResearchStore(self.db_path)

        self.assertEqual(6, self.scalar("PRAGMA user_version"))
        self.assertEqual(
            "paid-v1-run", self.scalar("SELECT actor_run_id FROM scrape_runs")
        )
        run_columns = {
            row["name"]
            for row in self.store.connection.execute("PRAGMA table_info(scrape_runs)")
        }
        self.assertTrue({"plan_fingerprint", "next_offset"} <= run_columns)
        package_columns = {
            row["name"]
            for row in self.store.connection.execute("PRAGMA table_info(packages)")
        }
        self.assertTrue({"url", "provider", "category"} <= package_columns)
        listing_columns = {
            row["name"]
            for row in self.store.connection.execute("PRAGMA table_info(listings)")
        }
        snapshot_columns = {
            row["name"]
            for row in self.store.connection.execute(
                "PRAGMA table_info(listing_snapshots)"
            )
        }
        self.assertIn("kind", listing_columns)
        self.assertIn("kind", snapshot_columns)
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'listing_enrichments'"
            ),
        )
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'listing_enrichment_attempts'"
            ),
        )
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_listings_kind_scope_quality'"
            ),
        )

    def test_v4_database_migrates_kind_in_place_without_losing_rows(self) -> None:
        self.store.close()
        self.db_path.unlink()
        v4_schema = SCHEMA_PATH.read_text(encoding="utf-8").replace(
            "    kind TEXT NOT NULL DEFAULT 'unknown' CHECK (length(trim(kind)) > 0),\n",
            "",
        ).replace(
            "CREATE INDEX IF NOT EXISTS idx_listings_kind_scope_quality\n"
            "    ON listings(kind, location_scope, active, rating DESC, review_count DESC);\n",
            "",
        ).replace("PRAGMA user_version = 6;", "PRAGMA user_version = 4;")
        connection = sqlite3.connect(self.db_path)
        connection.executescript(v4_schema)
        connection.execute(
            "INSERT INTO raw_payloads(id, source, sha256, canonical_json, created_at) "
            "VALUES (1, 'tripadvisor', ?, '{}', '2026-07-20T00:00:00Z')",
            ("a" * 64,),
        )
        connection.execute(
            """
            INSERT INTO places(
                id, place_key, canonical_name, normalized_name, location_scope,
                created_at, updated_at
            ) VALUES (
                1, 'legacy-place', 'Legacy place', 'legacy-place',
                'outside-budapest', '2026-07-20T00:00:00Z',
                '2026-07-20T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO listings(
                id, place_id, source, external_id, title, location_scope,
                latest_raw_payload_id, first_seen_at, last_seen_at
            ) VALUES (
                1, 1, 'tripadvisor', 'legacy-1', 'Legacy listing',
                'outside-budapest', 1, '2026-07-20T00:00:00Z',
                '2026-07-20T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO listing_snapshots(
                id, listing_id, raw_payload_id, scraped_at, title,
                location_scope, starts_in_budapest
            ) VALUES (
                1, 1, 1, '2026-07-20T00:00:00Z', 'Legacy listing',
                'outside-budapest', 0
            )
            """
        )
        connection.commit()
        connection.close()

        self.store = ResearchStore(self.db_path)

        self.assertEqual(6, self.scalar("PRAGMA user_version"))
        self.assertEqual("unknown", self.scalar("SELECT kind FROM listings WHERE id = 1"))
        self.assertEqual(
            "unknown", self.scalar("SELECT kind FROM listing_snapshots WHERE id = 1")
        )
        self.assertEqual("Legacy listing", self.scalar("SELECT title FROM listings WHERE id = 1"))
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_listings_kind_scope_quality'"
            ),
        )

    def test_v5_migration_backfills_first_raw_time_and_later_occurrence_time(self) -> None:
        self.store.close()
        self.db_path.unlink()
        v5_schema = SCHEMA_PATH.read_text(encoding="utf-8").replace(
            "    observed_at TEXT NOT NULL,\n", ""
        ).replace("PRAGMA user_version = 6;", "PRAGMA user_version = 5;")
        connection = sqlite3.connect(self.db_path)
        connection.executescript(v5_schema)
        connection.execute(
            """
            INSERT INTO raw_payloads(
                id, source, sha256, canonical_json, fetched_at, created_at
            ) VALUES (
                1, 'getyourguide', ?, '{}',
                '2026-07-20T07:55:00+00:00', '2026-07-20T08:00:00+00:00'
            )
            """,
            ("b" * 64,),
        )
        for run_id, created_at in (
            (1, "2026-07-20T08:00:00+00:00"),
            (2, "2026-07-22T11:30:00+00:00"),
        ):
            connection.execute(
                """
                INSERT INTO scrape_runs(id, source, status, started_at)
                VALUES (?, 'getyourguide', 'complete', ?)
                """,
                (run_id, created_at),
            )
            connection.execute(
                """
                INSERT INTO scrape_run_items(
                    run_id, item_index, status, raw_payload_id, created_at
                ) VALUES (?, 0, 'skipped', 1, ?)
                """,
                (run_id, created_at),
            )
        connection.commit()
        connection.close()

        self.store = ResearchStore(self.db_path)

        rows = self.store.connection.execute(
            "SELECT observed_at FROM scrape_run_items ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [
                "2026-07-20T07:55:00+00:00",
                "2026-07-22T11:30:00+00:00",
            ],
            [row["observed_at"] for row in rows],
        )
        column = next(
            row
            for row in self.store.connection.execute(
                "PRAGMA table_info(scrape_run_items)"
            )
            if row["name"] == "observed_at"
        )
        self.assertEqual(1, column["notnull"])
        self.assertEqual(6, self.scalar("PRAGMA user_version"))

    def test_failed_or_unparsed_run_items_keep_raw_payload_without_partial_listing(self) -> None:
        run_id = self.store.begin_run("tripadvisor")
        bad_payload = tapolca_payload()
        bad_payload["locationId"] = "invalid-run-item"
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest_item(
                run_id,
                bad_payload,
                normalized={"rating": 9.0},
                query_label="invalid test",
                result_rank=7,
            )
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM raw_payloads"))
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM listings"))
        failed = self.store.connection.execute("SELECT * FROM scrape_run_items").fetchone()
        self.assertEqual("failed", failed["status"])
        self.assertIsNotNone(failed["raw_payload_id"])
        self.assertIsNone(failed["listing_id"])
        self.assertIn("IntegrityError", failed["error"])

        sentinel = {"statusCode": 403, "message": "Access blocked", "captcha": True}
        retained = self.store.record_unparsed_item(
            run_id,
            sentinel,
            status="skipped",
            error="blocked sentinel",
            query_label="country-hungary",
            result_rank=8,
        )
        self.assertEqual(2, self.scalar("SELECT COUNT(*) FROM raw_payloads"))
        skipped = self.store.connection.execute(
            "SELECT * FROM scrape_run_items WHERE item_index = ?", (retained["item_index"],)
        ).fetchone()
        self.assertEqual("skipped", skipped["status"])
        self.assertEqual("blocked sentinel", skipped["error"])

    def test_failed_normalization_rolls_back_entire_item(self) -> None:
        before = self.store.stats()
        bad_payload = tapolca_payload()
        bad_payload["locationId"] = "invalid-rating"
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest_listing(
                "tripadvisor",
                bad_payload,
                normalized={"rating": 9.0},
            )
        self.assertEqual(before, self.store.stats())


if __name__ == "__main__":
    unittest.main(verbosity=2)
