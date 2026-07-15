import os
import unittest
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from scrape_ta import (
    PaginationPolicy,
    adjust_category_decision,
    cache_is_fresh,
    has_next_page,
    listing_identity,
    merge_failed_categories,
    parse,
)
from build_review import (
    classify,
    historical_aliases,
    listing_title,
    merge_city_data,
    replace_data_block,
    script_safe_json,
    unresolved_legacy_aliases,
)
from dedup import is_dup, norm


def row(rating=4.0, reviews=10):
    return {"rating": rating, "reviews": reviews}


class PaginationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = PaginationPolicy(min_results=90, max_pages=10)

    def test_inspects_top_90_unique_results_before_quality_stop(self):
        decision = self.policy.decide(
            [row()] * 30,
            page_number=2,
            new_count=30,
            total_unique=60,
            has_next=True,
        )
        self.assertTrue(decision.keep_going)

    def test_partial_pages_do_not_fake_the_top_90_baseline(self):
        decision = self.policy.decide(
            [row()] * 5,
            page_number=3,
            new_count=5,
            total_unique=15,
            has_next=True,
        )
        self.assertTrue(decision.keep_going)
        self.assertIn("15 so far", decision.reason)

    def test_stops_when_tail_is_weak_after_top_90(self):
        decision = self.policy.decide(
            [row()] * 30,
            page_number=3,
            new_count=30,
            total_unique=90,
            has_next=True,
        )
        self.assertFalse(decision.keep_going)
        self.assertEqual(decision.keeper_count, 0)

    def test_continues_when_tail_has_two_strong_candidates(self):
        rows = [row()] * 28 + [row(4.8, 700), row(4.7, 250)]
        decision = self.policy.decide(
            rows,
            page_number=3,
            new_count=30,
            total_unique=90,
            has_next=True,
        )
        self.assertTrue(decision.keep_going)
        self.assertEqual(decision.keeper_count, 2)

    def test_navigation_end_stops_even_before_three_pages(self):
        decision = self.policy.decide(
            [row(4.9, 500)] * 8,
            page_number=1,
            new_count=8,
            total_unique=8,
            has_next=False,
        )
        self.assertFalse(decision.keep_going)

    def test_repeated_page_stops(self):
        decision = self.policy.decide(
            [row(4.9, 500)] * 30,
            page_number=3,
            new_count=0,
            total_unique=90,
            has_next=True,
        )
        self.assertFalse(decision.keep_going)

    def test_closed_listing_never_keeps_paging_alive(self):
        closed = {"rating": 5.0, "reviews": 5000, "closed": True}
        decision = self.policy.decide(
            [row()] * 28 + [closed, closed],
            page_number=3,
            new_count=30,
            total_unique=90,
            has_next=True,
        )
        self.assertFalse(decision.keep_going)

    def test_tours_requires_two_consecutive_weak_tails(self):
        weak = self.policy.decide(
            [row()] * 30,
            page_number=3,
            new_count=30,
            total_unique=90,
            has_next=True,
        )
        first, streak = adjust_category_decision(
            self.policy, "c42", weak, 0, 90, True, 30
        )
        second, streak = adjust_category_decision(
            self.policy, "c42", weak, streak, 120, True, 30
        )
        self.assertTrue(first.keep_going)
        self.assertEqual(streak, 2)
        self.assertFalse(second.keep_going)


