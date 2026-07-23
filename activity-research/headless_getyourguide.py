#!/usr/bin/env python3
"""Fill GetYourGuide detail gaps with private, cached Camoufox evidence.

The paid detail actor occasionally omits valid URLs.  ``scrape`` renders only
outside-Budapest listings that have no successful GetYourGuide detail
enrichment, saving the exact HTML and visible text under the gitignored data
directory.  ``import`` stores that evidence losslessly in SQLite and projects a
small, identity-free review sample plus the live headline price.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
DEFAULT_EVIDENCE_DIR = HERE / "data" / "getyourguide-headless"
ENRICHMENT_KIND = "getyourguide-detail"
ENRICHMENT_VERSION = "getyourguide-browser-render-v1"
MAX_REVIEWS = 10

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from store import DEFAULT_DB_PATH, ResearchStore, canonical_json  # noqa: E402
from terminal_safety import terminal_line  # noqa: E402


PRODUCT_ID_RE = re.compile(r"-t(?P<id>\d+)/?(?:[?#].*)?$", re.I)
REVIEW_START_RE = re.compile(
    r"(?m)^(?P<rating>[1-5](?:\.\d+)?)\n(?P=rating) out of 5 stars\n"
)
DATE_RE = re.compile(
    r"(?m)^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+\d{1,2},\s+\d{4}$"
)
PRODUCT_BOUNDARY_RE = re.compile(r"(?im)^\s*Product ID\s*:?\s*(\d+)\s*$")
ROUTE_PATH_RE = re.compile(
    r"\\?\"route\\?\"\s*:\s*\{\s*"
    r"\\?\"name\\?\"\s*:\s*\\?\"Activity\\?\"\s*,\s*"
    r"\\?\"path\\?\"\s*:\s*\\?\"(?P<path>(?:\\.|[^\"])*)",
    re.I,
)


class _CanonicalLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "link":
            return
        values = {key.casefold(): value for key, value in attrs}
        rel = str(values.get("rel") or "").casefold().split()
        href = values.get("href")
        if "canonical" in rel and href:
            self.urls.append(href)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _product_id(url: str) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in {"getyourguide.com", "www.getyourguide.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError(f"unsupported GetYourGuide host or scheme: {url!r}")
    match = PRODUCT_ID_RE.search(parsed.path)
    if not match:
        raise ValueError(f"unsupported GetYourGuide activity URL: {url!r}")
    return match.group("id")


def _validated_candidate_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    """Bind one database row to a safe provider URL and numeric filename ID."""

    url = str(row.get("url") or "").strip()
    product_id = _product_id(url)
    external_id = str(row.get("external_id") or "").strip()
    if not external_id or not external_id.isascii() or not external_id.isdecimal():
        raise ValueError("GetYourGuide database external_id must be numeric")
    if external_id != product_id:
        raise ValueError("GetYourGuide database URL contradicts external_id")
    return url, external_id


def _route_product_ids(rendered_html: str) -> list[str]:
    result: list[str] = []
    for match in ROUTE_PATH_RE.finditer(rendered_html):
        path = match.group("path")
        path = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda value: chr(int(value.group(1), 16)),
            path,
        ).replace(r"\/", "/")
        route_match = PRODUCT_ID_RE.search(path)
        if route_match:
            result.append(route_match.group("id"))
    return result


def _validate_rendered_identity(
    evidence: Mapping[str, Any], expected_url: str, external_id: str
) -> None:
    """Bind wrapper metadata to authoritative rendered-page identity signals."""

    if str(evidence.get("externalId") or "") != external_id:
        raise ValueError("GetYourGuide evidence externalId contradicts the listing")
    if str(evidence.get("sourceUrl") or "") != expected_url:
        raise ValueError("GetYourGuide evidence URL contradicts the listing")
    if _product_id(expected_url) != external_id:
        raise ValueError("GetYourGuide database URL contradicts externalId")
    page_url = evidence.get("pageUrl")
    if page_url is not None and _product_id(str(page_url)) != external_id:
        raise ValueError("rendered GetYourGuide page URL contradicts externalId")

    text = evidence.get("visibleText")
    html = evidence.get("renderedHtml")
    if not isinstance(text, str) or not isinstance(html, str):
        raise ValueError("GetYourGuide rendered evidence is incomplete")
    visible_ids = PRODUCT_BOUNDARY_RE.findall(text)
    if not visible_ids or set(visible_ids) != {external_id}:
        raise ValueError("rendered GetYourGuide Product ID contradicts externalId")

    parser = _CanonicalLinkParser()
    parser.feed(html)
    if not parser.urls:
        raise ValueError("rendered GetYourGuide page has no canonical activity URL")
    canonical_ids = {_product_id(url) for url in parser.urls}
    if canonical_ids != {external_id}:
        raise ValueError("rendered GetYourGuide canonical URL contradicts externalId")

    route_ids = set(_route_product_ids(html))
    if not route_ids or route_ids != {external_id}:
        raise ValueError("rendered GetYourGuide activity route contradicts externalId")


def select_missing(
    db_path: str | Path = DEFAULT_DB_PATH, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Return quality-ranked outside listings without any successful detail."""

    connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT l.id, l.external_id, l.title, l.url, l.rating,
                   q.review_count, q.bayesian_rating
            FROM listing_quality_ranking AS q
            JOIN listings AS l ON l.id = q.listing_id
            WHERE l.source = 'getyourguide'
              AND l.location_scope = 'outside-budapest'
              AND l.url LIKE 'http%'
              AND NOT EXISTS (
                  SELECT 1
                  FROM listing_enrichments AS e
                  WHERE e.listing_id = l.id
                    AND e.enrichment_kind = ?
              )
            ORDER BY q.bayesian_rating DESC, q.review_count DESC, l.id
            LIMIT ?
            """,
            (ENRICHMENT_KIND, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _evidence_path(evidence_dir: Path, external_id: str) -> Path:
    return evidence_dir / f"getyourguide_{external_id}.render.json"


def _valid_evidence(path: Path, expected_url: str | None = None) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(value, Mapping):
        return False
    text = value.get("visibleText")
    html = value.get("renderedHtml")
    complete = (
        isinstance(text, str)
        and len(text) >= 500
        and isinstance(html, str)
        and len(html) >= 1_000
        and "Customer reviews" in text
    )
    if not complete:
        return False
    if expected_url:
        try:
            _validate_rendered_identity(value, expected_url, _product_id(expected_url))
        except (TypeError, ValueError):
            return False
    return True


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    partial = path.with_name(path.name + ".part")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(partial, 0o600)
    partial.replace(path)
    os.chmod(path, 0o600)


def scrape_missing(
    db_path: str | Path = DEFAULT_DB_PATH,
    evidence_dir: str | Path = DEFAULT_EVIDENCE_DIR,
    *,
    limit: int = 100,
    refresh: bool = False,
    wait_seconds: float = 8,
) -> dict[str, int]:
    """Render missing pages.  This function requires the Camoufox venv."""

    candidates = select_missing(db_path, limit=limit)
    directory = Path(evidence_dir)
    totals = {"selected": len(candidates), "cached": 0, "fetched": 0, "failed": 0}
    if not candidates:
        return totals

    validated_candidates: list[tuple[int, Mapping[str, Any], str, str]] = []
    for index, row in enumerate(candidates, 1):
        try:
            url, external_id = _validated_candidate_identity(row)
        except (TypeError, ValueError) as exc:
            totals["failed"] += 1
            print(
                f"[{index}/{len(candidates)}] "
                f"{terminal_line(row.get('title') or '<untitled>')}: "
                f"FAIL (invalid candidate: {terminal_line(exc)})",
                file=sys.stderr,
                flush=True,
            )
            continue
        validated_candidates.append((index, row, url, external_id))
    if not validated_candidates:
        return totals

    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:  # pragma: no cover - depends on local browser venv
        raise RuntimeError(
            "Camoufox is unavailable; run scrape with the repository stealth venv"
        ) from exc

    with Camoufox(headless=True) as browser:
        for index, row, url, external_id in validated_candidates:
            path = _evidence_path(directory, external_id)
            if not refresh and _valid_evidence(path, url):
                os.chmod(path.parent, 0o700)
                os.chmod(path, 0o600)
                totals["cached"] += 1
                print(
                    f"[{index}/{len(candidates)}] {terminal_line(row['title'])}: cached",
                    flush=True,
                )
                continue
            error = None
            for attempt in range(1, 4):
                page = browser.new_page()
                page.on("pageerror", lambda _error: None)
                try:
                    page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                    final_product_id = _product_id(str(page.url or ""))
                    if final_product_id != external_id:
                        raise ValueError(
                            "rendered GetYourGuide page URL contradicts external_id"
                        )
                    page.wait_for_timeout(round(max(0, wait_seconds) * 1_000))
                    visible_text = page.locator("body").inner_text(timeout=15_000)
                    rendered_html = page.content()
                    evidence = {
                        "transport": "camoufox-rendered-page",
                        "source": "getyourguide",
                        "sourceUrl": url,
                        "pageUrl": str(page.url),
                        "externalId": external_id,
                        "listingTitle": row["title"],
                        "browserTitle": page.title(),
                        "checkedAt": _utc_now(),
                        "visibleText": visible_text,
                        "renderedHtml": rendered_html,
                    }
                    _atomic_json(path, evidence)
                    if not _valid_evidence(path, url):
                        raise ValueError("rendered page lacks full activity/review evidence")
                    totals["fetched"] += 1
                    suffix = f" after {attempt} attempts" if attempt > 1 else ""
                    print(
                        f"[{index}/{len(candidates)}] "
                        f"{terminal_line(row['title'])}: fetched{suffix}",
                        flush=True,
                    )
                    error = None
                    break
                except Exception as exc:  # browser failures need bounded retries
                    error = terminal_line(
                        f"{type(exc).__name__}: {exc}", limit=180
                    )
                    if attempt < 3:
                        time.sleep(attempt * 2)
                finally:
                    page.close()
            if error:
                totals["failed"] += 1
                print(
                    f"[{index}/{len(candidates)}] "
                    f"{terminal_line(row['title'])}: FAIL ({error})",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(1)
    return totals


def _section(text: str, heading: str, endings: Sequence[str]) -> str | None:
    marker = f"\n{heading}\n"
    start = text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = len(text)
    for ending in endings:
        position = text.find(f"\n{ending}\n", start)
        if position >= 0:
            end = min(end, position)
    value = text[start:end].strip()
    return value or None


def _money(value: str | None) -> tuple[float | None, str | None]:
    if not value:
        return None, None
    currency = (
        "EUR" if "€" in value else "USD" if "$" in value else "GBP" if "£" in value else None
    )
    match = re.search(r"\d[\d,.]*(?:\s?\d+)?", value)
    if not match:
        return None, currency
    number = match.group(0).replace(" ", "")
    if number.count(",") == 1 and "." not in number:
        left, right = number.split(",")
        number = f"{left}.{right}" if len(right) == 2 else left + right
    else:
        number = number.replace(",", "")
    try:
        amount = Decimal(number)
    except InvalidOperation:
        return None, currency
    return (float(amount) if amount.is_finite() and amount >= 0 else None), currency


def _parse_reviews(text: str) -> list[dict[str, Any]]:
    customer = text.find("\nCustomer reviews\n")
    if customer < 0:
        return []
    review_text = text[customer:]
    filter_at = review_text.find("\nFilter\n")
    if filter_at >= 0:
        review_text = review_text[filter_at + len("\nFilter\n") :]
    product_boundary = PRODUCT_BOUNDARY_RE.search(review_text)
    if product_boundary:
        review_text = review_text[: product_boundary.start()]
    matches = list(REVIEW_START_RE.finditer(review_text))
    reviews: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        block = review_text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(review_text)]
        verified = block.find("\nVerified booking\n")
        if verified < 0:
            continue
        preface = block[:verified]
        body = block[verified + len("\nVerified booking\n") :]
        control = re.search(
            r"(?im)^\s*(?:Response from Provider|Helpful\?|See more reviews|Product ID\s*:?).*$",
            body,
        )
        if control:
            body = body[: control.start()]
        body = body.strip()
        if not body or body.casefold() == "see more reviews":
            continue
        date_match = DATE_RE.search(preface)
        reviews.append(
            {
                "external_id": None,
                "rating": float(match.group("rating")),
                "title": None,
                "body": body,
                "language": None,
                "review_date": date_match.group(0) if date_match else None,
                "helpful_count": None,
            }
        )
        if len(reviews) >= MAX_REVIEWS:
            break
    return reviews


def parse_evidence(
    evidence: Mapping[str, Any], listing: Mapping[str, Any]
) -> dict[str, Any]:
    text = evidence.get("visibleText")
    if not isinstance(text, str):
        raise ValueError("GetYourGuide evidence has no visible text")
    full_description = _section(
        text,
        "Full description",
        ("Includes", "Not suitable for", "Meeting point", "Important information"),
    )
    if full_description:
        full_description = re.sub(r"\nSee (?:more|less)\s*$", "", full_description).strip()
    highlights = _section(text, "Highlights", ("Full description", "Includes"))
    description = full_description
    if highlights:
        description = (
            f"{description}\n\nHighlights:\n{highlights}" if description else f"Highlights:\n{highlights}"
        )
    duration_match = re.search(r"(?m)^Duration\s+(.+)$", text)
    cancellation = _section(text, "Free cancellation", ("Reserve now & pay later", "Duration"))
    price_match = re.search(r"(?m)^From\n(?P<price>[^\n]+)\nper person$", text)
    price, currency = _money(price_match.group("price") if price_match else None)
    reviews = _parse_reviews(text)
    packages = []
    if price is not None:
        packages.append(
            {
                "external_id": None,
                "name": "Current headline from-price",
                "description": "Per-person headline price shown on the rendered activity page; options may vary by date, language, and party.",
                "price": price,
                "original_price": None,
                "currency": currency,
                "duration_text": duration_match.group(1).strip() if duration_match else None,
                "availability_text": "Check live availability for the selected date and option",
                "url": listing["url"],
                "provider": "GetYourGuide",
                "category": "Headline price",
                "sort_order": 0,
            }
        )
    return {
        "source": "getyourguide",
        "external_id": str(listing["external_id"]),
        "url": listing["url"],
        "title": listing["title"],
        "kind": listing.get("kind") or "experience",
        "description": description,
        "location_text": None,
        "rating": listing["rating"],
        "review_count": listing["review_count"],
        "price_from": price,
        "currency": currency,
        "duration_text": duration_match.group(1).strip() if duration_match else None,
        "cancellation_policy": cancellation,
        "location_scope": "unknown",
        "starts_in_budapest": False,
        "place": {
            "canonical_name": listing["title"],
            "location_scope": "unknown",
            "starts_in_budapest": False,
        },
        "categories": [],
        "media": [],
        "packages": packages,
        "reviews": reviews,
    }


def normalized_from_evidence(
    evidence: Mapping[str, Any], listing: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate identity/content binding, then build the normalized projection."""

    external_id = str(listing.get("external_id") or "")
    expected_url = str(listing.get("url") or "")
    _validate_rendered_identity(evidence, expected_url, external_id)
    text = evidence.get("visibleText")
    html = evidence.get("renderedHtml")
    if (
        not isinstance(text, str)
        or len(text) < 500
        or "Customer reviews" not in text
        or not isinstance(html, str)
        or len(html) < 1_000
    ):
        raise ValueError("GetYourGuide rendered evidence is incomplete")
    return parse_evidence(evidence, listing)


