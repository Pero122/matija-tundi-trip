import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from audit_detail_context import (  # noqa: E402
    audit_detail_context,
    load_visible_inventory,
)
from scrape_ta_details import (  # noqa: E402
    build_context,
    detail_cache_path,
    detail_graphql_cache_path,
    parse_detail_html,
    parse_graphql_evidence,
    rendered_html_pricing_fallback,
)


def visible_item(
    listing_id="123",
    *,
    route="AttractionProductReview",
    review_count=1,
    name=None,
):
    url = (
        f"https://www.tripadvisor.com/{route}-g274887-d{listing_id}-"
        f"Reviews-Place_{listing_id}-Budapest.html"
    )
    return {
        "key": f"{route}:{listing_id}",
        "name": name or f"Place {listing_id}",
        "category": "Tours",
        "subtype": "Walking Tours",
        "rating": 4.8,
        "reviewCount": review_count,
        "url": url,
        "type": "experience",
        "group": "tours",
    }


def review_card(number=1, *, rating=5, title=None, body=None):
    return f"""
      <div data-automation="reviewCard">
        <svg data-automation="bubbleRatingImage"><title>{rating} of 5 bubbles</title></svg>
        <a data-automation="review-title"
           href="/ShowUserReviews-g274887-d123-r{number}-Example.html">
          <span lang="hu">{title or f'Cím {number}'}</span>
        </a>
        <div data-automation="reviewText">{body or f'Hasznos részlet {number}.'}</div>
      </div>
    """


def rendered_page(
    item,
    *,
    reviews=None,
    live_total=None,
    filters=0,
    language="All languages (1)",
    package_name="Standard option",
    package_total="$25.00",
    package_suffix="",
):
    reviews = reviews if reviews is not None else [review_card()]
    total_markup = (
        f"<button><span>All reviews<!-- --> (<!-- -->{live_total}<!-- -->)</span></button>"
        if live_total is not None
        else ""
    )
    filter_markup = (
        f"<button><span>Filters<!-- --> (<!-- -->{filters}<!-- -->)</span></button>"
        if filters
        else ""
    )
    product_language = (
        '<div data-automation="apr-reviews">'
        f'<button aria-haspopup="listbox" aria-label="language: {language}">Language</button>'
        "</div>"
        if language
        else ""
    )
    package = (
        '<div data-automation="availabilityTourGrades">'
        '<div data-automation="tourGrade-0">'
        f'<div id="title-0-inline-booking-section">{package_name}</div>'
        '<span id="detailed-total-price-0-inline-booking-section">'
        f"Total price: {package_total}{package_suffix}</span>"
        "</div></div>"
    )
    return f"""
      <html><head><title>{item['name']} - Tripadvisor</title>
      <link rel="canonical" href="{item['url']}"></head><body>
      <div data-automation="attractionsAboutContent">
        Árvíztűrő tükörfúrógép: multilingual evidence remains intact.
      </div>
      {total_markup}{filter_markup}{product_language}
      {''.join(reviews)}
      {package}
      </body></html>
    """


def rendered_fallback_page(item, *, canonical_url=None):
    page = rendered_page(item, live_total=1).replace(
        '<div data-automation="availabilityTourGrades">',
        '<button data-automation="inline-booking-date-picker">July 19, 2026</button>'
        '<button data-automation="inline-booking-pax-picker">2 adults</button>'
        '<div data-automation="availabilityTourGrades">',
    )
    if canonical_url is not None:
        page = page.replace(item["url"], canonical_url)
    return page + ("x" * 31_000)


def source_listing(item):
    return {
        "name": item["name"],
        "url": item["url"],
        "city": "budapest",
        "rating": item["rating"],
        "reviews": item["reviewCount"],
        "catLabel": item["category"],
        "subtype": item["subtype"],
    }


