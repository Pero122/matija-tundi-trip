#!/usr/bin/env python3
"""Cache TripAdvisor detail pages and extract LLM-ready activity context.

The crawler deliberately reuses the repository's headless Camoufox launcher.
Each rendered detail page is cached by stable TripAdvisor identity, so normal
runs fetch only missing or invalid pages. Extracted descriptions and review
text are local research material and are written to the gitignored
``detail_context_<city>.json`` file.

Examples:
  ~/workspace/scripts/stealth/.venv/bin/python scrape_ta_details.py --limit 10
  ~/workspace/scripts/stealth/.venv/bin/python scrape_ta_details.py --id 34405806
  ~/workspace/scripts/stealth/.venv/bin/python scrape_ta_details.py \
      --id d34405806 --id 34122224 --refresh
"""

import argparse
import base64
import binascii
import copy
from collections import deque
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import html as html_lib
import json
import math
import os
import re
import select
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


HERE = Path(__file__).resolve().parent
RAW_DETAILS = HERE / "raw" / "details"
CF = Path.home() / "workspace/scripts/stealth/.venv/bin/python"
CF_SCRAPE = HERE / "fetch_ta_detail.py"

CITIES = ("budapest", "london")
DEFAULT_BATCH_LIMIT = 10
DEFAULT_WORKERS = 1
MAX_WORKERS = 3
MAX_REVIEW_SNIPPETS = 10
MIN_HTML_BYTES = 30_000
FETCH_RETRIES = 3
FETCH_TIMEOUT_SECONDS = 180
PAGE_SETTLE_SECONDS = 8
FETCH_DELAY_SECONDS = 3.0
DEFAULT_BLOCK_COOLDOWN_SECONDS = 900.0
MAX_BLOCK_COOLDOWN_SECONDS = 3600.0

GRAPHQL_SCHEMA_VERSION = 1
GRAPHQL_TRANSPORT = "tripadvisor-browser-graphql"
GRAPHQL_REVIEW_FIELDS = frozenset(
    {
        "id",
        "locationId",
        "title",
        "text",
        "rating",
        "language",
        "originalLanguage",
        "translationType",
        "publishedDate",
        "travelDate",
    }
)
GRAPHQL_PERSONAL_DATA_KEYS = frozenset(
    {
        "author",
        "authorid",
        "authorname",
        "avatar",
        "avatarurl",
        "displayname",
        "firstname",
        "lastname",
        "managementresponseauthor",
        "memberid",
        "memberprofile",
        "photo",
        "photos",
        "profileid",
        "profileurl",
        "responseauthor",
        "userid",
        "username",
        "userprofile",
    }
)
GRAPHQL_QUERY_IDS = {
    "AttractionProductReview": {
        "detail": "f7a273b890edf6c2",
        "reviews": "8793c5d897e589a1",
        "priceCalendar": "eb4cf849c5286ed5",
        "pax": "ee9e93e4b2cab211",
        "packages": "47cee02ce9a66960",
        "cancellation": "471725aa61475779",
    },
    "Attraction_Review": {
        "detail": "9598263f57e2fd6f",
        "reviews": "ef1a9f94012220d3",
    },
}
VENUE_REVIEW_SELECTION_POLICY = "exact-location-id-only"
GRAPHQL_MAX_POLL_ATTEMPTS = 3
GRAPHQL_MAX_DATE_CANDIDATES = 5
GRAPHQL_DATE_SELECTION_POLICY = "earliest-advertised-after-cutoff-up-to-5-dates"
RENDERED_PRICING_FALLBACK_KIND = "rendered-html-fallback"
RENDERED_PRICING_FALLBACK_REASONS = frozenset(
    {"pax_failed", "tour_grades_failed", "tour_grades_empty"}
)
GRAPHQL_BLOCKED_STATUS_RE = re.compile(
    r"(?:GraphQLBlockedHTTP:|GraphQL browser transport returned HTTP\s+)(403|429)\b"
)

DATADOME_CHALLENGE_MARKERS = (
    "captcha-delivery.com/captcha",
    "geo.captcha-delivery.com",
    "datadome captcha",
    "datadome bot protection",
    "security check to access tripadvisor",
)


class DataDomeBlocked(RuntimeError):
    """Signal a shared anti-bot block without turning queued work into failures."""

    def __init__(self, listing_id, message="Tripadvisor DataDome challenge detected"):
        self.listing_id = str(listing_id)
        super().__init__(f"d{self.listing_id}: {message}")


class SingleInstanceError(RuntimeError):
    """Raised when another crawler already owns a city-specific writer lock."""


class GraphQLEvidenceError(ValueError):
    """Raised when a staged GraphQL artifact is incomplete or inconsistent."""


