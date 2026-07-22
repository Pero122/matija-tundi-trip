#!/usr/bin/env python3
"""Render one TripAdvisor detail page, including available package options.

This is intentionally a browser workflow rather than a request client. Product
pages often reveal their tour-grade names, inclusions and scoped prices only
after the default date and traveler search has been applied.
"""

import argparse
import copy
import json
import math
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox


GRAPHQL_SCHEMA_VERSION = 1
GRAPHQL_TRANSPORT = "tripadvisor-browser-graphql"
GRAPHQL_ENDPOINT = "/data/graphql/ids"
GRAPHQL_MAX_POLL_ATTEMPTS = 3
GRAPHQL_POLL_DELAY_MS = 500
GRAPHQL_MAX_DATE_CANDIDATES = 5
GRAPHQL_DATE_SELECTION_POLICY = "earliest-advertised-after-cutoff-up-to-5-dates"

REVIEW_EVIDENCE_FIELDS = (
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
)
_PERSONAL_DATA_KEYS = {
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

PRODUCT_DETAIL_QUERY_ID = "f7a273b890edf6c2"
PRODUCT_REVIEWS_QUERY_ID = "8793c5d897e589a1"
PRICE_CALENDAR_QUERY_ID = "eb4cf849c5286ed5"
PAX_QUERY_ID = "ee9e93e4b2cab211"
PACKAGES_QUERY_ID = "47cee02ce9a66960"
CANCELLATION_QUERY_ID = "471725aa61475779"
VENUE_DETAIL_QUERY_ID = "9598263f57e2fd6f"
VENUE_REVIEWS_QUERY_ID = "ef1a9f94012220d3"
VENUE_REVIEW_SELECTION_POLICY = "exact-location-id-only"

_DETAIL_ROUTE_RE = re.compile(
    r"/(AttractionProductReview|Attraction_Review)-[^?#]*?-d(\d+)(?:[-./?]|$)",
    re.I,
)


class GraphQLEvidenceError(RuntimeError):
    """Raised when browser GraphQL evidence is incomplete or inconsistent."""

    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def request_mode(request):
    """Return a validated persistent-server request mode.

    Omitting ``mode`` preserves the historical HTML behavior. Keeping this
    validation outside ``serve_requests`` also gives callers a small, pure
    schema seam to test before any browser or filesystem work begins.
    """
    mode = request.get("mode", "html")
    if mode not in {"html", "graphql"}:
        raise ValueError(f"unsupported request mode: {mode!r}")
    return mode


def _checked_at(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_detail_route(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host == "tripadvisor.com" or host.endswith(".tripadvisor.com")
    ):
        raise GraphQLEvidenceError("sourceUrl is not a Tripadvisor HTTP URL")
    match = _DETAIL_ROUTE_RE.search(parsed.path)
    if not match:
        raise GraphQLEvidenceError("sourceUrl is not a supported detail route")
    route = (
        "AttractionProductReview"
        if match.group(1).lower() == "attractionproductreview"
        else "Attraction_Review"
    )
    return route, int(match.group(2))


def _browser_graphql_batch(page, specifications):
    """Run one same-origin persisted-query batch inside the browser page."""
    payload = [
        {
            "variables": variables,
            "extensions": {"preRegisteredQueryId": persisted_query_id},
        }
        for _name, persisted_query_id, variables in specifications
    ]
    result = page.evaluate(
        """async (payload) => {
          const response = await fetch('/data/graphql/ids', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify(payload)
          });
          const text = await response.text();
          let body = null;
          try { body = JSON.parse(text); } catch (_) {}
          return {
            ok: response.ok,
            status: response.status,
            contentType: response.headers.get('content-type'),
            body,
            responseBytes: text.length
          };
        }""",
        payload,
    )
    if not isinstance(result, dict):
        raise GraphQLEvidenceError("GraphQL browser transport returned no metadata")
    if result.get("status") != 200 or result.get("ok") is not True:
        status = result.get("status")
        raise GraphQLEvidenceError(
            f"GraphQL browser transport returned HTTP {status!r}",
            http_status=status if isinstance(status, int) else None,
        )
    body = result.get("body")
    if not isinstance(body, list) or len(body) != len(specifications):
        raise GraphQLEvidenceError("GraphQL batch response count did not match request")
    return body, int(result["status"])


def _strip_unrelated_personal_data(value):
    """Remove identity/media payloads that are not needed for trip decisions."""
    if isinstance(value, list):
        return [_strip_unrelated_personal_data(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _strip_unrelated_personal_data(child)
        for key, child in value.items()
        if str(key).replace("_", "").lower() not in _PERSONAL_DATA_KEYS
    }


def _project_review_evidence(raw_response):
    """Persist review content and provenance without reviewer identity."""
    blocks = raw_response.get("data", {}).get(
        "ReviewsProxy_getReviewListPageForLocation"
    )
    if not isinstance(blocks, list):
        return {"data": {"ReviewsProxy_getReviewListPageForLocation": []}}
    projected = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        reviews = []
        for review in block.get("reviews", []):
            if not isinstance(review, dict):
                continue
            reviews.append(
                {
                    key: _strip_unrelated_personal_data(review[key])
                    for key in REVIEW_EVIDENCE_FIELDS
                    if key in review
                }
            )
        projected.append({"totalCount": block.get("totalCount"), "reviews": reviews})
    return {
        "data": {"ReviewsProxy_getReviewListPageForLocation": projected}
    }


def _evidence_response(name, raw_response):
    if name == "reviews":
        return _project_review_evidence(raw_response)
    return _strip_unrelated_personal_data(raw_response)


def _allowed_partial_graphql_errors(name, raw_response):
    """Allow only a known optional product-detail field failure."""
    errors = raw_response.get("errors")
    if not errors:
        return True
    products = raw_response.get("data", {}).get("fullProduct")
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


def _append_query_records(page, query_records, specifications):
    raw_responses, http_status = _browser_graphql_batch(page, specifications)
    for (name, persisted_query_id, variables), raw_response in zip(
        specifications, raw_responses
    ):
        if not isinstance(raw_response, dict):
            raise GraphQLEvidenceError(f"{name} returned a non-object response")
        errors = raw_response.get("errors")
        if errors and not _allowed_partial_graphql_errors(name, raw_response):
            messages = [
                str(error.get("message", error)) if isinstance(error, dict) else str(error)
                for error in errors
            ]
            raise GraphQLEvidenceError(f"{name} GraphQL error: {'; '.join(messages)}")
        if not isinstance(raw_response.get("data"), dict):
            raise GraphQLEvidenceError(f"{name} response did not contain data")
        attempts = query_records.setdefault(name, [])
        attempts.append(
            {
                "attempt": len(attempts) + 1,
                "persistedQueryId": persisted_query_id,
                "variables": copy.deepcopy(variables),
                "httpStatus": http_status,
                "evidenceResponse": _evidence_response(name, raw_response),
            }
        )
    return raw_responses


def _first_mapping(value):
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


def _validate_review_response(raw_response, detail_id, *, allow_linked_locations=False):
    block = _first_mapping(
        raw_response.get("data", {}).get(
            "ReviewsProxy_getReviewListPageForLocation"
        )
    )
    if block is None:
        raise GraphQLEvidenceError("reviews response did not contain a review list")
    reviews = [review for review in block.get("reviews", []) if isinstance(review, dict)]
    total_count = block.get("totalCount")
    if not isinstance(total_count, int) or total_count < 0:
        raise GraphQLEvidenceError("reviews response did not contain a valid totalCount")
    review_ids = {str(review.get("id")) for review in reviews if review.get("id") is not None}
    if len(review_ids) < min(10, total_count):
        raise GraphQLEvidenceError(
            f"reviews returned {len(review_ids)} unique rows for totalCount {total_count}"
        )
    location_ids = {
        str(review.get("locationId"))
        for review in reviews
        if review.get("locationId") is not None
    }
    mismatched = location_ids - {str(detail_id)}
    if mismatched and not allow_linked_locations:
        raise GraphQLEvidenceError(
            f"reviews identity mismatch for detailId {detail_id}: {sorted(map(str, mismatched))}"
        )
    return block


def _quarantine_venue_review_response(raw_response, detail_id):
    """Retain only reviews that explicitly belong to the requested venue.

    Tripadvisor can return a consolidated venue feed whose rows belong to a
    different operator/location ID. The aggregate count and rejected IDs are
    useful provenance, but foreign or identity-less review bodies must never
    enter the persisted research artifact.
    """
    block = _validate_review_response(
        raw_response, detail_id, allow_linked_locations=True
    )
    requested_id = str(detail_id)
    returned_reviews = [
        review for review in block.get("reviews", []) if isinstance(review, dict)
    ]
    accepted = [
        review
        for review in returned_reviews
        if review.get("locationId") is not None
        and str(review.get("locationId")) == requested_id
    ]
    rejected_location_ids = sorted(
        {
            str(review.get("locationId"))
            for review in returned_reviews
            if review.get("locationId") is not None
            and str(review.get("locationId")) != requested_id
        }
    )
    missing_location_id_count = sum(
        review.get("locationId") is None for review in returned_reviews
    )
    quarantined_count = len(returned_reviews) - len(accepted)
    projected = _project_review_evidence(
        {
            "data": {
                "ReviewsProxy_getReviewListPageForLocation": [
                    {"totalCount": len(accepted), "reviews": accepted}
                ]
            }
        }
    )
    selection = {
        "policy": VENUE_REVIEW_SELECTION_POLICY,
        "requestedLocationId": int(detail_id),
        "sourceTotalCount": block["totalCount"],
        "returnedCount": len(returned_reviews),
        "acceptedCount": len(accepted),
        "quarantinedCount": quarantined_count,
        "missingLocationIdCount": missing_location_id_count,
        "rejectedLocationIds": rejected_location_ids,
    }
    return projected, selection


def _product_from_response(raw_response, detail_id):
    product = _first_mapping(raw_response.get("data", {}).get("fullProduct"))
    if product is None:
        raise GraphQLEvidenceError("product detail response did not contain fullProduct")
    if str(product.get("activityId")) != str(detail_id):
        raise GraphQLEvidenceError(
            f"product identity mismatch: expected {detail_id}, got {product.get('activityId')!r}"
        )
    return product


def _collect_identity_values(value, keys=("locationId", "detailId", "contentId")):
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, (str, int)):
                found.add(str(child))
            found.update(_collect_identity_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_identity_values(child, keys))
    elif isinstance(value, str):
        # Sparse WPS venue responses can omit locationId while retaining the
        # exact bound route in their top-level JSON-LD/share metadata.
        found.update(match.group(2) for match in _DETAIL_ROUTE_RE.finditer(value))
    return found


def _venue_result_from_response(raw_response, detail_id):
    result = _first_mapping(raw_response.get("data", {}).get("Result"))
    if result is None:
        raise GraphQLEvidenceError("venue detail response did not contain Result")
    identities = _collect_identity_values(result)
    if str(detail_id) not in identities:
        raise GraphQLEvidenceError(
            f"venue identity mismatch: detailId {detail_id} was absent from Result"
        )
    return result


def _polling_token(result):
    status = result.get("status") if isinstance(result, dict) else None
    polling = status.get("pollingStatus") if isinstance(status, dict) else None
    if not isinstance(polling, dict):
        return None, 0
    return polling.get("updateToken"), polling.get("delayForNextPollInMillis") or 0


def _settled_status(raw_response, result_key):
    result = raw_response.get("data", {}).get(result_key)
    if not isinstance(result, dict):
        raise GraphQLEvidenceError(f"{result_key} response did not contain its result")
    return result.get("resultStatus"), result


def _travel_date_candidates(
    calendar_response,
    product,
    now,
    limit=GRAPHQL_MAX_DATE_CANDIDATES,
):
    """Return a bounded, chronological list of bookable advertised dates."""
    calendar = _first_mapping(
        calendar_response.get("data", {}).get("priceCalendar")
    )
    if calendar is None:
        return []
    settings = product.get("bookingConfirmationSettings") or {}
    cutoff_hours = settings.get("bookingCutoffInHours") or 0
    try:
        cutoff_hours = max(0, int(cutoff_hours))
    except (TypeError, ValueError):
        cutoff_hours = 0
    lead_days = max(1, math.ceil(cutoff_hours / 24) + 1)
    earliest = now.astimezone(timezone.utc).date() + timedelta(days=lead_days)
    candidates = {}
    for row in calendar.get("datesAndPrices", []):
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            continue
        try:
            candidate = datetime.strptime(row["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        price = row.get("price")
        if (
            candidate >= earliest
            and isinstance(price, (int, float))
            and not isinstance(price, bool)
            and price >= 0
        ):
            candidates[row["date"]] = candidate
    try:
        bounded_limit = max(0, min(int(limit), GRAPHQL_MAX_DATE_CANDIDATES))
    except (TypeError, ValueError):
        bounded_limit = GRAPHQL_MAX_DATE_CANDIDATES
    return [
        date_text
        for date_text, _candidate in sorted(
            candidates.items(), key=lambda item: (item[1], item[0])
        )[:bounded_limit]
    ]


def _select_travel_date(calendar_response, product, now):
    """Compatibility helper returning the first bounded date candidate."""
    candidates = _travel_date_candidates(calendar_response, product, now, limit=1)
    return candidates[0] if candidates else None


def _positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _choose_passenger_mix(pax_result):
    result = pax_result.get("result")
    if not isinstance(result, dict):
        return None
    age_bands = [row for row in result.get("ageBands", []) if isinstance(row, dict)]
    independent = [row for row in age_bands if not row.get("validWithAgeBands")]
    candidates = independent or age_bands
    candidates.sort(
        key=lambda row: (
            0 if re.search(r"\badult\b", str(row.get("title", "")), re.I) else 1,
            str(row.get("id", "")),
        )
    )
    global_min = _positive_int(result.get("minTravelers")) or 1
    global_max = _positive_int(result.get("maxTravelers"))
    for band in candidates:
        if band.get("id") is None:
            continue
        band_limits = band.get("travelerMinMax") or {}
        band_min = _positive_int(band_limits.get("numFrom")) or 1
        band_max = _positive_int(band_limits.get("numTo"))
        minimum = max(1, global_min, band_min)
        maxima = [value for value in (global_max, band_max) if value is not None]
        maximum = min(maxima) if maxima else None
        count = max(2, minimum)
        if maximum is not None and count > maximum:
            count = maximum
        if count >= minimum and count > 0:
            return [{"bandId": band["id"], "count": count}]
    return None


def _selected_product_language(product):
    """Choose English when offered, otherwise the first published language."""
    services = product.get("languageServices")
    if not isinstance(services, dict):
        return None
    language_map = services.get("languageInfoMap")
    if isinstance(language_map, dict):
        options = list(language_map.values())
    elif isinstance(language_map, list):
        options = language_map
    else:
        return None
    languages = []
    for option in options:
        if not isinstance(option, dict):
            continue
        language = option.get("language")
        if isinstance(language, str) and language.strip():
            languages.append(language.strip())
    return next(
        (language for language in languages if language.casefold() == "en"),
        languages[0] if languages else None,
    )


def _product_graphql_evidence(page, detail_id, queries, now):
    detail_variables = {
        "activityId": detail_id,
        "currency": "USD",
        "language": "en",
    }
    review_variables = {
        "locationId": detail_id,
        "limit": 10,
        "offset": 0,
        "filters": [],
        "sortType": "DEFAULT",
        "sortBy": "DATE",
        "language": "en",
        "doMachineTranslation": True,
    }
    detail_response, reviews_response = _append_query_records(
        page,
        queries,
        [
            ("detail", PRODUCT_DETAIL_QUERY_ID, detail_variables),
            ("reviews", PRODUCT_REVIEWS_QUERY_ID, review_variables),
        ],
    )
    product = _product_from_response(detail_response, detail_id)
    _validate_review_response(reviews_response, detail_id)
    product_code = product.get("productCode")
    selection = {
        "travelDate": None,
        "travelDateSource": "priceCalendar.datesAndPrices",
        "paxAttemptedDates": [],
        "paxDatePolicy": GRAPHQL_DATE_SELECTION_POLICY,
        "selectedLanguage": None,
        "passengerMix": None,
        "packageOptionsStatus": "UNAVAILABLE",
        "packageOptionsUnavailableReason": "product_code_missing",
    }
    if not isinstance(product_code, str) or not product_code.strip():
        return selection

    calendar_variables = {"currency": "USD", "productCode": product_code}
    cancellation_variables = {"currency": "USD", "productCode": product_code}
    calendar_response, _cancellation_response = _append_query_records(
        page,
        queries,
        [
            ("priceCalendar", PRICE_CALENDAR_QUERY_ID, calendar_variables),
            ("cancellation", CANCELLATION_QUERY_ID, cancellation_variables),
        ],
    )
    travel_dates = _travel_date_candidates(calendar_response, product, now)
    if not travel_dates:
        selection["packageOptionsUnavailableReason"] = "advertised_date_missing"
        return selection

    selected_language = _selected_product_language(product)
    selection["selectedLanguage"] = selected_language
    pax_result = None
    status = None
    travel_date = None
    for candidate_date in travel_dates:
        travel_date = candidate_date
        selection["travelDate"] = travel_date
        selection["paxAttemptedDates"].append(travel_date)
        pax_variables = {
            "currencies": ["USD"],
            "locale": "en-US",
            "travelDate": travel_date,
            "selectedLanguage": selected_language,
            "productCode": product_code,
        }
        for attempt in range(GRAPHQL_MAX_POLL_ATTEMPTS):
            pax_response = _append_query_records(
                page, queries, [("pax", PAX_QUERY_ID, pax_variables)]
            )[0]
            status, pax_result = _settled_status(pax_response, "paxMix")
            if status in {"SUCCESS", "FAILED"}:
                break
            if attempt + 1 < GRAPHQL_MAX_POLL_ATTEMPTS:
                page.wait_for_timeout(GRAPHQL_POLL_DELAY_MS)
        else:
            raise GraphQLEvidenceError(
                f"paxMix did not settle after {GRAPHQL_MAX_POLL_ATTEMPTS} attempts"
            )
        if status == "SUCCESS":
            break
    if status == "FAILED":
        selection["packageOptionsStatus"] = "UNKNOWN"
        selection["packageOptionsUnavailableReason"] = "pax_failed"
        return selection

    passenger_mix = _choose_passenger_mix(pax_result)
    selection["passengerMix"] = passenger_mix
    if not passenger_mix:
        selection["packageOptionsUnavailableReason"] = "passenger_mix_missing"
        return selection

    package_variables = {
        "productCode": product_code,
        "travelDate": travel_date,
        "passengerMix": passenger_mix,
        "currencies": ["USD"],
        "locale": "en-US",
    }
    for attempt in range(GRAPHQL_MAX_POLL_ATTEMPTS):
        package_response = _append_query_records(
            page, queries, [("packages", PACKAGES_QUERY_ID, package_variables)]
        )[0]
        package_status, _package_result = _settled_status(
            package_response, "tourGrades"
        )
        if package_status in {"SUCCESS", "FAILED"}:
            break
        if attempt + 1 < GRAPHQL_MAX_POLL_ATTEMPTS:
            page.wait_for_timeout(GRAPHQL_POLL_DELAY_MS)
    else:
        raise GraphQLEvidenceError(
            f"tourGrades did not settle after {GRAPHQL_MAX_POLL_ATTEMPTS} attempts"
        )
    package_result = (
        _package_result.get("result") if isinstance(_package_result, dict) else None
    )
    package_rows = (
        package_result.get("tourGrades") if isinstance(package_result, dict) else None
    )
    if package_status == "SUCCESS" and isinstance(package_rows, list) and any(
        isinstance(row, dict) for row in package_rows
    ):
        selection["packageOptionsStatus"] = "AVAILABLE"
        selection["packageOptionsUnavailableReason"] = None
    elif package_status == "SUCCESS":
        selection["packageOptionsStatus"] = "UNKNOWN"
        selection["packageOptionsUnavailableReason"] = "tour_grades_empty"
    else:
        selection["packageOptionsStatus"] = "UNKNOWN"
        selection["packageOptionsUnavailableReason"] = "tour_grades_failed"
    return selection


def _venue_graphql_evidence(page, detail_id, queries, session_id, pageview_uid):
    tracking = {"screenName": "Attraction_Review", "pageviewUid": pageview_uid}
    detail_variables = {
        "request": {
            "tracking": tracking,
            "routeParameters": {
                "contentType": "attraction",
                "contentId": str(detail_id),
            },
            "clientState": None,
            "updateToken": None,
        },
        "commerce": {},
        "sessionId": session_id,
        "tracking": tracking,
        "currency": "USD",
        "currentGeoPoint": None,
        "unitLength": "KILOMETERS",
    }
    review_variables = {
        "locationId": detail_id,
        "filters": [],
        "limit": 10,
        "offset": 0,
        "sortType": "DEFAULT",
        "sortBy": "DATE",
        "language": "en",
        "doMachineTranslation": True,
        "photosPerReviewLimit": 7,
    }
    detail_response, reviews_response = _append_query_records(
        page,
        queries,
        [
            ("detail", VENUE_DETAIL_QUERY_ID, detail_variables),
            ("reviews", VENUE_REVIEWS_QUERY_ID, review_variables),
        ],
    )
    result = _venue_result_from_response(detail_response, detail_id)
    projected_reviews, review_selection = _quarantine_venue_review_response(
        reviews_response, detail_id
    )
    queries["reviews"][-1]["evidenceResponse"] = projected_reviews

    for attempt in range(1, GRAPHQL_MAX_POLL_ATTEMPTS):
        update_token, delay = _polling_token(result)
        if update_token is None:
            return review_selection
        page.wait_for_timeout(
            min(1_000, max(300, int(delay) if str(delay).isdigit() else 300))
        )
        detail_variables = copy.deepcopy(detail_variables)
        detail_variables["request"]["updateToken"] = update_token
        detail_response = _append_query_records(
            page,
            queries,
            [("detail", VENUE_DETAIL_QUERY_ID, detail_variables)],
        )[0]
        result = _venue_result_from_response(detail_response, detail_id)
    update_token, _delay = _polling_token(result)
    if update_token is not None:
        raise GraphQLEvidenceError(
            f"venue detail did not settle after {GRAPHQL_MAX_POLL_ATTEMPTS} attempts"
        )
    return review_selection


def render_graphql_evidence(
    browser,
    url,
    wait_seconds=0,
    *,
    now=None,
    session_id=None,
    pageview_uid=None,
):
    """Collect immutable raw persisted-query evidence through Camoufox."""
    route, detail_id = _parse_detail_route(url)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    page = browser.new_page()
    page.on("pageerror", lambda _error: None)
    try:
        try:
            page.goto(url, timeout=45_000)
        except Exception as exc:
            print(f"nav-warn: {str(exc)[:160]}", file=sys.stderr)
        if wait_seconds > 0:
            page.wait_for_timeout(max(0, wait_seconds) * 1_000)
        queries = {}
        evidence = {
            "schemaVersion": GRAPHQL_SCHEMA_VERSION,
            "transport": GRAPHQL_TRANSPORT,
            "route": route,
            "detailId": detail_id,
            "sourceUrl": url,
            "checkedAt": _checked_at(now),
            "queries": queries,
        }
        if route == "AttractionProductReview":
            evidence["selection"] = _product_graphql_evidence(
                page, detail_id, queries, now
            )
        else:
            evidence["reviewSelection"] = _venue_graphql_evidence(
                page,
                detail_id,
                queries,
                session_id or uuid.uuid4().hex.upper(),
                pageview_uid or str(uuid.uuid4()),
            )
        return evidence
    finally:
        try:
            page.close()
        except Exception:
            pass


def _click_cookie_consent(page):
    button = page.get_by_role("button", name="I Accept", exact=True)
    if button.count():
        try:
            button.first.click(timeout=3_000)
            page.wait_for_timeout(250)
        except Exception:
            pass


def _load_product_packages(page):
    if "AttractionProductReview" not in page.url:
        return

    pax = page.locator('[data-automation="inline-booking-pax-picker"]')
    if pax.count():
        try:
            pax.first.scroll_into_view_if_needed(timeout=5_000)
            page.wait_for_timeout(250)
            pax.first.click(timeout=8_000)
            page.wait_for_timeout(250)
            update = page.get_by_role("button", name="Update search", exact=True)
            if update.count():
                update.first.click(timeout=5_000)
                page.wait_for_timeout(4_000)
        except Exception as exc:
            print(f"availability-warn: {str(exc)[:160]}", file=sys.stderr)

    # Some product-page layouts expose only the mid-page availability CTA and
    # do not render an inline passenger picker. The CTA is an independent
    # fallback, so always try it when package options are still absent.
    if not page.locator('[data-automation="availabilityTourGrades"]').count():
        cta = page.locator('[data-automation="midPageCheckAvailabilityCta"]')
        if cta.count():
            try:
                cta.first.scroll_into_view_if_needed(timeout=5_000)
                cta.first.click(timeout=8_000)
                page.wait_for_timeout(4_000)
            except Exception as exc:
                print(f"availability-warn: {str(exc)[:160]}", file=sys.stderr)


def _select_product_all_languages(page):
    """Select the explicit All languages option on product review pages."""
    try:
        # Tripadvisor currently has two product layouts: one wraps the picker
        # in ``single-select-filter-language`` and one exposes only a generic
        # listbox button inside ``apr-reviews``. Scope both to the review
        # surface so the site-wide locale control cannot be mistaken for it.
        button = page.locator(
            '[data-automation="apr-reviews"] '
            'button[aria-haspopup="listbox"][aria-label^="language:"], '
            '[data-automation="single-select-filter-language"] '
            'button[aria-haspopup="listbox"]'
        )
        if not button.count():
            return False
        label = button.first.get_attribute("aria-label") or button.first.inner_text()
        if re.search(r"\ball languages\b", label, re.I):
            return False
        button.first.scroll_into_view_if_needed(timeout=5_000)
        button.first.click(timeout=8_000)
        page.wait_for_timeout(500)
        option = page.get_by_role(
            "option", name=re.compile(r"^All languages(?:\s*\([\d,.]+\))?$", re.I)
        )
        if not option.count():
            option = page.get_by_text("All languages", exact=True)
        if not option.count():
            return False
        option.first.click(timeout=8_000)
        page.wait_for_timeout(2_000)
        return True
    except Exception as exc:
        print(f"product-language-warn: {str(exc)[:160]}", file=sys.stderr)
        return False


def _reset_venue_review_filters(page):
    """Change venue reviews from the automatic English filter to all languages."""
    try:
        button = page.locator('button[aria-label="Click to open the filter"]')
        if not button.count():
            return False
        label = button.first.inner_text()
        if not re.search(r"Filters\s*\(\s*[1-9][\d,.]*\s*\)", label, re.I):
            return False

        language = page.locator('[data-automation="ugcLanguageFilter"]')
        for _ in range(2):
            button.first.scroll_into_view_if_needed(timeout=5_000)
            button.first.click(timeout=8_000)
            page.wait_for_timeout(1_500)
            if language.count():
                break
        if not language.count():
            return False

        language.first.locator("button").first.click(timeout=8_000)
        page.wait_for_timeout(500)
        all_languages = page.locator(
            '[data-automation="ugcLanguageFilterOption_0"]'
        )
        if not all_languages.count():
            return False
        all_languages.first.click(timeout=8_000)
        page.wait_for_timeout(250)
        apply = page.get_by_role("button", name="Apply", exact=True)
        if not apply.count():
            return False
        apply.first.click(timeout=8_000)
        page.wait_for_timeout(2_000)
        return True
    except Exception as exc:
        print(f"venue-language-warn: {str(exc)[:160]}", file=sys.stderr)
        return False


def _load_all_review_languages(page):
    """Clear locale filters and wake lazy review sections before snapshotting.

    Tripadvisor can advertise reviews while initially rendering an empty
    language-filter result. Other layouts (notably low-volume product pages)
    do not create review cards until their review section enters the viewport.
    Keep this best-effort: a control changing shape must not abort the page
    render, but it also must not leave us polling an unrelated page forever.
    """
    try:
        cards = page.locator('[data-automation="reviewCard"]')
        review_surface = page.locator(
            '[data-test-target="reviews-tab"], '
            '[data-automation="apr-reviews"], #REVIEWS'
        )
        review_anchor = page.locator('a[href="#REVIEWS"]')

        clear = page.get_by_role(
            "button",
            name=re.compile(
                r"^Clear (?:all )?filters?(?: and show all languages)?$", re.I
            ),
        )
        review_control = None
        review_control_name = re.compile(
            r"^(?:All reviews|Reviews)(?:\s*\(\s*[\d,.]+\s*\))?$",
            re.I,
        )
        for role in ("button", "tab", "link"):
            candidate = page.get_by_role(role, name=review_control_name)
            if candidate.count():
                review_control = candidate
                break

        # The generic server tests and pages without reviews expose none of
        # these surfaces. Return immediately instead of adding idle waits to
        # every render.
        if not (
            cards.count()
            or review_surface.count()
            or review_anchor.count()
            or clear.count()
            or review_control is not None
        ):
            return

        if review_surface.count():
            review_surface.first.scroll_into_view_if_needed(timeout=5_000)
            page.wait_for_timeout(500)
        elif review_anchor.count():
            review_anchor.first.scroll_into_view_if_needed(timeout=5_000)
            review_anchor.first.click(timeout=8_000)
            page.wait_for_timeout(500)

        clear_clicked = False
        if clear.count():
            clear.first.scroll_into_view_if_needed(timeout=5_000)
            clear.first.click(timeout=8_000)
            page.wait_for_timeout(1_500)
            clear_clicked = True

        product_language_changed = _select_product_all_languages(page)
        venue_filters_changed = _reset_venue_review_filters(page)
        if cards.count() >= 10 and (
            clear_clicked or product_language_changed or venue_filters_changed
        ):
            return

        # Venue controls commonly say just "All reviews" (without a count),
        # while product layouts may use "Reviews". Clicking the active review
        # control is harmless and reliably wakes several lazy layouts.
        if review_control is not None:
            review_control.first.scroll_into_view_if_needed(timeout=5_000)
            review_control.first.click(timeout=8_000)
            page.wait_for_timeout(1_500)
            # Some venue layouts do not render the Filters toolbar until the
            # All reviews control is activated. Re-run the all-language
            # actions against the newly mounted controls before accepting ten
            # otherwise still locale-filtered cards.
            if not clear_clicked and clear.count():
                clear.first.scroll_into_view_if_needed(timeout=5_000)
                clear.first.click(timeout=8_000)
                page.wait_for_timeout(1_500)
                clear_clicked = True
            if not product_language_changed:
                product_language_changed = _select_product_all_languages(page)
            if not venue_filters_changed:
                venue_filters_changed = _reset_venue_review_filters(page)
            if cards.count() >= 10:
                return

        unchanged = 0
        for _ in range(8):
            before = cards.count()
            if before >= 10:
                break
            if before:
                cards.nth(before - 1).scroll_into_view_if_needed(timeout=5_000)
            elif review_surface.count():
                review_surface.first.scroll_into_view_if_needed(timeout=5_000)
            elif review_anchor.count():
                review_anchor.first.scroll_into_view_if_needed(timeout=5_000)
            else:
                break
            page.wait_for_timeout(750)
            after = cards.count()
            if after >= 10:
                break
            if after == before:
                unchanged += 1
                if unchanged >= 2:
                    break
            else:
                unchanged = 0
    except Exception as exc:
        print(f"reviews-warn: {str(exc)[:160]}", file=sys.stderr)


def render_page_html(browser, url, wait_seconds):
    """Render one fresh page while allowing the browser process to be reused."""
    page = browser.new_page()
    page.on("pageerror", lambda _error: None)
    try:
        try:
            page.goto(url, timeout=45_000)
        except Exception as exc:
            print(f"nav-warn: {str(exc)[:160]}", file=sys.stderr)
        page.wait_for_timeout(max(0, wait_seconds) * 1_000)
        _click_cookie_consent(page)
        _load_product_packages(page)
        _load_all_review_languages(page)
        return page.content()
    finally:
        try:
            page.close()
        except Exception:
            pass


def serve_requests(browser, input_stream=sys.stdin, output_stream=sys.stdout):
    """Serve newline-delimited render requests using one Camoufox browser.

    The caller supplies an output path rather than transporting multi-megabyte
    HTML documents through the control pipe. Each request still gets a brand
    new page, matching the standalone command's page-level behavior while
    avoiding browser startup for every listing.
    """
    for raw_line in input_stream:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        request_id = None
        try:
            request = json.loads(raw_line)
            request_id = request.get("id")
            if request.get("cmd") == "close":
                print(
                    json.dumps({"id": request_id, "ok": True, "closed": True}),
                    file=output_stream,
                    flush=True,
                )
                return 0
            url = request["url"]
            output_path = Path(request["output"])
            mode = request_mode(request)
            wait_seconds = int(request.get("wait", 0 if mode == "graphql" else 7))
            if mode == "graphql":
                rendered = json.dumps(
                    render_graphql_evidence(browser, url, wait_seconds),
                    ensure_ascii=False,
                    indent=2,
                ) + "\n"
            else:
                rendered = render_page_html(browser, url, wait_seconds)
            output_path.write_text(rendered, encoding="utf-8")
            response = {
                "id": request_id,
                "ok": True,
                "mode": mode,
                "bytes": len(rendered.encode("utf-8")),
            }
        except Exception as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
            if isinstance(exc, GraphQLEvidenceError) and exc.http_status is not None:
                response["httpStatus"] = exc.http_status
        print(json.dumps(response), file=output_stream, flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["html", "graphql", "serve"])
    parser.add_argument("url", nargs="?")
    parser.add_argument("--wait", type=int)
    args = parser.parse_args(argv)
    if args.cmd == "serve" and args.url:
        parser.error("serve does not accept a URL; send JSON requests on stdin")
    if args.cmd in {"html", "graphql"} and not args.url:
        parser.error(f"{args.cmd} requires a URL")

    wait_seconds = args.wait
    if wait_seconds is None:
        wait_seconds = 0 if args.cmd == "graphql" else 7

    with Camoufox(headless=True) as browser:
        if args.cmd == "serve":
            return serve_requests(browser)
        if args.cmd == "graphql":
            print(
                json.dumps(
                    render_graphql_evidence(browser, args.url, wait_seconds),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(render_page_html(browser, args.url, wait_seconds))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
