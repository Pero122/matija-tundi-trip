#!/usr/bin/env python3
"""Bridge the repository's cached Camoufox Tripadvisor research into SQLite.

`export` writes the quality-ranked outside-Budapest inventory expected by
`budapest-london/tripadvisor/scrape_ta_details.py --city hungary --graphql`.
`import` stores each exact GraphQL evidence object as a private raw payload and
normalizes its description, up to ten reviews, and booking/package evidence.
No command in this module launches a browser or a paid actor.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
TA_ROOT = PROJECT_ROOT / "budapest-london" / "tripadvisor"
DEFAULT_CANDIDATES = TA_ROOT / "candidates_hungary.json"
DEFAULT_CONTEXT = TA_ROOT / "detail_context_hungary.json"
DEFAULT_RAW_DIR = TA_ROOT / "raw" / "details"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(TA_ROOT) not in sys.path:
    sys.path.insert(0, str(TA_ROOT))

from query import query_listings  # noqa: E402
from scrape_ta_details import (  # noqa: E402
    cache_looks_valid,
    parse_graphql_evidence,
    parse_detail_html,
    validate_graphql_evidence,
)
from store import DEFAULT_DB_PATH, ResearchStore, canonical_json  # noqa: E402


DETAIL_URL_RE = re.compile(
    r"/(AttractionProductReview|Attraction_Review)-g(?P<geo>\d+)-d(?P<id>\d+)-",
    re.I,
)
GRAPHQL_ENRICHMENT_VERSION = "tripadvisor-browser-graphql-v1"
HTML_ENRICHMENT_VERSION = "tripadvisor-browser-html-v1"
MAX_REVIEWS = 10


def _route_identity(value: Mapping[str, Any]) -> tuple[str, str, str]:
    url = str(value.get("url") or value.get("canonical_url") or "").strip()
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or not (hostname == "tripadvisor.com" or hostname.endswith(".tripadvisor.com"))
    ):
        raise ValueError(f"unsupported Tripadvisor host or scheme: {url!r}")
    match = DETAIL_URL_RE.search(url)
    if not match:
        raise ValueError(f"unsupported Tripadvisor detail URL: {url!r}")
    route = (
        "AttractionProductReview"
        if match.group(1).casefold() == "attractionproductreview"
        else "Attraction_Review"
    )
    return route, match.group("id"), match.group("geo")


def _graphql_path(route: str, listing_id: str, raw_dir: Path) -> Path:
    route_slug = re.sub(r"[^a-z0-9]+", "_", route.casefold()).strip("_")
    return raw_dir / f"{route_slug}_{listing_id}.graphql.json"


def _html_path(route: str, listing_id: str, raw_dir: Path) -> Path:
    route_slug = re.sub(r"[^a-z0-9]+", "_", route.casefold()).strip("_")
    return raw_dir / f"{route_slug}_{listing_id}.html"


def export_candidates(
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = DEFAULT_CANDIDATES,
    *,
    limit: int = 90,
) -> list[dict[str, Any]]:
    rows = query_listings(
        db_path,
        sources="tripadvisor",
        scopes="outside-budapest",
        sort="quality",
        limit=min(10_000, max(1, limit)),
    )
    connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    candidates: list[dict[str, Any]] = []
    try:
        for row in rows:
            try:
                _route, _listing_id, geo = _route_identity(row)
            except ValueError:
                continue
            photos = [
                media["url"]
                for media in connection.execute(
                    """
                    SELECT url FROM media
                    WHERE listing_id = ? AND active = 1 AND media_type = 'image'
                    ORDER BY sort_order, id
                    LIMIT 5
                    """,
                    (row["id"],),
                ).fetchall()
            ]
            categories = [
                value.strip()
                for value in str(row.get("categories") or "").split("|")
                if value.strip()
            ]
            candidates.append(
                {
                    "name": row["title"],
                    "rating": row.get("rating"),
                    "reviews": int(row.get("review_count") or 0),
                    "rank": len(candidates) + 1,
                    "subtype": categories[1] if len(categories) > 1 else "",
                    "badge": "",
                    "openStatus": "",
                    "closed": False,
                    "blurb": row.get("description") or "",
                    "url": row["url"],
                    "photos": photos,
                    "city": "hungary",
                    "geo": geo,
                    "origin": "hungary-outside-budapest",
                    "cat": "",
                    "catLabel": categories[0] if categories else "",
                    "status": "new",
                    "matched": None,
                    "score": row.get("bayesian_rating"),
                }
            )
    finally:
        connection.close()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(output, 0o600)
    return candidates


def _money(value: Any) -> tuple[float | None, str | None]:
    if value is None or isinstance(value, bool):
        return None, None
    text = str(value).replace("\u00a0", " ").strip()
    currency = None
    if "$" in text:
        currency = "USD"
    elif "€" in text:
        currency = "EUR"
    elif "£" in text:
        currency = "GBP"
    elif re.search(r"\b(?:HUF|Ft)\b", text, re.I):
        currency = "HUF"
    else:
        match = re.search(r"\b([A-Z]{3})\b", text)
        currency = match.group(1) if match else None
    number = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not number:
        return None, currency
    try:
        amount = Decimal(number.group(0).replace(",", ""))
    except InvalidOperation:
        return None, currency
    return (float(amount) if amount.is_finite() and amount >= 0 else None), currency


def _normalized_context(row: Mapping[str, Any]) -> dict[str, Any]:
    pricing = row.get("pricing_evidence")
    pricing = pricing if isinstance(pricing, Mapping) else {}
    price_from, currency = _money(pricing.get("base_price"))
    packages: list[dict[str, Any]] = []
    for index, value in enumerate(pricing.get("packages") or []):
        if not isinstance(value, Mapping):
            continue
        price, package_currency = _money(
            value.get("total_price") or value.get("unit_price")
        )
        facts = [
            str(value.get("description") or "").strip(),
            f"Party: {value['party']}" if value.get("party") else "",
            f"Unit price: {value['unit_price']}" if value.get("unit_price") else "",
        ]
        availability = "; ".join(
            part
            for part in (
                str(value.get("availability") or "").strip(),
                str(value.get("available_times") or "").strip(),
            )
            if part
        )
        packages.append(
            {
                "external_id": None,
                "name": str(value.get("name") or f"Option {index + 1}"),
                "description": "\n".join(part for part in facts if part),
                "price": price,
                "original_price": None,
                "currency": package_currency or currency,
                "duration_text": None,
                "availability_text": availability or None,
                "url": str(row.get("url") or "") or None,
                "provider": "Tripadvisor",
                "category": "Bookable option",
                "sort_order": index,
            }
        )
    reviews = []
    for review in (row.get("reviews") or [])[:MAX_REVIEWS]:
        if not isinstance(review, Mapping):
            continue
        reviews.append(
            {
                "external_id": None,
                "rating": review.get("rating"),
                "title": review.get("title"),
                "body": review.get("text") or review.get("body"),
                "language": review.get("language"),
                "review_date": review.get("review_date") or review.get("date"),
                "helpful_count": review.get("helpful_count"),
            }
        )
    categories = [row.get("category"), row.get("subtype")]
    return {
        "source": "tripadvisor",
        "external_id": str(row["id"]),
        "url": row.get("url") or row.get("canonical_url"),
        "title": row.get("name") or row.get("page_title") or f"Tripadvisor {row['id']}",
        "kind": row.get("kind") or "attraction",
        "description": row.get("description"),
        "location_text": None,
        "rating": row.get("rating"),
        "review_count": row.get("review_count"),
        "price_from": price_from,
        "currency": currency,
        "duration_text": None,
        "location_scope": "unknown",
        "starts_in_budapest": False,
        "place": {
            "canonical_name": row.get("name") or row.get("page_title"),
            "location_scope": "unknown",
            "starts_in_budapest": False,
        },
        "categories": [str(value) for value in categories if value],
        "media": [],
        "packages": packages,
        "reviews": reviews,
    }


def normalized_from_evidence(
    evidence: Mapping[str, Any], listing: Mapping[str, Any]
) -> dict[str, Any]:
    """Strictly parse one bound GraphQL artifact into the store projection."""

    expected_url = str(listing.get("url") or "")
    transport = str(evidence.get("transport") or "")
    if transport == "tripadvisor-browser-graphql":
        metadata = validate_graphql_evidence(evidence, expected_url=expected_url)
        if str(metadata.get("detail_id")) != str(listing.get("external_id")):
            raise ValueError("GraphQL detail identity does not match the database listing")
        parsed = parse_graphql_evidence(evidence, expected_url=expected_url)
    elif transport == "tripadvisor-browser-html":
        if str(evidence.get("sourceUrl") or "") != expected_url:
            raise ValueError("rendered Tripadvisor evidence URL contradicts the listing")
        if str(evidence.get("externalId") or "") != str(listing.get("external_id")):
            raise ValueError("rendered Tripadvisor evidence ID contradicts the listing")
        html = evidence.get("renderedHtml")
        if not isinstance(html, str) or len(html.encode("utf-8")) < 30_000:
            raise ValueError("rendered Tripadvisor HTML evidence is incomplete")
        parsed = parse_detail_html(
            html,
            listing_id=str(listing["external_id"]),
            review_limit=MAX_REVIEWS,
        )
        canonical = {"url": parsed.get("canonical_url")}
        expected_route, expected_id, _geo = _route_identity(listing)
        actual_route, actual_id, _actual_geo = _route_identity(canonical)
        if (actual_route, actual_id) != (expected_route, expected_id):
            raise ValueError("rendered Tripadvisor canonical identity mismatch")
    else:
        raise ValueError(f"unsupported Tripadvisor browser transport: {transport!r}")
    return _normalized_context(
        {
            "id": str(listing["external_id"]),
            "url": expected_url,
            "canonical_url": expected_url,
            "name": listing["title"],
            "kind": listing.get("kind") or "attraction",
            "page_title": parsed.get("page_title") or listing["title"],
            "category": None,
            "subtype": None,
            "rating": listing.get("rating"),
            "review_count": listing.get("review_count"),
            "description": parsed.get("description"),
            "reviews": parsed.get("reviews") or [],
            "pricing_evidence": parsed.get("pricing_evidence") or {},
        }
    )


def import_context(
    db_path: str | Path = DEFAULT_DB_PATH,
    context_path: str | Path = DEFAULT_CONTEXT,
    *,
    raw_dir: str | Path = DEFAULT_RAW_DIR,
) -> dict[str, int]:
    context_file = Path(context_path)
    rows = json.loads(context_file.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Tripadvisor detail context must be a JSON list")
    totals = {"context_rows": len(rows), "stored": 0, "reviews": 0, "packages": 0, "skipped": 0}
    with ResearchStore(db_path) as store:
        prepared: list[
            tuple[
                int,
                Mapping[str, Any],
                Path,
                Mapping[str, Any],
                dict[str, Any],
                str,
            ]
        ] = []
        fingerprint_parts = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                totals["skipped"] += 1
                continue
            route, listing_id, _geo = _route_identity(row)
            if str(row.get("id") or "") != listing_id:
                raise ValueError("Tripadvisor context row ID contradicts its URL")
            expected_key = f"{route}:{listing_id}"
            if row.get("key") and str(row["key"]) != expected_key:
                raise ValueError("Tripadvisor context key contradicts its URL")
            listing_row = store.connection.execute(
                """
                SELECT id, external_id, title, kind, url, rating, review_count
                FROM listings
                WHERE source = 'tripadvisor' AND external_id = ?
                """,
                (listing_id,),
            ).fetchone()
            if listing_row is None:
                totals["skipped"] += 1
                continue
            listing = dict(listing_row)
            database_route, database_id, _database_geo = _route_identity(listing)
            if database_route != route or database_id != listing_id:
                raise ValueError("Tripadvisor context URL contradicts the database listing")
            graphql_path = _graphql_path(route, listing_id, Path(raw_dir))
            html_path = _html_path(route, listing_id, Path(raw_dir))
            if graphql_path.is_file():
                evidence_path = graphql_path
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                if not isinstance(evidence, Mapping):
                    raise ValueError(f"GraphQL evidence is not an object: {evidence_path}")
                enrichment_version = GRAPHQL_ENRICHMENT_VERSION
            elif cache_looks_valid(
                html_path,
                listing["url"],
                expected_review_count=listing.get("review_count"),
            ):
                evidence_path = html_path
                evidence = {
                    "transport": "tripadvisor-browser-html",
                    "source": "tripadvisor",
                    "sourceUrl": listing["url"],
                    "externalId": listing_id,
                    "checkedAt": datetime.fromtimestamp(
                        html_path.stat().st_mtime, tz=timezone.utc
                    ).replace(microsecond=0).isoformat(),
                    "renderedHtml": html_path.read_text(
                        encoding="utf-8", errors="replace"
                    ),
                }
                enrichment_version = HTML_ENRICHMENT_VERSION
            else:
                raise ValueError(
                    f"no valid GraphQL or rendered HTML evidence for {expected_key}"
                )
            normalized = normalized_from_evidence(evidence, listing)
            evidence_sha = hashlib.sha256(
                canonical_json(evidence).encode("utf-8")
            ).hexdigest()
            fingerprint_parts.append(
                (expected_key, evidence_sha, hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest())
            )
            prepared.append(
                (
                    index,
                    row,
                    evidence_path,
                    evidence,
                    normalized,
                    enrichment_version,
                )
            )

        fingerprint = hashlib.sha256(
            canonical_json(sorted(fingerprint_parts)).encode("utf-8")
        ).hexdigest()[:20]
        actor_run_id = f"headless:tripadvisor:hungary:{fingerprint}"
        run_id = store.begin_run(
            "tripadvisor",
            actor_run_id=actor_run_id,
            dataset_id=str(context_file),
            input_data={"transport": "graphql", "max_reviews": MAX_REVIEWS},
            metadata={
                "actor_config_key": "tripadvisor-headless-details",
                "actor_id": "repository-camoufox",
                "phase_label": "outside-budapest-headless-details",
                "enrichment_version": "tripadvisor-browser-mixed-v1",
            },
        )
        for index, row, evidence_path, evidence, normalized, enrichment_version in prepared:
            stored = store.ingest_item(
                run_id,
                evidence,
                source="tripadvisor",
                normalized=normalized,
                item_index=index,
                query_label="outside-budapest-headless-details",
                destination="Hungary outside Budapest",
                result_rank=index + 1,
                item_metadata={
                    "transport": evidence.get("transport"),
                    "evidence_path": str(evidence_path),
                    "context_path": str(context_file),
                },
            )
            store.mark_enrichment(
                stored["listing_id"],
                kind="tripadvisor-headless-detail",
                version=enrichment_version,
                raw_payload_id=stored["raw_payload_id"],
            )
            totals["stored"] += 1
            totals["reviews"] += len(normalized["reviews"])
            totals["packages"] += len(normalized["packages"])
        status = "complete" if totals["skipped"] == 0 else "partial"
        store.finish_run(run_id, status=status, stats=totals)
    return totals


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, default=DEFAULT_CANDIDATES)
    export.add_argument("--limit", type=int, default=90)
    ingest = commands.add_parser("import")
    ingest.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    ingest.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "export":
        rows = export_candidates(args.db, args.output, limit=args.limit)
        print(f"Exported {len(rows)} quality-ranked Tripadvisor candidates to {args.output}")
        return 0
    totals = import_context(args.db, args.context, raw_dir=args.raw_dir)
    print(
        f"Imported {totals['stored']}/{totals['context_rows']} headless contexts: "
        f"{totals['reviews']} reviews, {totals['packages']} package options; "
        f"{totals['skipped']} skipped."
    )
    return 0 if totals["skipped"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