@contextmanager
def single_instance_lock(path):
    """Hold a nonblocking advisory lock for one crawler's whole lifetime."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SingleInstanceError(
                f"another crawler already owns {path.name}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class AdaptiveBlockController:
    """Thread-safe stop signal shared by one live browser-fetch phase."""

    def __init__(self):
        self._event = threading.Event()

    def trip(self):
        self._event.set()

    def clear(self):
        self._event.clear()

    def is_blocked(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)

DETAIL_URL_RE = re.compile(
    r"/(AttractionProductReview|Attraction_Review)-g\d+-d(\d+)-", re.I
)
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
BODY_MARKERS = frozenset(
    {
        "review-body",
        "review-content",
        "review-text",
        "review-text-content",
        "reviewbody",
        "reviewcontent",
        "reviewtext",
        "reviewtextcontent",
    }
)
TEXT_BREAK_TAGS = frozenset({"br", "div", "li", "p", "section"})
MONEY_NUMBER_PATTERN = (
    r"(?:\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{1,2})?|"
    r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|"
    r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|"
    r"\d+(?:[.,]\d{1,2})?)"
)
MONEY_CODE_PATTERN = r"(?:USD|EUR|GBP|HUF|NZD|AUD|CAD|Ft)"
MONEY_SYMBOL_PATTERN = r"(?:US\$|CA\$|NZ\$|A\$|C\$|[$€£¥])"
MONEY_TEXT_RE = re.compile(
    rf"(?:{MONEY_SYMBOL_PATTERN}|(?<![A-Za-z]){MONEY_CODE_PATTERN}(?![A-Za-z]))"
    rf"\s*{MONEY_NUMBER_PATTERN}|"
    rf"{MONEY_NUMBER_PATTERN}\s*"
    rf"(?:{MONEY_SYMBOL_PATTERN}|(?<![A-Za-z]){MONEY_CODE_PATTERN}(?![A-Za-z]))",
    re.I,
)
ADDITIONAL_COST_RE = re.compile(
    r"\b(?:additional|extra)\s+(?:cost|charge|fee)s?\b|"
    r"\bsurcharges?\b|\bsupplements?\b|"
    r"\b(?:cost|charge|fee)s?\s+(?:is|are)?\s+not\s+included\b|"
    r"\bpay(?:able)?\s+separately\b",
    re.I,
)
PARTY_CHARGE_RE = re.compile(
    rf"(\d+)\s+(Adults?|Seniors?|Youths?|Children|Infants)\s+x\s+"
    rf"({MONEY_TEXT_RE.pattern})",
    re.I,
)
TOTAL_PRICE_RE = re.compile(
    rf"\bTotal price:\s*({MONEY_TEXT_RE.pattern})(?:\s+for\s+([^.;]+))?",
    re.I,
)
ALL_REVIEWS_COUNT_RE = re.compile(
    r"All reviews(?:<!--.*?-->|[\s(])*([0-9][0-9,.]*)",
    re.I | re.S,
)
REVIEW_FILTER_COUNT_RE = re.compile(
    r">\s*Filters(?:<!--.*?-->|[\s(])*([0-9][0-9,.]*)",
    re.I | re.S,
)
PRODUCT_REVIEW_LANGUAGE_RE = re.compile(
    r'<button(?=[^>]*\baria-haspopup="listbox")'
    r'(?=[^>]*\baria-label="language:\s*([^\"]+)")[^>]*>',
    re.I,
)
PRODUCT_REVIEW_COUNT_RE = re.compile(
    r'data-automation="reviewCount"[^>]*>.{0,300}?'
    r'\(\s*([0-9][0-9,.]*)\s*\)',
    re.I | re.S,
)
PRODUCT_REVIEW_NEXT_RE = re.compile(
    r'data-smoke-attr="pagination-next-arrow"',
    re.I,
)
NINE_CARD_PAGINATION_EXCEPTIONS = frozenset(
    {
        ("AttractionProductReview", "34325420"),
        ("Attraction_Review", "276808"),
    }
)


def clean_text(value):
    """Normalize rendered text without truncating useful review context."""
    value = html_lib.unescape(value or "")
    value = "".join(
        " " if char == "\ufffd" or "\ud800" <= char <= "\udfff" else char
        for char in value
    )
    value = re.sub("[\ufff9-\ufffb]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return unicodedata.normalize("NFC", value)


def rendered_review_total(html_text):
    """Return the live review total shown by the rendered review surface."""
    totals = []
    for raw in ALL_REVIEWS_COUNT_RE.findall(html_text or ""):
        digits = re.sub(r"\D", "", raw)
        if digits:
            totals.append(int(digits))
    product_scope = rendered_product_review_scope(html_text)
    for raw in PRODUCT_REVIEW_COUNT_RE.findall(product_scope):
        digits = re.sub(r"\D", "", raw)
        if digits:
            totals.append(int(digits))
    return max(totals) if totals else None


def rendered_review_filter_count(html_text):
    """Return the active rendered review-filter count, if exposed."""
    counts = []
    for raw in REVIEW_FILTER_COUNT_RE.findall(html_text or ""):
        digits = re.sub(r"\D", "", raw)
        if digits:
            counts.append(int(digits))
    return max(counts) if counts else 0


def rendered_product_review_scope(html_text):
    """Return the bounded rendered product-review subtree used by filters."""
    html_text = html_text or ""
    marker = 'data-automation="apr-reviews"'
    start = html_text.find(marker)
    return html_text[start : start + 100_000] if start >= 0 else ""


def rendered_product_review_language(html_text):
    """Return the selected scoped product-review language, when rendered."""
    match = PRODUCT_REVIEW_LANGUAGE_RE.search(rendered_product_review_scope(html_text))
    return clean_text(match.group(1)) if match else ""


def rendered_review_pagination_scope(html_text, expected_identity=None):
    """Bound pagination evidence to the rendered review surface."""
    html_text = html_text or ""
    if expected_identity and expected_identity[0] == "AttractionProductReview":
        return rendered_product_review_scope(html_text)
    marker = 'data-automation="reviewCard"'
    start = html_text.find(marker)
    return html_text[start : start + 150_000] if start >= 0 else ""


def review_coverage_target(
    html_text, expected_review_count=None, expected_identity=None
):
    """Use the rendered live total, falling back to discovery metadata."""
    live_total = rendered_review_total(html_text)
    if live_total is not None:
        target = min(MAX_REVIEW_SNIPPETS, live_total)
        # Two verified Tripadvisor layouts paginate after nine rendered review
        # cards. Treat their scoped page-2 controls as source evidence that
        # nine is the full first-page sample; every other identity still
        # requires up to ten.
        review_scope = rendered_review_pagination_scope(
            html_text, expected_identity=expected_identity
        )
        product_language = rendered_product_review_language(html_text)
        language_is_complete = (
            not expected_identity
            or expected_identity[0] != "AttractionProductReview"
            or product_language.lower().startswith("all languages")
        )
        if (
            target == MAX_REVIEW_SNIPPETS
            and expected_identity in NINE_CARD_PAGINATION_EXCEPTIONS
            and rendered_review_filter_count(html_text) == 0
            and language_is_complete
            and review_scope.count('data-automation="reviewCard"') == 9
            and PRODUCT_REVIEW_NEXT_RE.search(review_scope)
        ):
            return 9
        return target
    try:
        expected = max(0, int(expected_review_count or 0))
    except (TypeError, ValueError):
        expected = 0
    return min(MAX_REVIEW_SNIPPETS, expected)


def detail_identity(value):
    """Return ``(route_kind, numeric_id)`` for a listing or detail URL."""
    url = value.get("url", "") if isinstance(value, dict) else str(value)
    match = DETAIL_URL_RE.search(url)
    if not match:
        raise ValueError(f"not a supported TripAdvisor detail URL: {url!r}")
    route = (
        "AttractionProductReview"
        if match.group(1).lower() == "attractionproductreview"
        else "Attraction_Review"
    )
    return route, match.group(2)


def normalize_requested_id(value):
    """Accept a numeric ID, ``d123``, a stable key, or a full detail URL."""
    return requested_identity(value)[1]


def requested_identity(value):
    """Return ``(optional_route, numeric_id)`` without losing explicit routes."""
    value = str(value).strip()
    match = DETAIL_URL_RE.search(value)
    if match:
        route = (
            "AttractionProductReview"
            if match.group(1).lower() == "attractionproductreview"
            else "Attraction_Review"
        )
        return route, match.group(2)
    match = re.fullmatch(
        r"(?:(AttractionProductReview|Attraction_Review):)?d?(\d+)",
        value,
        re.I,
    )
    if not match:
        raise ValueError(f"invalid TripAdvisor detail ID: {value!r}")
    route = match.group(1)
    if route:
        route = (
            "AttractionProductReview"
            if route.lower() == "attractionproductreview"
            else "Attraction_Review"
        )
    return route, match.group(2)


def detail_key(value):
    route, listing_id = detail_identity(value)
    return f"{route}:{listing_id}"


def detail_cache_path(listing, raw_dir=RAW_DETAILS):
    route, listing_id = detail_identity(listing)
    route_slug = re.sub(r"[^a-z0-9]+", "_", route.lower()).strip("_")
    return Path(raw_dir) / f"{route_slug}_{listing_id}.html"


def detail_graphql_cache_path(listing, raw_dir=RAW_DETAILS):
    """Return the route-qualified immutable GraphQL evidence path."""
    route, listing_id = detail_identity(listing)
    route_slug = re.sub(r"[^a-z0-9]+", "_", route.lower()).strip("_")
    return Path(raw_dir) / f"{route_slug}_{listing_id}.graphql.json"


def _first_mapping(value):
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


def _collect_graphql_identity_values(value):
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"locationId", "detailId", "contentId"} and isinstance(
                child, (str, int)
            ):
                found.add(str(child))
            found.update(_collect_graphql_identity_values(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_graphql_identity_values(child))
    elif isinstance(value, str):
        found.update(match.group(2) for match in DETAIL_URL_RE.finditer(value))
    return found


def _graphql_privacy_violation(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace("_", "").lower()
            child_path = f"{path}.{key}"
            if normalized in GRAPHQL_PERSONAL_DATA_KEYS:
                return child_path
            violation = _graphql_privacy_violation(child, child_path)
            if violation:
                return violation
    elif isinstance(value, list):
        for index, child in enumerate(value):
            violation = _graphql_privacy_violation(child, f"{path}[{index}]")
            if violation:
                return violation
    return ""


def _graphql_partial_errors_allowed(name, response):
    errors = response.get("errors") if isinstance(response, dict) else None
    if not errors:
        return True
    products = response.get("data", {}).get("fullProduct")
    return (
        name == "detail"
        and isinstance(products, list)
        and bool(products)
        and all(
            isinstance(error, dict)
            and isinstance(error.get("path"), list)
            and error["path"][:3] == ["fullProduct", 0, "aboutOperator"]
            for error in errors
        )
    )


def _graphql_attempts(evidence, name, query_id):
    queries = evidence.get("queries")
    attempts = queries.get(name) if isinstance(queries, dict) else None
    if not isinstance(attempts, list) or not attempts:
        raise GraphQLEvidenceError(f"missing GraphQL {name} query evidence")
    for index, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict):
            raise GraphQLEvidenceError(f"GraphQL {name} attempt {index} is not an object")
        if attempt.get("attempt") != index:
            raise GraphQLEvidenceError(
                f"GraphQL {name} attempts are not consecutively numbered"
            )
        if attempt.get("persistedQueryId") != query_id:
            raise GraphQLEvidenceError(
                f"GraphQL {name} used unexpected persisted query ID"
            )
        if attempt.get("httpStatus") != 200:
            raise GraphQLEvidenceError(f"GraphQL {name} did not return HTTP 200")
        if not isinstance(attempt.get("variables"), dict):
            raise GraphQLEvidenceError(f"GraphQL {name} variables are missing")
        response = attempt.get("evidenceResponse")
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise GraphQLEvidenceError(f"GraphQL {name} response data are missing")
        if not _graphql_partial_errors_allowed(name, response):
            raise GraphQLEvidenceError(f"GraphQL {name} retained disallowed errors")
        violation = _graphql_privacy_violation(response)
        if violation:
            raise GraphQLEvidenceError(
                f"GraphQL {name} response retained personal field {violation}"
            )
    return attempts


def _graphql_review_block(evidence, route, detail_id):
    attempts = _graphql_attempts(
        evidence, "reviews", GRAPHQL_QUERY_IDS[route]["reviews"]
    )
    if len(attempts) != 1:
        raise GraphQLEvidenceError("GraphQL reviews must be captured in one attempt")
    variables = attempts[0]["variables"]
    expected = {
        "locationId": int(detail_id),
        "filters": [],
        "limit": 10,
        "offset": 0,
        "sortType": "DEFAULT",
        "sortBy": "DATE",
        "language": "en",
        "doMachineTranslation": True,
    }
    for field_name, expected_value in expected.items():
        actual = variables.get(field_name)
        if field_name == "locationId":
            matches = str(actual) == str(expected_value)
        else:
            matches = actual == expected_value
        if not matches:
            raise GraphQLEvidenceError(
                f"GraphQL reviews variable {field_name} is {actual!r}, "
                f"expected {expected_value!r}"
            )
    if route == "Attraction_Review" and variables.get("photosPerReviewLimit") != 7:
        raise GraphQLEvidenceError("venue review photo projection limit is not 7")

    block = _first_mapping(
        attempts[0]["evidenceResponse"]
        .get("data", {})
        .get("ReviewsProxy_getReviewListPageForLocation")
    )
    if block is None:
        raise GraphQLEvidenceError("GraphQL reviews response has no review block")
    total = block.get("totalCount")
    reviews = block.get("reviews")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise GraphQLEvidenceError("GraphQL review totalCount is invalid")
    if not isinstance(reviews, list):
        raise GraphQLEvidenceError("GraphQL reviews are not a list")
    ids = set()
    for index, review in enumerate(reviews):
        if not isinstance(review, dict):
            raise GraphQLEvidenceError(f"GraphQL review {index} is not an object")
        unexpected = set(review) - GRAPHQL_REVIEW_FIELDS
        if unexpected:
            raise GraphQLEvidenceError(
                f"GraphQL review {index} retained unprojected fields: "
                + ", ".join(sorted(unexpected))
            )
        review_id = review.get("id")
        if review_id is None or str(review_id) in ids:
            raise GraphQLEvidenceError("GraphQL reviews have missing or duplicate IDs")
        ids.add(str(review_id))
        review_location_id = review.get("locationId")
        if route == "Attraction_Review":
            if review_location_id is None or str(review_location_id) != str(detail_id):
                raise GraphQLEvidenceError(
                    "GraphQL venue review evidence retained a non-exact location row"
                )
        elif review_location_id is not None and str(review_location_id) != str(
            detail_id
        ):
            raise GraphQLEvidenceError("GraphQL review location identity mismatch")

    if route == "Attraction_Review":
        selection = evidence.get("reviewSelection")
        if not isinstance(selection, dict):
            raise GraphQLEvidenceError(
                "GraphQL venue reviewSelection provenance is missing"
            )
        required_fields = {
            "policy",
            "requestedLocationId",
            "sourceTotalCount",
            "returnedCount",
            "acceptedCount",
            "quarantinedCount",
            "missingLocationIdCount",
            "rejectedLocationIds",
        }
        if set(selection) != required_fields:
            raise GraphQLEvidenceError(
                "GraphQL venue reviewSelection schema is invalid"
            )
        if selection.get("policy") != VENUE_REVIEW_SELECTION_POLICY or str(
            selection.get("requestedLocationId")
        ) != str(detail_id):
            raise GraphQLEvidenceError(
                "GraphQL venue reviewSelection identity is invalid"
            )
        count_names = (
            "sourceTotalCount",
            "returnedCount",
            "acceptedCount",
            "quarantinedCount",
            "missingLocationIdCount",
        )
        counts = {name: selection.get(name) for name in count_names}
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        ):
            raise GraphQLEvidenceError(
                "GraphQL venue reviewSelection counts are invalid"
            )
        rejected_ids = selection.get("rejectedLocationIds")
        if (
            not isinstance(rejected_ids, list)
            or any(not isinstance(value, str) or not value for value in rejected_ids)
            or rejected_ids != sorted(set(rejected_ids))
            or str(detail_id) in rejected_ids
        ):
            raise GraphQLEvidenceError(
                "GraphQL venue rejected location IDs are invalid"
            )
        if counts["returnedCount"] != (
            counts["acceptedCount"] + counts["quarantinedCount"]
        ) or counts["missingLocationIdCount"] > counts["quarantinedCount"]:
            raise GraphQLEvidenceError(
                "GraphQL venue reviewSelection counts are inconsistent"
            )
        if counts["sourceTotalCount"] < counts["acceptedCount"]:
            raise GraphQLEvidenceError(
                "GraphQL venue source total is smaller than accepted reviews"
            )
        if total != counts["acceptedCount"] or len(reviews) != counts[
            "acceptedCount"
        ]:
            raise GraphQLEvidenceError(
                "GraphQL venue persisted reviews contradict reviewSelection"
            )
    target = min(MAX_REVIEW_SNIPPETS, total)
    if len(ids) < target:
        raise GraphQLEvidenceError(
            f"GraphQL reviews contain {len(ids)} unique rows; {target} required"
        )
    return block


def quarantine_venue_review_evidence(evidence):
    """Upgrade a legacy venue artifact by removing non-exact review rows.

    This is intentionally a pure migration helper: callers decide whether and
    how to persist the returned JSON value. It lets existing headless-browser
    evidence be made safe without another TripAdvisor request.
    """
    if not isinstance(evidence, dict) or evidence.get("route") != "Attraction_Review":
        return evidence, False
    if "reviewSelection" in evidence:
        validate_graphql_evidence(evidence, expected_url=evidence.get("sourceUrl"))
        return evidence, False
    detail_id = str(evidence.get("detailId"))
    queries = evidence.get("queries")
    attempts = queries.get("reviews") if isinstance(queries, dict) else None
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise GraphQLEvidenceError(
            "legacy venue evidence must contain one reviews attempt"
        )
    response = attempts[0].get("evidenceResponse")
    block = _first_mapping(
        response.get("data", {}).get("ReviewsProxy_getReviewListPageForLocation")
        if isinstance(response, dict)
        else None
    )
    if block is None or not isinstance(block.get("reviews"), list):
        raise GraphQLEvidenceError("legacy venue review block is malformed")
    source_total = block.get("totalCount")
    if isinstance(source_total, bool) or not isinstance(source_total, int) or source_total < 0:
        raise GraphQLEvidenceError("legacy venue review totalCount is invalid")

    migrated = copy.deepcopy(evidence)
    migrated_block = _first_mapping(
        migrated["queries"]["reviews"][0]["evidenceResponse"]
        .get("data", {})
        .get("ReviewsProxy_getReviewListPageForLocation")
    )
    returned_reviews = [
        review for review in migrated_block["reviews"] if isinstance(review, dict)
    ]
    accepted = [
        review
        for review in returned_reviews
        if review.get("locationId") is not None
        and str(review.get("locationId")) == detail_id
    ]
    rejected_ids = sorted(
        {
            str(review.get("locationId"))
            for review in returned_reviews
            if review.get("locationId") is not None
            and str(review.get("locationId")) != detail_id
        }
    )
    missing_count = sum(review.get("locationId") is None for review in returned_reviews)
    migrated_block["reviews"] = accepted
    migrated_block["totalCount"] = len(accepted)
    migrated["reviewSelection"] = {
        "policy": VENUE_REVIEW_SELECTION_POLICY,
        "requestedLocationId": int(detail_id),
        "sourceTotalCount": source_total,
        "returnedCount": len(returned_reviews),
        "acceptedCount": len(accepted),
        "quarantinedCount": len(returned_reviews) - len(accepted),
        "missingLocationIdCount": missing_count,
        "rejectedLocationIds": rejected_ids,
    }
    validate_graphql_evidence(migrated, expected_url=migrated.get("sourceUrl"))
    return migrated, migrated != evidence


def _parse_graphql_checked_at(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GraphQLEvidenceError("GraphQL checkedAt must be an ISO UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GraphQLEvidenceError("GraphQL checkedAt is invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise GraphQLEvidenceError("GraphQL checkedAt is not UTC")
    return parsed.astimezone(timezone.utc)


def _graphql_selected_product_language(product):
    services = product.get("languageServices") if isinstance(product, dict) else None
    language_map = services.get("languageInfoMap") if isinstance(services, dict) else None
    if isinstance(language_map, dict):
        options = list(language_map.values())
    elif isinstance(language_map, list):
        options = language_map
    else:
        return None
    languages = [
        option["language"].strip()
        for option in options
        if isinstance(option, dict)
        and isinstance(option.get("language"), str)
        and option["language"].strip()
    ]
    return next(
        (language for language in languages if language.casefold() == "en"),
        languages[0] if languages else None,
    )


def _graphql_advertised_date_candidates(product, calendar, checked_at):
    settings = product.get("bookingConfirmationSettings") or {}
    cutoff_hours = settings.get("bookingCutoffInHours") or 0
    try:
        cutoff_hours = max(0, int(cutoff_hours))
    except (TypeError, ValueError):
        cutoff_hours = 0
    lead_days = max(1, math.ceil(cutoff_hours / 24) + 1)
    earliest = checked_at.astimezone(timezone.utc).date().toordinal() + lead_days
    candidates = {}
    for row in calendar.get("datesAndPrices", []):
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            continue
        try:
            parsed = date.fromisoformat(row["date"])
        except ValueError:
            continue
        price = row.get("price")
        if (
            parsed.toordinal() >= earliest
            and isinstance(price, (int, float))
            and not isinstance(price, bool)
            and price >= 0
        ):
            candidates[row["date"]] = parsed
    return [
        date_text
        for date_text, _parsed in sorted(
            candidates.items(), key=lambda item: (item[1], item[0])
        )[:GRAPHQL_MAX_DATE_CANDIDATES]
    ]


def validate_graphql_evidence(evidence, expected_url=None):
    """Fail closed on the browser evidence envelope and persisted-query contract."""
    if not isinstance(evidence, dict):
        raise GraphQLEvidenceError("GraphQL evidence must be an object")
    if evidence.get("schemaVersion") != GRAPHQL_SCHEMA_VERSION:
        raise GraphQLEvidenceError("unsupported GraphQL evidence schema version")
    if evidence.get("transport") != GRAPHQL_TRANSPORT:
        raise GraphQLEvidenceError("unexpected GraphQL evidence transport")
    route = evidence.get("route")
    if route not in GRAPHQL_QUERY_IDS:
        raise GraphQLEvidenceError(f"unsupported GraphQL route {route!r}")
    source_url = evidence.get("sourceUrl")
    try:
        source_identity = detail_identity(source_url)
    except (TypeError, ValueError) as exc:
        raise GraphQLEvidenceError("GraphQL sourceUrl is not a detail URL") from exc
    detail_id = str(evidence.get("detailId"))
    if source_identity != (route, detail_id):
        raise GraphQLEvidenceError("GraphQL envelope route/detail/source mismatch")
    if expected_url is not None:
        expected_identity = detail_identity(expected_url)
        if source_identity != expected_identity or source_url != expected_url:
            raise GraphQLEvidenceError("GraphQL sourceUrl does not match the requested URL")
    checked_at = _parse_graphql_checked_at(evidence.get("checkedAt"))
    queries = evidence.get("queries")
    if not isinstance(queries, dict):
        raise GraphQLEvidenceError("GraphQL queries must be an object")

    review_block = _graphql_review_block(evidence, route, detail_id)
    detail_attempts = _graphql_attempts(
        evidence, "detail", GRAPHQL_QUERY_IDS[route]["detail"]
    )
    expected_names = {"detail", "reviews"}
    if route == "AttractionProductReview":
        detail_variables = detail_attempts[0]["variables"]
        expected_detail_variables = {
            "activityId": int(detail_id),
            "currency": "USD",
            "language": "en",
        }
        if detail_variables != expected_detail_variables:
            raise GraphQLEvidenceError("GraphQL product detail variables are invalid")
        product = _first_mapping(
            detail_attempts[0]["evidenceResponse"].get("data", {}).get("fullProduct")
        )
        if product is None or str(product.get("activityId")) != detail_id:
            raise GraphQLEvidenceError("GraphQL fullProduct identity mismatch")
        selection = evidence.get("selection")
        if not isinstance(selection, dict):
            raise GraphQLEvidenceError("GraphQL product selection is missing")
        if selection.get("travelDateSource") != "priceCalendar.datesAndPrices":
            raise GraphQLEvidenceError("GraphQL travel-date provenance is invalid")
        if selection.get("packageOptionsStatus") not in {
            "AVAILABLE",
            "UNAVAILABLE",
            "UNKNOWN",
        }:
            raise GraphQLEvidenceError("GraphQL package-options status is invalid")
        product_code = product.get("productCode")
        if isinstance(product_code, str) and product_code.strip():
            expected_names.update({"priceCalendar", "cancellation"})
            for name in ("priceCalendar", "cancellation"):
                attempts = _graphql_attempts(
                    evidence, name, GRAPHQL_QUERY_IDS[route][name]
                )
                if len(attempts) != 1 or attempts[0]["variables"] != {
                    "currency": "USD",
                    "productCode": product_code,
                }:
                    raise GraphQLEvidenceError(
                        f"GraphQL {name} variables do not match the product"
                    )
            calendar_attempt = queries["priceCalendar"][0]
            calendar = _first_mapping(
                calendar_attempt["evidenceResponse"]
                .get("data", {})
                .get("priceCalendar")
            )
            if calendar is None or not isinstance(calendar.get("datesAndPrices"), list):
                raise GraphQLEvidenceError("GraphQL price calendar is malformed")
            cancellation_product = _first_mapping(
                queries["cancellation"][0]["evidenceResponse"]
                .get("data", {})
                .get("fullProduct")
            )
            if cancellation_product is None:
                raise GraphQLEvidenceError("GraphQL cancellation product is missing")
            cancellation_activity_id = cancellation_product.get("activityId")
            if cancellation_activity_id is not None and str(
                cancellation_activity_id
            ) != detail_id:
                raise GraphQLEvidenceError("GraphQL cancellation identity mismatch")
            if cancellation_activity_id is None:
                product_title = _find_graphql_text(product, ("title", "name"))
                cancellation_title = _find_graphql_text(
                    cancellation_product, ("title", "name")
                )
                if (
                    not product_title
                    or cancellation_title != product_title
                    or not isinstance(
                        cancellation_product.get("cancellationConditions"), dict
                    )
                ):
                    raise GraphQLEvidenceError(
                        "GraphQL cancellation title/schema mismatch"
                    )

            travel_date = selection.get("travelDate")
            if travel_date is not None:
                if not isinstance(travel_date, str) or not any(
                    isinstance(row, dict) and row.get("date") == travel_date
                    for row in calendar["datesAndPrices"]
                ):
                    raise GraphQLEvidenceError(
                        "GraphQL selected travel date is absent from the calendar"
                    )
                expected_names.add("pax")
                pax_attempts = _graphql_attempts(
                    evidence, "pax", GRAPHQL_QUERY_IDS[route]["pax"]
                )
                expected_pax = {
                    "currencies": ["USD"],
                    "locale": "en-US",
                    "productCode": product_code,
                }
                retry_fields = {
                    "paxAttemptedDates",
                    "paxDatePolicy",
                    "selectedLanguage",
                }
                retry_contract = any(field in selection for field in retry_fields)
                if retry_contract and not retry_fields.issubset(selection):
                    raise GraphQLEvidenceError(
                        "GraphQL pax retry provenance is incomplete"
                    )
                if retry_contract:
                    attempted_dates = selection["paxAttemptedDates"]
                    if (
                        not isinstance(attempted_dates, list)
                        or not attempted_dates
                        or len(attempted_dates) > GRAPHQL_MAX_DATE_CANDIDATES
                        or any(not isinstance(value, str) for value in attempted_dates)
                        or len(set(attempted_dates)) != len(attempted_dates)
                        or attempted_dates[-1] != travel_date
                    ):
                        raise GraphQLEvidenceError(
                            "GraphQL pax attempted-date provenance is invalid"
                        )
                    candidates = _graphql_advertised_date_candidates(
                        product, calendar, checked_at
                    )
                    if attempted_dates != candidates[: len(attempted_dates)]:
                        raise GraphQLEvidenceError(
                            "GraphQL pax attempted dates are not the advertised prefix"
                        )
                    if selection["paxDatePolicy"] != GRAPHQL_DATE_SELECTION_POLICY:
                        raise GraphQLEvidenceError(
                            "GraphQL pax date-selection policy is invalid"
                        )
                    if selection["selectedLanguage"] != _graphql_selected_product_language(
                        product
                    ):
                        raise GraphQLEvidenceError(
                            "GraphQL selected language contradicts languageInfoMap"
                        )
                else:
                    attempted_dates = [travel_date]

                selected_languages = set()
                observed_dates = []
                attempts_by_date = {}
                for attempt in pax_attempts:
                    variables = attempt["variables"]
                    if any(
                        variables.get(name) != value
                        for name, value in expected_pax.items()
                    ):
                        raise GraphQLEvidenceError("GraphQL pax variables are invalid")
                    attempt_date = variables.get("travelDate")
                    if not isinstance(attempt_date, str):
                        raise GraphQLEvidenceError(
                            "GraphQL pax travelDate must be text"
                        )
                    if attempt_date not in attempted_dates:
                        raise GraphQLEvidenceError(
                            "GraphQL pax variables used an unrecorded travel date"
                        )
                    if not observed_dates or observed_dates[-1] != attempt_date:
                        if attempt_date in observed_dates:
                            raise GraphQLEvidenceError(
                                "GraphQL pax attempts returned to an earlier date"
                            )
                        observed_dates.append(attempt_date)
                    attempts_by_date.setdefault(attempt_date, []).append(attempt)
                    selected_language = variables.get("selectedLanguage")
                    if selected_language is not None and not isinstance(
                        selected_language, str
                    ):
                        raise GraphQLEvidenceError(
                            "GraphQL selectedLanguage must be text or null"
                        )
                    selected_languages.add(selected_language)
                if observed_dates != attempted_dates:
                    raise GraphQLEvidenceError(
                        "GraphQL pax attempts contradict attempted-date provenance"
                    )
                if len(selected_languages) != 1:
                    raise GraphQLEvidenceError(
                        "GraphQL pax attempts changed selectedLanguage"
                    )
                if retry_contract and selected_languages != {
                    selection["selectedLanguage"]
                }:
                    raise GraphQLEvidenceError(
                        "GraphQL pax selectedLanguage contradicts its provenance"
                    )
                for index, attempted_date in enumerate(attempted_dates):
                    date_attempts = attempts_by_date[attempted_date]
                    if len(date_attempts) > GRAPHQL_MAX_POLL_ATTEMPTS:
                        raise GraphQLEvidenceError(
                            "GraphQL pax polling exceeded the per-date bound"
                        )
                    statuses = [
                        attempt["evidenceResponse"]
                        .get("data", {})
                        .get("paxMix", {})
                        .get("resultStatus")
                        for attempt in date_attempts
                    ]
                    if any(status in {"SUCCESS", "FAILED"} for status in statuses[:-1]):
                        raise GraphQLEvidenceError(
                            "GraphQL pax polling continued after a settled result"
                        )
                    if statuses[-1] not in {"SUCCESS", "FAILED"}:
                        raise GraphQLEvidenceError(
                            "GraphQL pax result did not settle"
                        )
                    if index + 1 < len(attempted_dates) and statuses[-1] != "FAILED":
                        raise GraphQLEvidenceError(
                            "GraphQL pax retry advanced without a failed date"
                        )
                pax_result = pax_attempts[-1]["evidenceResponse"].get("data", {}).get(
                    "paxMix"
                )
                if not isinstance(pax_result, dict) or pax_result.get(
                    "resultStatus"
                ) not in {"SUCCESS", "FAILED"}:
                    raise GraphQLEvidenceError("GraphQL pax result did not settle")
                if pax_result.get("resultStatus") == "FAILED" and (
                    selection.get("packageOptionsStatus") != "UNKNOWN"
                    or selection.get("packageOptionsUnavailableReason") != "pax_failed"
                ):
                    raise GraphQLEvidenceError(
                        "GraphQL terminal pax failure lacks unknown provenance"
                    )

                passenger_mix = selection.get("passengerMix")
                if passenger_mix is not None:
                    if not isinstance(passenger_mix, list) or not passenger_mix:
                        raise GraphQLEvidenceError(
                            "GraphQL passengerMix must be a non-empty list or null"
                        )
                    expected_names.add("packages")
                    package_attempts = _graphql_attempts(
                        evidence, "packages", GRAPHQL_QUERY_IDS[route]["packages"]
                    )
                    expected_package_variables = {
                        "productCode": product_code,
                        "travelDate": travel_date,
                        "passengerMix": passenger_mix,
                        "currencies": ["USD"],
                        "locale": "en-US",
                    }
                    if any(
                        attempt["variables"] != expected_package_variables
                        for attempt in package_attempts
                    ):
                        raise GraphQLEvidenceError("GraphQL package variables are invalid")
                    package_result = package_attempts[-1]["evidenceResponse"].get(
                        "data", {}
                    ).get("tourGrades")
                    if not isinstance(package_result, dict) or package_result.get(
                        "resultStatus"
                    ) not in {"SUCCESS", "FAILED"}:
                        raise GraphQLEvidenceError("GraphQL package result did not settle")
                    package_succeeded = package_result.get("resultStatus") == "SUCCESS"
                    package_value = package_result.get("result")
                    package_rows = (
                        package_value.get("tourGrades")
                        if isinstance(package_value, dict)
                        else None
                    )
                    package_has_rows = isinstance(package_rows, list) and any(
                        isinstance(row, dict) for row in package_rows
                    )
                    expected_package_status = (
                        "AVAILABLE"
                        if package_succeeded and package_has_rows
                        else "UNKNOWN"
                    )
                    if selection.get("packageOptionsStatus") != expected_package_status:
                        raise GraphQLEvidenceError(
                            "GraphQL package-options status contradicts tourGrades"
                        )
                    expected_reason = (
                        None
                        if package_succeeded and package_has_rows
                        else "tour_grades_empty"
                        if package_succeeded
                        else "tour_grades_failed"
                    )
                    if selection.get("packageOptionsUnavailableReason") != expected_reason:
                        raise GraphQLEvidenceError(
                            "GraphQL package-options reason contradicts tourGrades"
                        )
                elif pax_result.get("resultStatus") == "SUCCESS" and (
                    selection.get("packageOptionsStatus") != "UNAVAILABLE"
                    or selection.get("packageOptionsUnavailableReason")
                    != "passenger_mix_missing"
                ):
                    raise GraphQLEvidenceError(
                        "GraphQL missing passenger mix lacks unavailable provenance"
                    )
            elif (
                selection.get("packageOptionsStatus") != "UNAVAILABLE"
                or selection.get("packageOptionsUnavailableReason")
                != "advertised_date_missing"
            ):
                raise GraphQLEvidenceError(
                    "GraphQL missing travel date lacks unavailable provenance"
                )
            elif any(
                field in selection
                for field in ("paxAttemptedDates", "paxDatePolicy", "selectedLanguage")
            ) and (
                selection.get("paxAttemptedDates") != []
                or selection.get("paxDatePolicy") != GRAPHQL_DATE_SELECTION_POLICY
                or selection.get("selectedLanguage") is not None
            ):
                raise GraphQLEvidenceError(
                    "GraphQL missing travel date has invalid retry provenance"
                )
        elif selection.get("travelDate") is not None or selection.get(
            "passengerMix"
        ) is not None:
            raise GraphQLEvidenceError("GraphQL selection exists without a product code")
        elif (
            selection.get("packageOptionsStatus") != "UNAVAILABLE"
            or selection.get("packageOptionsUnavailableReason")
            != "product_code_missing"
        ):
            raise GraphQLEvidenceError(
                "GraphQL missing product code lacks unavailable provenance"
            )
        elif any(
            field in selection
            for field in ("paxAttemptedDates", "paxDatePolicy", "selectedLanguage")
        ) and (
            selection.get("paxAttemptedDates") != []
            or selection.get("paxDatePolicy") != GRAPHQL_DATE_SELECTION_POLICY
            or selection.get("selectedLanguage") is not None
        ):
            raise GraphQLEvidenceError(
                "GraphQL missing product code has invalid retry provenance"
            )
    else:
        if "selection" in evidence:
            raise GraphQLEvidenceError("venue GraphQL evidence must not have a selection")
        for attempt in detail_attempts:
            variables = attempt["variables"]
            request = variables.get("request")
            route_parameters = (
                request.get("routeParameters") if isinstance(request, dict) else None
            )
            if (
                not isinstance(route_parameters, dict)
                or route_parameters.get("contentType") != "attraction"
                or str(route_parameters.get("contentId")) != detail_id
                or variables.get("currency") != "USD"
            ):
                raise GraphQLEvidenceError("GraphQL venue detail variables are invalid")
        result = _first_mapping(
            detail_attempts[-1]["evidenceResponse"].get("data", {}).get("Result")
        )
        if result is None or detail_id not in _collect_graphql_identity_values(result):
            raise GraphQLEvidenceError("GraphQL WPS Result identity mismatch")

    unexpected_names = set(queries) - expected_names
    missing_names = expected_names - set(queries)
    if unexpected_names or missing_names:
        raise GraphQLEvidenceError(
            "GraphQL query set mismatch; "
            f"missing={sorted(missing_names)}, unexpected={sorted(unexpected_names)}"
        )
    return {
        "route": route,
        "detail_id": detail_id,
        "review_total": review_block["totalCount"],
    }


def graphql_cache_looks_valid(path, expected_url=None):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with path.open(encoding="utf-8") as handle:
            evidence = json.load(handle)
        validate_graphql_evidence(evidence, expected_url=expected_url)
    except (OSError, ValueError, json.JSONDecodeError, GraphQLEvidenceError):
        return False
    return True


def _graphql_response(evidence, name, *, last=False):
    attempts = evidence["queries"][name]
    return attempts[-1 if last else 0]["evidenceResponse"]


def _text_fragments(value):
    fragments = []
    if isinstance(value, str):
        cleaned = clean_text(value)
        if cleaned:
            fragments.append(cleaned)
    elif isinstance(value, list):
        for child in value:
            fragments.extend(_text_fragments(child))
    elif isinstance(value, dict):
        for key in ("text", "value", "content", "label", "title", "description"):
            if key in value:
                fragments.extend(_text_fragments(value[key]))
    return _unique_nonempty(fragments)


def _find_graphql_text(mapping, field_names):
    """Find text only under explicitly meaningful content keys."""
    wanted = {name.casefold() for name in field_names}
    if isinstance(mapping, dict):
        for key, value in mapping.items():
            if str(key).casefold() in wanted:
                fragments = _text_fragments(value)
                if fragments:
                    return max(fragments, key=len)
        for value in mapping.values():
            found = _find_graphql_text(value, field_names)
            if found:
                return found
    elif isinstance(mapping, list):
        for value in mapping:
            found = _find_graphql_text(value, field_names)
            if found:
                return found
    return ""


TA_ENCODED_TEXT_RE = re.compile(
    r"^(?P<prefix>[A-Za-z0-9]{3,8})_(?P<text>[\s\S]+)_(?P<suffix>[A-Za-z0-9]{3,8})$"
)


def _decode_ta_content_text(value):
    """Decode only Tripadvisor's token-wrapped, strict-base64 content strings."""
    if not isinstance(value, str) or len(value) < 12 or len(value) % 4:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
        return ""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8", "strict")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""
    match = TA_ENCODED_TEXT_RE.fullmatch(decoded)
    return clean_text(match.group("text")) if match else ""