class ParserTests(unittest.TestCase):
    def test_product_card_keeps_product_url_not_operator_url(self):
        html = (
            '"View details for Buda Castle Vampire Tour"},'
            '"rating":4.7,"reviewCount":661,'
            '"cardLink":{"webLinkUrl":"/AttractionProductReview-g274887-d17162384-'
            'Buda_Castle_Vampire_Tour-Budapest.html"},'
            '"cardPhoto":{"urlTemplate":"https://example.com/tour.jpg?w={width}&h={height}"},'
            '"descriptiveText":{"text":"Dark history and folklore."},'
            '"operatorName":{"webLinkUrl":"/Attraction_Review-g274887-d8442537-Reviews-'
            'Mysterium_Tours-Budapest.html"}'
            '"View details for sentinel"}'
        )
        listings = parse(html, "budapest", "c42")
        self.assertEqual(len(listings), 1)
        self.assertIn("AttractionProductReview", listings[0]["url"])
        self.assertIn("d17162384", listings[0]["url"])
        self.assertEqual(listings[0]["rating"], 4.7)
        self.assertEqual(listings[0]["reviews"], 661)
        self.assertEqual(listings[0]["origin"], "budapest")

    def test_daily_closed_status_is_not_permanent_closure(self):
        def parsed(status):
            html = (
                '"View details for Night Venue"},'
                '"rating":4.8,"reviewCount":500,'
                '"webLinkUrl":"/Attraction_Review-g274887-d123-Night-Venue.html",'
                f'"openStatus":{{"text":"{status}"}}'
                '"View details for sentinel"}'
            )
            return parse(html, "budapest", "c20")[0]["closed"]

        self.assertFalse(parsed("Open now"))
        self.assertFalse(parsed("Closed now"))
        self.assertTrue(parsed("Permanently closed"))
        self.assertTrue(parsed("Temporarily closed"))

    def test_next_page_marker(self):
        self.assertTrue(has_next_page('<a aria-label="Next page">next</a>'))
        self.assertFalse(has_next_page("last page"))

    def test_product_url_is_always_an_experience(self):
        listing = {
            "url": "https://www.tripadvisor.com/AttractionProductReview-g274887-d1-Example.html",
            "name": "Unexpected Budapest",
            "subtype": "",
        }
        self.assertEqual(classify(listing), "experience")


class CacheTests(unittest.TestCase):
    def test_cache_expires_unless_it_is_recent(self):
        with TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text("View details for" + "x" * 50_000)
            modified = page.stat().st_mtime
            self.assertTrue(cache_is_fresh(page, 24, now=modified + 23 * 3600))
            self.assertFalse(cache_is_fresh(page, 24, now=modified + 25 * 3600))


class FailedCategoryTests(unittest.TestCase):
    def test_failed_category_membership_merges_into_refreshed_identity(self):
        url = "https://www.tripadvisor.com/Attraction_Review-g274887-d42-Example.html"
        prior = {
            "url": url,
            "cat": "c42",
            "catLabel": "Tours",
            "alsoCats": ["Nightlife"],
        }
        current = {
            "url": url,
            "cat": "c20",
            "catLabel": "Nightlife",
            "alsoCats": [],
        }
        by_id = {listing_identity(current): current}
        rows = [current]
        preserved = merge_failed_categories([prior], by_id, rows, {"c42"})
        self.assertEqual(preserved, 0)
        self.assertIn("Tours", current["alsoCats"])


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.market = [("Great Market Hall", norm("Great Market Hall"), False, "venue")]

    def test_product_is_not_hidden_by_venue_containment(self):
        title = norm("Budapest Great Market Hall Chef-Led Private Tasting Tour")
        self.assertIsNone(is_dup(title, "experience", self.market))

    def test_exact_alias_still_deduplicates(self):
        self.assertEqual(
            is_dup(norm("Great Market Hall"), "experience", self.market),
            "Great Market Hall",
        )

    def test_same_kind_containment_still_deduplicates(self):
        self.assertEqual(
            is_dup(norm("Budapest Great Market Hall"), "venue", self.market),
            "Great Market Hall",
        )


