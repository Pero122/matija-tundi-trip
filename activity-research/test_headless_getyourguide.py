from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from headless_getyourguide import (
    ENRICHMENT_KIND,
    ENRICHMENT_VERSION,
    _validated_candidate_identity,
    _valid_evidence,
    import_evidence,
    normalized_from_evidence,
    parse_evidence,
    scrape_missing,
    select_missing,
)
from store import ResearchStore


VISIBLE_TEXT = """Skip to main content
Certified by GetYourGuide
From Budapest: Danube Bend Tour
4.7
123 reviews
Free cancellation
Cancel up to 24 hours in advance for a full refund
Reserve now & pay later
Duration 9.5 hours
Highlights
Ride a boat and see the Danube Bend
Visit three distinctive towns
Full description
This is a relaxed full-day trip with history, scenery, and a seasonal boat return.
Includes
Guide
From
€109
per person
Customer reviews
Overall rating
4.7/5
Based on 123 reviews
Sort by:
Recommended
Filter
5
5 out of 5 stars
A
Alice – Canada
Couple
Jul 3, 2026
Verified booking
The guide was excellent and the boat ride made the day memorable.
Response from Provider
July 4, 2026
Thank you Alice.
Helpful?
4
4 out of 5 stars
B
Bob – Ireland
Friend group
Jun 2, 2026
Verified booking
Beautiful scenery and enough free time in each town.
Helpful?
Product ID: 15256
"""


