import base64
import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from scrape_ta_details import (  # noqa: E402
    AdaptiveBlockController,
    DataDomeBlocked,
    GraphQLEvidenceError as CacheGraphQLEvidenceError,
    MIN_HTML_BYTES,
    PersistentCamoufoxRunner,
    PersistentCamoufoxRunnerPool,
    SingleInstanceError,
    build_context,
    block_cooldown_seconds,
    cache_looks_valid,
    clean_text,
    detail_cache_path,
    detail_graphql_cache_path,
    evidence_checked_at,
    fetch_detail,
    graphql_cache_looks_valid,
    html_looks_like_datadome_challenge,
    html_looks_valid,
    merge_context_rows,
    normalize_requested_id,
    parse_args,
    parse_detail_html,
    parse_graphql_evidence,
    rendered_html_pricing_fallback,
    rendered_product_review_language,
    rendered_review_filter_count,
    rendered_review_total,
    quarantine_venue_review_evidence,
    review_coverage_target,
    run_fetch_phase,
    select_listings,
    single_instance_lock,
    validate_graphql_evidence,
)
from fetch_ta_detail import (  # noqa: E402
    CANCELLATION_QUERY_ID,
    GraphQLEvidenceError,
    PACKAGES_QUERY_ID,
    PAX_QUERY_ID,
    PRICE_CALENDAR_QUERY_ID,
    PRODUCT_DETAIL_QUERY_ID,
    PRODUCT_REVIEWS_QUERY_ID,
    VENUE_DETAIL_QUERY_ID,
    VENUE_REVIEWS_QUERY_ID,
    _load_all_review_languages,
    _load_product_packages,
    _product_graphql_evidence,
    _reset_venue_review_filters,
    _selected_product_language,
    _select_product_all_languages,
    _travel_date_candidates,
    main as fetch_detail_main,
    render_graphql_evidence,
    request_mode,
    serve_requests,
)


def listing(listing_id, route="Attraction_Review", name=None):
    return {
        "name": name or f"Place {listing_id}",
        "url": (
            f"https://www.tripadvisor.com/{route}-g274887-d{listing_id}-"
            f"Reviews-Place_{listing_id}-Budapest.html"
        ),
        "city": "budapest",
        "rating": 4.8,
        "reviews": 42,
        "catLabel": "Museums",
        "subtype": "Exhibitions",
    }


def review_card(number, rating=5):
    return f"""
      <div data-automation="reviewCard">
        <svg data-automation="bubbleRatingImage"><title>{rating} of 5 bubbles</title></svg>
        <a data-automation="review-title"
           href="/ShowUserReviews-g274887-d123-r{number}-Example.html">
          <span lang="en">Title {number}</span>
        </a>
        <div><span lang="en">Body {number}<br>with useful detail.</span></div>
      </div>
    """


def valid_cached_html(listing_id="123"):
    canonical = listing(listing_id)["url"]
    core = f"""
      <html><head><title>Example - Tripadvisor</title>
      <link rel="canonical" href="{canonical}"></head><body>
      <div data-automation="reviewCard">
        <a data-automation="review-title" href="/ShowUserReviews-d{listing_id}-r1">
          <span lang="en">Useful visit</span>
        </a>
        <div data-automation="reviewText">Concrete review evidence.</div>
      </div>
      </body></html>
    """
    return core + ("x" * (MIN_HTML_BYTES + 100))