def import_evidence(
    db_path: str | Path = DEFAULT_DB_PATH,
    evidence_dir: str | Path = DEFAULT_EVIDENCE_DIR,
) -> dict[str, int]:
    directory = Path(evidence_dir)
    paths = sorted(directory.glob("getyourguide_*.render.json"))
    totals = {
        "evidence_files": len(paths),
        "stored": 0,
        "reviews": 0,
        "packages": 0,
        "skipped": 0,
    }
    if not paths:
        return totals

    with ResearchStore(db_path) as store:
        prepared: list[tuple[int, Path, Mapping[str, Any], dict[str, Any], dict[str, Any]]] = []
        fingerprint_parts = []
        for index, path in enumerate(paths):
            try:
                evidence = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                totals["skipped"] += 1
                continue
            if not isinstance(evidence, Mapping):
                totals["skipped"] += 1
                continue
            external_id = str(evidence.get("externalId") or "")
            listing_row = store.connection.execute(
                """
                SELECT id, external_id, title, kind, url, rating, review_count
                FROM listings
                WHERE source = 'getyourguide' AND external_id = ?
                """,
                (external_id,),
            ).fetchone()
            if listing_row is None:
                totals["skipped"] += 1
                continue
            listing = dict(listing_row)
            expected_name = _evidence_path(directory, external_id).name
            if path.name != expected_name:
                raise ValueError("GetYourGuide evidence filename contradicts externalId")
            if _product_id(str(evidence.get("sourceUrl") or "")) != external_id:
                raise ValueError("GetYourGuide evidence URL contradicts externalId")
            if _product_id(str(listing["url"])) != external_id:
                raise ValueError("GetYourGuide database URL contradicts externalId")
            if not _valid_evidence(path, str(listing["url"])):
                raise ValueError(f"GetYourGuide evidence is invalid or stale: {path}")
            normalized = normalized_from_evidence(evidence, listing)
            fingerprint_parts.append(
                (path.name, hashlib.sha256(canonical_json(evidence).encode("utf-8")).hexdigest())
            )
            prepared.append((index, path, evidence, listing, normalized))

        if not prepared:
            return totals
        fingerprint = hashlib.sha256(
            canonical_json(sorted(fingerprint_parts)).encode("utf-8")
        ).hexdigest()[:20]
        run_id = store.begin_run(
            "getyourguide",
            actor_run_id=f"headless:getyourguide:hungary:{fingerprint}",
            dataset_id=str(directory),
            input_data={"transport": "rendered-html", "max_reviews": MAX_REVIEWS},
            metadata={
                "actor_config_key": "getyourguide-headless-details",
                "actor_id": "repository-camoufox",
                "phase_label": "outside-budapest-headless-details",
                "enrichment_version": ENRICHMENT_VERSION,
            },
        )
        for index, path, evidence, listing, normalized in prepared:
            stored = store.ingest_item(
                run_id,
                evidence,
                source="getyourguide",
                normalized=normalized,
                item_index=index,
                query_label="outside-budapest-headless-details",
                destination="Hungary outside Budapest",
                result_rank=index + 1,
                item_metadata={
                    "transport": "camoufox-rendered-page",
                    "evidence_path": str(path),
                },
            )
            store.mark_enrichment(
                stored["listing_id"],
                kind=ENRICHMENT_KIND,
                version=ENRICHMENT_VERSION,
                raw_payload_id=stored["raw_payload_id"],
            )
            store.record_enrichment_attempt(
                stored["listing_id"],
                kind=ENRICHMENT_KIND,
                version=ENRICHMENT_VERSION,
                run_id=run_id,
                status="succeeded",
                requested_url=listing["url"],
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
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    scrape = commands.add_parser("scrape")
    scrape.add_argument("--limit", type=int, default=100)
    scrape.add_argument("--refresh", action="store_true")
    scrape.add_argument("--wait", type=float, default=8)
    commands.add_parser("import")
    commands.add_parser("status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        rows = select_missing(args.db)
        print(f"{len(rows)} outside-Budapest GetYourGuide listing(s) lack detail evidence")
        for row in rows:
            print(
                f"  {terminal_line(row['external_id'])}: "
                f"{terminal_line(row['title'])}"
            )
        return 0
    if args.command == "scrape":
        totals = scrape_missing(
            args.db,
            args.evidence_dir,
            limit=args.limit,
            refresh=args.refresh,
            wait_seconds=args.wait,
        )
        print(
            f"Rendered {totals['fetched']} new and reused {totals['cached']} cached "
            f"of {totals['selected']} selected; {totals['failed']} failed."
        )
        return 1 if totals["failed"] else 0
    totals = import_evidence(args.db, args.evidence_dir)
    print(
        f"Imported {totals['stored']}/{totals['evidence_files']} evidence files: "
        f"{totals['reviews']} reviews, {totals['packages']} price options; "
        f"{totals['skipped']} skipped."
    )
    return 1 if totals["skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
