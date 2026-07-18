#!/usr/bin/env python3
"""Read-only completion gate for cached Tripadvisor research evidence.

The normal strict run compares the validator's visible, route-qualified
Tripadvisor inventory with ``detail_context_budapest.json`` exactly.  It then
reparses every corresponding raw page with the current parser and proves that
the stored evidence is still a byte-for-byte-equivalent JSON value.

Use ``--allow-partial`` only while a crawl is active.  Partial mode reports
missing keys without failing, but it still rejects unexpected keys and every
integrity problem in rows that already exist.  This script never fetches a
page and never writes either raw or derived evidence.

Examples:
  python audit_detail_context.py
  python audit_detail_context.py --allow-partial
  python audit_detail_context.py --allow-partial --json
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Iterable
import unicodedata
from urllib.parse import urlparse

from scrape_ta_details import (
    MAX_REVIEW_SNIPPETS,
    PARTY_CHARGE_RE,
    clean_text,
    detail_cache_path,
    detail_graphql_cache_path,
    detail_identity,
    parse_graphql_evidence,
    parse_detail_html,
    rendered_product_review_language,
    rendered_product_review_scope,
    rendered_review_filter_count,
    rendered_review_total,
)


HERE = Path(__file__).resolve().parent
DEFAULT_CONTEXT = HERE / "detail_context_budapest.json"
DEFAULT_RAW_DIR = HERE / "raw" / "details"
DEFAULT_VALIDATOR = HERE / "validate_discover_groups.mjs"
ROUTE_KEY_RE = re.compile(
    r"^(Attraction_Review|AttractionProductReview):(\d+)$"
)
GQL_DETAIL_URL_RE = re.compile(
    r"/(?:Attraction_Review|AttractionProductReview)-g\d+-d(\d+)-",
    re.I,
)
ALLOWED_DESCRIPTION_SOURCES = {
    "",
    "tripadvisor_about",
    "tripadvisor_json_ld",
    "tripadvisor_meta",
    "tripadvisor_graphql_product",
    "tripadvisor_graphql_wps",
}
ALLOWED_AVAILABILITY = {
    "available",
    "sold-out",
    "closed",
    "unavailable",
    "date-required",
    "unknown",
    "free",
    "not-published",
}
NEGATIVE_AVAILABILITY = {"sold-out", "closed", "unavailable"}
PARSED_FIELDS = (
    "page_title",
    "canonical_url",
    "description",
    "description_source",
    "reviews",
    "pricing_evidence",
)
AUDIT_GRAPHQL_SCHEMA_VERSION = 1
AUDIT_GRAPHQL_TRANSPORT = "tripadvisor-browser-graphql"
AUDIT_GRAPHQL_REVIEW_FIELDS = {
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
AUDIT_GRAPHQL_PERSONAL_KEYS = {
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
AUDIT_GRAPHQL_QUERY_IDS = {
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
AUDIT_VENUE_REVIEW_SELECTION_POLICY = "exact-location-id-only"
AUDIT_GRAPHQL_MAX_POLL_ATTEMPTS = 3
AUDIT_GRAPHQL_MAX_DATE_CANDIDATES = 5
AUDIT_GRAPHQL_DATE_SELECTION_POLICY = (
    "earliest-advertised-after-cutoff-up-to-5-dates"
)
AUDIT_RENDERED_FALLBACK_KIND = "rendered-html-fallback"
AUDIT_RENDERED_FALLBACK_REASONS = {
    "pax_failed",
    "tour_grades_failed",
    "tour_grades_empty",
}
AUDIT_RENDERED_FALLBACK_FIELDS = {
    "kind",
    "route",
    "detailId",
    "canonicalUrl",
    "checkedAt",
    "graphqlFailureReason",
}

# These real pages previously exposed two subtle parser regressions.  Keep the
# assertions semantic rather than pinning dynamic prices: a broken NaN party
# must be reconstructed from the rendered charge lines, while a standalone
# total must never acquire an invented party or per-person rate.
CRITICAL_NAN_PARTY_KEY = "AttractionProductReview:24152959"
CRITICAL_STANDALONE_KEYS = {
    "AttractionProductReview:25077094",
    "AttractionProductReview:11473990",
    "AttractionProductReview:28032940",
    "AttractionProductReview:11763907",
    "AttractionProductReview:13000415",
    "AttractionProductReview:11470531",
    "AttractionProductReview:21137356",
    "AttractionProductReview:19978697",
    "AttractionProductReview:11473861",
    "AttractionProductReview:26361280",
    "AttractionProductReview:11471004",
    "AttractionProductReview:11467260",
    "AttractionProductReview:17448110",
    "AttractionProductReview:20100566",
    "AttractionProductReview:24806901",
    "AttractionProductReview:11473860",
    "AttractionProductReview:18910152",
}
PAGINATION_SHORTFALL_KEY = "AttractionProductReview:34325420"
VENUE_PAGINATION_SHORTFALL_KEY = "Attraction_Review:276808"
PAGINATION_NEXT_MARKER = 'data-smoke-attr="pagination-next-arrow"'
AUDIT_NUMBER = (
    r"(?:\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{1,2})?|"
    r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|"
    r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|"
    r"\d+(?:[.,]\d{1,2})?)"
)
AUDIT_CURRENCY = r"(?:US\$|CA\$|NZ\$|A\$|C\$|[$€£¥]|USD|EUR|GBP|HUF|NZD|AUD|CAD|Ft)"
AUDIT_MONEY = rf"(?:{AUDIT_CURRENCY}\s*{AUDIT_NUMBER}|{AUDIT_NUMBER}\s*{AUDIT_CURRENCY})"
AUDIT_MONEY_RE = re.compile(AUDIT_MONEY, re.I)
AUDIT_TOTAL_RE = re.compile(
    rf"\bTotal price:\s*({AUDIT_MONEY})(?:\s+for\s+([^.;]+))?", re.I
)
AUDIT_CHARGE_RE = re.compile(
    rf"(\d+)\s+(Adults?|Seniors?|Youths?|Children|Infants)\s+x\s+({AUDIT_MONEY})",
    re.I,
)
VOID_TAGS = {
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


@dataclass(frozen=True)
class AuditIssue:
    key: str
    code: str
    message: str


@dataclass
class AuditReport:
    expected: int
    present: int
    audited: int
    allow_partial: bool
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    issues: list[AuditIssue] = field(default_factory=list)
    coverage_sources: dict[str, int] = field(
        default_factory=lambda: {"live": 0, "discovery": 0, "graphql": 0}
    )

    @property
    def ok(self) -> bool:
        return not self.issues and (self.allow_partial or not self.missing)

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": "partial" if self.allow_partial else "strict",
            "expected": self.expected,
            "present": self.present,
            "audited": self.audited,
            "missing": self.missing,
            "unexpected": self.unexpected,
            "coverageSources": self.coverage_sources,
            "issues": [asdict(issue) for issue in self.issues],
        }


class _RenderedPriceSurfaceParser(HTMLParser):
    """Extract visible package/base-price text independently of the app parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.capture: dict[str, Any] | None = None
        self.package_texts: list[str] = []
        self.base_price_texts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        automation = attrs_dict.get("data-automation", "") or ""
        if self.capture is None:
            kind = None
            if automation.startswith("tourGrade-"):
                kind = "package"
            elif automation == "commerce_module_visible_price":
                kind = "base"
            if kind:
                self.capture = {
                    "kind": kind,
                    "tag": tag,
                    "depth": self.depth,
                    "chunks": [],
                }
        if tag not in VOID_TAGS:
            self.depth += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.capture["chunks"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID_TAGS:
            self.depth = max(0, self.depth - 1)
        if (
            self.capture is not None
            and tag == self.capture["tag"]
            and self.depth == self.capture["depth"]
        ):
            value = clean_text(" ".join(self.capture["chunks"]))
            target = (
                self.package_texts
                if self.capture["kind"] == "package"
                else self.base_price_texts
            )
            target.append(value)
            self.capture = None


def _rendered_price_surfaces(html_text: str) -> tuple[list[str], list[str]]:
    parser = _RenderedPriceSurfaceParser()
    parser.feed(html_text)
    parser.close()
    return parser.package_texts, parser.base_price_texts


def _rendered_venue_review_scope(html_text: str) -> str:
    """Return the bounded venue-review surface around its first review card."""
    html_text = html_text or ""
    first_card = html_text.find('data-automation="reviewCard"')
    if first_card < 0:
        return ""
    window_start = max(0, first_card - 100_000)
    review_start = html_text.rfind("All reviews", window_start, first_card)
    if review_start < 0:
        return ""
    return html_text[review_start : first_card + 200_000]


def _money_value(value: str) -> tuple[str, Decimal] | None:
    """Return a comparable currency/value pair for a rendered money token."""
    match = AUDIT_MONEY_RE.search(value or "")
    if not match:
        return None
    token = match.group(0).replace("\u00a0", " ").strip()
    currency_match = re.search(AUDIT_CURRENCY, token, re.I)
    number_match = re.search(AUDIT_NUMBER, token)
    if not currency_match or not number_match:
        return None
    currency_token = currency_match.group(0).upper().replace(" ", "")
    currency = {
        "$": "USD",
        "US$": "USD",
        "€": "EUR",
        "£": "GBP",
        "FT": "HUF",
        "¥": "JPY",
        "A$": "AUD",
        "C$": "CAD",
        "CA$": "CAD",
        "NZ$": "NZD",
    }.get(currency_token, currency_token)
    number = number_match.group(0).replace(" ", "").replace("\u00a0", "")
    if "," in number and "." in number:
        decimal_separator = "," if number.rfind(",") > number.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        number = number.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in number:
        tail = number.rsplit(",", 1)[1]
        number = number.replace(",", "." if len(tail) <= 2 else "")
    elif "." in number:
        tail = number.rsplit(".", 1)[1]
        if len(tail) == 3:
            number = number.replace(".", "")
    try:
        return currency, Decimal(number)
    except InvalidOperation:
        return None


def _issue(
    issues: list[AuditIssue], key: str, code: str, message: str
) -> None:
    issues.append(AuditIssue(key=key, code=code, message=message))


def load_visible_inventory(
    validator: Path = DEFAULT_VALIDATOR,
    *,
    site_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, Any]]:
    """Load the validator-authoritative visible inventory without web access."""
    command = [
        "node",
        str(validator),
        "--print-visible-json",
        "--allow-partial-research",
        "--inventory-only",
    ]
    if site_root is not None:
        command.extend(["--site-root", str(site_root)])
    result = runner(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise ValueError(
            "visible inventory validation failed: "
            + (result.stderr.strip() or result.stdout.strip())[:800]
        )
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("validator did not return a JSON inventory") from exc
    if not isinstance(rows, list):
        raise ValueError("validator inventory must be a JSON list")
    return rows


def load_context_rows(path: Path = DEFAULT_CONTEXT) -> list[Any]:
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(rows, list):
        raise ValueError(f"{Path(path).name} must contain a JSON list")
    return rows


def _route_inventory(
    inventory: Iterable[Any], issues: list[AuditIssue]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(inventory):
        if not isinstance(item, dict):
            _issue(issues, "*", "inventory-row", f"row {index} is not an object")
            continue
        key = item.get("key")
        if not isinstance(key, str) or not ROUTE_KEY_RE.fullmatch(key):
            # Editorial ideas deliberately do not have raw Tripadvisor pages.
            continue
        if key in result:
            _issue(issues, key, "inventory-duplicate", "visible key is duplicated")
            continue
        result[key] = item
    if not result:
        _issue(issues, "*", "inventory-empty", "no route-qualified visible keys")
    return result


def _context_index(
    rows: Iterable[Any], issues: list[AuditIssue]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            _issue(issues, "*", "context-row", f"row {index} is not an object")
            continue
        key = row.get("key")
        if not isinstance(key, str) or not key:
            _issue(issues, "*", "context-key", f"row {index} has no string key")
            continue
        if key in result:
            _issue(issues, key, "context-duplicate", "context key is duplicated")
            continue
        result[key] = row
    return result


def _iter_strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _iter_strings(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_strings(child, f"{path}[{index}]")


def _audit_text(row: dict[str, Any], key: str, issues: list[AuditIssue]) -> None:
    for path, value in _iter_strings(row):
        bad_control = next(
            (
                ord(char)
                for char in value
                if ord(char) < 32 or 0x7F <= ord(char) <= 0x9F
            ),
            None,
        )
        if bad_control is not None:
            _issue(
                issues,
                key,
                "text-control",
                f"{path} contains control U+{bad_control:04X}",
            )
        if "\ufffd" in value or any("\ud800" <= char <= "\udfff" for char in value):
            _issue(issues, key, "text-unicode", f"{path} contains invalid Unicode")
        if any(char in value for char in ("\ufff9", "\ufffa", "\ufffb")):
            _issue(issues, key, "text-interlinear", f"{path} contains annotation controls")
        if unicodedata.normalize("NFC", value) != value:
            _issue(issues, key, "text-nfc", f"{path} is not NFC-normalized")

    evidence_paths = {
        "page_title": row.get("page_title"),
        "description": row.get("description"),
        "reviews": row.get("reviews"),
        "pricing_evidence": row.get("pricing_evidence"),
    }
    for path, value in _iter_strings(evidence_paths):
        if clean_text(value) != value:
            _issue(
                issues,
                key,
                "text-normalization",
                f"{path} is not parser-normalized text",
            )


def _valid_rating(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 <= float(value) <= 5
    )


def _valid_review_rating(value: Any) -> bool:
    return _valid_rating(value) and float(value) >= 1


def _audit_reviews(
    reviews: Any, key: str, issues: list[AuditIssue]
) -> None:
    if not isinstance(reviews, list):
        _issue(issues, key, "reviews-type", "reviews must be a list")
        return
    if len(reviews) > MAX_REVIEW_SNIPPETS:
        _issue(
            issues,
            key,
            "reviews-limit",
            f"stored {len(reviews)} reviews; maximum is {MAX_REVIEW_SNIPPETS}",
        )
    seen: set[tuple[str, str]] = set()
    for index, review in enumerate(reviews):
        if not isinstance(review, dict):
            _issue(issues, key, "review-type", f"review {index} is not an object")
            continue
        title = review.get("title")
        text = review.get("text")
        if not isinstance(title, str) or not isinstance(text, str):
            _issue(
                issues, key, "review-text", f"review {index} title/text must be strings"
            )
            continue
        if not title and not text:
            _issue(issues, key, "review-empty", f"review {index} is empty")
        identity = (title, text)
        if identity in seen:
            _issue(issues, key, "review-duplicate", f"review {index} is duplicated")
        seen.add(identity)
        if not _valid_review_rating(review.get("rating")):
            _issue(
                issues,
                key,
                "review-rating",
                f"review {index} has invalid rating {review.get('rating')!r}",
            )


def _audit_pricing(
    pricing: Any, key: str, issues: list[AuditIssue]
) -> None:
    if not isinstance(pricing, dict):
        _issue(issues, key, "pricing-type", "pricing_evidence must be an object")
        return
    for field_name in ("base_price", "booking_date", "travelers"):
        if not isinstance(pricing.get(field_name), str):
            _issue(issues, key, "pricing-field", f"{field_name} must be a string")

    provenance = pricing.get("provenance")
    if provenance is not None:
        if not isinstance(provenance, dict):
            _issue(
                issues,
                key,
                "pricing-provenance",
                "pricing provenance must be an object",
            )
        else:
            if set(provenance) != AUDIT_RENDERED_FALLBACK_FIELDS:
                _issue(
                    issues,
                    key,
                    "pricing-provenance-fields",
                    "rendered fallback provenance fields are incomplete or unexpected",
                )
            route_match = ROUTE_KEY_RE.fullmatch(key)
            expected_route, expected_id = (
                route_match.groups() if route_match else (None, None)
            )
            if provenance.get("kind") != AUDIT_RENDERED_FALLBACK_KIND:
                _issue(
                    issues,
                    key,
                    "pricing-provenance-kind",
                    f"unexpected provenance kind {provenance.get('kind')!r}",
                )
            if (
                provenance.get("route") != expected_route
                or str(provenance.get("detailId")) != expected_id
            ):
                _issue(
                    issues,
                    key,
                    "pricing-provenance-identity",
                    "rendered fallback route/detail ID does not match the row",
                )
            try:
                canonical_identity = detail_identity(provenance.get("canonicalUrl"))
            except (TypeError, ValueError):
                canonical_identity = None
            if canonical_identity != (expected_route, expected_id):
                _issue(
                    issues,
                    key,
                    "pricing-provenance-canonical",
                    "rendered fallback canonical URL does not match the row",
                )
            provenance_date = provenance.get("checkedAt")
            try:
                valid_date = (
                    isinstance(provenance_date, str)
                    and date.fromisoformat(provenance_date).isoformat()
                    == provenance_date
                )
            except ValueError:
                valid_date = False
            if not valid_date:
                _issue(
                    issues,
                    key,
                    "pricing-provenance-date",
                    f"invalid rendered fallback checkedAt {provenance_date!r}",
                )
            if provenance.get("graphqlFailureReason") not in (
                AUDIT_RENDERED_FALLBACK_REASONS
            ):
                _issue(
                    issues,
                    key,
                    "pricing-provenance-reason",
                    "rendered fallback does not name an allowed GraphQL failure",
                )

    availability = pricing.get("availability")
    if not isinstance(availability, dict):
        _issue(issues, key, "availability-type", "availability must be an object")
        availability_status = None
    else:
        availability_status = availability.get("status")
        if availability_status not in ALLOWED_AVAILABILITY:
            _issue(
                issues,
                key,
                "availability-status",
                f"invalid availability status {availability_status!r}",
            )
        for field_name in ("message", "source", "reason"):
            if field_name in availability and not isinstance(
                availability[field_name], str
            ):
                _issue(
                    issues,
                    key,
                    "availability-field",
                    f"availability.{field_name} must be a string",
                )
        if availability_status != "unknown":
            for field_name in ("message", "source"):
                if not isinstance(availability.get(field_name), str) or not availability[
                    field_name
                ].strip():
                    _issue(
                        issues,
                        key,
                        "availability-field",
                        f"availability.{field_name} is required for {availability_status}",
                    )

    expected_status = {
        "available": "available",
        "date-required": "date-required",
        "sold-out": "unavailable",
        "closed": "unavailable",
        "unavailable": "unavailable",
        "free": "free",
        "not-published": "not-published",
        "unknown": None,
    }.get(availability_status)
    if (
        availability_status in NEGATIVE_AVAILABILITY
        and pricing.get("base_price")
        and isinstance(availability, dict)
        and availability.get("reason")
        in {"pax_failed", "tour_grades_failed", "tour_grades_empty"}
    ):
        # The calendar's advertised from-price remains decision-useful even
        # when the concrete package lookup fails. Omitting the global status
        # lets the downstream normalizer retain that starting price.
        expected_status = None
    actual_status = pricing.get("status")
    if actual_status != expected_status:
        _issue(
            issues,
            key,
            "pricing-status",
            f"status {actual_status!r} does not match availability {availability_status!r}",
        )

    packages = pricing.get("packages")
    if not isinstance(packages, list):
        _issue(issues, key, "packages-type", "packages must be a list")
        return
    seen_packages: set[str] = set()
    package_statuses: list[str] = []
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            _issue(issues, key, "package-type", f"package {index} is not an object")
            continue
        encoded = json.dumps(package, ensure_ascii=False, sort_keys=True)
        if encoded in seen_packages:
            _issue(issues, key, "package-duplicate", f"package {index} is duplicated")
        seen_packages.add(encoded)
        for field_name in (
            "name",
            "description",
            "available_times",
            "total_price",
            "party",
            "unit_price",
            "unit",
        ):
            if not isinstance(package.get(field_name), str):
                _issue(
                    issues,
                    key,
                    "package-field",
                    f"package {index}.{field_name} must be a string",
                )
        status = package.get("availability")
        package_statuses.append(status)
        if status not in ALLOWED_AVAILABILITY:
            _issue(
                issues,
                key,
                "package-availability",
                f"package {index} has invalid availability {status!r}",
            )
        if status in NEGATIVE_AVAILABILITY and (
            not isinstance(package.get("availability_message"), str)
            or not package["availability_message"].strip()
        ):
            _issue(
                issues,
                key,
                "package-availability-message",
                f"package {index} needs a negative availability message",
            )
        total = package.get("total_price")
        party = package.get("party")
        if total and party == "" and (
            package.get("unit_price") or package.get("unit")
        ):
            _issue(
                issues,
                key,
                "standalone-total",
                f"package {index} invents a unit rate for a standalone total",
            )
        if bool(package.get("unit_price")) != bool(package.get("unit")):
            _issue(
                issues,
                key,
                "package-unit",
                f"package {index} unit price and unit must appear together",
            )
        additional = package.get("additional_costs", [])
        if not isinstance(additional, list):
            _issue(
                issues,
                key,
                "additional-costs",
                f"package {index}.additional_costs must be a list",
            )
        else:
            for extra_index, extra in enumerate(additional):
                if not isinstance(extra, dict) or not all(
                    isinstance(extra.get(field_name), str) and extra[field_name]
                    for field_name in ("amount", "source_text")
                ):
                    _issue(
                        issues,
                        key,
                        "additional-cost",
                        f"package {index} extra {extra_index} is malformed",
                    )
                elif "unit" in extra and (
                    not isinstance(extra["unit"], str) or not extra["unit"].strip()
                ):
                    _issue(
                        issues,
                        key,
                        "additional-cost-unit",
                        f"package {index} extra {extra_index} unit is malformed",
                    )
        if "source_description" in package and not isinstance(
            package["source_description"], str
        ):
            _issue(
                issues,
                key,
                "package-source-description",
                f"package {index}.source_description must be a string",
            )

    if packages and all(status in NEGATIVE_AVAILABILITY for status in package_statuses):
        if availability_status not in NEGATIVE_AVAILABILITY:
            _issue(
                issues,
                key,
                "package-global-availability",
                "all packages are negative but global availability is not negative",
            )
    if (
        availability_status in NEGATIVE_AVAILABILITY
        and "available" in package_statuses
    ):
        _issue(
            issues,
            key,
            "package-global-contradiction",
            "global availability is negative while a package is available",
        )
    if re.search(
        r"\b(?:nan|undefined)\b", json.dumps(pricing, ensure_ascii=False), re.I
    ):
        _issue(
            issues,
            key,
            "pricing-nan",
            "pricing evidence contains NaN/undefined text",
        )


def _audit_raw_pricing(
    raw_html: str,
    pricing: Any,
    key: str,
    issues: list[AuditIssue],
) -> None:
    """Cross-check rendered money independently from ``parse_detail_html``."""
    if not isinstance(pricing, dict):
        return
    packages = pricing.get("packages")
    if not isinstance(packages, list):
        return
    raw_packages, raw_base_prices = _rendered_price_surfaces(raw_html)
    if len(raw_packages) != len(packages):
        _issue(
            issues,
            key,
            "raw-package-count",
            f"rendered {len(raw_packages)} package surfaces but parsed {len(packages)}",
        )

    raw_base_money = next(
        (match.group(0) for text in raw_base_prices if (match := AUDIT_MONEY_RE.search(text))),
        "",
    )
    if raw_base_money and not pricing.get("base_price"):
        _issue(
            issues,
            key,
            "raw-base-price-missing",
            f"rendered base price {raw_base_money!r} was not parsed",
        )
    elif raw_base_money:
        raw_base_value = _money_value(raw_base_money)
        parsed_base_value = _money_value(pricing.get("base_price", ""))
        if (
            raw_base_value is None
            or parsed_base_value is None
            or raw_base_value != parsed_base_value
        ):
            _issue(
                issues,
                key,
                "raw-base-price-mismatch",
                f"parsed base {pricing.get('base_price')!r}; rendered {raw_base_money!r}",
            )

    for index, surface in enumerate(raw_packages):
        if index >= len(packages) or not isinstance(packages[index], dict):
            continue
        package = packages[index]
        total_match = AUDIT_TOTAL_RE.search(surface)
        raw_total = total_match.group(1) if total_match else ""
        raw_party = clean_text(total_match.group(2)) if total_match and total_match.group(2) else ""
        charges: list[tuple[int, str, str]] = []
        for match in AUDIT_CHARGE_RE.finditer(surface):
            charge = (int(match.group(1)), match.group(2).lower(), match.group(3))
            if charge not in charges:
                charges.append(charge)

        parsed_total = package.get("total_price", "")
        if raw_total and not parsed_total:
            _issue(
                issues,
                key,
                "raw-total-missing",
                f"package {index} rendered Total price {raw_total!r} but parsed none",
            )
        if raw_total and parsed_total:
            raw_value = _money_value(raw_total)
            parsed_value = _money_value(parsed_total)
            if raw_value is None or parsed_value is None or raw_value != parsed_value:
                _issue(
                    issues,
                    key,
                    "raw-total-mismatch",
                    f"package {index} parsed {parsed_total!r}; rendered {raw_total!r}",
                )

        # A standalone total intentionally does not inherit a nearby charge
        # line.  Charges are required only when the total explicitly names a
        # party, or when no total is rendered at all.
        if charges and (not total_match or raw_party):
            if not package.get("unit_price") or not package.get("unit"):
                _issue(
                    issues,
                    key,
                    "raw-charge-missing",
                    f"package {index} rendered traveller charges but parsed no unit rate",
                )
            else:
                _, first_kind, first_money = charges[0]
                raw_unit_value = _money_value(first_money)
                parsed_unit_value = _money_value(package.get("unit_price", ""))
                if (
                    raw_unit_value is None
                    or parsed_unit_value is None
                    or raw_unit_value != parsed_unit_value
                ):
                    _issue(
                        issues,
                        key,
                        "raw-charge-price-mismatch",
                        f"package {index} parsed unit price "
                        f"{package.get('unit_price')!r}; rendered {first_money!r}",
                    )
                if package.get("unit", "").casefold() != first_kind.casefold():
                    _issue(
                        issues,
                        key,
                        "raw-charge-unit-mismatch",
                        f"package {index} parsed unit {package.get('unit')!r}; "
                        f"rendered {first_kind!r}",
                    )

        additional = package.get("additional_costs", [])
        parsed_numeric = bool(parsed_total or package.get("unit_price")) or bool(
            isinstance(additional, list)
            and any(isinstance(extra, dict) and extra.get("amount") for extra in additional)
        )
        if (
            package.get("availability") == "available"
            and AUDIT_MONEY_RE.search(surface)
            and not parsed_numeric
        ):
            _issue(
                issues,
                key,
                "raw-priced-surface-empty",
                f"available package {index} renders money but has no parsed numeric evidence",
            )

        if not (total_match and raw_party and charges):
            continue
        total_value = _money_value(raw_total)
        charge_values = [
            (count, _money_value(money)) for count, _, money in charges
        ]
        if total_value is None or any(value is None for _, value in charge_values):
            _issue(
                issues,
                key,
                "price-math-input",
                f"package {index} rendered money could not be normalized",
            )
            continue
        currencies = {total_value[0]} | {
            value[0] for _, value in charge_values if value is not None
        }
        if len(currencies) != 1:
            _issue(
                issues,
                key,
                "price-math-currency",
                f"package {index} mixes currencies in its total and charges",
            )
            continue
        charge_total = sum(
            (Decimal(count) * value[1] for count, value in charge_values if value),
            Decimal("0"),
        )
        tolerance = (
            Decimal("1")
            if total_value[0] in {"HUF", "JPY"}
            else max(Decimal("0.05"), Decimal("0.02") * len(charge_values))
        )
        if abs(total_value[1] - charge_total) > tolerance:
            _issue(
                issues,
                key,
                "price-math",
                f"package {index} total {total_value[1]} != charge sum {charge_total} "
                f"within {tolerance} {total_value[0]}",
            )


def _audit_critical_regressions(
    key: str,
    row: dict[str, Any],
    raw_html: str,
    issues: list[AuditIssue],
) -> None:
    packages = row.get("pricing_evidence", {}).get("packages", [])
    if key == CRITICAL_NAN_PARTY_KEY:
        charges: list[tuple[str, str]] = []
        for match in PARTY_CHARGE_RE.finditer(raw_html):
            charge = (match.group(1), match.group(2).lower())
            if charge not in charges:
                charges.append(charge)
        expected = " and ".join(f"{count} {kind}" for count, kind in charges)
        if not expected:
            expected = "4 adults and 4 seniors"
        if not expected or not any(
            isinstance(package, dict) and package.get("party") == expected
            for package in packages
        ):
            _issue(
                issues,
                key,
                "critical-nan-party",
                f"rendered NaN party was not repaired to {expected!r}",
            )

    if key in CRITICAL_STANDALONE_KEYS:
        standalone_totals = [
            item
            for item in packages
            if isinstance(item, dict) and item.get("total_price")
        ]
        if not standalone_totals:
            _issue(
                issues,
                key,
                "critical-standalone-missing",
                "expected at least one rendered standalone package total",
            )
        elif any(
            package.get(field_name)
            for package in standalone_totals
            for field_name in ("party", "unit_price", "unit")
        ):
            _issue(
                issues,
                key,
                "critical-standalone-total",
                "rendered standalone totals must stay opaque without a unit rate",
            )


def _gql_first(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


def _gql_text(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        for name in ("text", "value", "content", "title", "name"):
            result = _gql_text(value.get(name))
            if result:
                return result
    if isinstance(value, list):
        for child in value:
            result = _gql_text(child)
            if result:
                return result
    return ""


def _gql_collect_identities(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for name, child in value.items():
            if name in {"locationId", "detailId", "contentId"} and isinstance(
                child, (str, int)
            ):
                found.add(str(child))
            found.update(_gql_collect_identities(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_gql_collect_identities(child))
    elif isinstance(value, str):
        found.update(match.group(1) for match in GQL_DETAIL_URL_RE.finditer(value))
    return found


def _gql_privacy_paths(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for name, child in value.items():
            child_path = f"{path}.{name}"
            if str(name).replace("_", "").casefold() in AUDIT_GRAPHQL_PERSONAL_KEYS:
                found.append(child_path)
            found.extend(_gql_privacy_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_gql_privacy_paths(child, f"{path}[{index}]"))
    return found


def _gql_partial_errors_allowed(name: str, response: dict[str, Any]) -> bool:
    errors = response.get("errors")
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


def _audit_gql_attempts(
    evidence: dict[str, Any],
    route: str,
    name: str,
    key: str,
    issues: list[AuditIssue],
) -> list[dict[str, Any]]:
    queries = evidence.get("queries")
    attempts = queries.get(name) if isinstance(queries, dict) else None
    if not isinstance(attempts, list) or not attempts:
        _issue(issues, key, "graphql-query-missing", f"missing {name} query")
        return []
    expected_id = AUDIT_GRAPHQL_QUERY_IDS[route][name]
    valid: list[dict[str, Any]] = []
    for index, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict):
            _issue(
                issues,
                key,
                "graphql-attempt",
                f"{name} attempt {index} is not an object",
            )
            continue
        valid.append(attempt)
        if attempt.get("attempt") != index:
            _issue(
                issues,
                key,
                "graphql-attempt-order",
                f"{name} attempt {index} has ordinal {attempt.get('attempt')!r}",
            )
        if attempt.get("persistedQueryId") != expected_id:
            _issue(
                issues,
                key,
                "graphql-query-id",
                f"{name} persisted ID is {attempt.get('persistedQueryId')!r}",
            )
        if attempt.get("httpStatus") != 200:
            _issue(
                issues,
                key,
                "graphql-http",
                f"{name} attempt {index} has HTTP {attempt.get('httpStatus')!r}",
            )
        if not isinstance(attempt.get("variables"), dict):
            _issue(
                issues,
                key,
                "graphql-variables",
                f"{name} attempt {index} has no variables object",
            )
        response = attempt.get("evidenceResponse")
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            _issue(
                issues,
                key,
                "graphql-response",
                f"{name} attempt {index} has no projected data object",
            )
            continue
        if not _gql_partial_errors_allowed(name, response):
            _issue(
                issues,
                key,
                "graphql-errors",
                f"{name} attempt {index} retained disallowed GraphQL errors",
            )
        privacy_paths = _gql_privacy_paths(response)
        if privacy_paths:
            _issue(
                issues,
                key,
                "graphql-privacy",
                f"{name} retained personal fields: {', '.join(privacy_paths[:5])}",
            )
    return valid


def _audit_gql_review_contract(
    evidence: dict[str, Any],
    route: str,
    expected_id: str,
    key: str,
    issues: list[AuditIssue],
) -> int | None:
    attempts = _audit_gql_attempts(evidence, route, "reviews", key, issues)
    if not attempts:
        return None
    if len(attempts) != 1:
        _issue(
            issues, key, "graphql-review-attempts", "reviews must use one query attempt"
        )
    variables = attempts[0].get("variables", {})
    expected_variables = {
        "filters": [],
        "limit": 10,
        "offset": 0,
        "sortType": "DEFAULT",
        "sortBy": "DATE",
        "language": "en",
        "doMachineTranslation": True,
    }
    for name, expected in expected_variables.items():
        if variables.get(name) != expected:
            _issue(
                issues,
                key,
                "graphql-review-variables",
                f"reviews.{name} is {variables.get(name)!r}; expected {expected!r}",
            )
    if str(variables.get("locationId")) != expected_id:
        _issue(
            issues,
            key,
            "graphql-review-identity",
            f"review locationId is {variables.get('locationId')!r}",
        )
    if route == "Attraction_Review" and variables.get("photosPerReviewLimit") != 7:
        _issue(
            issues,
            key,
            "graphql-review-projection",
            "venue photosPerReviewLimit must be 7",
        )
    block = _gql_first(
        attempts[0]
        .get("evidenceResponse", {})
        .get("data", {})
        .get("ReviewsProxy_getReviewListPageForLocation")
    )
    if block is None:
        _issue(issues, key, "graphql-review-block", "review block is missing")
        return None
    total = block.get("totalCount")
    reviews = block.get("reviews")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        _issue(issues, key, "graphql-review-total", f"invalid totalCount {total!r}")
        return None
    if not isinstance(reviews, list):
        _issue(issues, key, "graphql-reviews", "reviews is not a list")
        return total
    review_ids: set[str] = set()
    for index, review in enumerate(reviews):
        if not isinstance(review, dict):
            _issue(issues, key, "graphql-review", f"review {index} is not an object")
            continue
        extra = set(review) - AUDIT_GRAPHQL_REVIEW_FIELDS
        if extra:
            _issue(
                issues,
                key,
                "graphql-review-projection",
                f"review {index} retained fields {sorted(extra)}",
            )
        review_id = review.get("id")
        if review_id is None or str(review_id) in review_ids:
            _issue(
                issues,
                key,
                "graphql-review-unique",
                f"review {index} has a missing/duplicate ID",
            )
        else:
            review_ids.add(str(review_id))
        review_location_id = review.get("locationId")
        if route == "Attraction_Review" and (
            review_location_id is None or str(review_location_id) != expected_id
        ):
            _issue(
                issues,
                key,
                "graphql-review-identity",
                f"venue review {index} is not explicitly bound to {expected_id}",
            )
        elif (
            route == "AttractionProductReview"
            and review_location_id is not None
            and str(review_location_id) != expected_id
        ):
            _issue(
                issues,
                key,
                "graphql-review-identity",
                f"review {index} belongs to {review_location_id!r}",
            )

    if route == "Attraction_Review":
        selection = evidence.get("reviewSelection")
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
        if not isinstance(selection, dict) or set(selection) != required_fields:
            _issue(
                issues,
                key,
                "graphql-review-selection",
                "venue reviewSelection provenance/schema is missing or invalid",
            )
        else:
            if selection.get("policy") != AUDIT_VENUE_REVIEW_SELECTION_POLICY or str(
                selection.get("requestedLocationId")
            ) != expected_id:
                _issue(
                    issues,
                    key,
                    "graphql-review-selection",
                    "venue reviewSelection policy/identity is invalid",
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
                _issue(
                    issues,
                    key,
                    "graphql-review-selection",
                    "venue reviewSelection counts are invalid",
                )
            else:
                if counts["returnedCount"] != (
                    counts["acceptedCount"] + counts["quarantinedCount"]
                ) or counts["missingLocationIdCount"] > counts["quarantinedCount"]:
                    _issue(
                        issues,
                        key,
                        "graphql-review-selection",
                        "venue reviewSelection counts are inconsistent",
                    )
                if counts["sourceTotalCount"] < counts["acceptedCount"]:
                    _issue(
                        issues,
                        key,
                        "graphql-review-selection",
                        "source total is smaller than accepted venue reviews",
                    )
                if total != counts["acceptedCount"] or len(reviews) != counts[
                    "acceptedCount"
                ]:
                    _issue(
                        issues,
                        key,
                        "graphql-review-selection",
                        "persisted venue reviews contradict reviewSelection",
                    )
            rejected_ids = selection.get("rejectedLocationIds")
            if (
                not isinstance(rejected_ids, list)
                or any(
                    not isinstance(value, str) or not value for value in rejected_ids
                )
                or rejected_ids != sorted(set(rejected_ids))
                or expected_id in rejected_ids
            ):
                _issue(
                    issues,
                    key,
                    "graphql-review-selection",
                    "rejected venue location IDs are invalid",
                )
    target = min(MAX_REVIEW_SNIPPETS, total)
    if len(review_ids) < target:
        _issue(
            issues,
            key,
            "review-coverage",
            f"GraphQL has {len(review_ids)} unique reviews; totalCount requires {target}",
        )
    return total


def _raw_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _raw_price(value: Any, fallback_currency: str = "USD") -> tuple[str, Decimal] | None:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        amount = _raw_decimal(value)
        return (fallback_currency, amount) if amount is not None else None
    if not isinstance(value, dict):
        return None
    currency = str(
        value.get("currency") or value.get("currencyCode") or fallback_currency
    ).upper()
    for name in ("amount", "value", "total", "price"):
        child = value.get(name)
        if isinstance(child, dict):
            parsed = _raw_price(child, currency)
        else:
            amount = _raw_decimal(child)
            parsed = (currency, amount) if amount is not None else None
        if parsed is not None:
            return parsed
    for name in ("totalPrice", "displayPrice", "pricing"):
        if name in value and (parsed := _raw_price(value[name], currency)) is not None:
            return parsed
    return None


def _audit_product_language(product: Any) -> str | None:
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


def _audit_advertised_dates(
    product: Any, calendar: Any, checked_at: datetime
) -> list[str]:
    settings = product.get("bookingConfirmationSettings") if isinstance(product, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    cutoff_hours = settings.get("bookingCutoffInHours") or 0
    try:
        cutoff_hours = max(0, int(cutoff_hours))
    except (TypeError, ValueError):
        cutoff_hours = 0
    lead_days = max(1, math.ceil(cutoff_hours / 24) + 1)
    earliest = checked_at.astimezone(timezone.utc).date().toordinal() + lead_days
    rows = calendar.get("datesAndPrices", []) if isinstance(calendar, dict) else []
    candidates: dict[str, date] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            continue
        try:
            candidate = date.fromisoformat(row["date"])
        except ValueError:
            continue
        price = row.get("price")
        if (
            candidate.toordinal() >= earliest
            and isinstance(price, (int, float))
            and not isinstance(price, bool)
            and price >= 0
        ):
            candidates[row["date"]] = candidate
    return [
        date_text
        for date_text, _candidate in sorted(
            candidates.items(), key=lambda item: (item[1], item[0])
        )[:AUDIT_GRAPHQL_MAX_DATE_CANDIDATES]
    ]


def _audit_graphql_pricing(
    evidence: dict[str, Any],
    pricing: Any,
    route: str,
    key: str,
    issues: list[AuditIssue],
) -> None:
    if route != "AttractionProductReview" or not isinstance(pricing, dict):
        return
    queries = evidence.get("queries")
    selection = evidence.get("selection")
    if not isinstance(queries, dict) or not isinstance(selection, dict):
        return
    provenance = pricing.get("provenance")
    is_rendered_fallback = (
        isinstance(provenance, dict)
        and provenance.get("kind") == AUDIT_RENDERED_FALLBACK_KIND
    )
    detail_attempts = queries.get("detail")
    product = None
    if isinstance(detail_attempts, list) and detail_attempts:
        product = _gql_first(
            detail_attempts[0]
            .get("evidenceResponse", {})
            .get("data", {})
            .get("fullProduct")
        )
    product_code = product.get("productCode") if isinstance(product, dict) else None
    if not isinstance(product_code, str) or not product_code:
        return

    calendar_attempts = queries.get("priceCalendar")
    calendar = None
    if isinstance(calendar_attempts, list) and calendar_attempts:
        calendar = _gql_first(
            calendar_attempts[0]
            .get("evidenceResponse", {})
            .get("data", {})
            .get("priceCalendar")
        )
    travel_date = selection.get("travelDate")
    rows = calendar.get("datesAndPrices", []) if isinstance(calendar, dict) else []
    selected_row = next(
        (
            row
            for row in rows
            if isinstance(row, dict) and row.get("date") == travel_date
        ),
        None,
    )
    if travel_date and selected_row is None:
        _issue(
            issues,
            key,
            "graphql-price-date",
            f"selected date {travel_date!r} is absent from the raw calendar",
        )
    if selected_row is not None and not is_rendered_fallback:
        raw_base = _raw_price(selected_row.get("price"), "USD")
        parsed_base = _money_value(pricing.get("base_price", ""))
        if raw_base != parsed_base:
            _issue(
                issues,
                key,
                "graphql-base-price",
                f"stored base {parsed_base!r}; raw calendar price {raw_base!r}",
            )
    if not is_rendered_fallback and pricing.get("booking_date") != (travel_date or ""):
        _issue(
            issues,
            key,
            "graphql-booking-date",
            f"stored booking date {pricing.get('booking_date')!r}; selected {travel_date!r}",
        )

    pax_attempts = queries.get("pax")
    if isinstance(pax_attempts, list) and pax_attempts:
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
            _issue(
                issues,
                key,
                "graphql-pax-retry-provenance",
                "pax retry provenance is incomplete",
            )
        attempted_dates = (
            selection.get("paxAttemptedDates") if retry_contract else [travel_date]
        )
        attempted_dates_valid = (
            isinstance(attempted_dates, list)
            and bool(attempted_dates)
            and len(attempted_dates) <= AUDIT_GRAPHQL_MAX_DATE_CANDIDATES
            and all(isinstance(value, str) for value in attempted_dates)
            and len(set(attempted_dates)) == len(attempted_dates)
            and attempted_dates[-1] == travel_date
        )
        if not attempted_dates_valid:
            _issue(
                issues,
                key,
                "graphql-pax-dates",
                f"invalid attempted-date provenance {attempted_dates!r}",
            )
            attempted_dates = [travel_date]
        if retry_contract:
            if selection.get("paxDatePolicy") != AUDIT_GRAPHQL_DATE_SELECTION_POLICY:
                _issue(
                    issues,
                    key,
                    "graphql-pax-date-policy",
                    f"invalid pax date policy {selection.get('paxDatePolicy')!r}",
                )
            try:
                checked_at_value = evidence.get("checkedAt")
                if not isinstance(checked_at_value, str) or not checked_at_value.endswith("Z"):
                    raise ValueError
                checked_at_dt = datetime.fromisoformat(
                    checked_at_value[:-1] + "+00:00"
                )
                candidates = _audit_advertised_dates(
                    product, calendar, checked_at_dt
                )
            except ValueError:
                candidates = []
            if attempted_dates_valid and attempted_dates != candidates[: len(attempted_dates)]:
                _issue(
                    issues,
                    key,
                    "graphql-pax-date-prefix",
                    f"attempted dates {attempted_dates!r} are not {candidates!r}'s prefix",
                )
            expected_language = _audit_product_language(product)
            if selection.get("selectedLanguage") != expected_language:
                _issue(
                    issues,
                    key,
                    "graphql-pax-language-provenance",
                    f"selected language {selection.get('selectedLanguage')!r}; expected {expected_language!r}",
                )

        selected_languages: list[Any] = []
        observed_dates: list[Any] = []
        attempts_by_date: dict[Any, list[dict[str, Any]]] = {}
        for attempt in pax_attempts:
            variables = attempt.get("variables", {}) if isinstance(attempt, dict) else {}
            for name, expected in expected_pax.items():
                if variables.get(name) != expected:
                    _issue(
                        issues,
                        key,
                        "graphql-pax-variables",
                        f"pax.{name} is {variables.get(name)!r}; expected {expected!r}",
                    )
            attempt_date = variables.get("travelDate")
            valid_attempt_date = isinstance(attempt_date, str)
            if not valid_attempt_date:
                _issue(
                    issues,
                    key,
                    "graphql-pax-variables",
                    f"pax.travelDate must be text, got {attempt_date!r}",
                )
            if not valid_attempt_date or attempt_date not in attempted_dates:
                _issue(
                    issues,
                    key,
                    "graphql-pax-variables",
                    f"pax.travelDate {attempt_date!r} is not recorded",
                )
            safe_attempt_date = attempt_date if valid_attempt_date else None
            if not observed_dates or observed_dates[-1] != safe_attempt_date:
                if safe_attempt_date in observed_dates:
                    _issue(
                        issues,
                        key,
                        "graphql-pax-date-order",
                        "pax attempts returned to an earlier date",
                    )
                observed_dates.append(safe_attempt_date)
            attempts_by_date.setdefault(safe_attempt_date, []).append(attempt)
            selected_language = variables.get("selectedLanguage")
            if not any(selected_language == value for value in selected_languages):
                selected_languages.append(selected_language)
        if observed_dates != attempted_dates:
            _issue(
                issues,
                key,
                "graphql-pax-dates",
                f"query dates {observed_dates!r}; recorded {attempted_dates!r}",
            )
        if len(selected_languages) != 1 or any(
            value is not None and not isinstance(value, str)
            for value in selected_languages
        ):
            _issue(
                issues,
                key,
                "graphql-pax-language",
                f"invalid selectedLanguage sequence {selected_languages!r}",
            )
        if retry_contract and selected_languages != [selection.get("selectedLanguage")]:
            _issue(
                issues,
                key,
                "graphql-pax-language-provenance",
                "pax variables contradict selected-language provenance",
            )
        for index, attempted_date in enumerate(attempted_dates):
            date_attempts = attempts_by_date.get(attempted_date, [])
            if not date_attempts:
                continue
            if len(date_attempts) > AUDIT_GRAPHQL_MAX_POLL_ATTEMPTS:
                _issue(
                    issues,
                    key,
                    "graphql-pax-poll-bound",
                    f"{attempted_date} has {len(date_attempts)} pax polls",
                )
            statuses = [
                attempt.get("evidenceResponse", {})
                .get("data", {})
                .get("paxMix", {})
                .get("resultStatus")
                for attempt in date_attempts
                if isinstance(attempt, dict)
            ]
            if any(status in {"SUCCESS", "FAILED"} for status in statuses[:-1]):
                _issue(
                    issues,
                    key,
                    "graphql-pax-after-settle",
                    f"pax polling continued after {attempted_date} settled",
                )
            if not statuses or statuses[-1] not in {"SUCCESS", "FAILED"}:
                _issue(
                    issues,
                    key,
                    "graphql-pax-unsettled",
                    f"pax did not settle for {attempted_date}",
                )
            elif index + 1 < len(attempted_dates) and statuses[-1] != "FAILED":
                _issue(
                    issues,
                    key,
                    "graphql-pax-retry-cause",
                    f"pax advanced beyond {attempted_date} without FAILED",
                )
        pax_result = (
            pax_attempts[-1]
            .get("evidenceResponse", {})
            .get("data", {})
            .get("paxMix")
        )
        if isinstance(pax_result, dict) and pax_result.get("resultStatus") == "FAILED":
            if (
                selection.get("packageOptionsStatus") != "UNKNOWN"
                or selection.get("packageOptionsUnavailableReason") != "pax_failed"
            ):
                _issue(
                    issues,
                    key,
                    "graphql-pax-provenance",
                    "terminal pax FAILED lacks unknown/pax_failed provenance",
                )
            if (
                not is_rendered_fallback
                and pricing.get("availability", {}).get("status") != "unknown"
            ):
                _issue(
                    issues,
                    key,
                    "graphql-pax-failed-state",
                    "terminal pax FAILED must leave current availability unknown",
                )
            if (
                not is_rendered_fallback
                and selected_row is not None
                and not pricing.get("base_price")
            ):
                _issue(
                    issues,
                    key,
                    "graphql-pax-failed-price",
                    "terminal pax FAILED must retain the calendar from-price",
                )

    passenger_mix = selection.get("passengerMix")
    if isinstance(passenger_mix, list) and not is_rendered_fallback:
        raw_count = sum(
            item.get("count", 0)
            for item in passenger_mix
            if isinstance(item, dict)
            and isinstance(item.get("count"), int)
            and not isinstance(item.get("count"), bool)
        )
        stored_numbers = [int(value) for value in re.findall(r"\d+", pricing.get("travelers", ""))]
        if raw_count and sum(stored_numbers) != raw_count:
            _issue(
                issues,
                key,
                "graphql-travelers",
                f"stored travelers {pricing.get('travelers')!r}; raw count is {raw_count}",
            )

    package_attempts = queries.get("packages")
    if not isinstance(package_attempts, list) or not package_attempts:
        return
    if len(package_attempts) > AUDIT_GRAPHQL_MAX_POLL_ATTEMPTS:
        _issue(
            issues,
            key,
            "graphql-package-poll-bound",
            f"package lookup has {len(package_attempts)} polls",
        )
    for attempt in package_attempts:
        variables = attempt.get("variables", {}) if isinstance(attempt, dict) else {}
        expected = {
            "productCode": product_code,
            "travelDate": travel_date,
            "passengerMix": passenger_mix,
            "currencies": ["USD"],
            "locale": "en-US",
        }
        if variables != expected:
            _issue(
                issues,
                key,
                "graphql-package-variables",
                "package variables do not exactly match the recorded selection",
            )
    package_statuses = [
        attempt.get("evidenceResponse", {})
        .get("data", {})
        .get("tourGrades", {})
        .get("resultStatus")
        for attempt in package_attempts
        if isinstance(attempt, dict)
    ]
    if any(status in {"SUCCESS", "FAILED"} for status in package_statuses[:-1]):
        _issue(
            issues,
            key,
            "graphql-package-after-settle",
            "package polling continued after a settled result",
        )
    package_result = (
        package_attempts[-1]
        .get("evidenceResponse", {})
        .get("data", {})
        .get("tourGrades")
    )
    result = package_result.get("result") if isinstance(package_result, dict) else None
    package_status = (
        package_result.get("resultStatus") if isinstance(package_result, dict) else None
    )
    grades = result.get("tourGrades", []) if isinstance(result, dict) else []
    grades = [grade for grade in grades if isinstance(grade, dict)]
    package_has_rows = bool(grades)
    expected_options_status = (
        "AVAILABLE" if package_status == "SUCCESS" and package_has_rows else "UNKNOWN"
    )
    expected_reason = (
        None
        if package_status == "SUCCESS" and package_has_rows
        else "tour_grades_empty"
        if package_status == "SUCCESS"
        else "tour_grades_failed"
    )
    if (
        selection.get("packageOptionsStatus") != expected_options_status
        or selection.get("packageOptionsUnavailableReason") != expected_reason
    ):
        _issue(
            issues,
            key,
            "graphql-package-provenance",
            "selection package-options state contradicts tourGrades",
        )
    if is_rendered_fallback:
        return
    stored_packages = pricing.get("packages")
    if not isinstance(stored_packages, list):
        return
    if len(grades) != len(stored_packages):
        _issue(
            issues,
            key,
            "graphql-package-count",
            f"raw has {len(grades)} packages; stored has {len(stored_packages)}",
        )
    for index, grade in enumerate(grades):
        if index >= len(stored_packages) or not isinstance(stored_packages[index], dict):
            continue
        stored = stored_packages[index]
        raw_title = clean_text(str(grade.get("title") or grade.get("name") or ""))
        if raw_title and stored.get("name") != raw_title:
            _issue(
                issues,
                key,
                "graphql-package-name",
                f"package {index} name differs from raw title",
            )
        raw_total = _raw_price(
            grade.get("price")
            if "price" in grade
            else grade.get("totalPrice") or grade.get("pricing"),
            "USD",
        )
        stored_total = _money_value(stored.get("total_price", ""))
        if raw_total != stored_total:
            _issue(
                issues,
                key,
                "graphql-package-price",
                f"package {index} stored {stored_total!r}; raw {raw_total!r}",
            )
        line_items = grade.get("lineItems") or grade.get("charges")
        if raw_total is not None and isinstance(line_items, list) and line_items:
            calculated = Decimal(0)
            calculable = True
            for line in line_items:
                if not isinstance(line, dict):
                    calculable = False
                    break
                quantity = _raw_decimal(line.get("quantity") or line.get("count") or 1)
                unit = _raw_price(
                    line.get("unitPrice") or line.get("price") or line.get("amount"),
                    raw_total[0],
                )
                if quantity is None or unit is None or unit[0] != raw_total[0]:
                    calculable = False
                    break
                calculated += quantity * unit[1]
            if calculable and calculated != raw_total[1]:
                _issue(
                    issues,
                    key,
                    "graphql-price-math",
                    f"package {index} raw total {raw_total[1]} != line sum {calculated}",
                )


def _audit_rendered_fallback_projection(
    evidence: dict[str, Any],
    item: dict[str, Any],
    pricing: Any,
    raw_dir: Path,
    expected_route: str,
    expected_id: str,
    key: str,
    issues: list[AuditIssue],
) -> dict[str, Any] | None:
    provenance = pricing.get("provenance") if isinstance(pricing, dict) else None
    claimed_fallback = (
        isinstance(provenance, dict)
        and provenance.get("kind") == AUDIT_RENDERED_FALLBACK_KIND
    )
    selection = evidence.get("selection")
    reason = (
        selection.get("packageOptionsUnavailableReason")
        if isinstance(selection, dict)
        else None
    )
    if reason not in AUDIT_RENDERED_FALLBACK_REASONS:
        if claimed_fallback:
            _issue(
                issues,
                key,
                "fallback-graphql-state",
                f"GraphQL selection reason {reason!r} does not allow rendered fallback",
            )
        return None
    try:
        html_path = detail_cache_path({"url": item.get("url", "")}, raw_dir=raw_dir)
        raw_html = html_path.read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError, UnicodeDecodeError) as exc:
        if claimed_fallback:
            _issue(
                issues,
                key,
                "fallback-raw-missing",
                f"cannot read exact rendered fallback: {exc}",
            )
        return None
    lowered = raw_html.lower()
    if (
        len(raw_html.encode("utf-8", errors="ignore")) < 30_000
        or "<html" not in lowered
        or "tripadvisor" not in lowered
        or "captcha-delivery.com/captcha" in lowered
        or "verify you are human" in lowered
    ):
        if claimed_fallback:
            _issue(
                issues,
                key,
                "fallback-raw-invalid",
                "rendered fallback is too small, blocked, or not a Tripadvisor page",
            )
        return None
    parsed = parse_detail_html(raw_html, listing_id=expected_id, review_limit=0)
    try:
        canonical_identity = detail_identity(parsed.get("canonical_url"))
    except (TypeError, ValueError):
        canonical_identity = None
    if canonical_identity != (expected_route, expected_id):
        if claimed_fallback:
            _issue(
                issues,
                key,
                "fallback-identity",
                f"rendered canonical identity is {canonical_identity!r}",
            )
        return None
    rendered_pricing = parsed.get("pricing_evidence")
    if not isinstance(rendered_pricing, dict):
        if claimed_fallback:
            _issue(
                issues,
                key,
                "fallback-pricing",
                "rendered fallback has no pricing object",
            )
        return None
    if not all(
        isinstance(rendered_pricing.get(field_name), str)
        and rendered_pricing[field_name].strip()
        for field_name in ("booking_date", "travelers")
    ) or not (
        isinstance(rendered_pricing.get("packages"), list)
        and rendered_pricing["packages"]
    ):
        if claimed_fallback:
            _issue(
                issues,
                key,
                "fallback-package-scope",
                "rendered fallback lacks a date, traveler scope, or package rows",
            )
        return None
    checked_at = datetime.fromtimestamp(html_path.stat().st_mtime).date().isoformat()
    expected_pricing = copy.deepcopy(rendered_pricing)
    expected_pricing["provenance"] = {
        "kind": AUDIT_RENDERED_FALLBACK_KIND,
        "route": expected_route,
        "detailId": int(expected_id),
        "canonicalUrl": parsed["canonical_url"],
        "checkedAt": checked_at,
        "graphqlFailureReason": reason,
    }
    if not claimed_fallback:
        _issue(
            issues,
            key,
            "fallback-missing",
            "eligible exact-ID rendered package evidence was not projected",
        )
    _audit_raw_pricing(raw_html, expected_pricing, key, issues)
    return expected_pricing


def _audit_graphql_raw(
    key: str,
    item: dict[str, Any],
    row: dict[str, Any],
    raw_path: Path,
    expected_route: str,
    expected_id: str,
    report: AuditReport,
) -> None:
    issues = report.issues
    try:
        evidence = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _issue(issues, key, "raw-read", f"cannot read {raw_path.name}: {exc}")
        return
    if not isinstance(evidence, dict):
        _issue(issues, key, "graphql-envelope", "GraphQL evidence is not an object")
        return
    if evidence.get("schemaVersion") != AUDIT_GRAPHQL_SCHEMA_VERSION:
        _issue(
            issues,
            key,
            "graphql-schema",
            f"schemaVersion is {evidence.get('schemaVersion')!r}",
        )
    if evidence.get("transport") != AUDIT_GRAPHQL_TRANSPORT:
        _issue(
            issues,
            key,
            "graphql-transport",
            f"transport is {evidence.get('transport')!r}",
        )
    if evidence.get("route") != expected_route or str(
        evidence.get("detailId")
    ) != expected_id:
        _issue(
            issues,
            key,
            "graphql-identity",
            "envelope route/detail ID does not match inventory",
        )
    source_url = evidence.get("sourceUrl")
    try:
        source_identity = detail_identity(source_url)
    except (TypeError, ValueError):
        source_identity = None
    if source_identity != (expected_route, expected_id) or source_url != item.get("url"):
        _issue(
            issues,
            key,
            "graphql-source",
            "sourceUrl does not exactly match the inventory detail URL",
        )

    checked_date = None
    checked_at = evidence.get("checkedAt")
    try:
        if not isinstance(checked_at, str) or not checked_at.endswith("Z"):
            raise ValueError
        checked_dt = datetime.fromisoformat(checked_at[:-1] + "+00:00")
        if checked_dt.utcoffset() is None or checked_dt.utcoffset().total_seconds() != 0:
            raise ValueError
        checked_date = checked_dt.astimezone(timezone.utc).date().isoformat()
    except ValueError:
        _issue(
            issues,
            key,
            "graphql-checked-at",
            f"invalid UTC checkedAt {checked_at!r}",
        )
    if row.get("checked_at") != checked_date:
        _issue(
            issues,
            key,
            "checked-at",
            f"checked_at is {row.get('checked_at')!r}; envelope date is {checked_date!r}",
        )

    queries = evidence.get("queries")
    if not isinstance(queries, dict):
        _issue(issues, key, "graphql-queries", "queries is not an object")
        return
    detail_attempts = _audit_gql_attempts(
        evidence, expected_route, "detail", key, issues
    )
    _audit_gql_review_contract(
        evidence, expected_route, expected_id, key, issues
    )
    report.coverage_sources["graphql"] += 1
    expected_query_names = {"detail", "reviews"}

    if expected_route == "AttractionProductReview" and detail_attempts:
        variables = detail_attempts[0].get("variables", {})
        expected_variables = {
            "activityId": int(expected_id),
            "currency": "USD",
            "language": "en",
        }
        if variables != expected_variables:
            _issue(
                issues,
                key,
                "graphql-detail-variables",
                f"product detail variables are {variables!r}",
            )
        product = _gql_first(
            detail_attempts[0]
            .get("evidenceResponse", {})
            .get("data", {})
            .get("fullProduct")
        )
        if product is None or str(product.get("activityId")) != expected_id:
            _issue(
                issues, key, "graphql-detail-identity", "fullProduct identity mismatch"
            )
        product_code = product.get("productCode") if isinstance(product, dict) else None
        selection = evidence.get("selection")
        if not isinstance(selection, dict) or selection.get(
            "travelDateSource"
        ) != "priceCalendar.datesAndPrices":
            _issue(
                issues,
                key,
                "graphql-selection",
                "product selection/provenance is missing",
            )
            selection = {}
        if isinstance(product_code, str) and product_code:
            expected_query_names.update({"priceCalendar", "cancellation"})
            for name in ("priceCalendar", "cancellation"):
                attempts = _audit_gql_attempts(
                    evidence, expected_route, name, key, issues
                )
                expected_variables = {
                    "currency": "USD",
                    "productCode": product_code,
                }
                if attempts and (
                    len(attempts) != 1
                    or attempts[0].get("variables") != expected_variables
                ):
                    _issue(
                        issues,
                        key,
                        "graphql-price-variables",
                        f"{name} variables do not match productCode",
                    )
            cancellation_attempts = queries.get("cancellation")
            if isinstance(cancellation_attempts, list) and cancellation_attempts:
                cancellation_product = _gql_first(
                    cancellation_attempts[0]
                    .get("evidenceResponse", {})
                    .get("data", {})
                    .get("fullProduct")
                )
                cancellation_activity_id = (
                    cancellation_product.get("activityId")
                    if isinstance(cancellation_product, dict)
                    else None
                )
                cancellation_conditions = (
                    cancellation_product.get("cancellationConditions")
                    if isinstance(cancellation_product, dict)
                    else None
                )
                titles_match = (
                    isinstance(product, dict)
                    and isinstance(cancellation_product, dict)
                    and _gql_text(product.get("title"))
                    == _gql_text(cancellation_product.get("title"))
                    and bool(_gql_text(product.get("title")))
                )
                if cancellation_product is None or (
                    cancellation_activity_id is not None
                    and str(cancellation_activity_id) != expected_id
                ) or (
                    cancellation_activity_id is None
                    and (
                        not titles_match
                        or not isinstance(cancellation_conditions, dict)
                    )
                ):
                    _issue(
                        issues,
                        key,
                        "graphql-cancellation-identity",
                        "cancellation query vars/title/schema do not identify the product",
                    )
            if selection.get("travelDate") is not None:
                expected_query_names.add("pax")
                _audit_gql_attempts(evidence, expected_route, "pax", key, issues)
            if selection.get("passengerMix") is not None:
                expected_query_names.add("packages")
                _audit_gql_attempts(
                    evidence, expected_route, "packages", key, issues
                )
        _audit_graphql_pricing(
            evidence, row.get("pricing_evidence"), expected_route, key, issues
        )
    elif expected_route == "Attraction_Review" and detail_attempts:
        for index, attempt in enumerate(detail_attempts):
            variables = attempt.get("variables", {})
            request = variables.get("request") if isinstance(variables, dict) else None
            route_parameters = (
                request.get("routeParameters") if isinstance(request, dict) else None
            )
            if (
                not isinstance(route_parameters, dict)
                or route_parameters.get("contentType") != "attraction"
                or str(route_parameters.get("contentId")) != expected_id
                or variables.get("currency") != "USD"
            ):
                _issue(
                    issues,
                    key,
                    "graphql-detail-variables",
                    f"venue detail attempt {index + 1} variables are invalid",
                )
        result = _gql_first(
            detail_attempts[-1]
            .get("evidenceResponse", {})
            .get("data", {})
            .get("Result")
        )
        if result is None or expected_id not in _gql_collect_identities(result):
            _issue(
                issues, key, "graphql-detail-identity", "WPS Result identity mismatch"
            )

    if set(queries) != expected_query_names:
        _issue(
            issues,
            key,
            "graphql-query-set",
            f"query names are {sorted(queries)}; expected {sorted(expected_query_names)}",
        )

    try:
        parsed = parse_graphql_evidence(evidence, expected_url=item.get("url"))
    except Exception as exc:
        _issue(issues, key, "raw-parse", f"current GraphQL parser failed: {exc}")
        return
    expected_parsed = copy.deepcopy(parsed)
    fallback_pricing = _audit_rendered_fallback_projection(
        evidence,
        item,
        row.get("pricing_evidence"),
        raw_path.parent,
        expected_route,
        expected_id,
        key,
        issues,
    )
    if fallback_pricing is not None:
        expected_parsed["pricing_evidence"] = fallback_pricing
    for field_name in PARSED_FIELDS:
        if row.get(field_name) != expected_parsed.get(field_name):
            _issue(
                issues,
                key,
                "stale-parse",
                f"stored {field_name} differs from the current GraphQL projection",
            )
    if row.get("description_source") not in ALLOWED_DESCRIPTION_SOURCES:
        _issue(
            issues,
            key,
            "description-source",
            f"invalid description source {row.get('description_source')!r}",
        )
    _audit_text(row, key, issues)
    _audit_reviews(row.get("reviews"), key, issues)
    _audit_pricing(row.get("pricing_evidence"), key, issues)


def _audit_row(
    key: str,
    item: dict[str, Any],
    row: dict[str, Any],
    raw_dir: Path,
    city: str,
    report: AuditReport,
) -> None:
    issues = report.issues
    route_match = ROUTE_KEY_RE.fullmatch(key)
    assert route_match is not None
    expected_route, expected_id = route_match.groups()

    for field_name, expected in (
        ("key", key),
        ("route", expected_route),
        ("id", expected_id),
        ("city", city),
        ("name", item.get("name", "")),
        ("url", item.get("url", "")),
        ("category", item.get("category", "")),
        ("subtype", item.get("subtype", "")),
        ("rating", item.get("rating")),
        ("review_count", item.get("reviewCount", 0)),
    ):
        if row.get(field_name) != expected:
            _issue(
                issues,
                key,
                "metadata",
                f"{field_name} is {row.get(field_name)!r}; expected {expected!r}",
            )

    if item.get("rating") is not None and not _valid_rating(item.get("rating")):
        _issue(issues, key, "listing-rating", f"invalid listing rating {item.get('rating')!r}")
    review_count = item.get("reviewCount", 0)
    if not isinstance(review_count, int) or isinstance(review_count, bool) or review_count < 0:
        _issue(issues, key, "listing-review-count", f"invalid review count {review_count!r}")

    for label, url in (
        ("inventory", item.get("url")),
        ("stored", row.get("url")),
        ("canonical", row.get("canonical_url")),
    ):
        try:
            identity = detail_identity(url)
        except (TypeError, ValueError) as exc:
            _issue(issues, key, f"{label}-identity", str(exc))
            continue
        if identity != (expected_route, expected_id):
            _issue(
                issues,
                key,
                f"{label}-identity",
                f"identity {identity!r} does not match {(expected_route, expected_id)!r}",
            )
        if label == "canonical":
            parsed_url = urlparse(str(url))
            if parsed_url.scheme != "https" or (parsed_url.hostname or "").lower() not in {
                "tripadvisor.com",
                "www.tripadvisor.com",
            }:
                _issue(
                    issues,
                    key,
                    "canonical-host",
                    "canonical URL must be HTTPS on tripadvisor.com",
                )

    try:
        raw_path = detail_cache_path({"url": item.get("url", "")}, raw_dir=raw_dir)
        graphql_path = detail_graphql_cache_path(
            {"url": item.get("url", "")}, raw_dir=raw_dir
        )
    except (TypeError, ValueError) as exc:
        _issue(issues, key, "raw-path", str(exc))
        return
    graphql_expected = str(row.get("description_source", "")).startswith(
        "tripadvisor_graphql_"
    )
    if graphql_expected or (not raw_path.is_file() and graphql_path.is_file()):
        if not graphql_path.is_file():
            _issue(issues, key, "raw-missing", f"missing {graphql_path.name}")
            return
        _audit_graphql_raw(
            key,
            item,
            row,
            graphql_path,
            expected_route,
            expected_id,
            report,
        )
        return
    if not raw_path.is_file():
        _issue(
            issues,
            key,
            "raw-missing",
            f"missing both {raw_path.name} and {graphql_path.name}",
        )
        return
    try:
        raw_html = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _issue(issues, key, "raw-read", f"cannot read {raw_path.name}: {exc}")
        return

    checked_at = datetime.fromtimestamp(raw_path.stat().st_mtime).date().isoformat()
    if row.get("checked_at") != checked_at:
        _issue(
            issues,
            key,
            "checked-at",
            f"checked_at is {row.get('checked_at')!r}; raw mtime date is {checked_at}",
        )

    try:
        parsed = parse_detail_html(raw_html, listing_id=expected_id)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _issue(issues, key, "raw-parse", f"current parser failed: {exc}")
        return
    for field_name in PARSED_FIELDS:
        if row.get(field_name) != parsed.get(field_name):
            _issue(
                issues,
                key,
                "stale-parse",
                f"stored {field_name} differs from the current raw-page parse",
            )

    if row.get("description_source") not in ALLOWED_DESCRIPTION_SOURCES:
        _issue(
            issues,
            key,
            "description-source",
            f"invalid description source {row.get('description_source')!r}",
        )

    filter_count = rendered_review_filter_count(raw_html)
    if filter_count:
        _issue(
            issues,
            key,
            "review-filter",
            f"rendered page still has {filter_count} active review filter(s)",
        )
    selected_language = rendered_product_review_language(raw_html)
    live_total = rendered_review_total(raw_html)
    if live_total is not None:
        target = min(MAX_REVIEW_SNIPPETS, live_total)
    else:
        try:
            discovery_total = max(0, int(item.get("reviewCount", 0) or 0))
        except (TypeError, ValueError):
            discovery_total = 0
        target = min(MAX_REVIEW_SNIPPETS, discovery_total)
    if (
        expected_route == "AttractionProductReview"
        and target
        and not selected_language
    ):
        _issue(
            issues,
            key,
            "review-language-missing",
            "product page does not expose its selected review language",
        )
    if selected_language and not selected_language.casefold().startswith("all languages"):
        _issue(
            issues,
            key,
            "review-language",
            f"product review language is {selected_language!r}, not All languages",
        )
    source = "live" if live_total is not None else "discovery"
    report.coverage_sources[source] += 1
    review_total = len(parsed.get("reviews", []))
    exact_product_pagination_exception = (
        key == PAGINATION_SHORTFALL_KEY
        and live_total is not None
        and live_total > 9
        and review_total == 9
        and raw_html.count('data-automation="reviewCard"') == 9
        and filter_count == 0
        and selected_language.casefold().startswith("all languages")
        and PAGINATION_NEXT_MARKER in rendered_product_review_scope(raw_html)
    )
    venue_review_scope = _rendered_venue_review_scope(raw_html)
    exact_venue_pagination_exception = (
        key == VENUE_PAGINATION_SHORTFALL_KEY
        and expected_route == "Attraction_Review"
        and live_total is not None
        and live_total > 9
        and review_total == 9
        and raw_html.count('data-automation="reviewCard"') == 9
        and filter_count == 0
        and "All reviews" in venue_review_scope
        and PAGINATION_NEXT_MARKER in venue_review_scope
    )
    pagination_exception = (
        exact_product_pagination_exception or exact_venue_pagination_exception
    )
    if review_total < target and not pagination_exception:
        _issue(
            issues,
            key,
            "review-coverage",
            f"parsed {review_total} unique reviews; {source} evidence requires {target}",
        )

    _audit_text(row, key, issues)
    _audit_reviews(row.get("reviews"), key, issues)
    _audit_pricing(row.get("pricing_evidence"), key, issues)
    _audit_raw_pricing(raw_html, row.get("pricing_evidence"), key, issues)
    _audit_critical_regressions(key, row, raw_html, issues)


def audit_detail_context(
    inventory: Iterable[Any],
    context_rows: Iterable[Any],
    *,
    raw_dir: Path = DEFAULT_RAW_DIR,
    city: str = "budapest",
    allow_partial: bool = False,
) -> AuditReport:
    """Audit in-memory inventory/context rows against immutable raw pages."""
    issues: list[AuditIssue] = []
    expected = _route_inventory(inventory, issues)
    present = _context_index(context_rows, issues)
    missing = sorted(set(expected) - set(present))
    unexpected = sorted(set(present) - set(expected))
    for key in unexpected:
        _issue(issues, key, "unexpected-context", "key is not validator-visible")
    if missing and not allow_partial:
        _issue(
            issues,
            "*",
            "missing-context",
            f"{len(missing)} validator-visible keys are missing; first: {', '.join(missing[:8])}",
        )

    auditable = sorted(set(expected) & set(present))
    report = AuditReport(
        expected=len(expected),
        present=len(present),
        audited=len(auditable),
        allow_partial=allow_partial,
        missing=missing,
        unexpected=unexpected,
        issues=issues,
    )
    for key in auditable:
        _audit_row(key, expected[key], present[key], Path(raw_dir), city, report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--validator", type=Path, default=DEFAULT_VALIDATOR)
    parser.add_argument(
        "--site-root",
        type=Path,
        help="read the validator inventory from this immutable staged site directory",
    )
    parser.add_argument("--city", default="budapest")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="report missing visible keys without failing while a crawl is active",
    )
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        inventory = load_visible_inventory(args.validator, site_root=args.site_root)
        contexts = load_context_rows(args.context)
        report = audit_detail_context(
            inventory,
            contexts,
            raw_dir=args.raw_dir,
            city=args.city,
            allow_partial=args.allow_partial,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.as_json(), ensure_ascii=False, indent=2))
    else:
        state = (
            "PARTIAL PASS"
            if report.ok and report.allow_partial and report.missing
            else "PASS" if report.ok else "FAIL"
        )
        print(
            f"{state}: audited {report.audited}/{report.expected} visible Tripadvisor "
            f"contexts ({len(report.missing)} missing, {len(report.unexpected)} unexpected, "
            f"{len(report.issues)} issue(s)); review totals: "
            f"{report.coverage_sources['live']} live, "
            f"{report.coverage_sources['discovery']} discovery fallback, "
            f"{report.coverage_sources['graphql']} GraphQL"
        )
        for issue in report.issues[:50]:
            print(f"  {issue.key} [{issue.code}] {issue.message}", file=sys.stderr)
        if len(report.issues) > 50:
            print(
                f"  ... {len(report.issues) - 50} more issue(s); rerun with --json",
                file=sys.stderr,
            )
        if report.allow_partial and report.missing:
            print(
                f"  partial crawl: {len(report.missing)} keys not audited yet; "
                f"first: {', '.join(report.missing[:8])}"
            )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