class BuilderTests(unittest.TestCase):
    def test_reviewed_title_repair_uses_stable_tripadvisor_identity(self):
        listing = {
            "name": "The",
            "url": (
                "https://www.tripadvisor.com/AttractionProductReview-g274887-"
                "d21032018-The_Puszta_Horse_Show-Budapest.html"
            ),
        }
        self.assertEqual(listing_title(listing), "The Puszta Horse Show")

    def test_reviewed_title_repair_does_not_match_title_alone(self):
        listing = {
            "name": "The",
            "url": (
                "https://www.tripadvisor.com/AttractionProductReview-g274887-"
                "d999-The-Budapest.html"
            ),
        }
        self.assertEqual(listing_title(listing), "The")

    def test_budapest_replacement_preserves_london_archive(self):
        old = [
            {"city": "budapest", "n": "old"},
            {"city": "london", "n": "sentinel"},
        ]
        merged = merge_city_data(old, [{"city": "budapest", "n": "new"}], ["budapest"])
        self.assertEqual(
            {(item["city"], item["n"]) for item in merged},
            {("budapest", "new"), ("london", "sentinel")},
        )

    def test_builder_replaces_only_data_block(self):
        html = "before\nconst DATA=[];\nconst SB_URL=\"x\";\nafter"
        updated = replace_data_block(html, [{"n": "new"}])
        self.assertTrue(updated.startswith("before\nconst DATA="))
        self.assertTrue(updated.endswith('const SB_URL="x";\nafter'))

    def test_inline_json_is_script_safe(self):
        payload = script_safe_json([{"n": "</script><img onerror=x>"}])
        self.assertNotIn("</script>", payload)
        self.assertNotIn("<img", payload)
        self.assertIn("\\u003c", payload)

    def test_legacy_operator_key_follows_corrected_product_with_same_name(self):
        old = [{
            "city": "budapest",
            "n": "Example Tour",
            "url": "https://www.tripadvisor.com/Attraction_Review-g274887-d1-Example.html",
        }]
        current = [{
            "city": "budapest",
            "n": "Example Tour",
            "url": "https://www.tripadvisor.com/AttractionProductReview-g274887-d2-Example.html",
        }]
        aliases = historical_aliases([old], current)
        self.assertIn(
            "budapest|Example Tour",
            aliases["AttractionProductReview:2"],
        )

    def test_same_name_beats_retained_operator_identity_for_legacy_key(self):
        old = [{
            "city": "budapest",
            "n": "Example Tour",
            "url": "https://www.tripadvisor.com/Attraction_Review-g274887-d1-Example.html",
        }]
        current = [
            {
                "city": "budapest",
                "n": "Example Tour",
                "url": "https://www.tripadvisor.com/AttractionProductReview-g274887-d2-Example.html",
            },
            {
                "city": "budapest",
                "n": "Example Operator",
                "url": "https://www.tripadvisor.com/Attraction_Review-g274887-d1-Operator.html",
            },
        ]
        aliases = historical_aliases([old], current)
        self.assertIn(
            "budapest|Example Tour",
            aliases["AttractionProductReview:2"],
        )
        self.assertNotIn(
            "budapest|Example Tour",
            aliases["Attraction_Review:1"],
        )

    def test_duplicate_titles_pair_old_and_new_id_order_without_collisions(self):
        old = [
            {
                "city": "budapest",
                "n": "Same Tour",
                "url": "https://www.tripadvisor.com/Attraction_Review-g274887-d10-A.html",
            },
            {
                "city": "budapest",
                "n": "Same Tour",
                "url": "https://www.tripadvisor.com/Attraction_Review-g274887-d20-B.html",
            },
        ]
        current = [
            {
                "city": "budapest",
                "n": "Same Tour",
                "url": "https://www.tripadvisor.com/AttractionProductReview-g274887-d100-A.html",
            },
            {
                "city": "budapest",
                "n": "Same Tour",
                "url": "https://www.tripadvisor.com/AttractionProductReview-g274887-d200-B.html",
            },
        ]
        aliases = historical_aliases([old], current)
        self.assertIn("budapest|Same Tour", aliases["AttractionProductReview:100"])
        self.assertIn(
            "budapest|Same Tour|ta:20",
            aliases["AttractionProductReview:200"],
        )
        self.assertEqual(unresolved_legacy_aliases([old], current, aliases), [])


if __name__ == "__main__":
    unittest.main()