class ScriptedGraphQLPage:
    """Strict page-context GraphQL fake; DOM fallbacks are intentionally absent."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.waits = []
        self.closed = False
        self.url = ""

    def on(self, *_args):
        pass

    def goto(self, url, timeout):
        self.url = url
        self.timeout = timeout
        return SimpleNamespace(status=403)

    def evaluate(self, script, payload):
        if not self.steps:
            raise AssertionError("unexpected GraphQL call")
        if "/data/graphql/ids" not in script:
            raise AssertionError("GraphQL call did not use the same-origin endpoint")
        step = self.steps.pop(0)
        actual = [
            (
                item["extensions"]["preRegisteredQueryId"],
                item["variables"],
            )
            for item in payload
        ]
        if actual != step["request"]:
            raise AssertionError(
                f"GraphQL request mismatch:\nexpected={step['request']!r}\nactual={actual!r}"
            )
        self.calls.append(copy_json(payload))
        response = step["response"]
        if isinstance(response, Exception):
            raise response
        return {
            "ok": step.get("ok", True),
            "status": step.get("status", 200),
            "contentType": "application/json",
            "body": copy_json(response),
            "responseBytes": len(json.dumps(response)),
        }

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def close(self):
        self.closed = True

    def assert_drained(self):
        if self.steps:
            raise AssertionError(f"{len(self.steps)} scripted GraphQL calls were unused")


class ScriptedGraphQLBrowser:
    def __init__(self, steps):
        self.page = ScriptedGraphQLPage(steps)

    def new_page(self):
        return self.page


def copy_json(value):
    return json.loads(json.dumps(value))


def graphql_reviews(detail_id, languages=("hu", "de")):
    reviews = [
        {
            "id": str(index + 1),
            "locationId": detail_id,
            "originalLanguage": language,
            "language": "en",
            "title": f"Translated title {index + 1}",
            "text": f"Translated review {index + 1}",
            "name": f"Private reviewer {index + 1}",
            "username": f"private-reviewer-{index + 1}",
            "avatar": "https://example.invalid/avatar.jpg",
            "userProfile": {
                "displayName": f"Private reviewer {index + 1}",
                "avatarUrl": "https://example.invalid/avatar.jpg",
            },
            "photos": [{"url": "https://example.invalid/user-photo.jpg"}],
            "managementResponse": {
                "author": "Private business responder",
                "text": "Thank you",
            },
        }
        for index, language in enumerate(languages)
    ]
    return {
        "data": {
            "ReviewsProxy_getReviewListPageForLocation": [
                {"totalCount": len(reviews), "reviews": reviews}
            ]
        }
    }


def product_graphql_fixture(listing_id=123, *, pax_failed=True, packages=None):
    """Caller-facing evidence shaped like the live persisted-query bundle."""
    url = listing(str(listing_id), route="AttractionProductReview")["url"]
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    product_code = f"P-{listing_id}"
    product_title = f"Product {listing_id}"
    reviews = [
        {
            "id": str(index),
            "locationId": listing_id,
            "title": f"English title {index}",
            "text": f"Translated useful review {index}",
            "rating": 5,
            "language": "en",
            "originalLanguage": "hu",
            "translationType": "MACHINE",
        }
        for index in range(1, 3)
    ]

    def attempt(query_id, variables, response, number=1):
        return {
            "attempt": number,
            "persistedQueryId": query_id,
            "variables": variables,
            "httpStatus": 200,
            "evidenceResponse": response,
        }

    passenger_mix = None if pax_failed else [{"bandId": "ADULT", "count": 2}]
    selection = {
        "travelDate": "2026-07-18",
        "travelDateSource": "priceCalendar.datesAndPrices",
        "passengerMix": passenger_mix,
        "packageOptionsStatus": "UNKNOWN" if pax_failed else "AVAILABLE",
        "packageOptionsUnavailableReason": "pax_failed" if pax_failed else None,
    }
    detail_response = {
        "data": {
            "fullProduct": [
                {
                    "activityId": listing_id,
                    "productCode": product_code,
                    "title": {"text": product_title},
                    "description": {
                        "text": "A concrete English explanation of what visitors do."
                    },
                    "languageServices": {"languageInfoMap": []},
                }
            ]
        }
    }
    queries = {
        "detail": [
            attempt(
                PRODUCT_DETAIL_QUERY_ID,
                {"activityId": listing_id, "currency": "USD", "language": "en"},
                detail_response,
            )
        ],
        "reviews": [
            attempt(
                PRODUCT_REVIEWS_QUERY_ID,
                {
                    "locationId": listing_id,
                    "limit": 10,
                    "offset": 0,
                    "filters": [],
                    "sortType": "DEFAULT",
                    "sortBy": "DATE",
                    "language": "en",
                    "doMachineTranslation": True,
                },
                {
                    "data": {
                        "ReviewsProxy_getReviewListPageForLocation": [
                            {"totalCount": 2, "reviews": reviews}
                        ]
                    }
                },
            )
        ],
        "priceCalendar": [
            attempt(
                PRICE_CALENDAR_QUERY_ID,
                {"currency": "USD", "productCode": product_code},
                {
                    "data": {
                        "priceCalendar": [
                            {
                                "datesAndPrices": [
                                    {"date": "2026-07-18", "price": 19.86}
                                ]
                            }
                        ]
                    }
                },
            )
        ],
        "cancellation": [
            attempt(
                CANCELLATION_QUERY_ID,
                {"currency": "USD", "productCode": product_code},
                {
                    "data": {
                        "fullProduct": [
                            {
                                "title": {"text": product_title},
                                "cancellationConditions": {
                                    "cancelConditionsV2": [],
                                    "cancellationPolicyType": "STANDARD",
                                },
                            }
                        ]
                    }
                },
            )
        ],
        "pax": [
            attempt(
                PAX_QUERY_ID,
                {
                    "currencies": ["USD"],
                    "locale": "en-US",
                    "travelDate": "2026-07-18",
                    "selectedLanguage": None,
                    "productCode": product_code,
                },
                {
                    "data": {
                        "paxMix": (
                            {"resultStatus": "FAILED", "result": None}
                            if pax_failed
                            else {
                                "resultStatus": "SUCCESS",
                                "result": {
                                    "ageBands": [
                                        {"id": "ADULT", "title": "Adult"}
                                    ]
                                },
                            }
                        )
                    }
                },
            )
        ],
    }
    if not pax_failed:
        grades = packages if packages is not None else [
            {
                "title": "Small group",
                "price": {"amount": 50, "currency": "USD"},
            }
        ]
        queries["packages"] = [
            attempt(
                PACKAGES_QUERY_ID,
                {
                    "productCode": product_code,
                    "travelDate": "2026-07-18",
                    "passengerMix": passenger_mix,
                    "currencies": ["USD"],
                    "locale": "en-US",
                },
                {
                    "data": {
                        "tourGrades": {
                            "resultStatus": "SUCCESS",
                            "result": {"tourGrades": grades},
                        }
                    }
                },
            )
        ]
    return {
        "schemaVersion": 1,
        "transport": "tripadvisor-browser-graphql",
        "route": "AttractionProductReview",
        "detailId": listing_id,
        "sourceUrl": url,
        "checkedAt": checked_at,
        "queries": queries,
        "selection": selection,
    }


class DetailParserTests(unittest.TestCase):
    def test_clean_text_normalizes_nfc_and_removes_invalid_unicode(self):
        self.assertEqual(clean_text("Cafe\u0301 \ud800bad\ufffd text"), "Café bad text")

    def test_extracts_authoritative_about_and_visible_review_fields(self):
        page = f"""
          <html><body>
            <div data-automation="attractionsAboutContent">
              The official &amp; useful <b>activity description</b>.<br>
              It explains what visitors actually do.
            </div>
            {review_card(1, 4.5)}
          </body></html>
        """
        parsed = parse_detail_html(page, listing_id="123")
        self.assertEqual(
            parsed["description"],
            "The official & useful activity description. It explains what visitors actually do.",
        )
        self.assertEqual(parsed["description_source"], "tripadvisor_about")
        self.assertEqual(
            parsed["reviews"],
            [
                {
                    "title": "Title 1",
                    "text": "Body 1 with useful detail.",
                    "rating": 4.5,
                }
            ],
        )

    def test_product_description_falls_back_to_matching_json_ld(self):
        unrelated = {
            "@type": "Product",
            "url": "https://example.test/AttractionProductReview-g1-d999-Other.html",
            "description": "Wrong product.",
        }
        target = {
            "@type": "Product",
            "url": "https://example.test/AttractionProductReview-g1-d123-Target.html",
            "description": "A 100-minute immersive history experience with VR.",
        }
        page = (
            '<html><script type="application/ld+json">'
            + json.dumps([unrelated, target])
            + "</script></html>"
        )
        parsed = parse_detail_html(page, listing_id="123")
        self.assertEqual(parsed["description"], target["description"])
        self.assertEqual(parsed["description_source"], "tripadvisor_json_ld")

    def test_unrelated_json_ld_does_not_beat_target_meta_description(self):
        unrelated = {
            "@type": "Product",
            "url": "https://example.test/AttractionProductReview-g1-d999-Other.html",
            "description": "A much longer description for the wrong product.",
        }
        page = (
            '<html><head><meta name="description" content="Correct target summary.">'
            '<script type="application/ld+json">'
            + json.dumps(unrelated)
            + "</script></head></html>"
        )
        parsed = parse_detail_html(page, listing_id="123")
        self.assertEqual(parsed["description"], "Correct target summary.")
        self.assertEqual(parsed["description_source"], "tripadvisor_meta")

    def test_product_about_block_is_extractable_without_json_ld(self):
        page = """
          <html><div data-automation="apr-product-info">
            <h2>About</h2><div>Explore nine immersive rooms and original objects.</div>
            <button>Read more</button>
          </div></html>
        """
        parsed = parse_detail_html(page)
        self.assertEqual(
            parsed["description"],
            "Explore nine immersive rooms and original objects.",
        )
        self.assertEqual(parsed["description_source"], "tripadvisor_about")

    def test_camel_case_review_text_marker_is_captured(self):
        page = """
          <html><div data-automation="reviewCard">
            <svg data-automation="bubbleRatingImage"><title>5 of 5 bubbles</title></svg>
            <a data-automation="review-title" href="/ShowUserReviews-x">
              <span lang="en">Great</span>
            </a>
            <div data-automation="reviewText">Concrete body text.</div>
          </div></html>
        """
        parsed = parse_detail_html(page)
        self.assertEqual(parsed["reviews"][0]["text"], "Concrete body text.")

    def test_json_ld_entities_do_not_break_json_before_parsing(self):
        document = {
            "@type": "Product",
            "url": "https://example.test/AttractionProductReview-g1-d123-Target.html",
            "description": "Rock &quot;history&quot; tour",
        }
        page = (
            '<html><script type="application/ld+json">'
            + json.dumps(document)
            + "</script></html>"
        )
        parsed = parse_detail_html(page, listing_id="123")
        self.assertEqual(parsed["description"], 'Rock "history" tour')

    def test_keeps_at_most_ten_unique_visible_review_cards(self):
        page = "<html>" + "".join(review_card(i) for i in range(12)) + "</html>"
        parsed = parse_detail_html(page)
        self.assertEqual(len(parsed["reviews"]), 10)
        self.assertEqual(parsed["reviews"][0]["title"], "Title 0")
        self.assertEqual(parsed["reviews"][-1]["title"], "Title 9")

    def test_extracts_scoped_price_and_available_package_options(self):
        page = """
          <html><body>
            <div data-automation="commerce_module_visible_price">$57.25</div>
            <button data-automation="inline-booking-date-picker">Saturday, July 18, 2026</button>
            <button data-automation="inline-booking-pax-picker">2</button>
            <div data-automation="availabilityTourGrades">
              <div data-automation="tourGrade-0">
                <span id="available-times-label-0-inline-booking-section">. Available times: 7:00 PM</span>
                <span id="detailed-total-price-0-inline-booking-section">. Total price: $114.50 for 2 adults</span>
                <div id="title-0-inline-booking-section">Goulash Cruise</div>
                <div id="description-0-inline-booking-section">Welcome drink, goulash and mini langos.</div>
                <div>2 Adults x <span>$57.25</span></div>
              </div>
            </div>
          </body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertEqual(pricing["base_price"], "$57.25")
        self.assertEqual(pricing["booking_date"], "Saturday, July 18, 2026")
        self.assertEqual(pricing["travelers"], "2")
        self.assertEqual(
            pricing["packages"],
            [{
                "name": "Goulash Cruise",
                "description": "Welcome drink, goulash and mini langos.",
                "available_times": "7:00 PM",
                "total_price": "$114.50",
                "party": "2 adults",
                "unit_price": "$57.25",
                "unit": "adults",
                "availability": "available",
            }],
        )
        self.assertEqual(pricing["status"], "available")
        self.assertEqual(pricing["availability"]["status"], "available")
        self.assertEqual(
            pricing["availability"]["source"],
            "data-automation:availabilityTourGrades",
        )

    def test_keeps_standalone_package_total_without_inventing_party_or_unit_price(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <span id="detailed-total-price-0-inline-booking-section">. Total price: $344.69 </span>
              <div id="title-0-inline-booking-section">Budapest City Walk in Jewish Quarter</div>
              <div id="description-0-inline-booking-section">Pickup included</div>
            </div>
          </div></body></html>
        """

        package = parse_detail_html(page)["pricing_evidence"]["packages"][0]

        self.assertEqual(package["total_price"], "$344.69")
        self.assertEqual(package["party"], "")
        self.assertEqual(package["unit_price"], "")
        self.assertEqual(package["unit"], "")
        self.assertEqual(package["availability"], "available")

    def test_standalone_totals_do_not_infer_party_from_option_name_or_nearby_text(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <div id="title-0-inline-booking-section">4 hours for 3-6 people</div>
              <span id="detailed-total-price-0-inline-booking-section">Total price: $1,332.03</span>
              <div>6 Adults x $222.01</div>
            </div>
            <div data-automation="tourGrade-1">
              <div id="title-1-inline-booking-section">Express private option</div>
              <span id="detailed-total-price-1-inline-booking-section">Total price: $262.90</span>
            </div>
          </div></body></html>
        """

        packages = parse_detail_html(page)["pricing_evidence"]["packages"]

        self.assertEqual(
            [package["total_price"] for package in packages],
            ["$1,332.03", "$262.90"],
        )
        self.assertEqual(
            [
                (package["party"], package["unit_price"], package["unit"])
                for package in packages
            ],
            [("", "", ""), ("", "", "")],
        )

    def test_parses_spaced_and_localized_prefix_or_suffix_money(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <div id="title-0-inline-booking-section">Forint prefix</div>
              <span id="detailed-total-price-0-inline-booking-section">Total price: HUF 20,000 for 2 adults</span>
              <div>2 Adults x HUF 10,000</div>
            </div>
            <div data-automation="tourGrade-1">
              <div id="title-1-inline-booking-section">Dollar prefix</div>
              <span id="detailed-total-price-1-inline-booking-section">Total price: USD 69.94 for 2 adults</span>
              <div>2 Adults x USD 34.97</div>
            </div>
            <div data-automation="tourGrade-2">
              <div id="title-2-inline-booking-section">Forint suffix</div>
              <span id="detailed-total-price-2-inline-booking-section">Total price: 20 000 Ft for 2 adults</span>
              <div>2 Adults x 10 000 Ft</div>
            </div>
          </div></body></html>
        """
        packages = parse_detail_html(page)["pricing_evidence"]["packages"]
        self.assertEqual(
            [(item["total_price"], item["unit_price"]) for item in packages],
            [
                ("HUF 20,000", "HUF 10,000"),
                ("USD 69.94", "USD 34.97"),
                ("20 000 Ft", "10 000 Ft"),
            ],
        )

    def test_structures_surcharge_and_redacts_model_facing_description(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <div id="title-0-inline-booking-section">Walking option</div>
              <div id="description-0-inline-booking-section">
                Bus and subway are used. The additional cost for transport tickets is: 8 EUR/person. Pickup included.
              </div>
              <span id="detailed-total-price-0-inline-booking-section">Total price: USD 69.94 for 2 adults</span>
              <div>2 Adults x USD 34.97</div>
            </div>
          </div></body></html>
        """
        package = parse_detail_html(page)["pricing_evidence"]["packages"][0]
        self.assertEqual(
            package["description"],
            "Bus and subway are used. The additional cost for transport tickets is: [price omitted]/person. Pickup included.",
        )
        self.assertEqual(
            package["source_description"],
            "Bus and subway are used. The additional cost for transport tickets is: 8 EUR/person. Pickup included.",
        )
        self.assertEqual(
            package["additional_costs"],
            [{
                "amount": "8 EUR",
                "unit": "person",
                "source_text": "The additional cost for transport tickets is: 8 EUR/person.",
            }],
        )

    def test_repairs_nan_party_summary_from_rendered_charge_lines(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <div id="title-0-inline-booking-section">Mixed-age group</div>
              <span id="detailed-total-price-0-inline-booking-section">Total price: $467.36 for 4 adults and NaN seniors</span>
              <div>4 Adults x $58.42</div>
              <div>4 Seniors x $58.42</div>
            </div>
          </div></body></html>
        """

        package = parse_detail_html(page)["pricing_evidence"]["packages"][0]

        self.assertEqual(package["party"], "4 adults and 4 seniors")
        self.assertEqual(package["unit_price"], "$58.42")
        self.assertEqual(package["unit"], "adults")

    def test_structures_explicit_fee_not_included(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <div id="title-0-inline-booking-section">Museum option</div>
              <div id="description-0-inline-booking-section">
                The entry fee is not included and costs 3 EUR per person.
              </div>
            </div>
          </div></body></html>
        """
        package = parse_detail_html(page)["pricing_evidence"]["packages"][0]
        self.assertEqual(
            package["additional_costs"],
            [{
                "amount": "3 EUR",
                "unit": "person",
                "source_text": "The entry fee is not included and costs 3 EUR per person.",
            }],
        )

    def test_scoped_no_commerce_sold_out_overrides_price_and_related_cards(self):
        page = """
          <html><body>
            <h1 data-automation="mainH1">A bookable tour</h1>
            <div data-automation="inlineBookingSection">
              <div data-automation="commerce_module_visible_price">$25.00</div>
              <button data-automation="inline-booking-date-picker">July 20, 2026</button>
              <div data-automation="noCommerceMessage">
                Sold out for your group on July 20. Change the date or group size to find availability.
              </div>
              <div data-automation="shelfCard">Related experience available today</div>
            </div>
          </body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertEqual(pricing["status"], "unavailable")
        self.assertEqual(pricing["availability"]["status"], "sold-out")
        self.assertEqual(
            pricing["availability"]["message"],
            "Sold out for your group on July 20. Change the date or group size to find availability.",
        )
        self.assertEqual(
            pricing["availability"]["source"],
            "data-automation:noCommerceMessage",
        )

    def test_package_availability_is_emitted_and_any_available_option_wins(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <div id="title-0-inline-booking-section">Early tour</div>
              <span>Sold out</span>
            </div>
            <div data-automation="tourGrade-1">
              <div id="title-1-inline-booking-section">Late tour</div>
              <span id="available-times-label-1-inline-booking-section">Available times: 8:00 PM</span>
              <span id="detailed-total-price-1-inline-booking-section">Total price: $50 for 2 adults</span>
              <div>2 Adults x <span>$25</span></div>
              <button>Reserve Now</button>
            </div>
          </div></body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertEqual(
            [package["availability"] for package in pricing["packages"]],
            ["sold-out", "available"],
        )
        self.assertEqual(
            pricing["packages"][0]["availability_message"], "Sold out"
        )
        self.assertEqual(pricing["status"], "available")
        self.assertEqual(pricing["availability"]["status"], "available")

    def test_all_rendered_package_options_sold_out_maps_to_unavailable(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <div id="title-0-inline-booking-section">First option</div><span>Sold out</span>
            </div>
            <div data-automation="tourGrade-1">
              <div id="title-1-inline-booking-section">Second option</div><span>Sold out</span>
            </div>
          </div></body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertEqual(pricing["status"], "unavailable")
        self.assertEqual(pricing["availability"]["status"], "sold-out")
        self.assertEqual(
            pricing["availability"]["source"],
            "data-automation:availabilityTourGrades",
        )

    def test_sold_out_words_in_package_title_or_description_are_not_status(self):
        page = """
          <html><body><div data-automation="availabilityTourGrades">
            <div data-automation="tourGrade-0">
              <div id="title-0-inline-booking-section">Sold out ticket alternatives</div>
              <div id="description-0-inline-booking-section">
                A guide explains what to do when popular tickets are sold out.
              </div>
              <span id="available-times-label-0-inline-booking-section">Available times: 9:00 AM</span>
              <span id="detailed-total-price-0-inline-booking-section">Total price: $20 for 2 adults</span>
              <div>2 Adults x <span>$10</span></div>
              <button>Reserve Now</button>
            </div>
          </div></body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertEqual(pricing["packages"][0]["availability"], "available")
        self.assertEqual(pricing["availability"]["status"], "available")

    def test_target_tripadvisor_closure_banner_maps_to_unavailable(self):
        page = """
          <html><body>
            <div class="status-banner">
              <span>Message from Tripadvisor<span> • </span></span>
              <span>Temporarily closed until further notice</span>
            </div>
            <div data-automation="reviewCard">
              <a data-automation="review-title">Old review</a>
              <div data-automation="reviewText">Tickets elsewhere were sold out.</div>
            </div>
          </body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertEqual(pricing["status"], "unavailable")
        self.assertEqual(pricing["availability"]["status"], "closed")
        self.assertEqual(
            pricing["availability"]["source"], "tripadvisor-status-banner"
        )
        self.assertIn("Temporarily closed", pricing["availability"]["message"])

    def test_review_title_and_related_card_status_claims_are_ignored(self):
        page = """
          <html><body>
            <h1 data-automation="mainH1">Permanently closed mystery exhibition</h1>
            <div data-automation="reviewCard">
              <a data-automation="review-title">Temporarily closed on my visit</a>
              <div data-automation="reviewText">No availability last winter.</div>
            </div>
            <div data-automation="shelfCard">Related tour sold out</div>
            <div data-automation="inlineBookingTitle">Select date and travelers</div>
          </body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertEqual(pricing["status"], "date-required")
        self.assertEqual(pricing["availability"]["status"], "date-required")
        self.assertEqual(
            pricing["availability"]["message"], "Select date and travelers"
        )

    def test_review_only_closure_claim_leaves_availability_unknown(self):
        page = """
          <html><body><div data-automation="reviewCard">
            <a data-automation="review-title">Closed when we visited</a>
            <div data-automation="reviewText">It was temporarily closed until spring.</div>
          </div></body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertNotIn("status", pricing)
        self.assertEqual(pricing["availability"], {"status": "unknown"})

    def test_explicit_scoped_unavailable_message_is_preserved(self):
        page = """
          <html><body><div data-automation="noCommerceMessage">
            This experience is currently unavailable for booking.
          </div></body></html>
        """
        pricing = parse_detail_html(page)["pricing_evidence"]
        self.assertEqual(pricing["status"], "unavailable")
        self.assertEqual(pricing["availability"]["status"], "unavailable")
        self.assertEqual(
            pricing["availability"]["message"],
            "This experience is currently unavailable for booking.",
        )

    def test_zero_review_limit_returns_no_cards(self):
        parsed = parse_detail_html("<html>" + review_card(1) + "</html>", review_limit=0)
        self.assertEqual(parsed["reviews"], [])

    def test_review_like_text_inside_script_is_not_treated_as_visible(self):
        encoded_state = json.dumps(
            {"data-automation": "reviewCard", "reviewTitle": "Not rendered"}
        )
        page = f'<html><script type="application/json">{encoded_state}</script></html>'
        self.assertEqual(parse_detail_html(page)["reviews"], [])


class CacheTests(unittest.TestCase):
    def test_reads_comment_split_all_language_review_total(self):
        page = "<button><span>All reviews<!-- --> (<!-- -->1,234<!-- -->)</span></button>"
        self.assertEqual(rendered_review_total(page), 1234)
        self.assertEqual(review_coverage_target(page, 9999), 10)

    def test_reads_scoped_generic_product_language_and_review_total(self):
        page = """
          <span data-automation="reviewCount"><span>(999)</span></span>
          <div data-automation="apr-reviews">
            <span data-automation="reviewCount"><span>(11)</span></span>
            <button aria-haspopup="listbox" aria-label="language: English (11)">
              English
            </button>
          </div>
        """
        self.assertEqual(rendered_product_review_language(page), "English (11)")
        self.assertEqual(rendered_review_total(page), 11)

    def test_nine_card_product_page_with_explicit_next_page_is_complete_sample(self):
        cards = "".join(review_card(index) for index in range(1, 10))
        page = f"""
          <div data-automation="apr-reviews">
            <span data-automation="reviewCount"><span>(229)</span></span>
            <button aria-haspopup="listbox" aria-label="language: All languages (229)">
              All languages
            </button>
            {cards}
            <a data-smoke-attr="pagination-next-arrow" href="?or10=">Next</a>
          </div>
        """
        self.assertEqual(
            review_coverage_target(
                page,
                229,
                expected_identity=("AttractionProductReview", "34325420"),
            ),
            9,
        )

    def test_nine_card_product_page_without_next_page_still_requires_ten(self):
        cards = "".join(review_card(index) for index in range(1, 10))
        page = f"""
          <div data-automation="apr-reviews">
            <span data-automation="reviewCount"><span>(229)</span></span>
            <button aria-haspopup="listbox" aria-label="language: All languages (229)">
              All languages
            </button>
            {cards}
          </div>
        """
        self.assertEqual(review_coverage_target(page, 229), 10)

    def test_nine_card_exception_is_identity_scoped(self):
        cards = "".join(review_card(index) for index in range(1, 10))
        page = f"""
          <div data-automation="apr-reviews">
            <span data-automation="reviewCount"><span>(229)</span></span>
            <button aria-haspopup="listbox" aria-label="language: All languages (229)">
              All languages
            </button>
            {cards}
            <a data-smoke-attr="pagination-next-arrow" href="?or10=">Next</a>
          </div>
        """
        self.assertEqual(
            review_coverage_target(
                page,
                229,
                expected_identity=("AttractionProductReview", "99999999"),
            ),
            10,
        )

    def test_verified_matthias_church_venue_layout_accepts_nine_cards(self):
        cards = "".join(review_card(index) for index in range(1, 10))
        page = f"""
          <button><span>All reviews (10,715)</span></button>
          {cards}
          <a data-smoke-attr="pagination-next-arrow" href="?or10=">Next</a>
        """
        self.assertEqual(
            review_coverage_target(
                page,
                10_715,
                expected_identity=("Attraction_Review", "276808"),
            ),
            9,
        )

    def test_reads_comment_split_active_review_filter_count(self):
        page = "<button><span>Filters<!-- --> (<!-- -->1<!-- -->)</span></button>"
        self.assertEqual(rendered_review_filter_count(page), 1)

    def test_challenge_shell_is_not_accepted_as_a_detail_page(self):
        page = valid_cached_html().replace(
            "</body>", "Verify you are human</body>"
        )
        self.assertFalse(html_looks_valid(page, listing("123")["url"]))

    def test_datadome_interstitial_is_distinct_from_normal_page_tag(self):
        challenge = """
          <html><head><title>DataDome CAPTCHA</title></head><body>
          <iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=abc">
          </iframe></body></html>
        """
        normal = valid_cached_html().replace(
            "</head>", '<script src="https://js.datadome.co/tags.js"></script></head>'
        )
        self.assertTrue(html_looks_like_datadome_challenge(challenge))
        self.assertFalse(html_looks_like_datadome_challenge(normal))

    def test_datadome_partial_trips_shared_block_without_overwriting_cache(self):
        class BlockingRunner:
            def __init__(self):
                self.calls = 0
                self.resets = 0

            def __call__(self, _command, stdout, **_kwargs):
                self.calls += 1
                stdout.write(
                    "<html><head><title>DataDome CAPTCHA</title></head>"
                    '<body><iframe src="https://geo.captcha-delivery.com/captcha/'
                    '?initialCid=abc"></iframe></body></html>'
                )
                return SimpleNamespace(returncode=0, stderr="")

            def reset(self):
                self.resets += 1

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            original = valid_cached_html()
            destination.write_text(original, encoding="utf-8")
            runner = BlockingRunner()
            controller = AdaptiveBlockController()

            with self.assertRaises(DataDomeBlocked):
                fetch_detail(
                    listing("123")["url"],
                    destination,
                    "123",
                    refresh=True,
                    runner=runner,
                    sleeper=lambda _seconds: None,
                    block_controller=controller,
                )

            self.assertTrue(controller.is_blocked())
            self.assertEqual(runner.calls, 1)
            self.assertEqual(runner.resets, 1)
            self.assertEqual(destination.read_text(encoding="utf-8"), original)
            self.assertFalse(destination.with_name("detail.html.part").exists())

    def test_graphql_rate_limit_trips_shared_block_without_retrying(self):
        class BlockingRunner:
            def __init__(self):
                self.calls = 0
                self.resets = 0

            def __call__(self, command, stdout, **_kwargs):
                self.calls += 1
                return subprocess.CompletedProcess(
                    command, 1, stderr="GraphQLBlockedHTTP:429\nrate limited"
                )

            def reset(self):
                self.resets += 1

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.graphql.json"
            runner = BlockingRunner()
            controller = AdaptiveBlockController()

            with self.assertRaises(DataDomeBlocked):
                fetch_detail(
                    listing("123", route="AttractionProductReview")["url"],
                    destination,
                    "123",
                    refresh=True,
                    runner=runner,
                    sleeper=lambda _seconds: None,
                    block_controller=controller,
                    transport="graphql",
                )

            self.assertTrue(controller.is_blocked())
            self.assertEqual(runner.calls, 1)
            self.assertEqual(runner.resets, 1)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_empty_review_shell_is_not_accepted_as_evidence(self):
        target = listing("123")["url"]
        page = (
            f'<html><head><title>Tripadvisor</title><link rel="canonical" href="{target}">'
            '</head><body><div data-automation="reviewCard"></div>'
            + ("x" * MIN_HTML_BYTES)
            + "</body></html>"
        )
        self.assertFalse(html_looks_valid(page, target))

    def test_matching_sparse_page_with_real_heading_is_valid_research(self):
        target = listing("123")["url"]
        page = (
            f'<html><head><title>Tripadvisor</title><link rel="canonical" href="{target}">'
            '</head><body><h1 data-automation="mainH1">Sparse but real place</h1>'
            + ("x" * MIN_HTML_BYTES)
            + "</body></html>"
        )
        self.assertTrue(html_looks_valid(page, target))

    def test_related_link_cannot_impersonate_the_canonical_listing(self):
        target = listing("123")["url"]
        page = valid_cached_html("999").replace(
            "</body>", f'<a href="{target}">Related listing</a></body>'
        )
        self.assertIn("-d123-", page)
        self.assertFalse(html_looks_valid(page, target))

    def test_valid_cache_skips_camoufox(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            destination.write_text(valid_cached_html(), encoding="utf-8")

            def should_not_run(*args, **kwargs):
                raise AssertionError("Camoufox runner should not be called")

            result = fetch_detail(
                listing("123")["url"],
                destination,
                "123",
                runner=should_not_run,
            )
            self.assertEqual(result, ("cached", True, False))

    def test_language_filtered_cache_is_incomplete(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            page = valid_cached_html().replace(
                "</body>",
                "<button><span>All reviews<!-- --> (<!-- -->9<!-- -->)</span></button></body>",
            )
            destination.write_text(page, encoding="utf-8")
            self.assertFalse(
                cache_looks_valid(
                    destination,
                    listing("123")["url"],
                    expected_review_count=42,
                )
            )

    def test_ten_cards_are_still_incomplete_when_locale_filter_is_active(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            page = valid_cached_html().replace(
                "</body>",
                "".join(review_card(index) for index in range(2, 11))
                + "<button><span>Filters<!-- --> (<!-- -->1<!-- -->)</span></button>"
                + "<button><span>All reviews<!-- --> (<!-- -->42<!-- -->)</span></button>"
                + "</body>",
            )
            destination.write_text(page, encoding="utf-8")
            self.assertEqual(len(parse_detail_html(page)["reviews"]), 10)
            self.assertFalse(
                cache_looks_valid(
                    destination,
                    listing("123")["url"],
                    expected_review_count=42,
                )
            )

    def test_generic_english_product_filter_invalidates_ten_card_cache(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            venue_url = listing("123")["url"]
            product_url = listing("123", route="AttractionProductReview")["url"]
            page = valid_cached_html().replace(venue_url, product_url).replace(
                "</body>",
                "".join(review_card(index) for index in range(2, 11))
                + '<div data-automation="apr-reviews">'
                + '<span data-automation="reviewCount"><span>(42)</span></span>'
                + '<button aria-haspopup="listbox" '
                + 'aria-label="language: English (42)">English</button>'
                + "</div></body>",
            )
            destination.write_text(page, encoding="utf-8")
            self.assertEqual(len(parse_detail_html(page)["reviews"]), 10)
            self.assertFalse(
                cache_looks_valid(
                    destination,
                    product_url,
                    expected_review_count=42,
                )
            )

    def test_reviewed_product_cache_requires_selected_language_evidence(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            venue_url = listing("123")["url"]
            product_url = listing("123", route="AttractionProductReview")["url"]
            page = valid_cached_html().replace(venue_url, product_url).replace(
                "</body>",
                "".join(review_card(index) for index in range(2, 11))
                + '<div data-automation="apr-reviews">'
                + '<span data-automation="reviewCount"><span>(42)</span></span>'
                + "</div></body>",
            )
            destination.write_text(page, encoding="utf-8")

            self.assertEqual(len(parse_detail_html(page)["reviews"]), 10)
            self.assertEqual(rendered_product_review_language(page), "")
            self.assertFalse(
                cache_looks_valid(
                    destination,
                    product_url,
                    expected_review_count=42,
                )
            )

    def test_zero_review_product_does_not_require_language_selector(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            product_url = listing("123", route="AttractionProductReview")["url"]
            page = (
                f'<html><head><title>Example - Tripadvisor</title>'
                f'<link rel="canonical" href="{product_url}"></head><body>'
                '<h1 data-automation="mainH1">Real zero-review product</h1>'
                '<div data-automation="apr-reviews">'
                '<span data-automation="reviewCount"><span>(0)</span></span>'
                "</div></body></html>"
                + ("x" * (MIN_HTML_BYTES + 100))
            )
            destination.write_text(page, encoding="utf-8")

            self.assertTrue(
                cache_looks_valid(
                    destination,
                    product_url,
                    expected_review_count=0,
                )
            )

    def test_all_language_product_live_total_overrides_stale_discovery_count(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            venue_url = listing("123")["url"]
            product_url = listing("123", route="AttractionProductReview")["url"]
            page = valid_cached_html().replace(venue_url, product_url).replace(
                "</body>",
                '<div data-automation="apr-reviews">'
                + '<span data-automation="reviewCount"><span>(1)</span></span>'
                + '<button aria-haspopup="listbox" '
                + 'aria-label="language: All languages (1)">All languages</button>'
                + "</div></body>",
            )
            destination.write_text(page, encoding="utf-8")
            self.assertTrue(
                cache_looks_valid(
                    destination,
                    product_url,
                    expected_review_count=42,
                )
            )

    def test_live_review_total_can_be_lower_than_stale_discovery_count(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            page = valid_cached_html().replace(
                "</body>",
                "<button><span>All reviews<!-- --> (<!-- -->1<!-- -->)</span></button></body>",
            )
            destination.write_text(page, encoding="utf-8")
            self.assertTrue(
                cache_looks_valid(
                    destination,
                    listing("123")["url"],
                    expected_review_count=42,
                )
            )

    def test_successful_fetch_atomically_promotes_valid_partial(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"

            def fake_runner(command, stdout, **kwargs):
                self.assertIn("fetch_ta_detail.py", command[1])
                self.assertIn("html", command)
                self.assertNotIn("--no-headless", command)
                stdout.write(valid_cached_html())
                return SimpleNamespace(returncode=0, stderr="")

            status, ok, fetched_live = fetch_detail(
                listing("123")["url"],
                destination,
                "123",
                runner=fake_runner,
                sleeper=lambda _: None,
            )
            self.assertEqual(status, "fetched")
            self.assertTrue(ok)
            self.assertTrue(fetched_live)
            self.assertTrue(destination.exists())
            self.assertFalse(destination.with_name("detail.html.part").exists())

    def test_invalid_persistent_page_resets_browser_before_retry(self):
        class ResettingRunner:
            def __init__(self):
                self.calls = 0
                self.resets = 0

            def __call__(self, _command, stdout, **_kwargs):
                self.calls += 1
                stdout.write(
                    "<html><body>invalid shell</body></html>"
                    if self.calls == 1
                    else valid_cached_html()
                )
                return SimpleNamespace(returncode=0, stderr="")

            def reset(self):
                self.resets += 1

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            runner = ResettingRunner()
            status, ok, fetched_live = fetch_detail(
                listing("123")["url"],
                destination,
                "123",
                runner=runner,
                sleeper=lambda _seconds: None,
            )
            self.assertEqual(status, "fetched after 2 attempts")
            self.assertTrue(ok)
            self.assertTrue(fetched_live)
            self.assertEqual(runner.calls, 2)
            self.assertEqual(runner.resets, 1)

    def test_incomplete_review_render_retries_until_live_target_is_covered(self):
        live_total = (
            "<button><span>All reviews<!-- --> (<!-- -->3<!-- -->)</span></button>"
        )
        incomplete = valid_cached_html().replace(
            "</body>", live_total + "</body>"
        )
        complete = incomplete.replace(
            "</body>", review_card(2) + review_card(3) + "</body>"
        )

        class ResettingRunner:
            def __init__(self):
                self.calls = 0
                self.resets = 0

            def __call__(self, _command, stdout, **_kwargs):
                self.calls += 1
                stdout.write(incomplete if self.calls == 1 else complete)
                return SimpleNamespace(returncode=0, stderr="")

            def reset(self):
                self.resets += 1

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            runner = ResettingRunner()
            status, ok, fetched_live = fetch_detail(
                listing("123")["url"],
                destination,
                "123",
                runner=runner,
                sleeper=lambda _seconds: None,
                expected_review_count=42,
            )

            self.assertEqual(status, "fetched after 2 attempts")
            self.assertTrue(ok)
            self.assertTrue(fetched_live)
            self.assertEqual(runner.calls, 2)
            self.assertEqual(runner.resets, 1)
            self.assertEqual(
                len(parse_detail_html(destination.read_text())["reviews"]), 3
            )


class AdaptiveBlockPhaseTests(unittest.TestCase):
    def test_repeated_blocks_back_off_to_a_bounded_hour(self):
        self.assertEqual(block_cooldown_seconds(900, 1), 900)
        self.assertEqual(block_cooldown_seconds(900, 2), 1800)
        self.assertEqual(block_cooldown_seconds(900, 3), 3600)
        self.assertEqual(block_cooldown_seconds(900, 9), 3600)
        self.assertEqual(block_cooldown_seconds(1, 1_000_000), 3600)
        self.assertEqual(block_cooldown_seconds(0, 9), 0)

    def test_sequential_phase_retries_blocked_and_untouched_work(self):
        items = [(1, "one"), (2, "two"), (3, "three")]
        controller = AdaptiveBlockController()
        consumed = []
        blocked_once = {2}

        def fetcher(index, value):
            if index in blocked_once:
                blocked_once.remove(index)
                raise DataDomeBlocked(index)
            return index, value

        remaining, blocked = run_fetch_phase(
            items,
            1,
            fetcher,
            consumed.append,
            controller,
        )

        self.assertIsInstance(blocked, DataDomeBlocked)
        self.assertTrue(controller.is_blocked())
        self.assertEqual(consumed, [(1, "one")])
        self.assertEqual(remaining, [(2, "two"), (3, "three")])

        controller.clear()
        remaining, blocked = run_fetch_phase(
            remaining,
            1,
            fetcher,
            consumed.append,
            controller,
        )
        self.assertIsNone(blocked)
        self.assertEqual(remaining, [])
        self.assertEqual(consumed, items)

    def test_parallel_phase_never_schedules_beyond_workers_after_block(self):
        items = [(index, str(index)) for index in range(1, 6)]
        controller = AdaptiveBlockController()
        barrier = Barrier(2)
        lock = Lock()
        started = []
        consumed = []

        def fetcher(index, value):
            with lock:
                started.append(index)
            barrier.wait(timeout=2)
            if index == 1:
                raise DataDomeBlocked(index)
            self.assertTrue(controller.wait(timeout=2))
            return index, value

        remaining, blocked = run_fetch_phase(
            items,
            2,
            fetcher,
            consumed.append,
            controller,
        )

        self.assertIsInstance(blocked, DataDomeBlocked)
        self.assertEqual(set(started), {1, 2})
        self.assertEqual(consumed, [(2, "2")])
        self.assertEqual(remaining, [items[0], *items[2:]])


class PersistentBrowserTests(unittest.TestCase):
    @staticmethod
    def _review_loader_fakes():
        class Locator:
            def __init__(
                self, count=0, on_click=None, text="", attributes=None, child=None
            ):
                self._count = count
                self._on_click = on_click
                self._text = text
                self._attributes = attributes or {}
                self._child = child
                self.clicks = 0
                self.scrolls = 0

            @property
            def first(self):
                return self

            def count(self):
                return self._count

            def click(self, **_kwargs):
                self.clicks += 1
                if self._on_click:
                    self._on_click()

            def scroll_into_view_if_needed(self, **_kwargs):
                self.scrolls += 1

            def nth(self, _index):
                return self

            def inner_text(self):
                return self._text

            def get_attribute(self, name):
                return self._attributes.get(name)

            def locator(self, _selector):
                return self._child or Locator()

        return Locator

    def test_review_loader_clears_language_filter_after_scrolling_zero_results(self):
        Locator = self._review_loader_fakes()

        class Page:
            def __init__(self):
                self.cards = Locator()
                self.surface = Locator(1)
                self.anchor = Locator()
                self.empty = Locator()
                self.clear = Locator(
                    1, on_click=lambda: setattr(self.cards, "_count", 10)
                )
                self.all_reviews = Locator(1)
                self.waits = []

            def get_by_role(self, role, name, **_kwargs):
                if role == "button" and name.search("Clear filter"):
                    return self.clear
                if role == "button" and "All reviews" in name.pattern:
                    return self.all_reviews
                return self.empty

            def locator(self, selector):
                if selector == '[data-automation="reviewCard"]':
                    return self.cards
                if selector == 'a[href="#REVIEWS"]':
                    return self.anchor
                if "button[aria-haspopup" in selector or selector == (
                    'button[aria-label="Click to open the filter"]'
                ):
                    return self.empty
                return self.surface

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        _load_all_review_languages(page)
        self.assertEqual(page.clear.clicks, 1)
        self.assertEqual(page.all_reviews.clicks, 0)
        self.assertEqual(page.surface.scrolls, 1)
        self.assertEqual(page.waits, [500, 1500])
        self.assertEqual(page.cards.count(), 10)

    def test_review_loader_clears_language_filter_with_ten_initial_cards(self):
        Locator = self._review_loader_fakes()

        class Page:
            def __init__(self):
                self.cards = Locator(10)
                self.surface = Locator(1)
                self.anchor = Locator()
                self.empty = Locator()
                self.clear = Locator(1)
                self.all_reviews = Locator(1)
                self.waits = []

            def get_by_role(self, role, name, **_kwargs):
                if role == "button" and name.search("Clear filter"):
                    return self.clear
                if role == "button" and "All reviews" in name.pattern:
                    return self.all_reviews
                return self.empty

            def locator(self, selector):
                if selector == '[data-automation="reviewCard"]':
                    return self.cards
                if selector == 'a[href="#REVIEWS"]':
                    return self.anchor
                if "button[aria-haspopup" in selector or selector == (
                    'button[aria-label="Click to open the filter"]'
                ):
                    return self.empty
                return self.surface

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        _load_all_review_languages(page)
        self.assertEqual(page.clear.clicks, 1)
        self.assertEqual(page.all_reviews.clicks, 0)
        self.assertEqual(page.waits, [500, 1500])

    def test_review_loader_clicks_countless_all_reviews_control(self):
        Locator = self._review_loader_fakes()

        class Page:
            def __init__(self):
                self.cards = Locator()
                self.surface = Locator(1)
                self.anchor = Locator()
                self.empty = Locator()
                self.clear = Locator()
                self.all_reviews = Locator(
                    1, on_click=lambda: setattr(self.cards, "_count", 10)
                )
                self.waits = []

            def get_by_role(self, role, name, **_kwargs):
                if role == "button" and name.search("Clear filter"):
                    return self.clear
                if role == "button" and "All reviews" in name.pattern:
                    return self.all_reviews
                return self.empty

            def locator(self, selector):
                if selector == '[data-automation="reviewCard"]':
                    return self.cards
                if selector == 'a[href="#REVIEWS"]':
                    return self.anchor
                if "button[aria-haspopup" in selector or selector == (
                    'button[aria-label="Click to open the filter"]'
                ):
                    return self.empty
                return self.surface

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        _load_all_review_languages(page)
        self.assertEqual(page.all_reviews.clicks, 1)
        self.assertEqual(page.waits, [500, 1500])
        self.assertEqual(page.cards.count(), 10)

    def test_review_loader_wakes_lazy_product_review_surface_without_controls(self):
        Locator = self._review_loader_fakes()

        class Page:
            def __init__(self):
                self.cards = Locator()
                self.surface = Locator(1)
                self.anchor = Locator()
                self.empty = Locator()
                self.waits = []

            def get_by_role(self, *_args, **_kwargs):
                return self.empty

            def locator(self, selector):
                if selector == '[data-automation="reviewCard"]':
                    return self.cards
                if selector == 'a[href="#REVIEWS"]':
                    return self.anchor
                if "button[aria-haspopup" in selector or selector == (
                    'button[aria-label="Click to open the filter"]'
                ):
                    return self.empty
                return self.surface

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)
                if milliseconds == 500:
                    self.cards._count = 3

        page = Page()
        _load_all_review_languages(page)
        self.assertGreaterEqual(page.surface.scrolls, 1)
        self.assertEqual(page.cards.scrolls, 2)
        self.assertEqual(page.cards.count(), 3)
        self.assertEqual(page.waits, [500, 750, 750])

    def test_product_language_picker_switches_generic_english_layout(self):
        Locator = self._review_loader_fakes()

        class Page:
            def __init__(self):
                self.empty = Locator()
                self.language = Locator(
                    1,
                    text="English",
                    attributes={"aria-label": "language: English (11)"},
                )
                self.all_languages = Locator(1)
                self.waits = []

            def locator(self, selector):
                if "button[aria-haspopup" in selector:
                    return self.language
                return self.empty

            def get_by_role(self, role, **_kwargs):
                return self.all_languages if role == "option" else self.empty

            def get_by_text(self, *_args, **_kwargs):
                return self.empty

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        self.assertTrue(_select_product_all_languages(page))
        self.assertEqual(page.language.clicks, 1)
        self.assertEqual(page.all_languages.clicks, 1)
        self.assertEqual(page.waits, [500, 2000])

    def test_product_zero_state_clicks_exact_clear_filter(self):
        Locator = self._review_loader_fakes()

        class Page:
            def __init__(self):
                self.cards = Locator()
                self.surface = Locator(1)
                self.empty = Locator()
                self.clear = Locator(
                    1, on_click=lambda: setattr(self.cards, "_count", 2)
                )
                self.waits = []

            def get_by_role(self, role, name, **_kwargs):
                if role == "button" and name.search("Clear filter"):
                    return self.clear
                return self.empty

            def locator(self, selector):
                if selector == '[data-automation="reviewCard"]':
                    return self.cards
                if selector == 'a[href="#REVIEWS"]':
                    return self.empty
                if "button[aria-haspopup" in selector or selector == (
                    'button[aria-label="Click to open the filter"]'
                ):
                    return self.empty
                return self.surface

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        _load_all_review_languages(page)
        self.assertEqual(page.clear.clicks, 1)
        self.assertEqual(page.cards.count(), 2)
        self.assertEqual(page.waits, [500, 1500, 750, 750])

    def test_venue_filter_toolbar_can_mount_after_all_reviews_click(self):
        Locator = self._review_loader_fakes()

        class Page:
            def __init__(self):
                self.cards = Locator(10)
                self.surface = Locator(1)
                self.empty = Locator()
                self.filter_button = Locator(text="Filters (1)")
                self.language_button = Locator(1)
                self.language = Locator(1, child=self.language_button)
                self.all_languages = Locator(1)
                self.apply = Locator(1)
                self.all_reviews = Locator(
                    1,
                    on_click=lambda: setattr(self.filter_button, "_count", 1),
                )
                self.waits = []

            def get_by_role(self, role, name, **_kwargs):
                if name == "Apply":
                    return self.apply
                if role == "button" and hasattr(name, "search"):
                    if name.search("Clear filter"):
                        return self.empty
                    if name.search("All reviews"):
                        return self.all_reviews
                return self.empty

            def locator(self, selector):
                if selector == '[data-automation="reviewCard"]':
                    return self.cards
                if selector == 'a[href="#REVIEWS"]':
                    return self.empty
                if "button[aria-haspopup" in selector:
                    return self.empty
                if selector == 'button[aria-label="Click to open the filter"]':
                    return self.filter_button
                if selector == '[data-automation="ugcLanguageFilter"]':
                    return self.language
                if selector == '[data-automation="ugcLanguageFilterOption_0"]':
                    return self.all_languages
                return self.surface

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        _load_all_review_languages(page)
        self.assertEqual(page.all_reviews.clicks, 1)
        self.assertEqual(page.filter_button.clicks, 1)
        self.assertEqual(page.language_button.clicks, 1)
        self.assertEqual(page.all_languages.clicks, 1)
        self.assertEqual(page.apply.clicks, 1)
        self.assertEqual(page.waits, [500, 1500, 1500, 500, 250, 2000])

    def test_venue_filter_dialog_selects_all_languages_and_applies(self):
        Locator = self._review_loader_fakes()

        class Page:
            def __init__(self):
                self.empty = Locator()
                self.filter_button = Locator(1, text="Filters (1)")
                self.language_button = Locator(1)
                self.language = Locator(1, child=self.language_button)
                self.all_languages = Locator(1)
                self.apply = Locator(1)
                self.waits = []

            def locator(self, selector):
                return {
                    'button[aria-label="Click to open the filter"]': self.filter_button,
                    '[data-automation="ugcLanguageFilter"]': self.language,
                    '[data-automation="ugcLanguageFilterOption_0"]': self.all_languages,
                }.get(selector, self.empty)

            def get_by_role(self, role, name, **_kwargs):
                if role == "button" and name == "Apply":
                    return self.apply
                return self.empty

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        self.assertTrue(_reset_venue_review_filters(page))
        self.assertEqual(page.filter_button.clicks, 1)
        self.assertEqual(page.language_button.clicks, 1)
        self.assertEqual(page.all_languages.clicks, 1)
        self.assertEqual(page.apply.clicks, 1)
        self.assertEqual(page.waits, [1500, 500, 250, 2000])

    def test_product_without_pax_picker_still_uses_availability_cta(self):
        class Locator:
            def __init__(self, count=0):
                self._count = count
                self.clicks = 0

            @property
            def first(self):
                return self

            def count(self):
                return self._count

            def scroll_into_view_if_needed(self, **_kwargs):
                pass

            def click(self, **_kwargs):
                self.clicks += 1

        class Page:
            def __init__(self):
                self.url = listing("123", route="AttractionProductReview")["url"]
                self.pax = Locator()
                self.grades = Locator()
                self.cta = Locator(1)
                self.waits = []

            def locator(self, selector):
                return {
                    '[data-automation="inline-booking-pax-picker"]': self.pax,
                    '[data-automation="availabilityTourGrades"]': self.grades,
                    '[data-automation="midPageCheckAvailabilityCta"]': self.cta,
                }[selector]

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = Page()
        _load_product_packages(page)
        self.assertEqual(page.cta.clicks, 1)
        self.assertEqual(page.waits, [4000])

    def test_language_info_map_prefers_english(self):
        product = {
            "languageServices": {
                "languageInfoMap": {
                    "de": {"language": "de"},
                    "en": {"language": "en"},
                    "hu": {"language": "hu"},
                }
            }
        }

        self.assertEqual(_selected_product_language(product), "en")

    def test_product_graphql_retries_dates_chronologically_with_a_hard_bound(self):
        url = listing("123", route="AttractionProductReview")["url"]
        detail_variables = {
            "activityId": 123,
            "currency": "USD",
            "language": "en",
        }
        review_variables = {
            "locationId": 123,
            "limit": 10,
            "offset": 0,
            "filters": [],
            "sortType": "DEFAULT",
            "sortBy": "DATE",
            "language": "en",
            "doMachineTranslation": True,
        }
        product = {
            "activityId": 123,
            "productCode": "P-123",
            "title": "Night tour",
            "bookingConfirmationSettings": {"bookingCutoffInHours": 0},
            "languageServices": {
                "languageInfoMap": [
                    {"language": "de"},
                    {"language": "en"},
                ]
            },
        }
        calendar_rows = [
            {"date": "2026-07-23", "price": 30},
            {"date": "2026-07-19", "price": 25},
            {"date": "2026-07-17", "price": 20},
            {"date": "2026-07-22", "price": 29},
            {"date": "2026-07-18", "price": 22},
            {"date": "2026-07-21", "price": 28},
            {"date": "2026-07-20", "price": 27},
            {"date": "2026-07-24", "price": True},
        ]
        calendar_response = {
            "data": {"priceCalendar": [{"datesAndPrices": calendar_rows}]}
        }
        candidates = _travel_date_candidates(
            calendar_response,
            product,
            datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(
            candidates,
            [
                "2026-07-17",
                "2026-07-18",
                "2026-07-19",
                "2026-07-20",
                "2026-07-21",
            ],
        )
        calendar_variables = {"currency": "USD", "productCode": "P-123"}
        failed = {"data": {"paxMix": {"resultStatus": "FAILED", "result": None}}}
        succeeded = {
            "data": {
                "paxMix": {
                    "resultStatus": "SUCCESS",
                    "result": {
                        "ageBands": [{"id": "ADULT", "title": "Adult"}]
                    },
                }
            }
        }
        passenger_mix = [{"bandId": "ADULT", "count": 2}]
        steps = [
            {
                "request": [
                    (PRODUCT_DETAIL_QUERY_ID, detail_variables),
                    (PRODUCT_REVIEWS_QUERY_ID, review_variables),
                ],
                "response": [
                    {"data": {"fullProduct": [product]}},
                    graphql_reviews(123),
                ],
            },
            {
                "request": [
                    (PRICE_CALENDAR_QUERY_ID, calendar_variables),
                    (CANCELLATION_QUERY_ID, calendar_variables),
                ],
                "response": [
                    calendar_response,
                    {"data": {"fullProduct": [{"activityId": 123}]}},
                ],
            },
        ]
        for travel_date, response in (
            ("2026-07-17", failed),
            ("2026-07-18", failed),
            ("2026-07-19", succeeded),
        ):
            steps.append(
                {
                    "request": [
                        (
                            PAX_QUERY_ID,
                            {
                                "currencies": ["USD"],
                                "locale": "en-US",
                                "travelDate": travel_date,
                                "selectedLanguage": "en",
                                "productCode": "P-123",
                            },
                        )
                    ],
                    "response": [response],
                }
            )
        steps.append(
            {
                "request": [
                    (
                        PACKAGES_QUERY_ID,
                        {
                            "productCode": "P-123",
                            "travelDate": "2026-07-19",
                            "passengerMix": passenger_mix,
                            "currencies": ["USD"],
                            "locale": "en-US",
                        },
                    )
                ],
                "response": [
                    {
                        "data": {
                            "tourGrades": {
                                "resultStatus": "SUCCESS",
                                "result": {"tourGrades": []},
                            }
                        }
                    }
                ],
            }
        )
        page = ScriptedGraphQLPage(steps)
        queries = {}

        selection = _product_graphql_evidence(
            page,
            123,
            queries,
            datetime(2026, 7, 16, tzinfo=timezone.utc),
        )

        page.assert_drained()
        self.assertEqual(selection["selectedLanguage"], "en")
        self.assertEqual(
            selection["paxAttemptedDates"],
            ["2026-07-17", "2026-07-18", "2026-07-19"],
        )
        self.assertEqual(selection["travelDate"], "2026-07-19")
        self.assertEqual(selection["passengerMix"], passenger_mix)
        self.assertEqual(selection["packageOptionsStatus"], "UNKNOWN")
        self.assertEqual(
            selection["packageOptionsUnavailableReason"], "tour_grades_empty"
        )

    def test_rendered_package_fallback_requires_exact_product_identity(self):
        evidence = product_graphql_fixture()
        exact_url = evidence["sourceUrl"]

        def rendered(url):
            return (
                f'<html><head><title>Product - Tripadvisor</title>'
                f'<link rel="canonical" href="{url}"></head><body>'
                '<div data-automation="attractionsAboutContent">Concrete tour.</div>'
                '<button data-automation="inline-booking-date-picker">July 19, 2026</button>'
                '<button data-automation="inline-booking-pax-picker">2 adults</button>'
                '<div data-automation="availabilityTourGrades">'
                '<div data-automation="tourGrade-0">'
                '<div id="title-0-inline-booking-section">Evening option</div>'
                '<span id="detailed-total-price-0-inline-booking-section">'
                'Total price: $50.00 for 2 adults</span></div></div>'
                '</body></html>'
                + ("x" * (MIN_HTML_BYTES + 100))
            )

        accepted = rendered_html_pricing_fallback(
            evidence,
            rendered(exact_url),
            expected_url=exact_url,
            checked_at="2026-07-18",
        )
        wrong_url = listing("999", route="AttractionProductReview")["url"]
        rejected = rendered_html_pricing_fallback(
            evidence,
            rendered(wrong_url),
            expected_url=exact_url,
            checked_at="2026-07-18",
        )

        self.assertEqual(accepted["packages"][0]["name"], "Evening option")
        self.assertEqual(accepted["provenance"]["detailId"], 123)
        self.assertEqual(accepted["provenance"]["graphqlFailureReason"], "pax_failed")
        self.assertIsNone(rejected)

    def test_graphql_product_evidence_records_exact_queries_and_booking_choice(self):
        url = listing("123", route="AttractionProductReview")["url"]
        detail_variables = {
            "activityId": 123,
            "currency": "USD",
            "language": "en",
        }
        review_variables = {
            "locationId": 123,
            "limit": 10,
            "offset": 0,
            "filters": [],
            "sortType": "DEFAULT",
            "sortBy": "DATE",
            "language": "en",
            "doMachineTranslation": True,
        }
        calendar_variables = {"currency": "USD", "productCode": "P-123"}
        pax_variables = {
            "currencies": ["USD"],
            "locale": "en-US",
            "travelDate": "2026-07-18",
            "selectedLanguage": None,
            "productCode": "P-123",
        }
        passenger_mix = [{"bandId": "ADULT", "count": 2}]
        package_variables = {
            "productCode": "P-123",
            "travelDate": "2026-07-18",
            "passengerMix": passenger_mix,
            "currencies": ["USD"],
            "locale": "en-US",
        }
        detail_response = {
            "errors": [
                {
                    "message": "Optional operator field failed",
                    "path": ["fullProduct", 0, "aboutOperator"],
                }
            ],
            "data": {
                "fullProduct": [
                    {
                        "activityId": 123,
                        "productCode": "P-123",
                        "title": "Night tour",
                        "bookingConfirmationSettings": {
                            "bookingCutoffInHours": 0
                        },
                    }
                ]
            }
        }
        reviews_response = graphql_reviews(123)
        calendar_response = {
            "data": {
                "priceCalendar": [
                    {
                        "datesAndPrices": [
                            {"date": "2026-07-22", "price": 30.0},
                            {"date": "2026-07-15", "price": 20.0},
                            {"date": "2026-07-18", "price": 25.0},
                        ]
                    }
                ]
            }
        }
        cancellation_response = {
            "data": {
                "fullProduct": [
                    {
                        "activityId": 123,
                        "productCode": "P-123",
                        "cancellationPolicy": {"type": "STANDARD"},
                    }
                ]
            }
        }
        pax_pending = {"data": {"paxMix": {"resultStatus": "PENDING"}}}
        pax_success = {
            "data": {
                "paxMix": {
                    "resultStatus": "SUCCESS",
                    "result": {
                        "minTravelers": 1,
                        "maxTravelers": 8,
                        "ageBands": [
                            {
                                "id": "ADULT",
                                "title": "Adult",
                                "validWithAgeBands": [],
                                "travelerMinMax": {"numFrom": 1, "numTo": 8},
                            }
                        ],
                    },
                }
            }
        }
        packages_pending = {
            "data": {"tourGrades": {"resultStatus": "PENDING"}}
        }
        packages_success = {
            "data": {
                "tourGrades": {
                    "resultStatus": "SUCCESS",
                    "result": {
                        "tourGrades": [
                            {
                                "title": "Small group",
                                "price": {"amount": 50, "currency": "USD"},
                            }
                        ]
                    },
                }
            }
        }
        steps = [
            {
                "request": [
                    (PRODUCT_DETAIL_QUERY_ID, detail_variables),
                    (PRODUCT_REVIEWS_QUERY_ID, review_variables),
                ],
                "response": [detail_response, reviews_response],
            },
            {
                "request": [
                    (PRICE_CALENDAR_QUERY_ID, calendar_variables),
                    (CANCELLATION_QUERY_ID, calendar_variables),
                ],
                "response": [calendar_response, cancellation_response],
            },
            {
                "request": [(PAX_QUERY_ID, pax_variables)],
                "response": [pax_pending],
            },
            {
                "request": [(PAX_QUERY_ID, pax_variables)],
                "response": [pax_success],
            },
            {
                "request": [(PACKAGES_QUERY_ID, package_variables)],
                "response": [packages_pending],
            },
            {
                "request": [(PACKAGES_QUERY_ID, package_variables)],
                "response": [packages_success],
            },
        ]
        browser = ScriptedGraphQLBrowser(steps)

        evidence = render_graphql_evidence(
            browser,
            url,
            now=datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
        )

        browser.page.assert_drained()
        self.assertTrue(browser.page.closed)
        self.assertEqual(evidence["schemaVersion"], 1)
        self.assertEqual(evidence["transport"], "tripadvisor-browser-graphql")
        self.assertEqual(evidence["route"], "AttractionProductReview")
        self.assertEqual(evidence["detailId"], 123)
        self.assertEqual(evidence["sourceUrl"], url)
        self.assertEqual(evidence["checkedAt"], "2026-07-16T12:00:00Z")
        self.assertEqual(evidence["selection"]["travelDate"], "2026-07-18")
        self.assertEqual(evidence["selection"]["passengerMix"], passenger_mix)
        self.assertEqual(len(evidence["queries"]["pax"]), 2)
        self.assertEqual(len(evidence["queries"]["packages"]), 2)
        self.assertEqual(
            evidence["queries"]["packages"][-1]["evidenceResponse"],
            packages_success,
        )
        stored_review = evidence["queries"]["reviews"][0]["evidenceResponse"][
            "data"
        ]["ReviewsProxy_getReviewListPageForLocation"][0]["reviews"][0]
        self.assertEqual(stored_review["originalLanguage"], "hu")
        self.assertEqual(stored_review["language"], "en")
        self.assertEqual(stored_review["text"], "Translated review 1")
        self.assertNotIn("name", stored_review)
        self.assertNotIn("username", stored_review)
        self.assertNotIn("avatar", stored_review)
        self.assertNotIn("userProfile", stored_review)
        self.assertNotIn("photos", stored_review)
        self.assertNotIn("managementResponse", stored_review)
        serialized_reviews = json.dumps(evidence["queries"]["reviews"])
        self.assertNotIn("Private reviewer", serialized_reviews)
        self.assertNotIn("Private business responder", serialized_reviews)
        for attempts in evidence["queries"].values():
            for attempt in attempts:
                self.assertIn("persistedQueryId", attempt)
                self.assertIn("variables", attempt)
                self.assertIn("evidenceResponse", attempt)
                self.assertNotIn("rawResponse", attempt)
                self.assertEqual(attempt["httpStatus"], 200)

    def test_graphql_venue_evidence_uses_wps_and_review_queries_only(self):
        url = listing("456")["url"]
        tracking = {"screenName": "Attraction_Review", "pageviewUid": "page-1"}
        detail_variables = {
            "request": {
                "tracking": tracking,
                "routeParameters": {
                    "contentType": "attraction",
                    "contentId": "456",
                },
                "clientState": None,
                "updateToken": None,
            },
            "commerce": {},
            "sessionId": "session-1",
            "tracking": tracking,
            "currency": "USD",
            "currentGeoPoint": None,
            "unitLength": "KILOMETERS",
        }
        review_variables = {
            "locationId": 456,
            "filters": [],
            "limit": 10,
            "offset": 0,
            "sortType": "DEFAULT",
            "sortBy": "DATE",
            "language": "en",
            "doMachineTranslation": True,
            "photosPerReviewLimit": 7,
        }
        detail_response = {
            "data": {
                "Result": [
                    {
                        "status": {"pollingStatus": None},
                        "container": {
                            "jsonLd": json.dumps(
                                {
                                    "@id": "/Attraction_Review-g274887-d456-Reviews-Ordinary_Venue-Budapest.html"
                                }
                            )
                        },
                        "dataModel": {
                            "title": "Ordinary venue",
                            "description": "A real place with an unusual exhibition.",
                        },
                    }
                ]
            }
        }
        reviews_response = graphql_reviews(456, ("hu", "fr", "de"))
        returned_reviews = reviews_response["data"][
            "ReviewsProxy_getReviewListPageForLocation"
        ][0]["reviews"]
        returned_reviews[1]["locationId"] = 999
        returned_reviews[2].pop("locationId")
        browser = ScriptedGraphQLBrowser(
            [
                {
                    "request": [
                        (VENUE_DETAIL_QUERY_ID, detail_variables),
                        (VENUE_REVIEWS_QUERY_ID, review_variables),
                    ],
                    "response": [detail_response, reviews_response],
                }
            ]
        )

        evidence = render_graphql_evidence(
            browser,
            url,
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            session_id="session-1",
            pageview_uid="page-1",
        )

        browser.page.assert_drained()
        self.assertTrue(browser.page.closed)
        self.assertEqual(evidence["route"], "Attraction_Review")
        self.assertEqual(set(evidence["queries"]), {"detail", "reviews"})
        self.assertEqual(
            evidence["queries"]["detail"][0]["persistedQueryId"],
            VENUE_DETAIL_QUERY_ID,
        )
        self.assertEqual(
            evidence["queries"]["reviews"][0]["variables"], review_variables
        )
        self.assertEqual(
            evidence["reviewSelection"],
            {
                "policy": "exact-location-id-only",
                "requestedLocationId": 456,
                "sourceTotalCount": 3,
                "returnedCount": 3,
                "acceptedCount": 1,
                "quarantinedCount": 2,
                "missingLocationIdCount": 1,
                "rejectedLocationIds": ["999"],
            },
        )
        stored_review_block = evidence["queries"]["reviews"][0][
            "evidenceResponse"
        ]["data"]["ReviewsProxy_getReviewListPageForLocation"][0]
        self.assertEqual(stored_review_block["totalCount"], 1)
        self.assertEqual(
            [review["locationId"] for review in stored_review_block["reviews"]],
            [456],
        )
        serialized = json.dumps(evidence["queries"]["reviews"])
        self.assertIn("Translated review 1", serialized)
        self.assertNotIn("Translated review 2", serialized)
        self.assertNotIn("Translated review 3", serialized)
        self.assertNotIn("selection", evidence)

    def test_graphql_evidence_fails_closed_on_protocol_and_identity_errors(self):
        url = listing("123", route="AttractionProductReview")["url"]
        variables = {
            "activityId": 123,
            "currency": "USD",
            "language": "en",
        }
        review_variables = {
            "locationId": 123,
            "limit": 10,
            "offset": 0,
            "filters": [],
            "sortType": "DEFAULT",
            "sortBy": "DATE",
            "language": "en",
            "doMachineTranslation": True,
        }
        cases = [
            (
                "wrong product",
                [
                    {
                        "data": {
                            "fullProduct": [
                                {"activityId": 999, "productCode": None}
                            ]
                        }
                    },
                    graphql_reviews(123),
                ],
                {},
                "product identity mismatch",
            ),
            (
                "wrong review",
                [
                    {"data": {"fullProduct": [{"activityId": 123}]}},
                    graphql_reviews(999),
                ],
                {},
                "reviews identity mismatch",
            ),
            (
                "graphql error",
                [
                    {"errors": [{"message": "bad query"}]},
                    graphql_reviews(123),
                ],
                {},
                "GraphQL error",
            ),
            (
                "http error",
                [
                    {"data": {"fullProduct": [{"activityId": 123}]}},
                    graphql_reviews(123),
                ],
                {"ok": False, "status": 429},
                "HTTP 429",
            ),
        ]
        for label, response, metadata, message in cases:
            with self.subTest(label=label):
                browser = ScriptedGraphQLBrowser(
                    [
                        {
                            "request": [
                                (PRODUCT_DETAIL_QUERY_ID, variables),
                                (PRODUCT_REVIEWS_QUERY_ID, review_variables),
                            ],
                            "response": response,
                            **metadata,
                        }
                    ]
                )
                with self.assertRaisesRegex(GraphQLEvidenceError, message):
                    render_graphql_evidence(browser, url)
                self.assertTrue(browser.page.closed)

    def test_legacy_venue_review_evidence_can_be_quarantined_offline(self):
        url = listing("456")["url"]
        tracking = {"screenName": "Attraction_Review", "pageviewUid": "page-1"}
        legacy = {
            "schemaVersion": 1,
            "transport": "tripadvisor-browser-graphql",
            "route": "Attraction_Review",
            "detailId": 456,
            "sourceUrl": url,
            "checkedAt": "2026-07-16T00:00:00Z",
            "queries": {
                "detail": [
                    {
                        "attempt": 1,
                        "persistedQueryId": VENUE_DETAIL_QUERY_ID,
                        "variables": {
                            "request": {
                                "tracking": tracking,
                                "routeParameters": {
                                    "contentType": "attraction",
                                    "contentId": "456",
                                },
                                "clientState": None,
                                "updateToken": None,
                            },
                            "commerce": {},
                            "sessionId": "session-1",
                            "tracking": tracking,
                            "currency": "USD",
                            "currentGeoPoint": None,
                            "unitLength": "KILOMETERS",
                        },
                        "httpStatus": 200,
                        "evidenceResponse": {
                            "data": {
                                "Result": [
                                    {
                                        "dataModel": {
                                            "locationId": 456,
                                            "title": "Venue 456",
                                        }
                                    }
                                ]
                            }
                        },
                    }
                ],
                "reviews": [
                    {
                        "attempt": 1,
                        "persistedQueryId": VENUE_REVIEWS_QUERY_ID,
                        "variables": {
                            "locationId": 456,
                            "filters": [],
                            "limit": 10,
                            "offset": 0,
                            "sortType": "DEFAULT",
                            "sortBy": "DATE",
                            "language": "en",
                            "doMachineTranslation": True,
                            "photosPerReviewLimit": 7,
                        },
                        "httpStatus": 200,
                        "evidenceResponse": {
                            "data": {
                                "ReviewsProxy_getReviewListPageForLocation": [
                                    {
                                        "totalCount": 3,
                                        "reviews": [
                                            {
                                                "id": "exact",
                                                "locationId": 456,
                                                "text": "Exact body",
                                            },
                                            {
                                                "id": "foreign",
                                                "locationId": 999,
                                                "text": "Foreign body",
                                            },
                                            {
                                                "id": "unknown",
                                                "text": "Identity-less body",
                                            },
                                        ],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }

        migrated, changed = quarantine_venue_review_evidence(legacy)
        selection = migrated["reviewSelection"]
        block = migrated["queries"]["reviews"][0]["evidenceResponse"]["data"][
            "ReviewsProxy_getReviewListPageForLocation"
        ][0]
        migrated_again, changed_again = quarantine_venue_review_evidence(migrated)

        self.assertTrue(changed)
        self.assertEqual(block["totalCount"], 1)
        self.assertEqual([review["id"] for review in block["reviews"]], ["exact"])
        self.assertEqual(selection["quarantinedCount"], 2)
        self.assertEqual(selection["missingLocationIdCount"], 1)
        self.assertEqual(selection["rejectedLocationIds"], ["999"])
        self.assertIs(migrated_again, migrated)
        self.assertFalse(changed_again)

    def test_graphql_pax_polling_is_bounded_and_never_requests_packages(self):
        url = listing("123", route="AttractionProductReview")["url"]
        detail_variables = {
            "activityId": 123,
            "currency": "USD",
            "language": "en",
        }
        review_variables = {
            "locationId": 123,
            "limit": 10,
            "offset": 0,
            "filters": [],
            "sortType": "DEFAULT",
            "sortBy": "DATE",
            "language": "en",
            "doMachineTranslation": True,
        }
        calendar_variables = {"currency": "USD", "productCode": "P-123"}
        pax_variables = {
            "currencies": ["USD"],
            "locale": "en-US",
            "travelDate": "2026-07-18",
            "selectedLanguage": None,
            "productCode": "P-123",
        }
        detail = {
            "data": {
                "fullProduct": [
                    {
                        "activityId": 123,
                        "productCode": "P-123",
                        "bookingConfirmationSettings": {
                            "bookingCutoffInHours": 0
                        },
                    }
                ]
            }
        }
        calendar = {
            "data": {
                "priceCalendar": [
                    {"datesAndPrices": [{"date": "2026-07-18", "price": 20}]}
                ]
            }
        }
        cancellation = {"data": {"fullProduct": [{"activityId": 123}]}}
        pending = {"data": {"paxMix": {"resultStatus": "PENDING"}}}
        steps = [
            {
                "request": [
                    (PRODUCT_DETAIL_QUERY_ID, detail_variables),
                    (PRODUCT_REVIEWS_QUERY_ID, review_variables),
                ],
                "response": [detail, graphql_reviews(123)],
            },
            {
                "request": [
                    (PRICE_CALENDAR_QUERY_ID, calendar_variables),
                    (CANCELLATION_QUERY_ID, calendar_variables),
                ],
                "response": [calendar, cancellation],
            },
            *[
                {
                    "request": [(PAX_QUERY_ID, pax_variables)],
                    "response": [pending],
                }
                for _ in range(3)
            ],
        ]
        browser = ScriptedGraphQLBrowser(steps)

        with self.assertRaisesRegex(GraphQLEvidenceError, "after 3 attempts"):
            render_graphql_evidence(
                browser,
                url,
                now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            )

        browser.page.assert_drained()
        self.assertEqual(browser.page.waits, [500, 500])
        requested_ids = [
            item["extensions"]["preRegisteredQueryId"]
            for call in browser.page.calls
            for item in call
        ]
        self.assertEqual(requested_ids.count(PAX_QUERY_ID), 3)
        self.assertNotIn(PACKAGES_QUERY_ID, requested_ids)
        self.assertTrue(browser.page.closed)

    def test_request_mode_parser_and_graphql_server_output(self):
        self.assertEqual(request_mode({}), "html")
        self.assertEqual(request_mode({"mode": "html"}), "html")
        self.assertEqual(request_mode({"mode": "graphql"}), "graphql")
        for invalid in (None, "GraphQL", "json", 1):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "unsupported request mode"):
                    request_mode({"mode": invalid})

        evidence = {
            "schemaVersion": 1,
            "transport": "tripadvisor-browser-graphql",
            "route": "Attraction_Review",
            "detailId": 456,
            "sourceUrl": listing("456")["url"],
            "checkedAt": "2026-07-16T00:00:00Z",
            "queries": {},
        }
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence.json"
            input_stream = StringIO(
                json.dumps(
                    {
                        "id": 7,
                        "mode": "graphql",
                        "url": listing("456")["url"],
                        "output": str(output),
                    }
                )
                + "\n"
            )
            output_stream = StringIO()
            browser = object()
            with patch(
                "fetch_ta_detail.render_graphql_evidence", return_value=evidence
            ) as collector:
                self.assertEqual(
                    serve_requests(browser, input_stream, output_stream), 0
                )

            collector.assert_called_once_with(browser, listing("456")["url"], 0)
            response = json.loads(output_stream.getvalue())
            self.assertEqual(response["id"], 7)
            self.assertTrue(response["ok"])
            self.assertEqual(response["mode"], "graphql")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), evidence)

    def test_graphql_cli_prints_evidence_with_zero_wait_by_default(self):
        url = listing("456")["url"]
        evidence = {
            "schemaVersion": 1,
            "transport": "tripadvisor-browser-graphql",
            "route": "Attraction_Review",
            "detailId": 456,
            "sourceUrl": url,
            "checkedAt": "2026-07-16T00:00:00Z",
            "queries": {},
        }
        browser = object()

        class BrowserContext:
            def __enter__(self):
                return browser

            def __exit__(self, *_args):
                pass

        output = StringIO()
        with (
            patch("fetch_ta_detail.Camoufox", return_value=BrowserContext()) as launch,
            patch(
                "fetch_ta_detail.render_graphql_evidence", return_value=evidence
            ) as collector,
            patch("sys.stdout", output),
        ):
            self.assertEqual(fetch_detail_main(["graphql", url]), 0)

        launch.assert_called_once_with(headless=True)
        collector.assert_called_once_with(browser, url, 0)
        self.assertEqual(json.loads(output.getvalue()), evidence)

    def test_server_reuses_browser_but_creates_and_closes_a_page_per_request(self):
        class EmptyLocator:
            def count(self):
                return 0

        class FakePage:
            def __init__(self):
                self.url = ""
                self.waits = []
                self.closed = False

            def on(self, *_args):
                pass

            def goto(self, url, timeout):
                self.url = url
                self.timeout = timeout

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

            def get_by_role(self, *_args, **_kwargs):
                return EmptyLocator()

            def locator(self, *_args, **_kwargs):
                return EmptyLocator()

            def content(self):
                return f"<html><body>{self.url}</body></html>"

            def close(self):
                self.closed = True

        class FakeBrowser:
            def __init__(self):
                self.pages = []

            def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

        with TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.html"
            second = Path(tmp) / "second.html"
            requests = [
                {
                    "id": 1,
                    "url": listing("1")["url"],
                    "output": str(first),
                    "wait": 2,
                },
                {
                    "id": 2,
                    "url": listing("2")["url"],
                    "output": str(second),
                    "wait": 3,
                },
                {"id": 3, "cmd": "close"},
            ]
            input_stream = StringIO("".join(json.dumps(row) + "\n" for row in requests))
            output_stream = StringIO()
            browser = FakeBrowser()

            self.assertEqual(serve_requests(browser, input_stream, output_stream), 0)

            responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
            self.assertEqual([row["id"] for row in responses], [1, 2, 3])
            self.assertTrue(all(row["ok"] for row in responses))
            self.assertEqual(len(browser.pages), 2)
            self.assertEqual([page.waits for page in browser.pages], [[2000], [3000]])
            self.assertTrue(all(page.closed for page in browser.pages))
            self.assertIn("d1-", first.read_text(encoding="utf-8"))
            self.assertIn("d2-", second.read_text(encoding="utf-8"))

    def test_runner_sends_multiple_fetches_through_one_server_process(self):
        class QueueOutput:
            def __init__(self):
                self.lines = []
                self.closed = False

            def readline(self):
                return self.lines.pop(0) if self.lines else ""

            def close(self):
                self.closed = True

        class FakeInput:
            def __init__(self, process):
                self.process = process
                self.pending = ""
                self.closed = False

            def write(self, value):
                self.pending += value

            def flush(self):
                request = json.loads(self.pending.strip())
                self.pending = ""
                self.process.requests.append(request)
                Path(request["output"]).write_text(
                    f"rendered {request['url']}", encoding="utf-8"
                )
                self.process.stdout.lines.append(
                    json.dumps({"id": request["id"], "ok": True}) + "\n"
                )

            def close(self):
                self.closed = True

        class FakeProcess:
            def __init__(self):
                self.pid = 1234
                self.returncode = None
                self.requests = []
                self.stdout = QueueOutput()
                self.stderr = StringIO("")
                self.stdin = FakeInput(self)

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.returncode = 0
                return 0

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        created = []

        def fake_popen(*_args, **_kwargs):
            process = FakeProcess()
            created.append(process)
            return process

        selector = lambda readers, _writes, _errors, _timeout: (readers, [], [])
        runner = PersistentCamoufoxRunner(popen=fake_popen, selector=selector)
        try:
            with TemporaryDirectory() as tmp:
                outputs = [Path(tmp) / "one.html", Path(tmp) / "two.html"]
                modes = ["html", "graphql"]
                for index, (mode, output) in enumerate(zip(modes, outputs), 1):
                    with output.open("w", encoding="utf-8") as handle:
                        result = runner(
                            ["python", "fetch_ta_detail.py", mode, listing(str(index))["url"], "--wait", "4"],
                            stdout=handle,
                            timeout=10,
                        )
                    self.assertEqual(result.returncode, 0)
                self.assertEqual(len(created), 1)
                self.assertEqual(len(created[0].requests), 2)
                self.assertEqual(
                    [request["wait"] for request in created[0].requests], [4, 4]
                )
                self.assertEqual(
                    [request["mode"] for request in created[0].requests], modes
                )
                self.assertTrue(all(path.read_text().startswith("rendered") for path in outputs))
        finally:
            runner.close()

    def test_runner_pool_is_sticky_per_worker_and_closes_every_runner(self):
        created = []
        created_lock = Lock()

        class DummyRunner:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        def factory():
            runner = DummyRunner()
            with created_lock:
                created.append(runner)
            return runner

        pool = PersistentCamoufoxRunnerPool(runner_factory=factory)
        barrier = Barrier(2)

        def use_runner():
            first = pool.runner()
            barrier.wait(timeout=2)
            return first, pool.runner()

        with ThreadPoolExecutor(max_workers=2) as executor:
            pairs = list(executor.map(lambda _index: use_runner(), range(2)))
        self.assertEqual(len(created), 2)
        self.assertTrue(all(first is second for first, second in pairs))
        self.assertIsNot(pairs[0][0], pairs[1][0])
        pool.close()
        self.assertTrue(all(runner.closed for runner in created))


class SelectionAndContextTests(unittest.TestCase):
    def test_single_instance_lock_rejects_a_second_writer(self):
        with TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".scrape-budapest.lock"
            with single_instance_lock(lock_path):
                with self.assertRaisesRegex(SingleInstanceError, "another crawler"):
                    with single_instance_lock(lock_path):
                        self.fail("a second writer unexpectedly acquired the lock")

            with single_instance_lock(lock_path):
                self.assertEqual(
                    lock_path.read_text(encoding="utf-8").strip(), str(os.getpid())
                )

    def test_city_specific_writer_locks_can_coexist(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with single_instance_lock(root / ".scrape-budapest.lock"):
                with single_instance_lock(root / ".scrape-london.lock"):
                    pass

    def test_checked_at_uses_cache_mtime_but_live_fetch_date(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "detail.html"
            destination.write_text(valid_cached_html(), encoding="utf-8")
            cached_at = datetime(2026, 7, 15, 12, 0).timestamp()
            os.utime(destination, (cached_at, cached_at))
            self.assertEqual(
                evidence_checked_at(destination, False, today=date(2026, 7, 16)),
                "2026-07-15",
            )
            self.assertEqual(
                evidence_checked_at(destination, True, today=date(2026, 7, 16)),
                "2026-07-16",
            )

    def test_id_targeting_preserves_request_order_then_applies_limit(self):
        rows = [listing("1"), listing("2"), listing("3")]
        selected = select_listings(rows, ["d3", "1"], limit=1)
        self.assertEqual(selected, [rows[2]])
        self.assertEqual(normalize_requested_id(rows[1]["url"]), "2")

    def test_missing_requested_id_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "999"):
            select_listings([listing("1")], ["999"])

    def test_route_qualified_id_selects_the_right_collision(self):
        venue = listing("123", route="Attraction_Review", name="Venue")
        product = listing("123", route="AttractionProductReview", name="Product")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            select_listings([venue, product], ["123"])
        self.assertEqual(
            select_listings(
                [venue, product], ["AttractionProductReview:123"]
            ),
            [product],
        )

    def test_cache_name_is_stable_across_slug_changes(self):
        first = listing("123")
        second = {**first, "url": first["url"].replace("Place_123", "Renamed")}
        self.assertEqual(detail_cache_path(first), detail_cache_path(second))

    def test_context_merge_replaces_existing_identity_without_duplicates(self):
        old = [{"key": "Attraction_Review:1", "description": "old"}]
        new = [{"key": "Attraction_Review:1", "description": "new"}]
        merged = merge_context_rows(old, new)
        self.assertEqual(merged, new)

    def test_context_contains_description_and_review_evidence(self):
        parsed = {
            "description": "What it is.",
            "description_source": "tripadvisor_about",
            "reviews": [{"title": "Good", "text": "Why go.", "rating": 5.0}],
        }
        context = build_context(listing("1"), parsed)
        self.assertEqual(context["description"], "What it is.")
        self.assertEqual(context["reviews"][0]["text"], "Why go.")

    def test_context_uses_bound_page_title_for_truncated_the_name(self):
        source = listing("1")
        source["name"] = "The"
        parsed = {
            "page_title": 'The "Puszta" Horse Show',
            "description": "A horse show.",
            "description_source": "tripadvisor_graphql_product",
            "reviews": [],
        }
        context = build_context(source, parsed)
        self.assertEqual(context["name"], "The Puszta Horse Show")

    def test_cli_defaults_to_budapest_and_accepts_repeatable_ids(self):
        args = parse_args(
            [
                "--id",
                "123",
                "--id",
                "d456",
                "--limit",
                "2",
                "--workers",
                "2",
                "--block-cooldown",
                "0.01",
            ]
        )
        self.assertEqual(args.city, "budapest")
        self.assertEqual(args.id, ["123", "d456"])
        self.assertEqual(args.limit, 2)
        self.assertEqual(args.workers, 2)
        self.assertEqual(args.block_cooldown, 0.01)
        self.assertFalse(args.fresh_browser_per_page)

    def test_cli_can_fall_back_to_a_fresh_browser_per_page(self):
        args = parse_args(["--id", "123", "--fresh-browser-per-page"])
        self.assertTrue(args.fresh_browser_per_page)

    def test_cli_explicitly_selects_graphql_transport(self):
        self.assertTrue(parse_args(["--id", "123", "--graphql"]).graphql)

    def test_cli_rejects_nonfinite_block_cooldowns(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args(["--id", "123", "--block-cooldown", value])


class GraphQLCallerIntegrationTests(unittest.TestCase):
    def test_venue_nav_title_and_token_wrapped_about_are_decoded(self):
        url = listing("456")["url"]
        encoded_about = base64.b64encode(
            b"uus_Smooth, quality music and an amazing sound._4Pi"
        ).decode("ascii")
        tracking = {"screenName": "Attraction_Review", "pageviewUid": "page-1"}

        def attempt(query_id, variables, response):
            return {
                "attempt": 1,
                "persistedQueryId": query_id,
                "variables": variables,
                "httpStatus": 200,
                "evidenceResponse": response,
            }

        evidence = {
            "schemaVersion": 1,
            "transport": "tripadvisor-browser-graphql",
            "route": "Attraction_Review",
            "detailId": 456,
            "sourceUrl": url,
            "checkedAt": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "reviewSelection": {
                "policy": "exact-location-id-only",
                "requestedLocationId": 456,
                "sourceTotalCount": 0,
                "returnedCount": 0,
                "acceptedCount": 0,
                "quarantinedCount": 0,
                "missingLocationIdCount": 0,
                "rejectedLocationIds": [],
            },
            "queries": {
                "detail": [
                    attempt(
                        VENUE_DETAIL_QUERY_ID,
                        {
                            "request": {
                                "tracking": tracking,
                                "routeParameters": {
                                    "contentType": "attraction",
                                    "contentId": "456",
                                },
                                "clientState": None,
                                "updateToken": None,
                            },
                            "commerce": {},
                            "sessionId": "session-1",
                            "tracking": tracking,
                            "currency": "USD",
                            "currentGeoPoint": None,
                            "unitLength": "KILOMETERS",
                        },
                        {
                            "data": {
                                "Result": [
                                    {
                                        "container": {"navTitle": "Budapest Jazz Club"},
                                        "sections": [
                                            {
                                                "about": {
                                                    "primary": {"about": encoded_about}
                                                }
                                            }
                                        ],
                                        "dataModel": {
                                            "attractionDetails": {"locationId": 456}
                                        },
                                    }
                                ]
                            }
                        },
                    )
                ],
                "reviews": [
                    attempt(
                        VENUE_REVIEWS_QUERY_ID,
                        {
                            "locationId": 456,
                            "filters": [],
                            "limit": 10,
                            "offset": 0,
                            "sortType": "DEFAULT",
                            "sortBy": "DATE",
                            "language": "en",
                            "doMachineTranslation": True,
                            "photosPerReviewLimit": 7,
                        },
                        {
                            "data": {
                                "ReviewsProxy_getReviewListPageForLocation": [
                                    {"totalCount": 0, "reviews": []}
                                ]
                            }
                        },
                    )
                ],
            },
        }

        parsed = parse_graphql_evidence(evidence, expected_url=url)

        self.assertEqual(parsed["page_title"], "Budapest Jazz Club")
        self.assertEqual(
            parsed["description"], "Smooth, quality music and an amazing sound."
        )
        self.assertEqual(parsed["description_source"], "tripadvisor_graphql_wps")

        evidence["queries"]["detail"][0]["evidenceResponse"]["data"]["Result"][0][
            "sections"
        ] = []
        parsed_without_about = parse_graphql_evidence(evidence, expected_url=url)
        self.assertEqual(parsed_without_about["description"], "")
        self.assertEqual(
            parsed_without_about["description_source"], "tripadvisor_graphql_wps"
        )

    def test_live_shaped_cancellation_and_failed_pax_keep_advertised_from_price(self):
        evidence = product_graphql_fixture()

        metadata = validate_graphql_evidence(
            evidence, expected_url=evidence["sourceUrl"]
        )
        parsed = parse_graphql_evidence(
            evidence, expected_url=evidence["sourceUrl"]
        )

        self.assertEqual(metadata["review_total"], 2)
        self.assertEqual(parsed["description_source"], "tripadvisor_graphql_product")
        self.assertEqual(len(parsed["reviews"]), 2)
        pricing = parsed["pricing_evidence"]
        self.assertEqual(pricing["base_price"], "USD 19.86")
        self.assertEqual(pricing["booking_date"], "2026-07-18")
        self.assertNotIn("status", pricing)
        self.assertEqual(pricing["availability"]["status"], "unknown")
        self.assertEqual(pricing["availability"]["reason"], "pax_failed")
        self.assertIn("not confirmed", pricing["availability"]["message"])
        self.assertEqual(pricing["availability"]["source"], "graphql:paxMix")
        self.assertIn("starting price", pricing["note"])

    def test_successful_packages_are_deterministic_and_include_cancellation_context(self):
        evidence = product_graphql_fixture(pax_failed=False)

        parsed = parse_graphql_evidence(
            evidence, expected_url=evidence["sourceUrl"]
        )

        pricing = parsed["pricing_evidence"]
        self.assertEqual(pricing["status"], "available")
        self.assertEqual(pricing["travelers"], "2 adults")
        self.assertEqual(pricing["packages"][0]["name"], "Small group")
        self.assertEqual(pricing["packages"][0]["total_price"], "USD 50.00")
        self.assertIn("STANDARD", pricing["packages"][0]["description"])

    def test_failed_tour_grades_leave_availability_unknown_with_source_context(self):
        evidence = product_graphql_fixture(pax_failed=False)
        evidence["queries"]["packages"][0]["evidenceResponse"]["data"][
            "tourGrades"
        ] = {"resultStatus": "FAILED", "result": None}
        evidence["selection"]["packageOptionsStatus"] = "UNKNOWN"
        evidence["selection"][
            "packageOptionsUnavailableReason"
        ] = "tour_grades_failed"

        parsed = parse_graphql_evidence(
            evidence, expected_url=evidence["sourceUrl"]
        )

        pricing = parsed["pricing_evidence"]
        self.assertEqual(pricing["base_price"], "USD 19.86")
        self.assertNotIn("status", pricing)
        self.assertEqual(pricing["availability"]["status"], "unknown")
        self.assertEqual(
            pricing["availability"]["reason"], "tour_grades_failed"
        )
        self.assertEqual(
            pricing["availability"]["source"], "graphql:tourGrades"
        )
        self.assertIn("not confirmed", pricing["availability"]["message"])

    def test_empty_successful_tour_grades_leave_availability_unknown(self):
        evidence = product_graphql_fixture(pax_failed=False, packages=[])
        evidence["selection"]["packageOptionsStatus"] = "UNKNOWN"
        evidence["selection"][
            "packageOptionsUnavailableReason"
        ] = "tour_grades_empty"

        parsed = parse_graphql_evidence(
            evidence, expected_url=evidence["sourceUrl"]
        )

        pricing = parsed["pricing_evidence"]
        self.assertEqual(pricing["availability"]["status"], "unknown")
        self.assertEqual(
            pricing["availability"]["reason"], "tour_grades_empty"
        )
        self.assertEqual(pricing["packages"], [])
        self.assertIn("no package rows", pricing["availability"]["message"])

    def test_unhashable_pax_travel_date_fails_closed_as_evidence_error(self):
        evidence = product_graphql_fixture()
        evidence["queries"]["pax"][0]["variables"]["travelDate"] = {
            "invalid": "object"
        }

        with self.assertRaisesRegex(
            CacheGraphQLEvidenceError, "travelDate must be text"
        ):
            validate_graphql_evidence(
                evidence, expected_url=evidence["sourceUrl"]
            )

    def test_graphql_cache_is_route_qualified_and_uses_embedded_checked_at(self):
        evidence = product_graphql_fixture()
        with TemporaryDirectory() as tmp:
            path = detail_graphql_cache_path(
                {"url": evidence["sourceUrl"]}, raw_dir=Path(tmp)
            )
            path.write_text(json.dumps(evidence), encoding="utf-8")
            self.assertTrue(graphql_cache_looks_valid(path, evidence["sourceUrl"]))
            self.assertTrue(path.name.endswith(".graphql.json"))
            future = datetime(2035, 1, 2, tzinfo=timezone.utc).timestamp()
            os.utime(path, (future, future))
            self.assertTrue(
                graphql_cache_looks_valid(path, evidence["sourceUrl"]),
                "copying or touching valid evidence on another date must not force a fetch",
            )

            evidence["queries"]["reviews"][0]["evidenceResponse"]["data"][
                "ReviewsProxy_getReviewListPageForLocation"
            ][0]["reviews"][0]["username"] = "private"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            self.assertFalse(graphql_cache_looks_valid(path, evidence["sourceUrl"]))

    def test_graphql_fetch_promotes_only_valid_complete_artifacts(self):
        evidence = product_graphql_fixture()

        class Runner:
            def __init__(self, payload):
                self.payload = payload
                self.commands = []
                self.resets = 0

            def __call__(self, command, stdout, **_kwargs):
                self.commands.append(command)
                stdout.write(json.dumps(self.payload))
                stdout.flush()
                return subprocess.CompletedProcess(command, 0, stderr="")

            def reset(self):
                self.resets += 1

        with TemporaryDirectory() as tmp:
            destination = detail_graphql_cache_path(
                {"url": evidence["sourceUrl"]}, raw_dir=Path(tmp)
            )
            good_runner = Runner(evidence)
            status, ok, fetched = fetch_detail(
                evidence["sourceUrl"],
                destination,
                "123",
                runner=good_runner,
                sleeper=lambda _seconds: None,
                transport="graphql",
            )
            self.assertEqual(status, "fetched")
            self.assertTrue(ok)
            self.assertTrue(fetched)
            self.assertIn("graphql", good_runner.commands[0])
            self.assertEqual(
                good_runner.commands[0][good_runner.commands[0].index("--wait") + 1],
                "0",
            )
            original = destination.read_bytes()

            invalid = copy_json(evidence)
            invalid["queries"]["reviews"][0]["variables"]["filters"] = ["en"]
            bad_runner = Runner(invalid)
            status, ok, fetched = fetch_detail(
                evidence["sourceUrl"],
                destination,
                "123",
                refresh=True,
                runner=bad_runner,
                sleeper=lambda _seconds: None,
                transport="graphql",
            )
            self.assertFalse(ok)
            self.assertTrue(fetched)
            self.assertIn("FAIL", status)
            self.assertEqual(len(bad_runner.commands), 3)
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(destination.with_name(destination.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