def _find_encoded_graphql_text(mapping, field_names):
    wanted = {name.casefold() for name in field_names}
    if isinstance(mapping, dict):
        for key, value in mapping.items():
            if str(key).casefold() in wanted:
                candidates = [value]
                if isinstance(value, dict):
                    candidates.extend(value.get(name) for name in ("text", "value"))
                for candidate in candidates:
                    decoded = _decode_ta_content_text(candidate)
                    if decoded:
                        return decoded
        for value in mapping.values():
            found = _find_encoded_graphql_text(value, field_names)
            if found:
                return found
    elif isinstance(mapping, list):
        for value in mapping:
            found = _find_encoded_graphql_text(value, field_names)
            if found:
                return found
    return ""


def _decimal_amount(value):
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _price_parts(value, fallback_currency="USD"):
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return _decimal_amount(value), fallback_currency
    if not isinstance(value, dict):
        return None, ""
    currency = value.get("currency") or value.get("currencyCode") or fallback_currency
    for key in ("amount", "value", "total", "price"):
        child = value.get(key)
        if isinstance(child, dict):
            amount, nested_currency = _price_parts(child, currency)
        else:
            amount, nested_currency = _decimal_amount(child), currency
        if amount is not None:
            return amount, str(nested_currency or fallback_currency).upper()
    for key in ("totalPrice", "displayPrice", "pricing"):
        if key in value:
            amount, nested_currency = _price_parts(value[key], currency)
            if amount is not None:
                return amount, nested_currency
    return None, ""