class HeadlessGetYourGuideTests(unittest.TestCase):
    def _listing(self):
        return {
            "external_id": "15256",
            "title": "From Budapest: Danube Bend Tour",
            "url": "https://www.getyourguide.com/budapest-l29/example-t15256/",
            "rating": 4.7,
            "review_count": 123,
        }

    def _evidence(self):
        url = self._listing()["url"]
        rendered_html = (
            '<html><head><link rel="canonical" href="'
            + url
            + '"></head><body><script>{"route":{"name":"Activity",'
            + '"path":"/budapest-l29/example-t15256/"}}</script>'
            + ("x" * 1_100)
            + "</body></html>"
        )
        return {
            "transport": "camoufox-rendered-page",
            "source": "getyourguide",
            "sourceUrl": url,
            "pageUrl": url,
            "externalId": "15256",
            "listingTitle": self._listing()["title"],
            "browserTitle": "Danube Bend Tour",
            "checkedAt": "2026-07-23T00:00:00+00:00",
            "visibleText": VISIBLE_TEXT,
            "renderedHtml": rendered_html,
        }

    def _seed(self, db_path: Path):
        payload = {
            "activityId": "15256",
            "activityUrl": self._listing()["url"],
            "title": self._listing()["title"],
            "rating": 4.7,
            "ratingCount": 123,
            "country": "Hungary",
            "city": "Szentendre",
            "location_scope": "outside-budapest",
        }
        with ResearchStore(db_path) as store:
            run_id = store.begin_run("getyourguide", actor_run_id="seed")
            stored = store.ingest_item(
                run_id,
                payload,
                source="getyourguide",
                item_index=0,
            )
            store.finish_run(run_id, stats={"items": 1})
            return stored["listing_id"], stored["raw_payload_id"]

    def test_parser_extracts_description_price_and_identity_free_reviews(self):
        normalized = parse_evidence(self._evidence(), self._listing())
        self.assertIn("relaxed full-day trip", normalized["description"])
        self.assertIn("Ride a boat", normalized["description"])
        self.assertEqual(normalized["price_from"], 109.0)
        self.assertEqual(normalized["currency"], "EUR")
        self.assertEqual(normalized["duration_text"], "9.5 hours")
        self.assertEqual(len(normalized["reviews"]), 2)
        self.assertNotIn("Alice", json.dumps(normalized["reviews"]))
        self.assertNotIn("Bob", json.dumps(normalized["reviews"]))
        self.assertIn("boat ride", normalized["reviews"][0]["body"])

    def test_review_parser_drops_control_only_review_and_product_boundary(self):
        evidence = self._evidence()
        evidence["visibleText"] = VISIBLE_TEXT.replace(
            "The guide was excellent and the boat ride made the day memorable.",
            "See more reviews",
        ).replace("Product ID: 15256", "Product ID 15256")

        normalized = parse_evidence(evidence, self._listing())

        self.assertEqual(1, len(normalized["reviews"]))
        self.assertIn("Beautiful scenery", normalized["reviews"][0]["body"])
        self.assertNotIn("See more reviews", json.dumps(normalized["reviews"]))
        self.assertNotIn("Product ID", json.dumps(normalized["reviews"]))

    def test_rendered_identity_rejects_swapped_product_wrapper(self):
        evidence = self._evidence()
        evidence["visibleText"] = evidence["visibleText"].replace(
            "Product ID: 15256", "Product ID: 99999"
        )
        evidence["renderedHtml"] = evidence["renderedHtml"].replace(
            "t15256/", "t99999/"
        )

        with self.assertRaisesRegex(ValueError, "Product ID"):
            normalized_from_evidence(evidence, self._listing())

    def test_rendered_identity_rejects_canonical_product_mismatch(self):
        evidence = self._evidence()
        evidence["renderedHtml"] = evidence["renderedHtml"].replace(
            self._listing()["url"],
            "https://www.getyourguide.com/budapest-l29/wrong-t99999/",
        )

        with self.assertRaisesRegex(ValueError, "canonical"):
            normalized_from_evidence(evidence, self._listing())

    def test_rendered_identity_rejects_activity_route_product_mismatch(self):
        evidence = self._evidence()
        evidence["renderedHtml"] = evidence["renderedHtml"].replace(
            '"path":"/budapest-l29/example-t15256/"',
            '"path":"/budapest-l29/example-t99999/"',
        )

        with self.assertRaisesRegex(ValueError, "route"):
            normalized_from_evidence(evidence, self._listing())

    def test_valid_evidence_requires_bound_url_and_full_render(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps(self._evidence()), encoding="utf-8")
            self.assertTrue(_valid_evidence(path, self._listing()["url"]))
            self.assertFalse(_valid_evidence(path, "https://example.com/wrong"))

    def test_candidate_identity_rejects_untrusted_urls_mismatches_and_traversal(self):
        valid = {
            "external_id": "15256",
            "url": self._listing()["url"],
        }
        self.assertEqual(
            _validated_candidate_identity(valid),
            (self._listing()["url"], "15256"),
        )
        invalid_rows = (
            {**valid, "url": "https://getyourguide.com.evil.test/activity-t15256/"},
            {**valid, "url": "http://www.getyourguide.com/activity-t15256/"},
            {**valid, "external_id": "999"},
            {**valid, "external_id": "../15256"},
        )
        for row in invalid_rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                _validated_candidate_identity(row)

    def test_invalid_candidate_is_rejected_before_browser_or_evidence_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "research.sqlite3"
            evidence_dir = root / "evidence"
            with ResearchStore(db_path) as store:
                run_id = store.begin_run("getyourguide", actor_run_id="unsafe-seed")
                store.ingest_item(
                    run_id,
                    {
                        "external_id": "15256",
                        "title": "Unsafe\x1b]52;c;clipboard\x07 candidate\x9b31m",
                        "url": "https://attacker.example/activity-t15256/",
                        "location_scope": "outside-budapest",
                    },
                    source="getyourguide",
                    item_index=0,
                )
                store.finish_run(run_id)

            stderr = StringIO()
            with redirect_stderr(stderr):
                totals = scrape_missing(db_path, evidence_dir)

            self.assertEqual(totals, {
                "selected": 1,
                "cached": 0,
                "fetched": 0,
                "failed": 1,
            })
            self.assertFalse(evidence_dir.exists())
            self.assertNotIn("\x1b", stderr.getvalue())
            self.assertNotIn("\x07", stderr.getvalue())
            self.assertNotIn("\x9b", stderr.getvalue())

    def test_cross_host_redirect_is_rejected_before_page_content_is_read(self):
        calls = {"goto": 0, "body": 0}

        class FakeLocator:
            def inner_text(self, **_kwargs):
                calls["body"] += 1
                return VISIBLE_TEXT

        class FakePage:
            url = "about:blank"

            def on(self, *_args):
                return None

            def goto(self, *_args, **_kwargs):
                calls["goto"] += 1
                self.url = "https://attacker.example/redirect-t15256/"

            def wait_for_timeout(self, *_args):
                raise AssertionError("redirect must be rejected before waiting")

            def locator(self, *_args):
                calls["body"] += 1
                return FakeLocator()

            def close(self):
                return None

        class FakeBrowser:
            def new_page(self):
                return FakePage()

        class FakeCamoufox:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return FakeBrowser()

            def __exit__(self, *_args):
                return None

        sync_api = types.ModuleType("camoufox.sync_api")
        sync_api.Camoufox = FakeCamoufox
        camoufox = types.ModuleType("camoufox")
        camoufox.sync_api = sync_api
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "research.sqlite3"
            evidence_dir = root / "evidence"
            self._seed(db_path)
            with patch.dict(
                sys.modules,
                {"camoufox": camoufox, "camoufox.sync_api": sync_api},
            ), patch("headless_getyourguide.time.sleep", return_value=None):
                totals = scrape_missing(db_path, evidence_dir, wait_seconds=0)

            self.assertEqual(totals["fetched"], 0)
            self.assertEqual(totals["failed"], 1)
            self.assertEqual(calls, {"goto": 3, "body": 0})
            self.assertFalse(evidence_dir.exists())

    def test_import_stores_raw_evidence_and_satisfies_missing_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "research.sqlite3"
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            listing_id, _raw_id = self._seed(db_path)
            self.assertEqual(len(select_missing(db_path)), 1)
            evidence_path = evidence_dir / "getyourguide_15256.render.json"
            evidence_path.write_text(json.dumps(self._evidence()), encoding="utf-8")

            totals = import_evidence(db_path, evidence_dir)
            self.assertEqual(totals["stored"], 1)
            self.assertEqual(totals["reviews"], 2)
            self.assertEqual(totals["packages"], 1)
            self.assertEqual(select_missing(db_path), [])

            with ResearchStore(db_path) as store:
                enrichment = store.connection.execute(
                    """
                    SELECT enrichment_kind, enrichment_version
                    FROM listing_enrichments WHERE listing_id = ?
                    """,
                    (listing_id,),
                ).fetchone()
                self.assertEqual(enrichment["enrichment_kind"], ENRICHMENT_KIND)
                self.assertEqual(enrichment["enrichment_version"], ENRICHMENT_VERSION)
                raw = store.connection.execute(
                    """
                    SELECT canonical_json FROM raw_payloads
                    WHERE source = 'getyourguide'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()[0]
                self.assertIn("renderedHtml", raw)
                reviewer_blob = " ".join(
                    row[0]
                    for row in store.connection.execute(
                        "SELECT body FROM reviews WHERE listing_id = ?", (listing_id,)
                    )
                )
                self.assertNotIn("Alice", reviewer_blob)


if __name__ == "__main__":
    unittest.main()
