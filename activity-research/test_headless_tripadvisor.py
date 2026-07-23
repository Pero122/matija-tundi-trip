from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from headless_tripadvisor import (  # noqa: E402
    GRAPHQL_ENRICHMENT_VERSION,
    export_candidates,
    import_context,
)
from normalizers import TRIPADVISOR_ACTOR, normalize_item  # noqa: E402
from store import ResearchStore, canonical_json  # noqa: E402


class HeadlessTripadvisorBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "research.sqlite3"
        self.raw_dir = self.root / "raw"
        self.raw_dir.mkdir()
        self.url = (
            "https://www.tripadvisor.com/Attraction_Review-g274891-d555-"
            "Reviews-Memorable_Tihany-Tihany_Veszprem_County.html"
        )
        payload = {
            "locationId": "555",
            "name": "Memorable Tihany experience",
            "webUrl": self.url,
            "rating": 4.8,
            "numberOfReviews": 500,
            "addressObj": {"city": "Tihany", "country": "Hungary"},
            "photos": [{"url": "https://images.example.test/tihany.jpg"}],
            "category": {"name": "Tours"},
        }
        with ResearchStore(self.db) as store:
            store.ingest_listing(
                "tripadvisor",
                payload,
                normalized=normalize_item(TRIPADVISOR_ACTOR, payload),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_export_builds_camoufox_candidate_shape_from_ranked_database(self) -> None:
        output = self.root / "candidates_hungary.json"

        rows = export_candidates(self.db, output, limit=10)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "Memorable Tihany experience")
        self.assertEqual(row["city"], "hungary")
        self.assertEqual(row["geo"], "274891")
        self.assertEqual(row["reviews"], 500)
        self.assertEqual(row["photos"], ["https://images.example.test/tihany.jpg"])
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), rows)

    def test_import_preserves_exact_graphql_raw_and_normalizes_reviews_prices(self) -> None:
        evidence = {
            "schemaVersion": 1,
            "transport": "tripadvisor-browser-graphql",
            "sourceUrl": self.url,
            "queries": {"reviews": [{"evidenceResponse": {"data": "exact"}}]},
        }
        evidence_path = self.raw_dir / "attraction_review_555.graphql.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        context = [
            {
                "key": "Attraction_Review:555",
                "id": "555",
                "route": "Attraction_Review",
                "city": "hungary",
                "name": "Memorable Tihany experience",
                "url": self.url,
                "category": "Tours",
                "subtype": "Boat Tours",
                "rating": 4.8,
                "review_count": 500,
                "description": "A detailed, locally cached description.",
                "reviews": [
                    {"title": "Unique", "text": "A genuinely rare outing.", "rating": 5},
                    {"title": "Relaxed", "text": "Beautiful and easy paced.", "rating": 4},
                ],
                "pricing_evidence": {
                    "base_price": "USD 25.50",
                    "packages": [
                        {
                            "name": "Boat option",
                            "description": "Timed departure",
                            "available_times": "10:00 AM, 2:00 PM",
                            "total_price": "USD 60.00",
                            "party": "2 adults",
                            "unit_price": "USD 30.00",
                            "availability": "available",
                        }
                    ],
                },
            }
        ]
        context_path = self.root / "detail_context_hungary.json"
        context_path.write_text(json.dumps(context), encoding="utf-8")

        parsed = {
            "page_title": "Memorable Tihany experience",
            "description": context[0]["description"],
            "reviews": context[0]["reviews"],
            "pricing_evidence": context[0]["pricing_evidence"],
        }
        with patch(
            "headless_tripadvisor.validate_graphql_evidence",
            return_value={"route": "Attraction_Review", "detail_id": "555"},
        ), patch("headless_tripadvisor.parse_graphql_evidence", return_value=parsed):
            totals = import_context(self.db, context_path, raw_dir=self.raw_dir)

        self.assertEqual(totals["stored"], 1)
        self.assertEqual(totals["reviews"], 2)
        self.assertEqual(totals["packages"], 1)
        with ResearchStore(self.db) as store:
            listing = store.connection.execute(
                """
                SELECT l.price_from, l.currency, l.description, p.locality
                FROM listings AS l JOIN places AS p ON p.id = l.place_id
                WHERE l.external_id = '555'
                """
            ).fetchone()
            package = store.connection.execute("SELECT * FROM packages").fetchone()
            enrichment = store.connection.execute(
                """
                SELECT e.enrichment_version, raw.canonical_json
                FROM listing_enrichments AS e
                JOIN raw_payloads AS raw ON raw.id = e.raw_payload_id
                WHERE e.enrichment_kind = 'tripadvisor-headless-detail'
                """
            ).fetchone()
            self.assertEqual((listing["price_from"], listing["currency"]), (25.5, "USD"))
            self.assertEqual(listing["locality"], "Tihany")
            self.assertIn("locally cached", listing["description"])
            self.assertEqual(package["price"], 60.0)
            self.assertEqual(package["provider"], "Tripadvisor")
            self.assertIn("10:00 AM", package["availability_text"])
            self.assertEqual(store.stats()["reviews"], 2)
            self.assertEqual(enrichment["enrichment_version"], GRAPHQL_ENRICHMENT_VERSION)
            self.assertEqual(enrichment["canonical_json"], canonical_json(evidence))

    def test_import_rejects_structurally_invalid_graphql_evidence(self) -> None:
        evidence_path = self.raw_dir / "attraction_review_555.graphql.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "transport": "tripadvisor-browser-graphql",
                    "sourceUrl": self.url,
                    "queries": {},
                }
            ),
            encoding="utf-8",
        )
        context_path = self.root / "detail_context_hungary.json"
        context_path.write_text(
            json.dumps(
                [
                    {
                        "key": "Attraction_Review:555",
                        "id": "555",
                        "url": self.url,
                        "name": "Memorable Tihany experience",
                    }
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            import_context(self.db, context_path, raw_dir=self.raw_dir)


if __name__ == "__main__":
    unittest.main()