def _format_graphql_money(amount, currency):
    if amount is None:
        return ""
    currency = clean_text(str(currency or "USD")).upper()
    return f"{currency} {Decimal(amount).quantize(Decimal('0.01'))}"


def _graphql_empty_pricing(status="unknown", message="", source=""):
    availability = {"status": status}
    if message:
        availability["message"] = message
    if source:
        availability["source"] = source
    result = {
        "base_price": "",
        "booking_date": "",
        "travelers": "",
        "packages": [],
        "availability": availability,
    }
    if status in {"available", "date-required", "free", "not-published"}:
        result["status"] = status
    elif status in {"sold-out", "closed", "unavailable"}:
        result["status"] = "unavailable"
    return result


def _graphql_party(selection, pax_response):
    passenger_mix = selection.get("passengerMix")
    if not isinstance(passenger_mix, list):
        return ""
    pax_mix = pax_response.get("data", {}).get("paxMix") if pax_response else None
    result = pax_mix.get("result") if isinstance(pax_mix, dict) else None
    bands = result.get("ageBands", []) if isinstance(result, dict) else []
    titles = {
        str(band.get("id")): clean_text(str(band.get("title", "")))
        for band in bands
        if isinstance(band, dict) and band.get("id") is not None
    }
    parts = []
    for item in passenger_mix:
        if not isinstance(item, dict):
            continue
        count = item.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            continue
        title = titles.get(str(item.get("bandId")), "traveler")
        if count != 1 and not title.casefold().endswith("s"):
            title += "s"
        parts.append(f"{count} {title.lower()}")
    return " and ".join(parts)


def _graphql_package_status(grade):
    status_text = " ".join(
        clean_text(str(grade.get(key, "")))
        for key in ("availability", "availabilityStatus", "status")
        if grade.get(key) is not None
    )
    if re.search(r"sold[ -]?out", status_text, re.I):
        return "sold-out"
    if re.search(r"unavailable|not available|failed", status_text, re.I):
        return "unavailable"
    if re.search(r"closed", status_text, re.I):
        return "closed"
    return "available"


def _graphql_product_pricing(evidence, product):
    product_code = product.get("productCode")
    if not isinstance(product_code, str) or not product_code.strip():
        return _graphql_empty_pricing(
            "not-published",
            "Tripadvisor did not publish a bookable product code.",
            "graphql:fullProduct.productCode",
        )
    selection = evidence["selection"]
    calendar = _first_mapping(
        _graphql_response(evidence, "priceCalendar")
        .get("data", {})
        .get("priceCalendar")
    )
    rows = calendar.get("datesAndPrices", []) if isinstance(calendar, dict) else []
    travel_date = selection.get("travelDate")
    if not travel_date:
        return _graphql_empty_pricing(
            "date-required",
            "No priced travel date was available in the captured calendar.",
            "graphql:priceCalendar.datesAndPrices",
        )
    selected_row = next(
        (row for row in rows if isinstance(row, dict) and row.get("date") == travel_date),
        {},
    )
    base_amount, base_currency = _price_parts(selected_row.get("price"), "USD")

    pax_response = (
        _graphql_response(evidence, "pax", last=True)
        if "pax" in evidence["queries"]
        else {}
    )
    party = _graphql_party(selection, pax_response)
    if not selection.get("passengerMix"):
        pax_mix = pax_response.get("data", {}).get("paxMix")
        pax_failed = isinstance(pax_mix, dict) and pax_mix.get("resultStatus") == "FAILED"
        pricing = _graphql_empty_pricing(
            "unknown" if pax_failed else "date-required",
            (
                "Tripadvisor's package lookup failed; current availability is not confirmed."
                if pax_failed
                else "A valid traveler mix was not returned for the selected date."
            ),
            "graphql:paxMix",
        )
        pricing.update(
            {
                "base_price": _format_graphql_money(base_amount, base_currency),
                "booking_date": travel_date,
            }
        )
        if pax_failed:
            pricing["availability"]["reason"] = "pax_failed"
            pricing["note"] = (
                "Tripadvisor advertised this starting price, but its package/traveler "
                "lookup failed. Current availability is not confirmed."
            )
        return pricing

    package_result = _graphql_response(evidence, "packages", last=True).get(
        "data", {}
    ).get("tourGrades")
    if not isinstance(package_result, dict) or package_result.get(
        "resultStatus"
    ) == "FAILED":
        pricing = _graphql_empty_pricing(
            "unknown",
            "Tripadvisor's package lookup failed; current availability is not confirmed.",
            "graphql:tourGrades",
        )
        pricing.update(
            {
                "base_price": _format_graphql_money(base_amount, base_currency),
                "booking_date": travel_date,
                "travelers": party,
            }
        )
        pricing["availability"]["reason"] = "tour_grades_failed"
        pricing["note"] = (
            "Tripadvisor advertised this starting price, but its package-options "
            "lookup failed. Current availability is not confirmed."
        )
        return pricing
    result = package_result.get("result")
    grades = result.get("tourGrades", []) if isinstance(result, dict) else []
    grades = [grade for grade in grades if isinstance(grade, dict)]
    if not grades:
        pricing = _graphql_empty_pricing(
            "unknown",
            "Tripadvisor returned no package rows; current availability is not confirmed.",
            "graphql:tourGrades.result.tourGrades",
        )
        pricing.update(
            {
                "base_price": _format_graphql_money(base_amount, base_currency),
                "booking_date": travel_date,
                "travelers": party,
            }
        )
        pricing["availability"]["reason"] = "tour_grades_empty"
        return pricing

    cancellation = _graphql_response(evidence, "cancellation")
    cancellation_product = _first_mapping(
        cancellation.get("data", {}).get("fullProduct")
    )
    cancellation_type = _find_graphql_text(
        cancellation_product or {},
        ("cancellationText", "cancellationDescription", "cancellationConditions"),
    )
    if not cancellation_type and isinstance(cancellation_product, dict):
        policy = cancellation_product.get("cancellationPolicy")
        if isinstance(policy, dict) and isinstance(policy.get("type"), str):
            cancellation_type = f"Cancellation policy: {clean_text(policy['type'])}"
    if not cancellation_type and isinstance(cancellation_product, dict):
        conditions = cancellation_product.get("cancellationConditions")
        if isinstance(conditions, dict) and isinstance(
            conditions.get("cancellationPolicyType"), str
        ):
            cancellation_type = (
                "Cancellation policy: "
                + clean_text(conditions["cancellationPolicyType"])
            )

    packages = []
    numeric_amounts = []
    for grade in grades:
        amount, currency = _price_parts(
            grade.get("price")
            or grade.get("totalPrice")
            or grade.get("pricing"),
            base_currency or "USD",
        )
        if amount is not None:
            numeric_amounts.append(amount)
        status = _graphql_package_status(grade)
        description = _find_graphql_text(
            grade,
            ("description", "details", "inclusions", "productDescription"),
        )
        if cancellation_type:
            description = " · ".join(
                item for item in (description, cancellation_type) if item
            )
        package = {
            "name": _find_graphql_text(grade, ("title", "name", "gradeTitle")),
            "description": description,
            "available_times": _find_graphql_text(
                grade, ("availableTimes", "startTime", "departureTime")
            ),
            "total_price": _format_graphql_money(amount, currency),
            "party": party,
            "unit_price": "",
            "unit": "",
            "availability": status,
        }
        if status in {"sold-out", "closed", "unavailable"}:
            package["availability_message"] = {
                "sold-out": "Sold out",
                "closed": "Closed",
                "unavailable": "Unavailable",
            }[status]
        packages.append(package)

    statuses = [package["availability"] for package in packages]
    all_zero = bool(numeric_amounts) and all(amount == 0 for amount in numeric_amounts)
    if all(status in {"sold-out", "closed", "unavailable"} for status in statuses):
        global_status = "unavailable"
        message = "All captured packages were unavailable."
    elif all_zero and len(numeric_amounts) == len(packages):
        global_status = "free"
        message = "All captured package totals were explicitly zero."
    elif not numeric_amounts:
        global_status = "not-published"
        message = "Packages were returned without a published numeric price."
    else:
        global_status = "available"
        message = "Priced packages were returned for the selected details."
    pricing = _graphql_empty_pricing(
        global_status, message, "graphql:tourGrades.result.tourGrades"
    )
    pricing.update(
        {
            "base_price": _format_graphql_money(base_amount, base_currency),
            "booking_date": travel_date,
            "travelers": party,
            "packages": packages,
        }
    )
    return pricing