def write_fixture(raw_dir, item, html):
    path = detail_cache_path({"url": item["url"]}, raw_dir=raw_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    checked_at = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    parsed = parse_detail_html(html, listing_id=item["key"].split(":", 1)[1])
    return build_context(source_listing(item), parsed, checked_at=checked_at)


def graphql_product_evidence(item, *, pax_failed=False):
    listing_id = int(item["key"].split(":", 1)[1])
    product_code = f"P-{listing_id}"
    title = item["name"]

    def attempt(query_id, variables, response):
        return {
            "attempt": 1,
            "persistedQueryId": query_id,
            "variables": variables,
            "httpStatus": 200,
            "evidenceResponse": response,
        }

    passenger_mix = None if pax_failed else [{"bandId": "ADULT", "count": 2}]
    queries = {
        "detail": [
            attempt(
                "f7a273b890edf6c2",
                {"activityId": listing_id, "currency": "USD", "language": "en"},
                {
                    "errors": [
                        {
                            "message": "Optional operator field failed",
                            "path": ["fullProduct", 0, "aboutOperator"],
                        }
                    ],
                    "data": {
                        "fullProduct": [
                            {
                                "activityId": listing_id,
                                "productCode": product_code,
                                "title": {"text": title},
                                "description": {
                                    "text": "A real activity with concrete things to do."
                                },
                            }
                        ]
                    }
                },
            )
        ],
        "reviews": [
            attempt(
                "8793c5d897e589a1",
                {
                    "locationId": listing_id,
                    "filters": [],
                    "limit": 10,
                    "offset": 0,
                    "sortType": "DEFAULT",
                    "sortBy": "DATE",
                    "language": "en",
                    "doMachineTranslation": True,
                },
                {
                    "data": {
                        "ReviewsProxy_getReviewListPageForLocation": [
                            {
                                "totalCount": 1,
                                "reviews": [
                                    {
                                        "id": "r1",
                                        "locationId": listing_id,
                                        "title": "Worth doing",
                                        "text": "Translated detail from a Hungarian review.",
                                        "rating": 5,
                                        "language": "en",
                                        "originalLanguage": "hu",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        ],
        "priceCalendar": [
            attempt(
                "eb4cf849c5286ed5",
                {"currency": "USD", "productCode": product_code},
                {
                    "data": {
                        "priceCalendar": [
                            {
                                "datesAndPrices": [
                                    {"date": "2026-07-18", "price": 25}
                                ]
                            }
                        ]
                    }
                },
            )
        ],
        "cancellation": [
            attempt(
                "471725aa61475779",
                {"currency": "USD", "productCode": product_code},
                {
                    "data": {
                        "fullProduct": [
                            {
                                "title": {"text": title},
                                "cancellationConditions": {
                                    "cancellationPolicyType": "STANDARD"
                                },
                            }
                        ]
                    }
                },
            )
        ],
        "pax": [
            attempt(
                "ee9e93e4b2cab211",
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
        queries["packages"] = [
            attempt(
                "47cee02ce9a66960",
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
                            "result": {
                                "tourGrades": [
                                    {
                                        "title": "Two adults",
                                        "price": {
                                            "amount": 50,
                                            "currency": "USD",
                                        },
                                        "lineItems": [
                                            {
                                                "quantity": 2,
                                                "unitPrice": {
                                                    "amount": 25,
                                                    "currency": "USD",
                                                },
                                            }
                                        ],
                                    }
                                ]
                            },
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
        "sourceUrl": item["url"],
        "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "queries": queries,
        "selection": {
            "travelDate": "2026-07-18",
            "travelDateSource": "priceCalendar.datesAndPrices",
            "passengerMix": passenger_mix,
            "packageOptionsStatus": "UNKNOWN" if pax_failed else "AVAILABLE",
            "packageOptionsUnavailableReason": "pax_failed" if pax_failed else None,
        },
    }


def graphql_venue_evidence(item):
    listing_id = int(item["key"].split(":", 1)[1])
    tracking = {"screenName": "Attraction_Review", "pageviewUid": "page-1"}

    def attempt(query_id, variables, response):
        return {
            "attempt": 1,
            "persistedQueryId": query_id,
            "variables": variables,
            "httpStatus": 200,
            "evidenceResponse": response,
        }

    return {
        "schemaVersion": 1,
        "transport": "tripadvisor-browser-graphql",
        "route": "Attraction_Review",
        "detailId": listing_id,
        "sourceUrl": item["url"],
        "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reviewSelection": {
            "policy": "exact-location-id-only",
            "requestedLocationId": listing_id,
            "sourceTotalCount": 2,
            "returnedCount": 2,
            "acceptedCount": 1,
            "quarantinedCount": 1,
            "missingLocationIdCount": 0,
            "rejectedLocationIds": ["999"],
        },
        "queries": {
            "detail": [
                attempt(
                    "9598263f57e2fd6f",
                    {
                        "request": {
                            "tracking": tracking,
                            "routeParameters": {
                                "contentType": "attraction",
                                "contentId": str(listing_id),
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
                                    "container": {
                                        "jsonLd": json.dumps(
                                            {
                                                "@id": item["url"],
                                                "name": item["name"],
                                            }
                                        )
                                    },
                                    "dataModel": {
                                        "title": item["name"],
                                        "description": "A venue with exact identity evidence.",
                                        "attractionDetails": {
                                            "locationId": listing_id
                                        },
                                    },
                                }
                            ]
                        }
                    },
                )
            ],
            "reviews": [
                attempt(
                    "ef1a9f94012220d3",
                    {
                        "locationId": listing_id,
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
                                {
                                    "totalCount": 1,
                                    "reviews": [
                                        {
                                            "id": "r1",
                                            "locationId": listing_id,
                                            "title": "Exact venue",
                                            "text": "This review belongs here.",
                                            "rating": 5,
                                            "language": "en",
                                            "originalLanguage": "hu",
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                )
            ],
        },
    }


def write_graphql_fixture(raw_dir, item, evidence):
    path = detail_graphql_cache_path({"url": item["url"]}, raw_dir=raw_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence), encoding="utf-8")
    parsed = parse_graphql_evidence(evidence, expected_url=item["url"])
    checked_at = evidence["checkedAt"].split("T", 1)[0]
    return path, build_context(source_listing(item), parsed, checked_at=checked_at)


class DetailContextAuditTests(unittest.TestCase):
    def test_strict_complete_fixture_passes_with_live_multilingual_coverage(self):
        with TemporaryDirectory() as tmp:
            item = visible_item()
            row = write_fixture(
                Path(tmp), item, rendered_page(item, live_total=1)
            )

            report = audit_detail_context([item], [row], raw_dir=Path(tmp))

        self.assertTrue(report.ok, report.as_json())
        self.assertEqual(report.audited, 1)
        self.assertEqual(
            report.coverage_sources, {"live": 1, "discovery": 0, "graphql": 0}
        )
        self.assertIn("Árvíztűrő", row["description"])

    def test_partial_allows_missing_but_strict_requires_exact_key_set(self):
        with TemporaryDirectory() as tmp:
            first = visible_item("123")
            second = visible_item("456")
            row = write_fixture(
                Path(tmp), first, rendered_page(first, live_total=1)
            )

            partial = audit_detail_context(
                [first, second], [row], raw_dir=Path(tmp), allow_partial=True
            )
            strict = audit_detail_context(
                [first, second], [row], raw_dir=Path(tmp)
            )
            unexpected = dict(row)
            unexpected["key"] = "AttractionProductReview:999"
            with_extra = audit_detail_context(
                [first, second],
                [row, unexpected],
                raw_dir=Path(tmp),
                allow_partial=True,
            )

        self.assertTrue(partial.ok, partial.as_json())
        self.assertEqual(partial.missing, [second["key"]])
        self.assertFalse(strict.ok)
        self.assertIn("missing-context", {issue.code for issue in strict.issues})
        self.assertFalse(with_extra.ok)
        self.assertEqual(with_extra.unexpected, [unexpected["key"]])

    def test_raw_identity_mtime_and_latest_parse_must_match(self):
        with TemporaryDirectory() as tmp:
            item = visible_item()
            row = write_fixture(
                Path(tmp), item, rendered_page(item, live_total=1)
            )
            row["description"] = "Stale description."
            row["checked_at"] = "2000-01-01"
            row["canonical_url"] = (
                "http://example.test/AttractionProductReview-g1-d999-Wrong.html"
            )

            report = audit_detail_context([item], [row], raw_dir=Path(tmp))

        codes = {issue.code for issue in report.issues}
        self.assertIn("stale-parse", codes)
        self.assertIn("checked-at", codes)
        self.assertIn("canonical-identity", codes)
        self.assertIn("canonical-host", codes)

    def test_all_language_filter_and_authoritative_review_target_are_enforced(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(review_count=50)
            row = write_fixture(
                Path(tmp),
                item,
                rendered_page(
                    item,
                    reviews=[review_card()],
                    live_total=2,
                    filters=1,
                    language="English (2)",
                ),
            )

            report = audit_detail_context([item], [row], raw_dir=Path(tmp))

        codes = {issue.code for issue in report.issues}
        self.assertIn("review-filter", codes)
        self.assertIn("review-language", codes)
        self.assertIn("review-coverage", codes)
        self.assertEqual(
            report.coverage_sources, {"live": 1, "discovery": 0, "graphql": 0}
        )

    def test_discovery_review_count_is_used_only_without_live_total(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(review_count=2)
            row = write_fixture(
                Path(tmp),
                item,
                rendered_page(
                    item,
                    reviews=[review_card()],
                    live_total=None,
                    language="",
                ),
            )

            report = audit_detail_context([item], [row], raw_dir=Path(tmp))

        self.assertIn("review-coverage", {issue.code for issue in report.issues})
        self.assertIn(
            "review-language-missing", {issue.code for issue in report.issues}
        )
        self.assertEqual(
            report.coverage_sources, {"live": 0, "discovery": 1, "graphql": 0}
        )

    def test_exact_scoped_pagination_exception_allows_known_nine_card_page(self):
        with TemporaryDirectory() as tmp:
            item = visible_item("34325420", review_count=227)
            cards = [review_card(index) for index in range(9)]
            page = rendered_page(item, reviews=cards, live_total=227).replace(
                '<div data-automation="apr-reviews">',
                '<div data-automation="apr-reviews">'
                '<a data-smoke-attr="pagination-next-arrow" aria-label="Next page"></a>',
            )
            row = write_fixture(Path(tmp), item, page)
            allowed = audit_detail_context([item], [row], raw_dir=Path(tmp))

            without_marker = page.replace(
                '<a data-smoke-attr="pagination-next-arrow" aria-label="Next page"></a>',
                "",
            )
            row = write_fixture(Path(tmp), item, without_marker)
            rejected = audit_detail_context([item], [row], raw_dir=Path(tmp))

            other = visible_item("34325421", review_count=227)
            other_page = rendered_page(other, reviews=cards, live_total=227).replace(
                '<div data-automation="apr-reviews">',
                '<div data-automation="apr-reviews">'
                '<a data-smoke-attr="pagination-next-arrow" aria-label="Next page"></a>',
            )
            other_row = write_fixture(Path(tmp), other, other_page)
            wrong_key = audit_detail_context(
                [other], [other_row], raw_dir=Path(tmp)
            )

        self.assertTrue(allowed.ok, allowed.as_json())
        self.assertIn("review-coverage", {issue.code for issue in rejected.issues})
        self.assertIn("review-coverage", {issue.code for issue in wrong_key.issues})

    def test_exact_venue_pagination_exception_allows_verified_matthias_church(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(
                "276808",
                route="Attraction_Review",
                review_count=10_715,
                name="Matthias Church",
            )
            cards = [review_card(index) for index in range(9)]
            next_page = (
                '<a data-smoke-attr="pagination-next-arrow" aria-label="Next page" '
                'href="/Attraction_Review-g274887-d276808-Reviews-or10-'
                'Matthias_Church-Budapest.html"></a>'
            )
            page = rendered_page(
                item,
                reviews=cards,
                live_total=10_715,
                language="",
            ).replace("</body>", f"{next_page}</body>")
            row = write_fixture(Path(tmp), item, page)
            allowed = audit_detail_context([item], [row], raw_dir=Path(tmp))

            other = visible_item(
                "276809",
                route="Attraction_Review",
                review_count=10_715,
                name="Other Church",
            )
            other_page = rendered_page(
                other,
                reviews=cards,
                live_total=10_715,
                language="",
            ).replace("</body>", f"{next_page}</body>")
            other_row = write_fixture(Path(tmp), other, other_page)
            wrong_key = audit_detail_context(
                [other], [other_row], raw_dir=Path(tmp)
            )

        self.assertTrue(allowed.ok, allowed.as_json())
        self.assertIn("review-coverage", {issue.code for issue in wrong_key.issues})

    def test_unicode_review_rating_uniqueness_and_pricing_invariants_fail_closed(self):
        with TemporaryDirectory() as tmp:
            item = visible_item()
            row = write_fixture(
                Path(tmp), item, rendered_page(item, live_total=1)
            )
            broken_review = {
                "title": "Broken \ufffd",
                "text": "Duplicated",
                "rating": 6,
            }
            row["reviews"] = [broken_review, dict(broken_review)]
            row["description"] = "A\u0301rvíztűrő decomposed text."
            package = row["pricing_evidence"]["packages"][0]
            package["party"] = "NaN seniors"
            package["unit_price"] = "$25.00"
            package["unit"] = "person"
            row["pricing_evidence"]["availability"]["status"] = "unavailable"
            row["pricing_evidence"]["status"] = "unavailable"

            report = audit_detail_context([item], [row], raw_dir=Path(tmp))

            second_package = dict(package)
            second_package["name"] = "Second unavailable option"
            package["availability"] = "sold-out"
            package["availability_message"] = "Sold out"
            second_package["availability"] = "unavailable"
            second_package["availability_message"] = "Unavailable"
            row["pricing_evidence"]["packages"] = [package, second_package]
            row["pricing_evidence"]["availability"]["status"] = "available"
            row["pricing_evidence"]["status"] = "available"
            mixed = audit_detail_context([item], [row], raw_dir=Path(tmp))

        codes = {issue.code for issue in report.issues}
        self.assertIn("text-unicode", codes)
        self.assertIn("text-nfc", codes)
        self.assertIn("review-rating", codes)
        self.assertIn("review-duplicate", codes)
        self.assertIn("pricing-nan", codes)
        self.assertIn("package-global-contradiction", codes)
        self.assertIn(
            "package-global-availability", {issue.code for issue in mixed.issues}
        )

    def test_raw_price_surface_catches_parser_omission_and_bad_charge_math(self):
        with TemporaryDirectory() as tmp:
            omitted = visible_item("777", review_count=0)
            omitted_page = rendered_page(
                omitted, reviews=[], live_total=0, package_total="$25.00"
            ).replace(
                '<span id="detailed-total-price-0-inline-booking-section">'
                "Total price: $25.00</span>",
                "<div>Total price: $25.00</div><button>Reserve Now</button>",
            )
            omitted_row = write_fixture(Path(tmp), omitted, omitted_page)
            omission = audit_detail_context(
                [omitted], [omitted_row], raw_dir=Path(tmp)
            )

            bad_math_item = visible_item("778", review_count=0)
            bad_math_page = rendered_page(
                bad_math_item,
                reviews=[],
                live_total=0,
                package_total="$30.00",
                package_suffix=(
                    " for 2 adults</span><div>2 Adults x $10.00</div><span>"
                ),
            )
            bad_math_row = write_fixture(Path(tmp), bad_math_item, bad_math_page)
            bad_math = audit_detail_context(
                [bad_math_item], [bad_math_row], raw_dir=Path(tmp)
            )

        omission_codes = {issue.code for issue in omission.issues}
        self.assertIn("raw-total-missing", omission_codes)
        self.assertIn("raw-priced-surface-empty", omission_codes)
        self.assertIn("price-math", {issue.code for issue in bad_math.issues})

    def test_raw_base_and_first_traveller_charge_must_equal_parsed_values(self):
        with TemporaryDirectory() as tmp:
            item = visible_item("779", review_count=0)
            page = rendered_page(
                item,
                reviews=[],
                live_total=0,
                package_total="$20.00",
                package_suffix=(
                    " for 2 adults</span><div>2 Adults x $10.00</div><span>"
                ),
            ).replace(
                '<div data-automation="availabilityTourGrades">',
                '<div data-automation="commerce_module_visible_price">$10.00</div>'
                '<div data-automation="availabilityTourGrades">',
            )
            row = write_fixture(Path(tmp), item, page)
            good = audit_detail_context([item], [row], raw_dir=Path(tmp))

            row["pricing_evidence"]["base_price"] = "$999.00"
            package = row["pricing_evidence"]["packages"][0]
            package["unit_price"] = "$999.00"
            package["unit"] = "seniors"
            bad = audit_detail_context([item], [row], raw_dir=Path(tmp))

        self.assertTrue(good.ok, good.as_json())
        codes = {issue.code for issue in bad.issues}
        self.assertIn("raw-base-price-mismatch", codes)
        self.assertIn("raw-charge-price-mismatch", codes)
        self.assertIn("raw-charge-unit-mismatch", codes)

    def test_real_nan_party_regression_is_repaired_exactly(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(
                "24152959", route="AttractionProductReview", review_count=0
            )
            page = rendered_page(
                item,
                reviews=[],
                live_total=0,
                package_name="Private Tour",
                package_total="$467.36",
                package_suffix=(
                    " for 4 adults and NaN seniors</span>"
                    "<div>4 Adults x $58.42</div>"
                    "<div>4 Seniors x $58.42</div><span>"
                ),
            )
            row = write_fixture(Path(tmp), item, page)

            good = audit_detail_context([item], [row], raw_dir=Path(tmp))
            row["pricing_evidence"]["packages"][0]["party"] = (
                "4 adults and NaN seniors"
            )
            bad = audit_detail_context([item], [row], raw_dir=Path(tmp))

        self.assertTrue(good.ok, good.as_json())
        self.assertEqual(
            row["pricing_evidence"]["packages"][0]["party"],
            "4 adults and NaN seniors",
        )
        codes = {issue.code for issue in bad.issues}
        self.assertIn("pricing-nan", codes)
        self.assertIn("critical-nan-party", codes)

    def test_real_standalone_total_regression_keeps_total_opaque(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(
                "11471004",
                route="AttractionProductReview",
                review_count=0,
                name="Budapest City Walk in Jewish Quarter",
            )
            page = rendered_page(
                item,
                reviews=[],
                live_total=0,
                package_name="Budapest City Walk in Jewish Quarter",
                package_total="$344.69",
            )
            row = write_fixture(Path(tmp), item, page)
            good = audit_detail_context([item], [row], raw_dir=Path(tmp))
            package = row["pricing_evidence"]["packages"][0]
            package["unit_price"] = "$57.45"
            package["unit"] = "adult"
            bad = audit_detail_context([item], [row], raw_dir=Path(tmp))

        self.assertTrue(good.ok, good.as_json())
        codes = {issue.code for issue in bad.issues}
        self.assertIn("standalone-total", codes)
        self.assertIn("critical-standalone-total", codes)

    def test_graphql_only_raw_evidence_passes_without_an_html_page(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(review_count=1)
            evidence = graphql_product_evidence(item)
            path, row = write_graphql_fixture(Path(tmp), item, evidence)

            report = audit_detail_context([item], [row], raw_dir=Path(tmp))

            html_path = detail_cache_path({"url": item["url"]}, raw_dir=Path(tmp))
            self.assertFalse(html_path.exists())
            self.assertTrue(path.exists())
        self.assertTrue(report.ok, report.as_json())
        self.assertEqual(
            report.coverage_sources, {"live": 0, "discovery": 0, "graphql": 1}
        )
        self.assertEqual(row["pricing_evidence"]["base_price"], "USD 25.00")
        self.assertEqual(
            row["pricing_evidence"]["packages"][0]["total_price"], "USD 50.00"
        )

    def test_graphql_failed_pax_keeps_price_and_audits_unknown_availability(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(review_count=1)
            evidence = graphql_product_evidence(item, pax_failed=True)
            _path, row = write_graphql_fixture(Path(tmp), item, evidence)

            report = audit_detail_context([item], [row], raw_dir=Path(tmp))

        self.assertTrue(report.ok, report.as_json())
        pricing = row["pricing_evidence"]
        self.assertEqual(pricing["base_price"], "USD 25.00")
        self.assertNotIn("status", pricing)
        self.assertEqual(pricing["availability"]["status"], "unknown")
        self.assertEqual(pricing["availability"]["reason"], "pax_failed")
        self.assertIn("not confirmed", pricing["availability"]["message"])
        self.assertEqual(pricing["availability"]["source"], "graphql:paxMix")

    def test_strict_audit_detects_an_unprojected_exact_rendered_fallback(self):
        with TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            item = visible_item(review_count=1)
            evidence = graphql_product_evidence(item, pax_failed=True)
            _path, row = write_graphql_fixture(raw_dir, item, evidence)
            html_path = detail_cache_path({"url": item["url"]}, raw_dir=raw_dir)
            html_path.write_text(rendered_fallback_page(item), encoding="utf-8")

            missing = audit_detail_context([item], [row], raw_dir=raw_dir)
            checked_at = datetime.fromtimestamp(html_path.stat().st_mtime).date().isoformat()
            row["pricing_evidence"] = rendered_html_pricing_fallback(
                evidence,
                html_path.read_text(encoding="utf-8"),
                expected_url=item["url"],
                checked_at=checked_at,
            )
            projected = audit_detail_context([item], [row], raw_dir=raw_dir)

        missing_codes = {issue.code for issue in missing.issues}
        self.assertIn("fallback-missing", missing_codes)
        self.assertIn("stale-parse", missing_codes)
        self.assertTrue(projected.ok, projected.as_json())

    def test_strict_audit_rejects_a_claimed_wrong_identity_fallback(self):
        with TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            item = visible_item(review_count=1)
            evidence = graphql_product_evidence(item, pax_failed=True)
            _path, row = write_graphql_fixture(raw_dir, item, evidence)
            html_path = detail_cache_path({"url": item["url"]}, raw_dir=raw_dir)
            html_path.write_text(rendered_fallback_page(item), encoding="utf-8")
            checked_at = datetime.fromtimestamp(html_path.stat().st_mtime).date().isoformat()
            row["pricing_evidence"] = rendered_html_pricing_fallback(
                evidence,
                html_path.read_text(encoding="utf-8"),
                expected_url=item["url"],
                checked_at=checked_at,
            )
            wrong_url = visible_item("999")["url"]
            html_path.write_text(
                rendered_fallback_page(item, canonical_url=wrong_url),
                encoding="utf-8",
            )

            report = audit_detail_context([item], [row], raw_dir=raw_dir)

        self.assertIn("fallback-identity", {issue.code for issue in report.issues})

    def test_unhashable_pax_travel_date_is_reported_without_crashing(self):
        with TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            item = visible_item(review_count=1)
            evidence = graphql_product_evidence(item, pax_failed=True)
            path, row = write_graphql_fixture(raw_dir, item, evidence)
            evidence["queries"]["pax"][0]["variables"]["travelDate"] = {
                "invalid": "object"
            }
            path.write_text(json.dumps(evidence), encoding="utf-8")

            report = audit_detail_context([item], [row], raw_dir=raw_dir)

        self.assertIn(
            "graphql-pax-variables", {issue.code for issue in report.issues}
        )

    def test_graphql_venue_review_quarantine_is_independently_audited(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(
                "456", route="Attraction_Review", review_count=2, name="Venue 456"
            )
            evidence = graphql_venue_evidence(item)
            path, row = write_graphql_fixture(Path(tmp), item, evidence)
            good = audit_detail_context([item], [row], raw_dir=Path(tmp))

            foreign_review = {
                "id": "foreign-r2",
                "locationId": 999,
                "title": "Foreign venue",
                "text": "This body must never be retained.",
                "rating": 5,
                "language": "en",
                "originalLanguage": "hu",
            }
            block = evidence["queries"]["reviews"][0]["evidenceResponse"]["data"][
                "ReviewsProxy_getReviewListPageForLocation"
            ][0]
            block["reviews"].append(foreign_review)
            block["totalCount"] = 2
            evidence["reviewSelection"]["acceptedCount"] = 2
            path.write_text(json.dumps(evidence), encoding="utf-8")
            contaminated = audit_detail_context([item], [row], raw_dir=Path(tmp))

            evidence["queries"]["reviews"][0]["evidenceResponse"]["data"][
                "ReviewsProxy_getReviewListPageForLocation"
            ][0] = {"totalCount": 1, "reviews": [block["reviews"][0]]}
            evidence.pop("reviewSelection")
            path.write_text(json.dumps(evidence), encoding="utf-8")
            missing_provenance = audit_detail_context(
                [item], [row], raw_dir=Path(tmp)
            )

        self.assertTrue(good.ok, good.as_json())
        contaminated_codes = {issue.code for issue in contaminated.issues}
        self.assertIn("graphql-review-identity", contaminated_codes)
        self.assertIn("raw-parse", contaminated_codes)
        missing_codes = {issue.code for issue in missing_provenance.issues}
        self.assertIn("graphql-review-selection", missing_codes)
        self.assertIn("raw-parse", missing_codes)

    def test_graphql_audit_independently_rejects_contract_privacy_and_price_math(self):
        with TemporaryDirectory() as tmp:
            item = visible_item(review_count=1)
            evidence = graphql_product_evidence(item)
            path, row = write_graphql_fixture(Path(tmp), item, evidence)

            evidence["checkedAt"] = "2020-01-01T00:00:00Z"
            evidence["queries"]["reviews"][0]["variables"]["filters"] = ["en"]
            raw_review = evidence["queries"]["reviews"][0]["evidenceResponse"][
                "data"
            ]["ReviewsProxy_getReviewListPageForLocation"][0]["reviews"][0]
            raw_review["username"] = "must-not-be-retained"
            raw_grade = evidence["queries"]["packages"][0]["evidenceResponse"][
                "data"
            ]["tourGrades"]["result"]["tourGrades"][0]
            raw_grade["price"]["amount"] = 60
            raw_grade["lineItems"][0]["unitPrice"]["amount"] = 20
            path.write_text(json.dumps(evidence), encoding="utf-8")

            report = audit_detail_context([item], [row], raw_dir=Path(tmp))

        codes = {issue.code for issue in report.issues}
        self.assertNotIn("graphql-checked-at-mtime", codes)
        self.assertIn("graphql-review-variables", codes)
        self.assertIn("graphql-privacy", codes)
        self.assertIn("graphql-package-price", codes)
        self.assertIn("graphql-price-math", codes)
        self.assertIn("raw-parse", codes)

    def test_validator_loader_keeps_staged_inventory_json_authoritative(self):
        payload = [visible_item(), {"key": "idea:geocaching", "name": "Geocaching"}]
        site_root = Path("staged-site")

        def runner(*args, **kwargs):
            self.assertIn("--print-visible-json", args[0])
            self.assertIn("--allow-partial-research", args[0])
            self.assertIn("--inventory-only", args[0])
            self.assertEqual(args[0][-2:], ["--site-root", str(site_root)])
            return subprocess.CompletedProcess(args[0], 0, json.dumps(payload), "")

        self.assertEqual(
            load_visible_inventory(
                Path("validator.mjs"), site_root=site_root, runner=runner
            ),
            payload,
        )


if __name__ == "__main__":
    unittest.main()
