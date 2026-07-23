#!/usr/bin/env python3
"""Search and rank the durable activity-research SQLite database."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from store import DEFAULT_DB_PATH, VALID_SCOPES
from terminal_safety import terminal_line


LISTING_SORTS = {
    "quality": "q.bayesian_rating DESC, l.review_count DESC, l.rating DESC, l.title COLLATE NOCASE",
    "rating": "l.rating IS NULL, l.rating DESC, l.review_count DESC, l.title COLLATE NOCASE",
    "reviews": "l.review_count IS NULL, l.review_count DESC, l.rating DESC, l.title COLLATE NOCASE",
    "price": "l.price_from IS NULL, l.price_from ASC, q.bayesian_rating DESC",
    "title": "l.title COLLATE NOCASE ASC",
    "recent": "l.last_seen_at DESC, l.title COLLATE NOCASE",
}

REVIEW_SORTS = {
    "recent": "r.review_date IS NULL, r.review_date DESC, r.id DESC",
    "rating": "r.rating IS NULL, r.rating DESC, r.review_date DESC",
    "helpful": "r.helpful_count IS NULL, r.helpful_count DESC, r.review_date DESC",
    "relevance": "search_rank ASC, r.review_date DESC",
}

def _terminal_cell(value: Any) -> str:
    """Render provider text without terminal control-sequence injection."""

    return terminal_line(value)


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"research database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _values(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _fts_query(search: str) -> str:
    tokens = re.findall(r"\w+", search, flags=re.UNICODE)
    if not tokens:
        raise ValueError("search must contain at least one letter or number")
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def query_listings(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    search: str | None = None,
    sources: str | Iterable[str] | None = None,
    kinds: str | Iterable[str] | None = None,
    scopes: str | Iterable[str] | None = None,
    starts_in_budapest: bool | None = None,
    category: str | None = None,
    min_rating: float | None = None,
    min_reviews: int | None = None,
    max_price: float | None = None,
    currency: str | None = None,
    sort: str = "quality",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if sort not in LISTING_SORTS:
        raise ValueError(f"unknown listing sort: {sort}")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    if offset < 0:
        raise ValueError("offset must not be negative")

    source_values = [_value.strip().lower() for _value in _values(sources) if _value.strip()]
    kind_values = [_value.strip().casefold() for _value in _values(kinds) if _value.strip()]
    scope_values = [_value.strip() for _value in _values(scopes) if _value.strip()]
    invalid_scopes = set(scope_values) - VALID_SCOPES
    if invalid_scopes:
        raise ValueError(f"invalid scopes: {', '.join(sorted(invalid_scopes))}")

    joins = ["JOIN listing_quality_ranking AS q ON q.listing_id = l.id"]
    where = ["l.active = 1"]
    params: list[Any] = []
    if search:
        joins.append("JOIN listing_fts AS lf ON lf.rowid = l.id")
        where.append("listing_fts MATCH ?")
        params.append(_fts_query(search))
    if source_values:
        where.append(f"l.source IN ({','.join('?' for _ in source_values)})")
        params.extend(source_values)
    if kind_values:
        where.append(f"l.kind IN ({','.join('?' for _ in kind_values)})")
        params.extend(kind_values)
    if scope_values:
        where.append(f"l.location_scope IN ({','.join('?' for _ in scope_values)})")
        params.extend(scope_values)
    if starts_in_budapest is not None:
        where.append("l.starts_in_budapest = ?")
        params.append(int(starts_in_budapest))
    if category:
        where.append(
            """
            EXISTS (
                SELECT 1
                FROM listing_categories AS lc_filter
                JOIN categories AS c_filter ON c_filter.id = lc_filter.category_id
                WHERE lc_filter.listing_id = l.id
                  AND (c_filter.slug = ? OR c_filter.name = ? COLLATE NOCASE)
            )
            """
        )
        params.extend([category, category])
    if min_rating is not None:
        where.append("l.rating >= ?")
        params.append(min_rating)
    if min_reviews is not None:
        where.append("l.review_count >= ?")
        params.append(min_reviews)
    if max_price is not None:
        where.append("l.price_from IS NOT NULL AND l.price_from <= ?")
        params.append(max_price)
    if currency:
        where.append("l.currency = ? COLLATE NOCASE")
        params.append(currency)

    sql = f"""
        SELECT
            l.id,
            l.source,
            l.kind,
            l.external_id,
            l.title,
            l.description,
            l.url,
            l.location_text,
            l.location_scope,
            l.starts_in_budapest,
            p.canonical_name AS place_name,
            p.country_code,
            p.region,
            p.locality,
            p.latitude,
            p.longitude,
            l.rating,
            l.review_count,
            q.bayesian_rating,
            l.price_from,
            l.currency,
            l.duration_text,
            l.first_seen_at,
            l.last_seen_at,
            (
                SELECT GROUP_CONCAT(c.name, ' | ')
                FROM listing_categories AS lc
                JOIN categories AS c ON c.id = lc.category_id
                WHERE lc.listing_id = l.id
            ) AS categories,
            (SELECT COUNT(*) FROM reviews AS r_count WHERE r_count.listing_id = l.id)
                AS stored_reviews,
            (SELECT COUNT(*) FROM media AS m WHERE m.listing_id = l.id AND m.active = 1)
                AS active_media,
            (SELECT COUNT(*) FROM packages AS pk WHERE pk.listing_id = l.id AND pk.active = 1)
                AS active_packages
        FROM listings AS l
        JOIN places AS p ON p.id = l.place_id
        {' '.join(joins)}
        WHERE {' AND '.join(where)}
        ORDER BY {LISTING_SORTS[sort]}
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    with closing(_connect(db_path)) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def query_reviews(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    search: str | None = None,
    sources: str | Iterable[str] | None = None,
    scopes: str | Iterable[str] | None = None,
    listing_external_id: str | None = None,
    min_rating: float | None = None,
    language: str | None = None,
    sort: str = "recent",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if sort not in REVIEW_SORTS:
        raise ValueError(f"unknown review sort: {sort}")
    if sort == "relevance" and not search:
        raise ValueError("review relevance sort requires --search")
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    if offset < 0:
        raise ValueError("offset must not be negative")

    source_values = [_value.strip().lower() for _value in _values(sources) if _value.strip()]
    scope_values = [_value.strip() for _value in _values(scopes) if _value.strip()]
    invalid_scopes = set(scope_values) - VALID_SCOPES
    if invalid_scopes:
        raise ValueError(f"invalid scopes: {', '.join(sorted(invalid_scopes))}")

    joins: list[str] = []
    where = ["1 = 1"]
    params: list[Any] = []
    rank_expression = "0.0"
    if search:
        joins.append("JOIN review_fts AS rf ON rf.rowid = r.id")
        where.append("review_fts MATCH ?")
        params.append(_fts_query(search))
        rank_expression = "bm25(review_fts)"
    if source_values:
        where.append(f"r.source IN ({','.join('?' for _ in source_values)})")
        params.extend(source_values)
    if scope_values:
        where.append(f"l.location_scope IN ({','.join('?' for _ in scope_values)})")
        params.extend(scope_values)
    if listing_external_id:
        where.append("l.external_id = ?")
        params.append(listing_external_id)
    if min_rating is not None:
        where.append("r.rating >= ?")
        params.append(min_rating)
    if language:
        where.append("r.language = ? COLLATE NOCASE")
        params.append(language)

    sql = f"""
        SELECT
            r.id,
            r.source,
            r.external_id,
            l.external_id AS listing_external_id,
            l.title AS listing_title,
            l.location_scope,
            r.rating,
            r.title,
            r.body,
            r.language,
            r.review_date,
            r.helpful_count,
            {rank_expression} AS search_rank
        FROM reviews AS r
        JOIN listings AS l ON l.id = r.listing_id
        {' '.join(joins)}
        WHERE {' AND '.join(where)}
        ORDER BY {REVIEW_SORTS[sort]}
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    with closing(_connect(db_path)) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def query_runs(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    source: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where = ["1 = 1"]
    params: list[Any] = []
    if source:
        where.append("run.source = ?")
        params.append(source.strip().lower())
    if status:
        where.append("run.status = ?")
        params.append(status)
    params.append(limit)
    sql = f"""
        SELECT
            run.*,
            COUNT(item.id) AS item_count,
            SUM(CASE WHEN item.status = 'stored' THEN 1 ELSE 0 END) AS stored_count,
            SUM(CASE WHEN item.status = 'failed' THEN 1 ELSE 0 END) AS failed_count
        FROM scrape_runs AS run
        LEFT JOIN scrape_run_items AS item ON item.run_id = run.id
        WHERE {' AND '.join(where)}
        GROUP BY run.id
        ORDER BY run.started_at DESC
        LIMIT ?
    """
    with closing(_connect(db_path)) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def database_stats(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    table_names = (
        "raw_payloads",
        "scrape_runs",
        "scrape_run_items",
        "places",
        "listings",
        "listing_snapshots",
        "categories",
        "media",
        "packages",
        "reviews",
    )
    with closing(_connect(db_path)) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in table_names
        }


def _add_common_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("table", "json", "jsonl"), default="table")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    listings = subparsers.add_parser("listings", help="search and rank listings")
    listings.add_argument("--search")
    listings.add_argument("--source", action="append", dest="sources")
    listings.add_argument("--kind", action="append", dest="kinds")
    listings.add_argument("--scope", action="append", choices=sorted(VALID_SCOPES), dest="scopes")
    listings.add_argument("--starts-in-budapest", choices=("any", "yes", "no"), default="any")
    listings.add_argument("--category")
    listings.add_argument("--min-rating", type=float)
    listings.add_argument("--min-reviews", type=int)
    listings.add_argument("--max-price", type=float)
    listings.add_argument("--currency")
    listings.add_argument("--sort", choices=tuple(LISTING_SORTS), default="quality")
    listings.add_argument("--limit", type=int, default=100)
    listings.add_argument("--offset", type=int, default=0)
    _add_common_output(listings)

    reviews = subparsers.add_parser("reviews", help="full-text search normalized reviews")
    reviews.add_argument("--search")
    reviews.add_argument("--source", action="append", dest="sources")
    reviews.add_argument("--scope", action="append", choices=sorted(VALID_SCOPES), dest="scopes")
    reviews.add_argument("--listing-external-id")
    reviews.add_argument("--min-rating", type=float)
    reviews.add_argument("--language")
    reviews.add_argument("--sort", choices=tuple(REVIEW_SORTS), default="recent")
    reviews.add_argument("--limit", type=int, default=100)
    reviews.add_argument("--offset", type=int, default=0)
    _add_common_output(reviews)

    runs = subparsers.add_parser("runs", help="inspect scrape provenance")
    runs.add_argument("--source")
    runs.add_argument("--status", choices=("running", "complete", "partial", "failed"))
    runs.add_argument("--limit", type=int, default=100)
    _add_common_output(runs)

    stats = subparsers.add_parser("stats", help="show database row counts")
    _add_common_output(stats)
    return parser


def _print_rows(rows: Sequence[dict[str, Any]], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if output_format == "jsonl":
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return
    if not rows:
        print("No results.")
        return
    preferred = (
        "source",
        "kind",
        "external_id",
        "title",
        "listing_title",
        "location_scope",
        "rating",
        "review_count",
        "bayesian_rating",
        "price_from",
        "currency",
        "status",
        "item_count",
    )
    columns = [column for column in preferred if any(column in row for row in rows)]
    if not columns:
        columns = list(rows[0])
    widths = {
        column: min(
            48,
            max(len(column), *(len(_terminal_cell(row.get(column, ""))) for row in rows)),
        )
        for column in columns
    }
    print("  ".join(column.ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        cells = []
        for column in columns:
            value = _terminal_cell(row.get(column, ""))
            if len(value) > widths[column]:
                value = value[: widths[column] - 1] + "…"
            cells.append(value.ljust(widths[column]))
        print("  ".join(cells))


def main() -> int:
    args = _parser().parse_args()
    if args.command == "listings":
        starts = None if args.starts_in_budapest == "any" else args.starts_in_budapest == "yes"
        rows = query_listings(
            args.db,
            search=args.search,
            sources=args.sources,
            kinds=args.kinds,
            scopes=args.scopes,
            starts_in_budapest=starts,
            category=args.category,
            min_rating=args.min_rating,
            min_reviews=args.min_reviews,
            max_price=args.max_price,
            currency=args.currency,
            sort=args.sort,
            limit=args.limit,
            offset=args.offset,
        )
        _print_rows(rows, args.format)
    elif args.command == "reviews":
        rows = query_reviews(
            args.db,
            search=args.search,
            sources=args.sources,
            scopes=args.scopes,
            listing_external_id=args.listing_external_id,
            min_rating=args.min_rating,
            language=args.language,
            sort=args.sort,
            limit=args.limit,
            offset=args.offset,
        )
        _print_rows(rows, args.format)
    elif args.command == "runs":
        _print_rows(
            query_runs(args.db, source=args.source, status=args.status, limit=args.limit),
            args.format,
        )
    else:
        stats = database_stats(args.db)
        _print_rows([{"table": key, "rows": value} for key, value in stats.items()], args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