def _recursive_string_values(value):
    if isinstance(value, str):
        cleaned = clean_text(value)
        return [cleaned] if cleaned else []
    if isinstance(value, dict):
        result = []
        for child in value.values():
            result.extend(_recursive_string_values(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_recursive_string_values(child))
        return result
    return []


def _graphql_venue_pricing(data_model):
    strings = _recursive_string_values(data_model)
    joined = " ".join(strings)
    if re.search(
        r"\b(?:free admission|free entry|admission is free|entry is free|"
        r"entrance is free|free to enter)\b",
        joined,
        re.I,
    ):
        return _graphql_empty_pricing(
            "free",
            "The venue evidence explicitly states that admission is free.",
            "graphql:Result.dataModel",
        )
    if re.search(r"\b(?:no public price|price not published)\b", joined, re.I):
        return _graphql_empty_pricing(
            "not-published",
            "The venue evidence explicitly says no public price is published.",
            "graphql:Result.dataModel",
        )
    price_text = ""
    if isinstance(data_model, dict):
        for key, value in data_model.items():
            if re.search(r"price|admission|ticket", str(key), re.I):
                for fragment in _recursive_string_values(value):
                    match = MONEY_TEXT_RE.search(fragment)
                    if match:
                        price_text = _money_text(match.group(0))
                        break
            if price_text:
                break
    if price_text:
        pricing = _graphql_empty_pricing(
            "available",
            "The venue evidence includes a published admission price.",
            "graphql:Result.dataModel",
        )
        pricing["base_price"] = price_text
        return pricing
    return _graphql_empty_pricing()


def parse_graphql_evidence(evidence, expected_url=None):
    """Project validated browser GraphQL evidence into the existing context schema."""
    metadata = validate_graphql_evidence(evidence, expected_url=expected_url)
    route = metadata["route"]
    detail_id = metadata["detail_id"]
    review_block = _first_mapping(
        _graphql_response(evidence, "reviews")
        .get("data", {})
        .get("ReviewsProxy_getReviewListPageForLocation")
    )
    reviews = []
    seen = set()
    for raw_review in review_block.get("reviews", []):
        title = clean_text(str(raw_review.get("title") or ""))
        text = clean_text(str(raw_review.get("text") or ""))
        identity = (title, text)
        if not (title or text) or identity in seen:
            continue
        rating = raw_review.get("rating")
        if isinstance(rating, (int, float)) and not isinstance(rating, bool):
            rating = float(rating)
        else:
            rating = None
        reviews.append({"title": title, "text": text, "rating": rating})
        seen.add(identity)
        if len(reviews) >= min(MAX_REVIEW_SNIPPETS, review_block["totalCount"]):
            break

    if route == "AttractionProductReview":
        product = _first_mapping(
            _graphql_response(evidence, "detail")
            .get("data", {})
            .get("fullProduct")
        )
        title = _find_graphql_text(product, ("title", "name"))
        description = _find_graphql_text(
            product,
            ("description", "longDescription", "overview", "about"),
        )
        description = _decode_ta_content_text(description) or description
        if not description:
            description = _find_encoded_graphql_text(
                product,
                ("longDescription", "briefDescription", "description", "about"),
            )
        # Preserve the transport provenance even when Tripadvisor publishes no
        # description. The strict audit must still bind this normalized row to
        # its GraphQL artifact instead of falling back to an older HTML cache.
        description_source = "tripadvisor_graphql_product"
        pricing = _graphql_product_pricing(evidence, product)
    else:
        result = _first_mapping(
            _graphql_response(evidence, "detail", last=True)
            .get("data", {})
            .get("Result")
        )
        data_model = result.get("dataModel", {}) if isinstance(result, dict) else {}
        title = _find_graphql_text(result, ("navTitle", "title", "name"))
        description = _find_graphql_text(
            result,
            ("description", "longDescription", "overview", "about"),
        )
        description = _decode_ta_content_text(description) or description
        if not description:
            description = _find_encoded_graphql_text(
                result,
                ("about", "longDescription", "briefDescription"),
            )
        description_source = "tripadvisor_graphql_wps"
        pricing = _graphql_venue_pricing(result)

    return {
        "page_title": title,
        "canonical_url": evidence["sourceUrl"],
        "description": description,
        "description_source": description_source,
        "reviews": reviews,
        "pricing_evidence": pricing,
    }


def source_path(city):
    """Prefer quality-sorted candidates, falling back to discovery listings."""
    candidates = HERE / f"candidates_{city}.json"
    return candidates if candidates.exists() else HERE / f"listings_{city}.json"


def load_listings(city):
    path = source_path(city)
    if not path.exists():
        raise FileNotFoundError(
            f"missing {path.name}; run scrape_ta.py (and preferably dedup.py) first"
        )
    with path.open(encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise ValueError(f"{path.name} must contain a JSON list")
    return rows, path


def select_listings(rows, requested_ids=None, limit=None):
    """Select supported listings, preserving requested-ID order when supplied."""
    supported = []
    for row in rows:
        try:
            detail_identity(row)
        except (TypeError, ValueError):
            continue
        supported.append(row)

    requested = [requested_identity(item) for item in (requested_ids or [])]
    if requested:
        by_key = {}
        by_id = {}
        for row in supported:
            identity = detail_identity(row)
            by_key.setdefault(identity, row)
            by_id.setdefault(identity[1], []).append(row)
        missing = []
        resolved = []
        for route, listing_id in requested:
            if route:
                row = by_key.get((route, listing_id))
            else:
                matches = by_id.get(listing_id, [])
                if len(matches) > 1:
                    routes = ", ".join(detail_identity(row)[0] for row in matches)
                    raise ValueError(
                        f"TripAdvisor ID {listing_id} is ambiguous ({routes}); "
                        "specify the route-qualified ID or full URL"
                    )
                row = matches[0] if matches else None
            if row is None:
                missing.append(f"{route + ':' if route else ''}{listing_id}")
            else:
                resolved.append(row)
        if missing:
            raise ValueError(
                "TripAdvisor IDs not found in the city dataset: " + ", ".join(missing)
            )
        selected = []
        seen = set()
        for row in resolved:
            key = detail_key(row)
            if key not in seen:
                selected.append(row)
                seen.add(key)
    else:
        selected = supported

    return selected[:limit] if limit is not None else selected


@dataclass
class _ReviewBuilder:
    root_tag: str
    root_depth: int
    titles: list = field(default_factory=list)
    bodies: list = field(default_factory=list)
    language_texts: list = field(default_factory=list)
    rating_texts: list = field(default_factory=list)


@dataclass
class _PackageBuilder:
    root_tag: str
    root_depth: int
    titles: list = field(default_factory=list)
    descriptions: list = field(default_factory=list)
    available_times: list = field(default_factory=list)
    total_prices: list = field(default_factory=list)
    raw_texts: list = field(default_factory=list)


@dataclass
class _TextCapture:
    kind: str
    root_tag: str
    root_depth: int
    owner: object = None
    chunks: list = field(default_factory=list)


def _unique_nonempty(values):
    out = []
    seen = set()
    for value in values:
        value = clean_text(value)
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _rating_from(values):
    for value in values:
        match = re.search(r"([0-5](?:\.\d+)?)\s*(?:of\s*5|bubbles?)", value, re.I)
        if match:
            return float(match.group(1))
    return None


def _money_text(value):
    match = MONEY_TEXT_RE.search(clean_text(value))
    return clean_text(match.group(0)) if match else ""


def _cost_unit(value):
    value = clean_text(value)
    aliases = (
        ("person", r"(?:/|\bper\s+)(?:person|people)\b|\bfejenk[eé]nt\b|/fő\b"),
        ("adult", r"(?:/|\bper\s+)adult\b"),
        ("child", r"(?:/|\bper\s+)child\b"),
        ("group", r"(?:/|\bper\s+)group\b"),
        ("ticket", r"(?:/|\bper\s+)ticket\b"),
        ("vehicle", r"(?:/|\bper\s+)vehicle\b"),
        ("family", r"(?:/|\bper\s+)family\b"),
    )
    for unit, pattern in aliases:
        if re.search(pattern, value, re.I):
            return unit
    return ""


def _sentence_around(value, start, end):
    left = max((value.rfind(mark, 0, start) for mark in ".;!?"), default=-1) + 1
    rights = [position for mark in ".;!?" if (position := value.find(mark, end)) >= 0]
    right = min(rights) + 1 if rights else len(value)
    return clean_text(value[left:right])


def _additional_costs(description):
    description = clean_text(description)
    costs = []
    seen = set()
    for match in MONEY_TEXT_RE.finditer(description):
        source_text = _sentence_around(description, match.start(), match.end())
        if not ADDITIONAL_COST_RE.search(source_text):
            continue
        amount = clean_text(match.group(0))
        unit = _cost_unit(source_text)
        identity = (amount.lower(), unit, source_text.lower())
        if identity in seen:
            continue
        seen.add(identity)
        costs.append(
            {
                "amount": amount,
                **({"unit": unit} if unit else {}),
                "source_text": source_text,
            }
        )
    return costs


def _redact_money(value):
    return clean_text(MONEY_TEXT_RE.sub("[price omitted]", clean_text(value)))


class _TripadvisorDetailParser(HTMLParser):
    """Small semantic parser for the stable attributes in rendered TA pages."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open_tags = []
        self.captures = []
        self.about_texts = []
        self.json_ld = []
        self.meta_descriptions = []
        self.canonical_urls = []
        self.page_titles = []
        self.visible_prices = []
        self.booking_dates = []
        self.booking_pax = []
        self.booking_titles = []
        self.booking_ctas = []
        self.no_commerce_messages = []
        self.top_commerce_texts = []
        self.right_rail_commerce_texts = []
        self.tripadvisor_messages = []
        self.review_builders = []
        self.current_review = None
        self.package_builders = []
        self.current_package = None

    def _start_capture(self, kind, tag, owner=None):
        self.captures.append(
            _TextCapture(kind, tag, len(self.open_tags), owner=owner)
        )

    def _append_spacing(self):
        for capture in self.captures:
            capture.chunks.append(" ")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in TEXT_BREAK_TAGS:
            self._append_spacing()
        attributes = {key.lower(): value or "" for key, value in attrs}
        is_void = tag in VOID_TAGS
        if not is_void:
            self.open_tags.append(tag)

        automation = attributes.get("data-automation", "")
        test_target = attributes.get("data-test-target", "")
        href = attributes.get("href", "")

        if tag == "link" and "canonical" in attributes.get("rel", "").lower().split():
            canonical_url = clean_text(href)
            if canonical_url:
                self.canonical_urls.append(canonical_url)

        if automation == "reviewCard":
            if self.current_review is not None:
                self._finish_review(self.current_review)
            self.current_review = _ReviewBuilder(tag, len(self.open_tags))

        if re.fullmatch(r"tourGrade-\d+", automation):
            if self.current_package is not None:
                self._finish_package(self.current_package)
            self.current_package = _PackageBuilder(tag, len(self.open_tags))
            self._start_capture("package_raw", tag, self.current_package)

        if automation == "attractionsAboutContent":
            self._start_capture("about", tag)
        if automation == "apr-product-info":
            self._start_capture("product_about", tag)
        if automation in {"mainH1", "masthead_h1"}:
            self._start_capture("page_title", tag)
        if automation == "commerce_module_visible_price":
            self._start_capture("visible_price", tag)
        if automation == "inline-booking-date-picker":
            self._start_capture("booking_date", tag)
        if automation == "inline-booking-pax-picker":
            self._start_capture("booking_pax", tag)
        if automation == "inlineBookingTitle":
            self._start_capture("booking_title", tag)
        if automation in {
            "attractions-commerce-modal-primary",
            "midPageCheckAvailabilityCta",
        }:
            self._start_capture("booking_cta", tag)
        if automation == "noCommerceMessage":
            self._start_capture("no_commerce_message", tag)
        if automation == "WebPresentation_ProductAboveTheFoldCommerce":
            self._start_capture("top_commerce", tag)
        if automation == "rightRailCommerceModule":
            self._start_capture("right_rail_commerce", tag)
        if tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self._start_capture("json_ld", tag)
        if tag == "meta" and attributes.get("name", "").lower() == "description":
            description = clean_text(attributes.get("content", ""))
            if description:
                self.meta_descriptions.append(description)

        review = self.current_review
        if review is not None:
            if automation == "bubbleRatingImage":
                self._start_capture("rating", tag, review)
            aria_label = attributes.get("aria-label", "")
            if re.search(r"[0-5](?:\.\d+)?\s*(?:of\s*5|bubbles?)", aria_label, re.I):
                review.rating_texts.append(aria_label)

            is_review_title = automation == "review-title" or (
                tag == "a" and "ShowUserReviews-" in href
            )
            if is_review_title:
                self._start_capture("title", tag, review)
            if "lang" in attributes:
                self._start_capture("language", tag, review)
            if automation.lower() in BODY_MARKERS or test_target.lower() in BODY_MARKERS:
                self._start_capture("body", tag, review)

        package = self.current_package
        element_id = attributes.get("id", "")
        if package is not None:
            if re.match(r"title-\d+-inline-booking-section$", element_id):
                self._start_capture("package_title", tag, package)
            elif re.match(r"description-\d+-inline-booking-section$", element_id):
                self._start_capture("package_description", tag, package)
            elif re.match(r"available-times-label-\d+-inline-booking-section$", element_id):
                self._start_capture("package_times", tag, package)
            elif re.match(r"detailed-total-price-\d+-inline-booking-section$", element_id):
                self._start_capture("package_total", tag, package)

        if tag == "br":
            self._append_spacing()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if (
            self.current_review is None
            and clean_text(data).lower() == "message from tripadvisor"
            and len(self.open_tags) >= 2
        ):
            parent_tag = self.open_tags[-2]
            parent_depth = len(self.open_tags) - 1
            if not any(
                capture.kind == "tripadvisor_message"
                and capture.root_tag == parent_tag
                and capture.root_depth == parent_depth
                for capture in self.captures
            ):
                self.captures.append(
                    _TextCapture("tripadvisor_message", parent_tag, parent_depth)
                )
        for capture in self.captures:
            capture.chunks.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in TEXT_BREAK_TAGS:
            self._append_spacing()
        depth = len(self.open_tags)
        closing = [
            capture
            for capture in self.captures
            if capture.root_tag == tag and capture.root_depth == depth
        ]
        for capture in closing:
            self._finish_capture(capture)
            self.captures.remove(capture)

        if (
            self.current_review is not None
            and self.current_review.root_tag == tag
            and self.current_review.root_depth == depth
        ):
            self._finish_review(self.current_review)
            self.current_review = None

        if (
            self.current_package is not None
            and self.current_package.root_tag == tag
            and self.current_package.root_depth == depth
        ):
            self._finish_package(self.current_package)
            self.current_package = None

        if tag in self.open_tags:
            index = len(self.open_tags) - 1 - self.open_tags[::-1].index(tag)
            del self.open_tags[index:]

    def close(self):
        super().close()
        for capture in list(self.captures):
            self._finish_capture(capture)
        self.captures.clear()
        if self.current_review is not None:
            self._finish_review(self.current_review)
            self.current_review = None
        if self.current_package is not None:
            self._finish_package(self.current_package)
            self.current_package = None

    def _finish_capture(self, capture):
        raw_value = "".join(capture.chunks)
        if capture.kind == "json_ld":
            raw_value = raw_value.strip()
            if raw_value:
                self.json_ld.append(raw_value)
            return
        value = clean_text(raw_value)
        if not value:
            return
        if capture.kind == "about":
            self.about_texts.append(value)
        elif capture.kind == "product_about":
            value = re.sub(r"^About\s*", "", value, flags=re.I)
            value = re.sub(r"\s*Read more$", "", value, flags=re.I)
            if value:
                self.about_texts.append(value)
        elif capture.kind == "title":
            capture.owner.titles.append(value)
        elif capture.kind == "body":
            capture.owner.bodies.append(value)
        elif capture.kind == "language":
            capture.owner.language_texts.append(value)
        elif capture.kind == "rating":
            capture.owner.rating_texts.append(value)
        elif capture.kind == "page_title":
            self.page_titles.append(value)
        elif capture.kind == "visible_price":
            self.visible_prices.append(value)
        elif capture.kind == "booking_date":
            self.booking_dates.append(value)
        elif capture.kind == "booking_pax":
            self.booking_pax.append(value)
        elif capture.kind == "booking_title":
            self.booking_titles.append(value)
        elif capture.kind == "booking_cta":
            self.booking_ctas.append(value)
        elif capture.kind == "no_commerce_message":
            self.no_commerce_messages.append(value)
        elif capture.kind == "top_commerce":
            self.top_commerce_texts.append(value)
        elif capture.kind == "right_rail_commerce":
            self.right_rail_commerce_texts.append(value)
        elif capture.kind == "tripadvisor_message":
            self.tripadvisor_messages.append(value)
        elif capture.kind == "package_title":
            capture.owner.titles.append(value)
        elif capture.kind == "package_description":
            capture.owner.descriptions.append(value)
        elif capture.kind == "package_times":
            capture.owner.available_times.append(value)
        elif capture.kind == "package_total":
            capture.owner.total_prices.append(value)
        elif capture.kind == "package_raw":
            capture.owner.raw_texts.append(value)

    def _finish_review(self, review):
        if review not in self.review_builders:
            self.review_builders.append(review)

    def _finish_package(self, package):
        if package not in self.package_builders:
            self.package_builders.append(package)


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _json_ld_description(documents, listing_id=None):
    candidates = []
    for raw in documents:
        try:
            document = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for node in _walk_json(document):
            description = node.get("description")
            if not isinstance(description, str):
                continue
            description = clean_text(description)
            if not description:
                continue
            identity_text = " ".join(
                str(node.get(key, "")) for key in ("@id", "url", "name")
            )
            target_match = bool(
                listing_id
                and re.search(rf"(?:^|[-d]){re.escape(str(listing_id))}(?:-|$)", identity_text)
            )
            node_types = node.get("@type", "")
            if isinstance(node_types, list):
                node_types = " ".join(map(str, node_types))
            type_match = bool(
                re.search(
                    r"Product|TouristAttraction|LocalBusiness|Thing",
                    str(node_types),
                    re.I,
                )
            )
            candidates.append((target_match, type_match, len(description), description))
    if listing_id is not None:
        candidates = [candidate for candidate in candidates if candidate[0]]
    return max(candidates, default=(False, False, 0, ""))[3]


def _review_dict(review):
    titles = _unique_nonempty(review.titles)
    language_texts = _unique_nonempty(review.language_texts)
    bodies = _unique_nonempty(review.bodies)

    title = titles[0] if titles else (language_texts[0] if language_texts else "")
    body_candidates = bodies + [text for text in language_texts if text != title]
    body = body_candidates[0] if body_candidates else ""
    if not title and not body:
        return None
    return {
        "title": title,
        "text": body,
        "rating": _rating_from(review.rating_texts),
    }


AVAILABILITY_PATTERNS = (
    ("sold-out", re.compile(r"\bsold[\s-]*out\b", re.I)),
    (
        "closed",
        re.compile(
            r"\b(?:temporarily|permanently)\s+closed\b|"
            r"\bclosed\s+until\b|\b(?:is|are|remains?)\s+closed\b",
            re.I,
        ),
    ),
    (
        "unavailable",
        re.compile(
            r"\b(?:currently\s+)?unavailable\b|"
            r"\bnot\s+(?:currently\s+)?available\b|"
            r"\bno\s+availability\b|\bnot\s+bookable\b|"
            r"\bbookings?\s+(?:are\s+)?(?:closed|unavailable)\b",
            re.I,
        ),
    ),
    (
        "date-required",
        re.compile(
            r"\b(?:select|choose|pick|change)\s+"
            r"(?:(?:a|the|your)\s+)?date\b|\bcheck\s+availability\b",
            re.I,
        ),
    ),
)


def _classify_availability_text(value, include_closed=True):
    """Classify only explicit booking/status language from a scoped element."""
    value = clean_text(value)
    if not value:
        return ""
    for status, pattern in AVAILABILITY_PATTERNS:
        if status == "closed" and not include_closed:
            continue
        if status == "closed" and re.search(r"\bclosed\s+now\b", value, re.I):
            continue
        if pattern.search(value):
            return status
    return ""


def _package_dict(package):
    titles = _unique_nonempty(package.titles)
    descriptions = _unique_nonempty(package.descriptions)
    available_times = _unique_nonempty(package.available_times)
    total_prices = _unique_nonempty(package.total_prices)
    raw_texts = _unique_nonempty(package.raw_texts)
    raw = raw_texts[0] if raw_texts else ""
    availability_raw = raw
    for descriptive_text in titles + descriptions:
        availability_raw = availability_raw.replace(descriptive_text, " ")
    party_charges = []
    seen_party_charges = set()
    for match in PARTY_CHARGE_RE.finditer(raw):
        charge = (match.group(1), match.group(2).lower(), _money_text(match.group(3)))
        if charge not in seen_party_charges:
            party_charges.append(charge)
            seen_party_charges.add(charge)
    total_match = TOTAL_PRICE_RE.search(" ".join(total_prices))
    description = descriptions[0] if descriptions else ""
    additional_costs = _additional_costs(description)
    redacted_description = _redact_money(description)
    availability = _classify_availability_text(
        availability_raw, include_closed=False
    )
    if availability not in {"sold-out", "closed", "unavailable"}:
        availability = (
            "available"
            if total_match
            or party_charges
            or available_times
            or re.search(r"\b(?:reserve|book)\s+now\b", raw, re.I)
            else "unknown"
        )
    party = clean_text(total_match.group(2)) if total_match and total_match.group(2) else ""
    if re.search(r"\bnan\b", party, re.I) and party_charges:
        party = " and ".join(
            f"{count} {traveller_type}" for count, traveller_type, _ in party_charges
        )
    # A standalone package total is not evidence of either party size or a
    # per-traveller rate. Even if nearby rendered text resembles a charge line,
    # keep it as an opaque package/group total unless Tripadvisor explicitly
    # appends `for <party>` to the scoped total-price label.
    may_use_party_charges = not total_match or bool(total_match.group(2))
    first_party_charge = (
        party_charges[0]
        if party_charges and may_use_party_charges
        else ("", "", "")
    )
    result = {
        "name": titles[0] if titles else "",
        "description": redacted_description,
        "available_times": re.sub(r"^\.\s*Available times:\s*", "", available_times[0], flags=re.I) if available_times else "",
        "total_price": _money_text(total_match.group(1)) if total_match else "",
        "party": party,
        "unit_price": first_party_charge[2],
        "unit": first_party_charge[1],
        "availability": availability,
    }
    if redacted_description != description:
        result["source_description"] = description
    if additional_costs:
        result["additional_costs"] = additional_costs
    if availability == "sold-out":
        result["availability_message"] = "Sold out"
    elif availability == "closed":
        result["availability_message"] = "Closed"
    elif availability == "unavailable":
        result["availability_message"] = "Unavailable"
    return result


def _pricing_availability(parser, packages):
    """Return granular target-page availability without scanning global text."""
    scoped = []
    scoped.extend(
        (message, "tripadvisor-status-banner")
        for message in _unique_nonempty(parser.tripadvisor_messages)
    )
    scoped.extend(
        (message, "data-automation:noCommerceMessage")
        for message in _unique_nonempty(parser.no_commerce_messages)
    )
    scoped.extend(
        (message, "data-automation:WebPresentation_ProductAboveTheFoldCommerce")
        for message in _unique_nonempty(parser.top_commerce_texts)
    )
    scoped.extend(
        (message, "data-automation:rightRailCommerceModule")
        for message in _unique_nonempty(parser.right_rail_commerce_texts)
    )
    scoped.extend(
        (message, "data-automation:attractions-commerce-modal-primary")
        for message in _unique_nonempty(parser.booking_ctas)
    )
    scoped.extend(
        (message, "data-automation:inlineBookingTitle")
        for message in _unique_nonempty(parser.booking_titles)
    )

    classified = [
        (_classify_availability_text(message), clean_text(message)[:500], source)
        for message, source in scoped
    ]
    negative_rank = {"closed": 0, "sold-out": 1, "unavailable": 2}
    negatives = [row for row in classified if row[0] in negative_rank]
    if negatives:
        status, message, source = min(
            negatives, key=lambda row: negative_rank[row[0]]
        )
        return {"status": status, "message": message, "source": source}

    package_statuses = [
        package.get("availability", "unknown")
        for package in packages
        if isinstance(package, dict)
    ]
    if "available" in package_statuses:
        return {
            "status": "available",
            "message": "Rendered booking options were available for the selected details.",
            "source": "data-automation:availabilityTourGrades",
        }
    if package_statuses and all(status == "sold-out" for status in package_statuses):
        return {
            "status": "sold-out",
            "message": "All rendered booking options were sold out.",
            "source": "data-automation:availabilityTourGrades",
        }
    if package_statuses and all(
        status in {"sold-out", "closed", "unavailable"}
        for status in package_statuses
    ):
        return {
            "status": "unavailable",
            "message": "All rendered booking options were unavailable.",
            "source": "data-automation:availabilityTourGrades",
        }

    for message in _unique_nonempty(parser.booking_ctas):
        if re.search(r"\b(?:reserve|book)\s+now\b", message, re.I):
            return {
                "status": "available",
                "message": clean_text(message)[:500],
                "source": "data-automation:attractions-commerce-modal-primary",
            }

    date_required = [row for row in classified if row[0] == "date-required"]
    if date_required:
        status, message, source = date_required[0]
        return {"status": status, "message": message, "source": source}
    return {"status": "unknown"}


def _pricing_evidence(parser):
    packages = [_package_dict(item) for item in parser.package_builders]
    availability = _pricing_availability(parser, packages)
    evidence = {
        "base_price": (_unique_nonempty(parser.visible_prices) or [""])[0],
        "booking_date": (_unique_nonempty(parser.booking_dates) or [""])[0],
        "travelers": (_unique_nonempty(parser.booking_pax) or [""])[0],
        "packages": packages,
        "availability": availability,
    }
    if availability["status"] in {"sold-out", "closed", "unavailable"}:
        evidence["status"] = "unavailable"
    elif availability["status"] in {"available", "date-required"}:
        evidence["status"] = availability["status"]
    return evidence


def parse_detail_html(html_text, listing_id=None, review_limit=MAX_REVIEW_SNIPPETS):
    """Extract scoped description, reviews and rendered booking evidence."""
    parser = _TripadvisorDetailParser()
    parser.feed(html_text)
    parser.close()

    abouts = _unique_nonempty(parser.about_texts)
    if abouts:
        description = max(abouts, key=len)
        description_source = "tripadvisor_about"
    else:
        description = _json_ld_description(parser.json_ld, listing_id=listing_id)
        description_source = "tripadvisor_json_ld" if description else ""
    if not description and parser.meta_descriptions:
        description = max(_unique_nonempty(parser.meta_descriptions), key=len)
        description_source = "tripadvisor_meta"

    reviews = []
    canonical_urls = _unique_nonempty(parser.canonical_urls)
    canonical_url = canonical_urls[0] if canonical_urls else ""
    review_limit = max(0, review_limit)
    if review_limit == 0:
        return {
            "page_title": (_unique_nonempty(parser.page_titles) or [""])[0],
            "canonical_url": canonical_url,
            "description": description,
            "description_source": description_source,
            "reviews": reviews,
            "pricing_evidence": _pricing_evidence(parser),
        }
    seen = set()
    for builder in parser.review_builders:
        review = _review_dict(builder)
        if not review:
            continue
        identity = (review["title"], review["text"])
        if identity in seen:
            continue
        reviews.append(review)
        seen.add(identity)
        if len(reviews) >= review_limit:
            break

    return {
        "page_title": (_unique_nonempty(parser.page_titles) or [""])[0],
        "canonical_url": canonical_url,
        "description": description,
        "description_source": description_source,
        "reviews": reviews,
        "pricing_evidence": _pricing_evidence(parser),
    }


def html_looks_like_datadome_challenge(html_text):
    """Recognize the small DataDome interstitial, not normal DataDome tags."""
    lowered = (html_text or "").lower()
    return any(marker in lowered for marker in DATADOME_CHALLENGE_MARKERS)


def html_looks_valid(html_text, expected_url=None):
    if len(html_text.encode("utf-8", errors="ignore")) < MIN_HTML_BYTES:
        return False
    lowered = html_text.lower()
    if "<html" not in lowered or "tripadvisor" not in lowered:
        return False
    if html_looks_like_datadome_challenge(html_text) or any(
        marker in lowered
        for marker in (
            "verify you are human",
            "captcha-delivery",
        )
    ):
        return False
    try:
        expected_identity = detail_identity(expected_url) if expected_url else None
    except (TypeError, ValueError):
        return False
    semantic_markup = any(
        marker in html_text
        for marker in (
            'data-automation="attractionsAboutContent"',
            'data-automation="apr-product-info"',
            'data-automation="reviewCard"',
            'data-automation="mainH1"',
            'data-automation="masthead_h1"',
        )
    )
    authoritative_schema = re.search(
        r'"@type"\s*:\s*"(?:LocalBusiness|Product|TouristAttraction)"',
        html_text,
    )
    if not (semantic_markup or authoritative_schema):
        return False

    parsed = parse_detail_html(
        html_text,
        listing_id=expected_identity[1] if expected_identity else None,
        review_limit=1,
    )
    canonical_url = parsed["canonical_url"]
    try:
        canonical = urlparse(canonical_url)
        if canonical.scheme != "https" or (canonical.hostname or "").lower() not in {
            "tripadvisor.com",
            "www.tripadvisor.com",
        }:
            return False
        canonical_identity = detail_identity(canonical_url)
    except (TypeError, ValueError):
        return False
    if expected_identity and canonical_identity != expected_identity:
        return False

    return bool(
        parsed["page_title"]
        or parsed["description"]
        or any(review.get("text") for review in parsed["reviews"])
    )


def rendered_html_pricing_fallback(
    evidence,
    html_text,
    *,
    expected_url=None,
    checked_at,
):
    """Return exact-ID rendered pricing only for failed product package lookups."""
    metadata = validate_graphql_evidence(
        evidence, expected_url=expected_url or evidence.get("sourceUrl")
    )
    if metadata["route"] != "AttractionProductReview":
        return None
    selection = evidence.get("selection")
    reason = (
        selection.get("packageOptionsUnavailableReason")
        if isinstance(selection, dict)
        else None
    )
    if reason not in RENDERED_PRICING_FALLBACK_REASONS:
        return None
    if not isinstance(checked_at, str):
        return None
    try:
        if date.fromisoformat(checked_at).isoformat() != checked_at:
            return None
    except ValueError:
        return None
    source_url = expected_url or evidence["sourceUrl"]
    if not html_looks_valid(html_text, expected_url=source_url):
        return None
    parsed = parse_detail_html(
        html_text,
        listing_id=metadata["detail_id"],
        review_limit=0,
    )
    try:
        canonical_identity = detail_identity(parsed.get("canonical_url"))
    except (TypeError, ValueError):
        return None
    if canonical_identity != (metadata["route"], metadata["detail_id"]):
        return None
    pricing = parsed.get("pricing_evidence")
    if not isinstance(pricing, dict):
        return None
    if not all(
        isinstance(pricing.get(field_name), str) and pricing[field_name].strip()
        for field_name in ("booking_date", "travelers")
    ):
        return None
    packages = pricing.get("packages")
    if not isinstance(packages, list) or not packages:
        return None
    fallback = copy.deepcopy(pricing)
    fallback["provenance"] = {
        "kind": RENDERED_PRICING_FALLBACK_KIND,
        "route": metadata["route"],
        "detailId": int(metadata["detail_id"]),
        "canonicalUrl": parsed["canonical_url"],
        "checkedAt": checked_at,
        "graphqlFailureReason": reason,
    }
    return fallback


def cache_looks_valid(path, expected_url=None, expected_review_count=None):
    path = Path(path)
    if not path.exists() or path.stat().st_size < MIN_HTML_BYTES:
        return False
    try:
        html_text = path.read_text(errors="replace")
    except OSError:
        return False
    if not html_looks_valid(html_text, expected_url):
        return False
    # Older renders can contain ten English cards and still have Tripadvisor's
    # automatic locale filter active. They are not valid all-language research
    # caches even though the numeric card target happens to be satisfied.
    if rendered_review_filter_count(html_text):
        return False
    expected_identity = detail_identity(expected_url) if expected_url else None
    target = review_coverage_target(
        html_text,
        expected_review_count,
        expected_identity=expected_identity,
    )
    selected_language = rendered_product_review_language(html_text)
    if (
        expected_identity
        and expected_identity[0] == "AttractionProductReview"
        and target
        and not selected_language
    ):
        return False
    if selected_language and not selected_language.lower().startswith("all languages"):
        return False
    if not target:
        return True
    parsed = parse_detail_html(
        html_text,
        listing_id=expected_identity[1] if expected_identity else None,
    )
    return len(parsed["reviews"]) >= target


class PersistentCamoufoxRunner:
    """``subprocess.run``-compatible client for one reusable browser process.

    ``fetch_detail`` keeps its established runner contract and retry logic. The
    only difference is that this runner sends each command to a long-lived
    ``fetch_ta_detail.py serve`` process. A fresh page is still created for
    every listing, while the expensive Camoufox/browser launch happens once
    per crawler worker.
    """

    def __init__(self, popen=subprocess.Popen, selector=select.select):
        self._popen = popen
        self._selector = selector
        self._process = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._stderr_lines = deque(maxlen=80)
        self._stderr_thread = None

    def _drain_stderr(self, process):
        try:
            for line in process.stderr:
                self._stderr_lines.append(line.rstrip())
        except (AttributeError, OSError, ValueError):
            pass

    def _start_locked(self):
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._stderr_lines.clear()
        process = self._popen(
            [str(CF), str(CF_SCRAPE), "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            try:
                process.kill()
            except OSError:
                pass
            raise OSError("persistent Camoufox process did not expose control pipes")
        self._process = process
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process,),
            name=f"camoufox-stderr-{process.pid}",
            daemon=True,
        )
        self._stderr_thread.start()
        return process

    def _stderr_tail(self):
        return "\n".join(self._stderr_lines)

    def _stop_locked(self, force=False):
        process = self._process
        self._process = None
        if process is None:
            return
        if not force:
            try:
                process.stdin.close()
            except (AttributeError, OSError, ValueError):
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                force = True
        if force and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _request_from_command(command, stdout):
        mode = next(
            (candidate for candidate in ("html", "graphql") if candidate in command),
            None,
        )
        if mode is None:
            raise ValueError("expected fetch_ta_detail.py html or graphql command")
        try:
            mode_index = command.index(mode)
            url = command[mode_index + 1]
        except (AttributeError, IndexError, ValueError) as exc:
            raise ValueError(f"invalid fetch_ta_detail.py {mode} command") from exc
        wait_seconds = PAGE_SETTLE_SECONDS
        if "--wait" in command:
            wait_index = command.index("--wait")
            try:
                wait_seconds = int(command[wait_index + 1])
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError("invalid --wait value") from exc
        output_name = getattr(stdout, "name", "")
        if not output_name:
            raise ValueError("persistent Camoufox runner requires a named output file")
        return mode, url, wait_seconds, str(Path(output_name).resolve())

    def __call__(self, command, stdout=None, timeout=None, **_kwargs):
        mode, url, wait_seconds, output_path = self._request_from_command(
            command, stdout
        )
        timeout = FETCH_TIMEOUT_SECONDS if timeout is None else timeout
        with self._lock:
            process = self._start_locked()
            self._request_id += 1
            request_id = self._request_id
            request = {
                "id": request_id,
                "mode": mode,
                "url": url,
                "wait": wait_seconds,
                "output": output_path,
            }
            try:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                error = f"persistent Camoufox write failed: {type(exc).__name__}"
                tail = self._stderr_tail()
                self._stop_locked(force=True)
                return subprocess.CompletedProcess(
                    command, 1, stderr="\n".join(item for item in (error, tail) if item)
                )

            ready, _, _ = self._selector([process.stdout], [], [], timeout)
            if not ready:
                self._stop_locked(force=True)
                raise subprocess.TimeoutExpired(command, timeout)
            response_line = process.stdout.readline()
            if not response_line:
                error = "persistent Camoufox process exited without a response"
                tail = self._stderr_tail()
                self._stop_locked(force=True)
                return subprocess.CompletedProcess(
                    command, 1, stderr="\n".join(item for item in (error, tail) if item)
                )
            try:
                response = json.loads(response_line)
            except json.JSONDecodeError:
                error = f"invalid persistent Camoufox response: {response_line[:160]!r}"
                tail = self._stderr_tail()
                self._stop_locked(force=True)
                return subprocess.CompletedProcess(
                    command, 1, stderr="\n".join(item for item in (error, tail) if item)
                )

            if response.get("id") != request_id or not response.get("ok"):
                error = response.get("error") or "mismatched persistent Camoufox response"
                http_status = response.get("httpStatus")
                if http_status in {403, 429}:
                    error = f"GraphQLBlockedHTTP:{http_status}\n{error}"
                tail = self._stderr_tail()
                self._stop_locked(force=True)
                return subprocess.CompletedProcess(
                    command, 1, stderr="\n".join(item for item in (error, tail) if item)
                )
            return subprocess.CompletedProcess(command, 0, stderr=self._stderr_tail())

    def close(self):
        with self._lock:
            self._stop_locked()

    def reset(self):
        """Discard suspect browser state before ``fetch_detail`` retries."""
        with self._lock:
            self._stop_locked(force=True)


class PersistentCamoufoxRunnerPool:
    """Lazily allocate one reusable browser subprocess per executor thread."""

    def __init__(self, runner_factory=PersistentCamoufoxRunner):
        self._runner_factory = runner_factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._runners = []

    def runner(self):
        runner = getattr(self._local, "runner", None)
        if runner is None:
            runner = self._runner_factory()
            self._local.runner = runner
            with self._lock:
                self._runners.append(runner)
        return runner

    def close(self):
        with self._lock:
            runners, self._runners = self._runners, []
        for runner in runners:
            runner.close()


def fetch_detail(
    url,
    destination,
    listing_id,
    refresh=False,
    offline_cache=False,
    runner=None,
    sleeper=time.sleep,
    expected_review_count=None,
    block_controller=None,
    transport="html",
):
    """Fetch one detail artifact, preserving any previous good cache."""
    destination = Path(destination)
    if transport not in {"html", "graphql"}:
        raise ValueError(f"unsupported detail transport: {transport!r}")
    cache_valid = (
        (lambda path: graphql_cache_looks_valid(path, url))
        if transport == "graphql"
        else (
            lambda path: cache_looks_valid(
                path, url, expected_review_count=expected_review_count
            )
        )
    )
    if not refresh and cache_valid(destination):
        return "cached", True, False
    if offline_cache:
        return "FAIL (offline cache missing or invalid)", False, False

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    runner = runner or subprocess.run
    errors = []
    for attempt in range(1, FETCH_RETRIES + 1):
        if block_controller is not None and block_controller.is_blocked():
            raise DataDomeBlocked(listing_id, "peer worker detected a shared block")
        try:
            partial.unlink(missing_ok=True)
            with partial.open("w", encoding="utf-8") as handle:
                result = runner(
                    [
                        str(CF),
                        str(CF_SCRAPE),
                        transport,
                        url,
                        "--wait",
                        str(0 if transport == "graphql" else PAGE_SETTLE_SECONDS),
                    ],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=FETCH_TIMEOUT_SECONDS,
                )
            try:
                partial_text = partial.read_text(encoding="utf-8", errors="replace")
            except OSError:
                partial_text = ""
            blocked_match = (
                GRAPHQL_BLOCKED_STATUS_RE.search(str(result.stderr or ""))
                if transport == "graphql"
                else None
            )
            if blocked_match:
                reset = getattr(runner, "reset", None)
                if callable(reset):
                    reset()
                if block_controller is not None:
                    block_controller.trip()
                partial.unlink(missing_ok=True)
                raise DataDomeBlocked(
                    listing_id,
                    f"Tripadvisor GraphQL returned HTTP {blocked_match.group(1)}",
                )
            if transport == "html" and html_looks_like_datadome_challenge(
                partial_text
            ):
                reset = getattr(runner, "reset", None)
                if callable(reset):
                    reset()
                if block_controller is not None:
                    block_controller.trip()
                partial.unlink(missing_ok=True)
                raise DataDomeBlocked(listing_id)
            if result.returncode == 0 and cache_valid(partial):
                partial.replace(destination)
                suffix = f" after {attempt} attempts" if attempt > 1 else ""
                return f"fetched{suffix}", True, True
            size = partial.stat().st_size if partial.exists() else 0
            errors.append(f"attempt {attempt}: rc={result.returncode} bytes={size}")
            reset = getattr(runner, "reset", None)
            if callable(reset):
                reset()
        except DataDomeBlocked:
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            try:
                partial_text = partial.read_text(encoding="utf-8", errors="replace")
            except OSError:
                partial_text = ""
            if transport == "html" and html_looks_like_datadome_challenge(
                partial_text
            ):
                reset = getattr(runner, "reset", None)
                if callable(reset):
                    reset()
                if block_controller is not None:
                    block_controller.trip()
                partial.unlink(missing_ok=True)
                raise DataDomeBlocked(listing_id) from exc
            errors.append(f"attempt {attempt}: {type(exc).__name__}")
        if attempt < FETCH_RETRIES:
            sleeper(attempt * 2)
    partial.unlink(missing_ok=True)
    return f"FAIL ({'; '.join(errors)})", False, True


def build_context(listing, parsed, checked_at=None):
    route, listing_id = detail_identity(listing)
    name = clean_text(str(listing.get("name", "")))
    page_title = clean_text(str(parsed.get("page_title", "")))
    if name.casefold() == "the" and len(page_title.split()) > 1:
        # One discovery result was truncated to the article alone. Prefer the
        # bound detail title only for this clearly incomplete source value.
        name = clean_text(re.sub(r'["“”]', "", page_title))
    return {
        "key": f"{route}:{listing_id}",
        "id": listing_id,
        "route": route,
        "city": listing.get("city", ""),
        "name": name,
        "url": listing.get("url", ""),
        "category": listing.get("catLabel", ""),
        "subtype": listing.get("subtype", ""),
        "rating": listing.get("rating"),
        "review_count": listing.get("reviews", 0),
        "canonical_url": parsed.get("canonical_url", ""),
        "page_title": page_title,
        "description": parsed["description"],
        "description_source": parsed["description_source"],
        "reviews": parsed["reviews"],
        "pricing_evidence": parsed.get("pricing_evidence", {}),
        "checked_at": checked_at or date.today().isoformat(),
    }


def evidence_checked_at(destination, fetched_live, today=None):
    """Date the evidence was actually fetched, not merely reparsed."""
    destination = Path(destination)
    if destination.name.endswith(".graphql.json"):
        with destination.open(encoding="utf-8") as handle:
            evidence = json.load(handle)
        return _parse_graphql_checked_at(evidence.get("checkedAt")).date().isoformat()
    if fetched_live:
        return (today or date.today()).isoformat()
    modified = datetime.fromtimestamp(destination.stat().st_mtime)
    return modified.date().isoformat()


def merge_context_rows(existing, updates):
    """Replace matching identities in place and append genuinely new rows."""
    remaining = {row["key"]: row for row in updates}
    merged = []
    for row in existing:
        key = row.get("key")
        if key in remaining:
            merged.append(remaining.pop(key))
        else:
            merged.append(row)
    for row in updates:
        if row["key"] in remaining:
            merged.append(remaining.pop(row["key"]))
    return merged


def atomic_write_json(path, value):
    path = Path(path)
    partial = path.with_name(path.name + ".part")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    partial.replace(path)


def load_context(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list):
        raise ValueError(f"{path.name} must contain a JSON list")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--city",
        choices=CITIES,
        default="budapest",
        help="city dataset to use (default: budapest)",
    )
    parser.add_argument(
        "--id",
        action="append",
        default=[],
        help="TripAdvisor detail ID or URL to target; repeat for multiple IDs",
    )
    parser.add_argument(
        "--ids-file",
        type=Path,
        help="newline-delimited TripAdvisor route-qualified IDs to target",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="process every supported listing instead of the default first batch",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help=f"maximum listings; without --id the default is {DEFAULT_BATCH_LIMIT}",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="force a new Camoufox render even when a valid cache exists",
    )
    parser.add_argument(
        "--graphql",
        action="store_true",
        help=(
            "prefer the browser GraphQL evidence transport and write a "
            "route-qualified .graphql.json cache; existing HTML caches are untouched"
        ),
    )
    parser.add_argument(
        "--offline-cache",
        action="store_true",
        help="parse cached pages only; never launch the browser",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            f"parallel reusable Camoufox browser processes "
            f"(1-{MAX_WORKERS}; default: {DEFAULT_WORKERS})"
        ),
    )
    parser.add_argument(
        "--block-cooldown",
        type=float,
        default=DEFAULT_BLOCK_COOLDOWN_SECONDS,
        help=(
            "seconds to pause after a shared DataDome challenge before retrying "
            f"with one worker (default: {DEFAULT_BLOCK_COOLDOWN_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--fresh-browser-per-page",
        action="store_true",
        help="compatibility fallback: launch a new Camoufox process for every page",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.refresh and args.offline_cache:
        parser.error("--refresh and --offline-cache are mutually exclusive")
    if args.all and args.limit is not None:
        parser.error("--all and --limit are mutually exclusive")
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    if not math.isfinite(args.block_cooldown) or args.block_cooldown < 0:
        parser.error("--block-cooldown must be a finite number zero or greater")
    for requested_id in args.id:
        try:
            normalize_requested_id(requested_id)
        except ValueError as exc:
            parser.error(str(exc))
    return args


def run_fetch_phase(items, workers, fetcher, consumer, block_controller):
    """Run a bounded fetch phase and return work left after a shared block.

    At most ``workers`` futures exist at once. Successful and ordinary failed
    fetch results are consumed exactly once. A DataDome signal stops new
    scheduling; blocked, cancelled and not-yet-started items remain retryable.
    """
    items = list(items)
    remaining = {index: listing for index, listing in items}
    if not items:
        return [], None

    if workers == 1:
        for index, listing in items:
            try:
                result = fetcher(index, listing)
            except DataDomeBlocked as exc:
                block_controller.trip()
                return [
                    (item_index, item_listing)
                    for item_index, item_listing in items
                    if item_index in remaining
                ], exc
            consumer(result)
            remaining.pop(index, None)
        return [], None

    executor = ThreadPoolExecutor(max_workers=workers)
    iterator = iter(items)
    in_flight = {}
    shutdown = False

    def submit_one():
        try:
            item = next(iterator)
        except StopIteration:
            return False
        index, listing = item
        in_flight[executor.submit(fetcher, index, listing)] = item
        return True

    for _ in range(min(workers, len(items))):
        submit_one()

    try:
        while in_flight:
            done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
            blocked = None
            for future in done:
                index, _listing = in_flight.pop(future)
                try:
                    result = future.result()
                except DataDomeBlocked as exc:
                    blocked = blocked or exc
                    block_controller.trip()
                else:
                    consumer(result)
                    remaining.pop(index, None)

            if blocked is not None:
                for future in in_flight:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                shutdown = True
                # A peer may have completed valid evidence while the blocking
                # future was being observed. Preserve it; every cancelled or
                # blocked item stays in ``remaining`` for the next phase.
                for future, (index, _listing) in list(in_flight.items()):
                    if future.cancelled():
                        continue
                    try:
                        result = future.result()
                    except DataDomeBlocked:
                        continue
                    else:
                        consumer(result)
                        remaining.pop(index, None)
                return [
                    (item_index, item_listing)
                    for item_index, item_listing in items
                    if item_index in remaining
                ], blocked

            while len(in_flight) < workers and submit_one():
                pass
    finally:
        if not shutdown:
            executor.shutdown(wait=True, cancel_futures=True)

    return [], None


def block_cooldown_seconds(
    base_seconds, block_cycle, maximum=MAX_BLOCK_COOLDOWN_SECONDS
):
    """Exponentially back off repeated shared blocks without waiting forever."""
    if base_seconds <= 0:
        return 0.0
    exponent = max(0, int(block_cycle) - 1)
    cooldown = min(float(base_seconds), float(maximum))
    for _step in range(exponent):
        cooldown = min(cooldown * 2, float(maximum))
        if cooldown >= maximum:
            break
    return cooldown


def run(args, sleeper=time.sleep):
    requested_ids = list(args.id)
    if args.ids_file:
        try:
            requested_ids.extend(
                line.strip()
                for line in args.ids_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        except OSError as exc:
            print(f"error: cannot read {args.ids_file}: {exc}", file=sys.stderr)
            return 2
    limit = args.limit
    if limit is None and not requested_ids and not args.all:
        limit = DEFAULT_BATCH_LIMIT

    try:
        listings, input_path = load_listings(args.city)
        selected = select_listings(listings, requested_ids, limit)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not selected:
        print("No supported TripAdvisor detail listings selected.", file=sys.stderr)
        return 2

    context_path = HERE / f"detail_context_{args.city}.json"
    try:
        context_rows = load_context(context_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {context_path.name}: {exc}", file=sys.stderr)
        return 2

    print(
        f"Selected {len(selected)} {args.city} listing(s) from {input_path.name}; "
        f"using {'GraphQL' if args.graphql else 'HTML'} evidence; normal runs reuse "
        "the matching raw detail cache."
    )
    failures = 0
    live_fetches = 0
    runner_pool = None
    block_controller = None

    def fetch_selected(index, listing):
        route, listing_id = detail_identity(listing)
        destination = (
            detail_graphql_cache_path(listing)
            if args.graphql
            else detail_cache_path(listing)
        )
        status, ok, fetched_live = fetch_detail(
            listing["url"],
            destination,
            listing_id,
            refresh=args.refresh,
            offline_cache=args.offline_cache,
            runner=runner_pool.runner() if runner_pool is not None else None,
            expected_review_count=listing.get("reviews", 0),
            block_controller=block_controller,
            transport="graphql" if args.graphql else "html",
        )
        if fetched_live:
            # Pace each persistent worker independently. This also slows the
            # conservative single-worker path used after a shared block.
            sleeper(FETCH_DELAY_SECONDS)
        return index, listing, listing_id, destination, status, ok, fetched_live

    def consume(result):
        nonlocal context_rows, failures, live_fetches
        index, listing, listing_id, destination, status, ok, fetched_live = result
        print(f"[{index}/{len(selected)}] d{listing_id} {listing.get('name', '')}: {status}", flush=True)
        if fetched_live:
            live_fetches += 1
        if not ok:
            failures += 1
            return

        if args.graphql:
            with destination.open(encoding="utf-8") as handle:
                graphql_evidence = json.load(handle)
            parsed = parse_graphql_evidence(
                graphql_evidence, expected_url=listing["url"]
            )
            rendered_path = detail_cache_path(listing)
            if rendered_path.is_file():
                rendered_checked_at = datetime.fromtimestamp(
                    rendered_path.stat().st_mtime
                ).date().isoformat()
                fallback = rendered_html_pricing_fallback(
                    graphql_evidence,
                    rendered_path.read_text(errors="replace"),
                    expected_url=listing["url"],
                    checked_at=rendered_checked_at,
                )
                if fallback is not None:
                    parsed["pricing_evidence"] = fallback
        else:
            parsed = parse_detail_html(
                destination.read_text(errors="replace"), listing_id=listing_id
            )
        update = build_context(
            listing,
            parsed,
            checked_at=evidence_checked_at(destination, fetched_live),
        )
        context_rows = merge_context_rows(context_rows, [update])
        atomic_write_json(context_path, context_rows)
        print(
            f"    {len(parsed['description'])} description chars, "
            f"{len(parsed['reviews'])} reviews, "
            f"{len(parsed['pricing_evidence']['packages'])} packages -> {context_path.name}",
            flush=True,
        )

    pending_items = list(enumerate(selected, 1))
    active_workers = args.workers
    block_cycles = 0
    while pending_items:
        block_controller = AdaptiveBlockController()
        runner_pool = (
            None if args.fresh_browser_per_page else PersistentCamoufoxRunnerPool()
        )
        try:
            pending_items, blocked = run_fetch_phase(
                pending_items,
                active_workers,
                fetch_selected,
                consume,
                block_controller,
            )
        finally:
            if runner_pool is not None:
                runner_pool.close()
            runner_pool = None

        if blocked is None:
            break

        block_cycles += 1
        previous_workers = active_workers
        active_workers = 1
        cooldown = block_cooldown_seconds(args.block_cooldown, block_cycles)
        print(
            f"Shared DataDome block #{block_cycles} ({blocked}); stopped "
            f"{previous_workers} worker(s). {len(pending_items)} listing(s) remain "
            f"retryable and were not counted as failures. Cooling down for "
            f"{cooldown:g}s, then resuming with one worker.",
            file=sys.stderr,
            flush=True,
        )
        if cooldown:
            sleeper(cooldown)

    print(
        f"Done: {len(selected) - failures}/{len(selected)} parsed; "
        f"{live_fetches} live Camoufox fetch(es), {failures} failure(s)."
    )
    return 2 if failures else 0


def main(argv=None, sleeper=time.sleep):
    args = parse_args(argv)
    lock_path = RAW_DETAILS / f".scrape-{args.city}.lock"
    try:
        with single_instance_lock(lock_path):
            return run(args, sleeper=sleeper)
    except SingleInstanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
