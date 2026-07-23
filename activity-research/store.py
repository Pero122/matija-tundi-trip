#!/usr/bin/env python3
"""Durable, idempotent SQLite storage for activity discovery research."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "activity-research.sqlite3"
SCHEMA_PATH = ROOT / "schema.sql"
VALID_SCOPES = {"budapest", "outside-budapest", "foreign", "unknown"}
VALID_RUN_STATUSES = {"running", "complete", "partial", "failed"}
SCHEMA_VERSION = 6


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    """Return the exact stable representation used for payload hashing/storage."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_or_none(value: Any) -> str | None:
    return None if value is None else canonical_json(value)


def _source(value: str) -> str:
    normalized = str(value).strip().lower()
    if not normalized:
        raise ValueError("source must not be empty")
    return normalized


def _dig(data: Any, dotted_path: str) -> Any:
    current = data
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _first(data: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        value = _dig(data, path)
        if value is not None and value != "":
            return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, Mapping):
        nested = _first(value, "text", "value", "name", "description", "content")
        return _text(nested)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = [part for item in value if (part := _text(item))]
        return "\n".join(parts) or None
    return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", " ")
    match = re.search(r"-?\d[\d.,]*", text)
    if not match:
        return None
    number = match.group(0)
    if "," in number and "." not in number and number.count(",") == 1:
        tail = number.rsplit(",", 1)[1]
        number = number.replace(",", ".") if len(tail) <= 2 else number.replace(",", "")
    else:
        number = number.replace(",", "")
    try:
        return float(number)
    except ValueError:
        return None


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"-?\d[\d, .]*", str(value))
    if not match:
        return None
    digits = re.sub(r"[^\d-]", "", match.group(0))
    try:
        return int(digits)
    except ValueError:
        return None


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value)
    ascii_text = folded.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or "uncategorized"


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        for key in ("items", "results", "data", "reviews", "options", "photos", "images"):
            nested = value.get(key)
            if isinstance(nested, (list, tuple)):
                return list(nested)
        return [value]
    return [value]


def _country_code(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    aliases = {
        "hungary": "HU",
        "magyarorszag": "HU",
        "magyarország": "HU",
        "austria": "AT",
        "slovakia": "SK",
        "slovenia": "SI",
        "croatia": "HR",
        "romania": "RO",
        "serbia": "RS",
        "ukraine": "UA",
    }
    lowered = text.casefold()
    if lowered in aliases:
        return aliases[lowered]
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return None


def _infer_scope(
    explicit: Any,
    country_code: str | None,
    locality: str | None,
    address: str | None,
    latitude: float | None,
    longitude: float | None,
) -> str:
    candidate = (_text(explicit) or "").casefold().replace("_", "-")
    aliases = {
        "budapest": "budapest",
        "outside-budapest": "outside-budapest",
        "hungary-outside-budapest": "outside-budapest",
        "foreign": "foreign",
        "outside-hungary": "foreign",
        "unknown": "unknown",
    }
    if candidate in aliases:
        return aliases[candidate]
    if country_code and country_code != "HU":
        return "foreign"
    location_words = " ".join(part for part in (locality, address) if part).casefold()
    if "budapest" in location_words:
        return "budapest"
    if latitude is not None and longitude is not None:
        if 47.34 <= latitude <= 47.66 and 18.82 <= longitude <= 19.36:
            return "budapest"
    if country_code == "HU":
        return "outside-budapest"
    return "unknown"


def _starts_in_budapest(data: Mapping[str, Any], title: str, description: str | None) -> bool:
    explicit = _first(
        data,
        "starts_in_budapest",
        "startsInBudapest",
        "departure.startsInBudapest",
        "meetingPoint.startsInBudapest",
    )
    if explicit is not None:
        return _boolean(explicit)
    departure = _text(
        _first(
            data,
            "departure.city",
            "departure.name",
            "meetingPoint.address",
            "meeting_point",
            "pickup",
        )
    )
    haystack = " ".join(part for part in (title, description, departure) if part).casefold()
    return bool(
        re.search(
            r"\b(from|depart(?:ure|ing)?\s+(?:from)?|pick(?:-?up)?\s+(?:from)?)\s+budapest\b",
            haystack,
        )
    )


def _category_records(data: Mapping[str, Any]) -> list[dict[str, str]]:
    candidates: list[Any] = []
    for key in ("categories", "category", "subcategories", "tags", "activityCategories"):
        candidates.extend(_items(data.get(key)))
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        name = _text(candidate)
        if isinstance(candidate, Mapping):
            name = _text(_first(candidate, "name", "title", "label", "slug"))
        if not name:
            continue
        slug = _slug(name)
        if slug not in seen:
            seen.add(slug)
            result.append({"slug": slug, "name": name})
    return result


def _media_records(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in ("media", "photos", "images", "pictures", "gallery"):
        candidates.extend(_items(data.get(key)))
    for key in ("image", "photo", "imageUrl", "image_url", "thumbnail"):
        if data.get(key) is not None:
            candidates.append(data[key])
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(candidates):
        if isinstance(item, str):
            record: dict[str, Any] = {"url": item}
        elif isinstance(item, Mapping):
            record = {
                "external_id": _text(_first(item, "external_id", "id", "photoId", "imageId")),
                "url": _text(
                    _first(
                        item,
                        "url",
                        "src",
                        "imageUrl",
                        "image_url",
                        "largeUrl",
                        "images.original.url",
                        "images.large.url",
                        "original.url",
                    )
                ),
                "caption": _text(_first(item, "caption", "title", "alt", "description")),
                "media_type": _text(_first(item, "media_type", "type")) or "image",
                "width": _integer(_first(item, "width", "images.original.width")),
                "height": _integer(_first(item, "height", "images.original.height")),
                "sort_order": _integer(_first(item, "sort_order", "order", "position")),
            }
        else:
            continue
        url = _text(record.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        record["url"] = url
        if record.get("sort_order") is None:
            record["sort_order"] = index
        result.append(record)
    return result


def _package_records(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in ("packages", "options", "variants", "ticketOptions", "priceOptions"):
        candidates.extend(_items(data.get(key)))
    offer_group = data.get("offerGroup")
    if isinstance(offer_group, Mapping):
        candidates.extend(_items(offer_group.get("offerList")))
    result: list[dict[str, Any]] = []
    for index, item in enumerate(candidates):
        if not isinstance(item, Mapping):
            name = _text(item)
            if name:
                result.append({"name": name, "external_id": str(index)})
            continue
        name = _text(_first(item, "name", "title", "label", "optionName")) or f"Option {index + 1}"
        result.append(
            {
                "external_id": _text(
                    _first(item, "external_id", "id", "optionId", "productId", "productCode")
                ),
                "name": name,
                "description": _text(_first(item, "description", "details", "summary")),
                "price": _float(_first(item, "price", "price.amount", "amount", "value")),
                "original_price": _float(
                    _first(item, "original_price", "originalPrice", "originalPrice.amount")
                ),
                "currency": _text(_first(item, "currency", "price.currency", "currencyCode")),
                "duration_text": _text(_first(item, "duration_text", "duration", "durationText")),
                "availability_text": _text(
                    _first(item, "availability_text", "availability", "availabilityText")
                ),
                "url": _text(_first(item, "url", "bookingUrl", "productUrl")),
                "provider": _text(_first(item, "provider", "partner", "supplier")),
                "category": _text(
                    _first(item, "category", "primaryCategory", "productCategory")
                ),
            }
        )
    return result


def _review_records(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in ("reviews", "sampleReviews", "reviewItems", "travelerReviews", "customerReviews"):
        candidates.extend(_items(data.get(key)))
    result: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        # Explicit whitelist: never carry reviewer identity into normalized data.
        result.append(
            {
                "external_id": _text(_first(item, "external_id", "id", "reviewId", "review_id")),
                "rating": _float(_first(item, "rating", "rating.value", "score")),
                "title": _text(_first(item, "title", "headline")),
                "body": _text(_first(item, "body", "text", "content", "review", "description")),
                "language": _text(_first(item, "language", "languageCode", "lang")),
                "review_date": _text(
                    _first(item, "review_date", "date", "publishedDate", "createdAt", "travelDate")
                ),
                "helpful_count": _integer(
                    _first(item, "helpful_count", "helpfulVotes", "helpfulCount")
                ),
            }
        )
    return result


def normalize_payload(source: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map common Apify/Tripadvisor/GetYourGuide fields into the store model."""
    if not isinstance(payload, Mapping):
        raise TypeError("listing payload must be a mapping")
    source = _source(source)
    title = _text(_first(payload, "title", "name", "activity.title", "product.title"))
    if not title:
        raise ValueError("listing payload has no title/name")
    description = _text(
        _first(payload, "description", "summary", "abstract", "about", "activity.description")
    )
    url = _text(_first(payload, "url", "webUrl", "web_url", "link", "activityUrl", "product.url"))
    external_id = _text(
        _first(
            payload,
            "external_id",
            "activityId",
            "activity_id",
            "locationId",
            "location_id",
            "productId",
            "product_id",
            "id",
        )
    )
    if not external_id:
        external_id = f"url:{url}" if url else f"sha256:{payload_sha256(payload)}"

    location = payload.get("place")
    if not isinstance(location, Mapping):
        location = payload.get("location")
    if not isinstance(location, Mapping):
        location = payload.get("destination")
    if not isinstance(location, Mapping):
        location = {}
    address_data = payload.get("address")
    if not isinstance(address_data, Mapping):
        address_data = location.get("address") if isinstance(location.get("address"), Mapping) else {}
    address = _text(
        _first(payload, "address", "addressString", "formattedAddress", "location.address")
    )
    if not address:
        address = _text(_first(address_data, "full", "formatted", "address", "street"))
    locality = _text(
        _first(
            payload,
            "city",
            "locality",
            "address.city",
            "address.locality",
            "location.city",
            "location.locality",
        )
    )
    region = _text(
        _first(payload, "region", "state", "address.region", "location.region", "location.state")
    )
    country_code = _country_code(
        _first(
            payload,
            "country_code",
            "countryCode",
            "country.code",
            "country",
            "address.countryCode",
            "address.country",
            "location.countryCode",
            "location.country.code",
            "location.country",
        )
    )
    latitude = _float(
        _first(payload, "latitude", "lat", "coordinates.latitude", "location.latitude", "location.lat")
    )
    longitude = _float(
        _first(payload, "longitude", "lng", "lon", "coordinates.longitude", "location.longitude", "location.lng")
    )
    starts_in_budapest = _starts_in_budapest(payload, title, description)
    scope = _infer_scope(
        _first(payload, "location_scope", "locationScope"),
        country_code,
        locality,
        address,
        latitude,
        longitude,
    )
    place_name = _text(_first(location, "canonical_name", "name", "title")) or locality or title
    location_text = _text(
        _first(payload, "location_text", "locationText", "addressString", "formattedAddress")
    ) or ", ".join(part for part in (place_name, locality, region, country_code) if part)

    return {
        "source": source,
        "external_id": external_id,
        "url": url,
        "title": title,
        "kind": _text(
            _first(payload, "kind", "type", "listingType", "businessType")
        )
        or "unknown",
        "description": description,
        "location_text": location_text or None,
        "rating": _float(
            _first(payload, "rating", "rating.value", "averageRating", "average_rating", "ratingScore")
        ),
        "review_count": _integer(
            _first(
                payload,
                "review_count",
                "reviewCount",
                "numberOfReviews",
                "reviewsCount",
                "reviews.count",
                "ratingCount",
            )
        ),
        "price_from": _float(
            _first(
                payload,
                "price_from",
                "priceFrom",
                "price.amount",
                "price.value",
                "startingPrice.amount",
                "fromPrice",
            )
        ),
        "currency": _text(
            _first(payload, "currency", "currencyCode", "price.currency", "startingPrice.currency")
        ),
        "duration_text": _text(
            _first(payload, "duration_text", "duration", "durationText", "activity.duration")
        ),
        "location_scope": scope,
        "starts_in_budapest": starts_in_budapest,
        "place": {
            "place_key": _text(_first(location, "place_key", "placeKey")),
            "canonical_name": place_name,
            "country_code": country_code,
            "region": region,
            "locality": locality,
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
            "location_scope": scope,
            "starts_in_budapest": starts_in_budapest,
        },
        "categories": _category_records(payload),
        "media": _media_records(payload),
        "packages": _package_records(payload),
        "reviews": _review_records(payload),
    }


def _merge_normalized_record(
    extracted: dict[str, Any], normalized: Mapping[str, Any]
) -> dict[str, Any]:
    """Adapt the stable provider normalizer shape without dropping raw-derived data."""
    incoming = dict(normalized)
    for key, value in incoming.items():
        if key in {"categories", "media", "packages", "reviews"} and not value and extracted.get(key):
            continue
        extracted[key] = value

    if incoming.get("price_from") is not None:
        extracted["price_from"] = incoming["price_from"]
    elif incoming.get("price") is not None:
        extracted["price_from"] = incoming["price"]
    if incoming.get("duration_text") is not None:
        extracted["duration_text"] = incoming["duration_text"]
    elif incoming.get("duration") is not None:
        extracted["duration_text"] = incoming["duration"]

    place = dict(extracted.get("place") or {})
    aliases = {
        "country": "country_code",
        "country_code": "country_code",
        "locality": "locality",
        "region": "region",
        "address": "address",
        "lat": "latitude",
        "latitude": "latitude",
        "lon": "longitude",
        "longitude": "longitude",
        "location_scope": "location_scope",
        "starts_in_budapest": "starts_in_budapest",
    }
    for incoming_key, place_key in aliases.items():
        if incoming.get(incoming_key) is not None:
            place[place_key] = incoming[incoming_key]
    if incoming.get("place") and isinstance(incoming["place"], Mapping):
        place.update(incoming["place"])
    if not _text(place.get("canonical_name")):
        place["canonical_name"] = _text(incoming.get("locality")) or extracted.get("title")
    extracted["place"] = place
    if not _text(extracted.get("location_text")):
        extracted["location_text"] = ", ".join(
            part
            for part in (
                _text(incoming.get("locality")),
                _text(incoming.get("region")),
                _text(incoming.get("country")),
            )
            if part
        ) or None
    return extracted


class ResearchStore:
    """Transaction-safe API around the activity-research database."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, *, initialize: bool = True):
        self.db_path = Path(db_path)
        parent = self.db_path.parent
        parent_was_created = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The default warehouse lives in a dedicated private directory.  A
        # caller-supplied DB may instead live in an existing shared directory
        # such as /tmp; changing that directory's mode would be surprising and
        # can fail even though creating the database itself is allowed.
        if parent_was_created or parent.resolve() == DEFAULT_DB_PATH.parent.resolve():
            os.chmod(parent, 0o700)
        self.connection = sqlite3.connect(self.db_path, timeout=30.0)
        os.chmod(self.db_path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        if initialize:
            self.initialize()
        self._secure_database_files()

    def _secure_database_files(self) -> None:
        """Keep the private warehouse and SQLite sidecars owner-readable only."""

        for path in (
            self.db_path,
            Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    def initialize(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"research database schema {version} is newer than supported "
                f"version {SCHEMA_VERSION}"
            )
        if version == 1:
            self._migrate_v1_to_v2()
            version = 2
        if version == 2:
            self._migrate_v2_to_v3()
            version = 3
        if version == 3:
            self._migrate_v3_to_v4()
            version = 4
        if version == 4:
            self._migrate_v4_to_v5()
            version = 5
        if version == 5:
            self._migrate_v5_to_v6()
            version = 6
        if version not in {0, SCHEMA_VERSION}:
            raise RuntimeError(f"unsupported research database schema version {version}")
        self.connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def _migrate_v1_to_v2(self) -> None:
        """Upgrade paid v1 warehouses in place without dropping scraped data."""

        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(scrape_runs)")
        }
        with self.transaction() as connection:
            if "plan_fingerprint" not in columns:
                connection.execute(
                    "ALTER TABLE scrape_runs ADD COLUMN plan_fingerprint TEXT"
                )
            if "next_offset" not in columns:
                connection.execute(
                    "ALTER TABLE scrape_runs ADD COLUMN next_offset INTEGER "
                    "NOT NULL DEFAULT 0 CHECK (next_offset >= 0)"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_enrichments (
                    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                    enrichment_kind TEXT NOT NULL,
                    enrichment_version TEXT NOT NULL,
                    raw_payload_id INTEGER NOT NULL REFERENCES raw_payloads(id) ON DELETE RESTRICT,
                    enriched_at TEXT NOT NULL,
                    PRIMARY KEY (listing_id, enrichment_kind, enrichment_version)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active_plan
                ON scrape_runs(plan_fingerprint)
                WHERE status = 'running' AND plan_fingerprint IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_enrichments_kind_time
                ON listing_enrichments(enrichment_kind, enriched_at DESC)
                """
            )
            connection.execute("PRAGMA user_version = 2")

    def _migrate_v2_to_v3(self) -> None:
        """Add searchable offer metadata without rebuilding paid data."""

        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(packages)")
        }
        with self.transaction() as connection:
            for name in ("url", "provider", "category"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE packages ADD COLUMN {name} TEXT")
            connection.execute("PRAGMA user_version = 3")

    def _migrate_v3_to_v4(self) -> None:
        """Track per-listing attempts so empty actor results are not repaid forever."""

        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_enrichment_attempts (
                    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                    enrichment_kind TEXT NOT NULL,
                    enrichment_version TEXT NOT NULL,
                    run_id INTEGER NOT NULL REFERENCES scrape_runs(id) ON DELETE CASCADE,
                    status TEXT NOT NULL
                        CHECK (status IN ('succeeded', 'not-returned', 'failed')),
                    requested_url TEXT,
                    attempted_at TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY (
                        listing_id, enrichment_kind, enrichment_version, run_id
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_enrichment_attempts_lookup
                ON listing_enrichment_attempts(
                    enrichment_kind, enrichment_version, status, listing_id
                )
                """
            )
            connection.execute("PRAGMA user_version = 4")

    def _migrate_v4_to_v5(self) -> None:
        """Persist the provider-normalized listing kind in current/history rows."""

        listing_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(listings)")
        }
        snapshot_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(listing_snapshots)")
        }
        with self.transaction() as connection:
            if "kind" not in listing_columns:
                connection.execute(
                    "ALTER TABLE listings ADD COLUMN kind TEXT NOT NULL "
                    "DEFAULT 'unknown' CHECK (length(trim(kind)) > 0)"
                )
            if "kind" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE listing_snapshots ADD COLUMN kind TEXT NOT NULL "
                    "DEFAULT 'unknown' CHECK (length(trim(kind)) > 0)"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_listings_kind_scope_quality
                ON listings(
                    kind, location_scope, active, rating DESC, review_count DESC
                )
                """
            )
            connection.execute("PRAGMA user_version = 5")

    def _migrate_v5_to_v6(self) -> None:
        """Give every run occurrence its own durable observation timestamp.

        Raw payloads are content-deduplicated, so ``raw_payloads.fetched_at``
        belongs to the first time that JSON was seen.  A later occurrence of
        the same JSON must retain its own run-item time rather than being
        replayed as if it happened at the first fetch.
        """

        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(scrape_run_items)")
        }
        with self.transaction() as connection:
            if "observed_at" not in columns:
                # SQLite cannot add a constraint-only NOT NULL column to a
                # populated table.  The non-empty default makes the column
                # structurally NOT NULL; every legacy row is replaced below,
                # and all v6 writers always supply the real value.
                connection.execute(
                    "ALTER TABLE scrape_run_items ADD COLUMN observed_at "
                    "TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                UPDATE scrape_run_items AS item
                SET observed_at = CASE
                    WHEN item.raw_payload_id IS NOT NULL
                     AND item.id = (
                        SELECT earlier.id
                        FROM scrape_run_items AS earlier
                        WHERE earlier.raw_payload_id = item.raw_payload_id
                        ORDER BY earlier.created_at, earlier.id
                        LIMIT 1
                     )
                    THEN COALESCE(
                        (SELECT raw.fetched_at
                         FROM raw_payloads AS raw
                         WHERE raw.id = item.raw_payload_id),
                        item.created_at
                    )
                    ELSE item.created_at
                END
                """
            )
            connection.execute("PRAGMA user_version = 6")

    def close(self) -> None:
        self._secure_database_files()
        self.connection.close()
        self._secure_database_files()

    def __enter__(self) -> "ResearchStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            if owns_transaction:
                self.connection.commit()
        except Exception:
            if owns_transaction:
                self.connection.rollback()
            raise

    def begin_run(
        self,
        source: str,
        *,
        actor_run_id: str | None = None,
        dataset_id: str | None = None,
        input_data: Any = None,
        metadata: Any = None,
        plan_fingerprint: str | None = None,
        started_at: str | None = None,
    ) -> int:
        source = _source(source)
        started_at = started_at or utc_now()
        with self.transaction() as connection:
            if actor_run_id:
                existing = connection.execute(
                    "SELECT id FROM scrape_runs WHERE source = ? AND actor_run_id = ?",
                    (source, actor_run_id),
                ).fetchone()
                if existing:
                    connection.execute(
                        """
                        UPDATE scrape_runs
                        SET dataset_id = COALESCE(?, dataset_id),
                            input_json = COALESCE(?, input_json),
                            metadata_json = COALESCE(?, metadata_json),
                            plan_fingerprint = COALESCE(?, plan_fingerprint)
                        WHERE id = ?
                        """,
                        (
                            dataset_id,
                            _json_or_none(input_data),
                            _json_or_none(metadata),
                            plan_fingerprint,
                            existing["id"],
                        ),
                    )
                    return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO scrape_runs(
                    source, actor_run_id, dataset_id, plan_fingerprint, status, started_at,
                    input_json, metadata_json
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    source,
                    actor_run_id,
                    dataset_id,
                    plan_fingerprint,
                    started_at,
                    _json_or_none(input_data),
                    _json_or_none(metadata),
                ),
            )
            return int(cursor.lastrowid)

    def claim_plan_run(
        self,
        source: str,
        plan_fingerprint: str,
        *,
        input_data: Any,
        metadata: Any,
        started_at: str | None = None,
    ) -> tuple[int, bool]:
        """Atomically claim one paid plan across processes before remote POST."""

        source = _source(source)
        fingerprint = str(plan_fingerprint).strip()
        if not fingerprint:
            raise ValueError("plan_fingerprint must not be empty")
        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT id FROM scrape_runs
                WHERE plan_fingerprint = ? AND status = 'running'
                """,
                (fingerprint,),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            pending_id = f"pending:{fingerprint[:24]}:{uuid.uuid4().hex}"
            cursor = connection.execute(
                """
                INSERT INTO scrape_runs(
                    source, actor_run_id, plan_fingerprint, status, started_at,
                    input_json, metadata_json
                ) VALUES (?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    source,
                    pending_id,
                    fingerprint,
                    started_at or utc_now(),
                    _json_or_none(input_data),
                    _json_or_none(metadata),
                ),
            )
            return int(cursor.lastrowid), True

    def attach_actor_run(
        self,
        run_id: int,
        actor_run_id: str,
        *,
        dataset_id: str | None = None,
        metadata: Any = None,
    ) -> None:
        """Attach the paid remote identity immediately after Apify responds."""

        remote_id = str(actor_run_id).strip()
        if not remote_id:
            raise ValueError("actor_run_id must not be empty")
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE scrape_runs
                SET actor_run_id = ?, dataset_id = COALESCE(?, dataset_id),
                    metadata_json = COALESCE(?, metadata_json)
                WHERE id = ?
                  AND (
                      actor_run_id IS NULL
                      OR actor_run_id LIKE 'pending:%'
                      OR actor_run_id = ?
                  )
                """,
                (remote_id, dataset_id, _json_or_none(metadata), run_id, remote_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(
                    f"unknown scrape run {run_id} or conflicting actor identity"
                )

    def update_run_offset(self, run_id: int, next_offset: int) -> None:
        if next_offset < 0:
            raise ValueError("next_offset must not be negative")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE scrape_runs SET next_offset = MAX(next_offset, ?) WHERE id = ?",
                (next_offset, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown scrape run {run_id}")

    def mark_enrichment(
        self,
        listing_id: int,
        *,
        kind: str,
        version: str,
        raw_payload_id: int,
        enriched_at: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO listing_enrichments(
                    listing_id, enrichment_kind, enrichment_version,
                    raw_payload_id, enriched_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(listing_id, enrichment_kind, enrichment_version)
                DO UPDATE SET raw_payload_id = excluded.raw_payload_id,
                              enriched_at = excluded.enriched_at
                WHERE COALESCE(julianday(excluded.enriched_at), 0) >=
                      COALESCE(julianday(listing_enrichments.enriched_at), 0)
                """,
                (listing_id, kind, version, raw_payload_id, enriched_at or utc_now()),
            )

    def record_enrichment_attempt(
        self,
        listing_id: int,
        *,
        kind: str,
        version: str,
        run_id: int,
        status: str,
        requested_url: str | None = None,
        attempted_at: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"succeeded", "not-returned", "failed"}:
            raise ValueError(f"invalid enrichment attempt status: {status}")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO listing_enrichment_attempts(
                    listing_id, enrichment_kind, enrichment_version, run_id,
                    status, requested_url, attempted_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    listing_id, enrichment_kind, enrichment_version, run_id
                ) DO UPDATE SET
                    status = excluded.status,
                    requested_url = COALESCE(
                        excluded.requested_url,
                        listing_enrichment_attempts.requested_url
                    ),
                    attempted_at = excluded.attempted_at,
                    error = excluded.error
                """,
                (
                    listing_id,
                    kind,
                    version,
                    run_id,
                    status,
                    requested_url,
                    attempted_at or utc_now(),
                    error,
                ),
            )

    def finish_run(
        self,
        run_id: int,
        *,
        status: str = "complete",
        stats: Any = None,
        metadata: Any = None,
        error: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        if status not in VALID_RUN_STATUSES - {"running"}:
            raise ValueError(f"invalid final run status: {status}")
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE scrape_runs
                SET status = ?, completed_at = ?, stats_json = ?,
                    metadata_json = COALESCE(?, metadata_json), error = ?
                WHERE id = ?
                """,
                (
                    status,
                    completed_at or utc_now(),
                    _json_or_none(stats),
                    _json_or_none(metadata),
                    error,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown scrape run {run_id}")

    def update_run_observation(
        self,
        run_id: int,
        *,
        stats: Any = None,
        metadata: Any = None,
        error: str | None = None,
    ) -> None:
        """Update replay telemetry without changing status or completion time."""

        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE scrape_runs
                SET stats_json = ?, metadata_json = COALESCE(?, metadata_json),
                    error = COALESCE(?, error)
                WHERE id = ?
                """,
                (_json_or_none(stats), _json_or_none(metadata), error, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown scrape run {run_id}")

    def ingest_item(
        self,
        run_id: int,
        payload: Mapping[str, Any],
        *,
        source: str | None = None,
        normalized: Mapping[str, Any] | None = None,
        item_index: int | None = None,
        query_label: str | None = None,
        destination: str | None = None,
        result_rank: int | None = None,
        item_metadata: Any = None,
        fetched_at: str | None = None,
    ) -> dict[str, int]:
        pending_error: Exception | None = None
        response: dict[str, int] | None = None
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT source FROM scrape_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run:
                raise KeyError(f"unknown scrape run {run_id}")
            effective_source = _source(source or run["source"])
            if item_index is None:
                row = connection.execute(
                    "SELECT COALESCE(MAX(item_index), -1) + 1 AS next_index "
                    "FROM scrape_run_items WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                item_index = int(row["next_index"])
            seen_at = fetched_at or utc_now()
            raw_payload_id = self._store_raw(connection, effective_source, payload, seen_at)
            connection.execute("SAVEPOINT normalize_listing")
            try:
                result = self._ingest_listing(
                    connection,
                    effective_source,
                    payload,
                    normalized=normalized,
                    fetched_at=seen_at,
                    raw_payload_id=raw_payload_id,
                )
            except Exception as exc:
                connection.execute("ROLLBACK TO normalize_listing")
                connection.execute("RELEASE normalize_listing")
                external_hint = _text(
                    _first(payload, "external_id", "activityId", "locationId", "productId", "id")
                )
                url_hint = _text(_first(payload, "url", "webUrl", "web_url", "link"))
                connection.execute(
                    """
                    INSERT INTO scrape_run_items(
                        run_id, item_index, external_id, url, query_label, destination,
                        result_rank, metadata_json, status, raw_payload_id, error,
                        observed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?)
                    ON CONFLICT(run_id, item_index) DO UPDATE SET
                        external_id = excluded.external_id,
                        url = excluded.url,
                        query_label = excluded.query_label,
                        destination = excluded.destination,
                        result_rank = excluded.result_rank,
                        metadata_json = excluded.metadata_json,
                        status = 'failed',
                        raw_payload_id = excluded.raw_payload_id,
                        listing_id = NULL,
                        error = excluded.error,
                        observed_at = excluded.observed_at
                    """,
                    (
                        run_id,
                        item_index,
                        external_hint,
                        url_hint,
                        query_label,
                        destination,
                        result_rank,
                        _json_or_none(item_metadata),
                        raw_payload_id,
                        f"{type(exc).__name__}: {exc}",
                        seen_at,
                        utc_now(),
                    ),
                )
                pending_error = exc
            else:
                connection.execute("RELEASE normalize_listing")
                connection.execute(
                    """
                    INSERT INTO scrape_run_items(
                        run_id, item_index, external_id, url, query_label, destination,
                        result_rank, metadata_json, status, raw_payload_id, listing_id,
                        observed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stored', ?, ?, ?, ?)
                    ON CONFLICT(run_id, item_index) DO UPDATE SET
                        external_id = excluded.external_id,
                        url = excluded.url,
                        query_label = excluded.query_label,
                        destination = excluded.destination,
                        result_rank = excluded.result_rank,
                        metadata_json = excluded.metadata_json,
                        status = 'stored',
                        raw_payload_id = excluded.raw_payload_id,
                        listing_id = excluded.listing_id,
                        error = NULL,
                        observed_at = excluded.observed_at
                    """,
                    (
                        run_id,
                        item_index,
                        result["external_id"],
                        result["url"],
                        query_label,
                        destination,
                        result_rank,
                        _json_or_none(item_metadata),
                        result["raw_payload_id"],
                        result["listing_id"],
                        seen_at,
                        utc_now(),
                    ),
                )
                response = {
                    "listing_id": int(result["listing_id"]),
                    "raw_payload_id": int(result["raw_payload_id"]),
                    "snapshot_id": int(result["snapshot_id"]),
                    "item_index": int(item_index),
                }
        if pending_error is not None:
            raise pending_error
        assert response is not None
        return response

    def record_unparsed_item(
        self,
        run_id: int,
        payload: Any,
        *,
        source: str | None = None,
        status: str = "skipped",
        error: str | None = None,
        item_index: int | None = None,
        query_label: str | None = None,
        destination: str | None = None,
        result_rank: int | None = None,
        item_metadata: Any = None,
        fetched_at: str | None = None,
    ) -> dict[str, int]:
        """Persist an exact blocked/malformed actor item without making a listing."""
        if status not in {"skipped", "failed"}:
            raise ValueError("unparsed item status must be skipped or failed")
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT source FROM scrape_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run:
                raise KeyError(f"unknown scrape run {run_id}")
            effective_source = _source(source or run["source"])
            if item_index is None:
                row = connection.execute(
                    "SELECT COALESCE(MAX(item_index), -1) + 1 AS next_index "
                    "FROM scrape_run_items WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                item_index = int(row["next_index"])
            seen_at = fetched_at or utc_now()
            raw_payload_id = self._store_raw(
                connection, effective_source, payload, seen_at
            )
            external_hint = _text(_first(payload, "external_id", "activityId", "locationId", "id")) if isinstance(payload, Mapping) else None
            url_hint = _text(_first(payload, "url", "webUrl", "link")) if isinstance(payload, Mapping) else None
            connection.execute(
                """
                INSERT INTO scrape_run_items(
                    run_id, item_index, external_id, url, query_label, destination,
                    result_rank, metadata_json, status, raw_payload_id, error,
                    observed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, item_index) DO UPDATE SET
                    external_id = excluded.external_id,
                    url = excluded.url,
                    query_label = excluded.query_label,
                    destination = excluded.destination,
                    result_rank = excluded.result_rank,
                    metadata_json = excluded.metadata_json,
                    status = excluded.status,
                    raw_payload_id = excluded.raw_payload_id,
                    listing_id = NULL,
                    error = excluded.error,
                    observed_at = excluded.observed_at
                """,
                (
                    run_id,
                    item_index,
                    external_hint,
                    url_hint,
                    query_label,
                    destination,
                    result_rank,
                    _json_or_none(item_metadata),
                    status,
                    raw_payload_id,
                    error,
                    seen_at,
                    utc_now(),
                ),
            )
            return {"raw_payload_id": raw_payload_id, "item_index": int(item_index)}

    def record_failed_item(
        self,
        run_id: int,
        *,
        error: str,
        item_index: int | None = None,
        query_label: str | None = None,
        destination: str | None = None,
        result_rank: int | None = None,
        item_metadata: Any = None,
        observed_at: str | None = None,
    ) -> int:
        with self.transaction() as connection:
            if not connection.execute("SELECT 1 FROM scrape_runs WHERE id = ?", (run_id,)).fetchone():
                raise KeyError(f"unknown scrape run {run_id}")
            if item_index is None:
                row = connection.execute(
                    "SELECT COALESCE(MAX(item_index), -1) + 1 AS next_index "
                    "FROM scrape_run_items WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                item_index = int(row["next_index"])
            connection.execute(
                """
                INSERT INTO scrape_run_items(
                    run_id, item_index, query_label, destination, result_rank,
                    metadata_json, status, error, observed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?)
                ON CONFLICT(run_id, item_index) DO UPDATE SET
                    query_label = excluded.query_label,
                    destination = excluded.destination,
                    result_rank = excluded.result_rank,
                    metadata_json = excluded.metadata_json,
                    status = 'failed', error = excluded.error,
                    observed_at = excluded.observed_at
                """,
                (
                    run_id,
                    item_index,
                    query_label,
                    destination,
                    result_rank,
                    _json_or_none(item_metadata),
                    error,
                    observed_at or utc_now(),
                    utc_now(),
                ),
            )
            return item_index

    def ingest_listing(
        self,
        source: str,
        payload: Mapping[str, Any],
        *,
        normalized: Mapping[str, Any] | None = None,
        fetched_at: str | None = None,
    ) -> dict[str, int]:
        with self.transaction() as connection:
            result = self._ingest_listing(
                connection,
                _source(source),
                payload,
                normalized=normalized,
                fetched_at=fetched_at,
            )
            return {
                "listing_id": int(result["listing_id"]),
                "raw_payload_id": int(result["raw_payload_id"]),
                "snapshot_id": int(result["snapshot_id"]),
            }

    def ingest_review(
        self,
        source: str,
        listing_external_id: str,
        payload: Mapping[str, Any],
        *,
        fetched_at: str | None = None,
    ) -> int:
        source = _source(source)
        seen_at = fetched_at or utc_now()
        with self.transaction() as connection:
            listing = connection.execute(
                "SELECT id FROM listings WHERE source = ? AND external_id = ?",
                (source, str(listing_external_id)),
            ).fetchone()
            if not listing:
                raise KeyError(f"unknown listing {source}/{listing_external_id}")
            raw_payload_id = self._store_raw(connection, source, payload, seen_at)
            records = _review_records({"reviews": [payload]})
            if not records:
                raise ValueError("review payload could not be normalized")
            return self._upsert_review(
                connection,
                int(listing["id"]),
                raw_payload_id,
                source,
                records[0],
                seen_at,
            )

    def stats(self) -> dict[str, int]:
        tables = {
            "raw_payloads": "raw_payloads",
            "runs": "scrape_runs",
            "run_items": "scrape_run_items",
            "places": "places",
            "listings": "listings",
            "snapshots": "listing_snapshots",
            "categories": "categories",
            "media": "media",
            "packages": "packages",
            "reviews": "reviews",
        }
        return {
            label: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for label, table in tables.items()
        }

    def _ingest_listing(
        self,
        connection: sqlite3.Connection,
        source: str,
        payload: Mapping[str, Any],
        *,
        normalized: Mapping[str, Any] | None,
        fetched_at: str | None,
        raw_payload_id: int | None = None,
    ) -> dict[str, Any]:
        seen_at = fetched_at or utc_now()
        if normalized is not None:
            # The provider adapter is the authoritative contract. Generic
            # extraction is only a tolerant source of extra aliases; a valid
            # provider record must not fail because the generic parser does not
            # recognize an actor-specific title such as ``activityTitle``.
            try:
                extracted = normalize_payload(source, payload)
            except (TypeError, ValueError):
                extracted = {}
            record = _merge_normalized_record(extracted, normalized)
        else:
            record = normalize_payload(source, payload)
        record["source"] = _source(str(record.get("source") or source))
        record["external_id"] = str(record.get("external_id") or "").strip()
        record["title"] = _text(record.get("title"))
        record["kind"] = (_text(record.get("kind")) or "unknown").casefold()
        if not record["external_id"] or not record["title"]:
            raise ValueError("normalized listing requires external_id and title")
        record["location_scope"] = _infer_scope(
            record.get("location_scope"), None, None, None, None, None
        )
        record["starts_in_budapest"] = _boolean(record.get("starts_in_budapest"))

        if raw_payload_id is None:
            raw_payload_id = self._store_raw(connection, source, payload, seen_at)
        place_record = record.get("place") if isinstance(record.get("place"), Mapping) else {}
        has_structured_geography = bool(
            any(
                _text(place_record.get(key))
                for key in ("country_code", "region", "locality", "address")
            )
            or _float(place_record.get("latitude")) is not None
            or _float(place_record.get("longitude")) is not None
        )
        existing_listing = connection.execute(
            "SELECT place_id, location_scope, location_text, description FROM listings "
            "WHERE source = ? AND external_id = ?",
            (record["source"], record["external_id"]),
        ).fetchone()
        incoming_scope = str(record.get("location_scope") or "unknown")
        preserve_rich_geography = bool(
            existing_listing
            and _text(existing_listing["description"])
            and not _text(record.get("description"))
        )
        if (
            existing_listing
            and (
                preserve_rich_geography
                or (
                    not has_structured_geography
                    and (
                        incoming_scope == "unknown"
                        or incoming_scope == str(existing_listing["location_scope"])
                    )
                )
            )
        ):
            # Detail actors often add reviews/media but omit geography. Keep
            # the structured discovery place unless the detail text provides
            # a concrete conflicting scope that warrants a new neutral place.
            place_id = int(existing_listing["place_id"])
        else:
            place_id = self._upsert_place(connection, record, seen_at)
        projection_record = record
        if preserve_rich_geography:
            # A later country-level collection card may contain a structured
            # search locality while omitting the product detail that previously
            # established where the activity actually happens.  Keep that rich
            # current projection, but retain this row's own scope in its
            # immutable snapshot below.
            projection_record = dict(record)
            projection_record["location_scope"] = str(
                existing_listing["location_scope"]
            )
            projection_record["location_text"] = existing_listing["location_text"]
        listing_id = self._upsert_listing(
            connection, projection_record, place_id, raw_payload_id, seen_at
        )
        snapshot_id = self._insert_snapshot(
            connection, listing_id, raw_payload_id, record, seen_at
        )
        self._sync_categories(connection, listing_id, record.get("categories") or [], seen_at)
        self._sync_media(connection, listing_id, record.get("media") or [], seen_at)
        self._sync_packages(connection, listing_id, record.get("packages") or [], seen_at)
        for review in record.get("reviews") or []:
            if isinstance(review, Mapping):
                self._upsert_review(
                    connection,
                    listing_id,
                    raw_payload_id,
                    record["source"],
                    review,
                    seen_at,
                )
        return {
            "listing_id": listing_id,
            "raw_payload_id": raw_payload_id,
            "snapshot_id": snapshot_id,
            "external_id": record["external_id"],
            "url": _text(record.get("url")),
        }

    @staticmethod
    def _store_raw(
        connection: sqlite3.Connection,
        source: str,
        payload: Any,
        fetched_at: str,
    ) -> int:
        serialized = canonical_json(payload)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO raw_payloads(source, sha256, canonical_json, fetched_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, sha256) DO NOTHING
            """,
            (source, digest, serialized, fetched_at, utc_now()),
        )
        row = connection.execute(
            "SELECT id FROM raw_payloads WHERE source = ? AND sha256 = ?",
            (source, digest),
        ).fetchone()
        return int(row["id"])

    @staticmethod
    def _upsert_place(
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        seen_at: str,
    ) -> int:
        place = record.get("place") if isinstance(record.get("place"), Mapping) else {}
        canonical_name = _text(place.get("canonical_name")) or str(record["title"])
        normalized_name = _slug(canonical_name)
        latitude = _float(place.get("latitude"))
        longitude = _float(place.get("longitude"))
        scope = _infer_scope(
            place.get("location_scope") or record.get("location_scope"),
            _country_code(place.get("country_code")),
            _text(place.get("locality")),
            _text(place.get("address")),
            latitude,
            longitude,
        )
        starts = _boolean(place.get("starts_in_budapest"), _boolean(record.get("starts_in_budapest")))
        explicit_key = _text(place.get("place_key"))
        if explicit_key:
            place_key = explicit_key
        else:
            identity = canonical_json(
                {
                    "country": _country_code(place.get("country_code")),
                    "locality": (_text(place.get("locality")) or "").casefold(),
                    "name": normalized_name,
                    "latitude": round(latitude, 5) if latitude is not None else None,
                    "longitude": round(longitude, 5) if longitude is not None else None,
                    # A route/destination scope is part of this warehouse's
                    # place semantics.  Keeping it in the identity prevents a
                    # Budapest pickup and an outside-Hungary destination with
                    # otherwise thin identical place fields from overwriting
                    # each other's map classification.
                    "scope": scope,
                }
            )
            place_key = "derived:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO places(
                place_key, canonical_name, normalized_name, country_code, region,
                locality, address, latitude, longitude, location_scope,
                starts_in_budapest, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(place_key) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                normalized_name = excluded.normalized_name,
                country_code = COALESCE(excluded.country_code, places.country_code),
                region = COALESCE(excluded.region, places.region),
                locality = COALESCE(excluded.locality, places.locality),
                address = COALESCE(excluded.address, places.address),
                latitude = COALESCE(excluded.latitude, places.latitude),
                longitude = COALESCE(excluded.longitude, places.longitude),
                location_scope = CASE WHEN excluded.location_scope = 'unknown'
                    THEN places.location_scope ELSE excluded.location_scope END,
                starts_in_budapest = MAX(
                    places.starts_in_budapest,
                    excluded.starts_in_budapest
                ),
                updated_at = excluded.updated_at
            WHERE COALESCE(julianday(excluded.updated_at), 0) >=
                  COALESCE(julianday(places.updated_at), 0)
            """,
            (
                place_key,
                canonical_name,
                normalized_name,
                _country_code(place.get("country_code")),
                _text(place.get("region")),
                _text(place.get("locality")),
                _text(place.get("address")),
                latitude,
                longitude,
                scope,
                int(starts),
                seen_at,
                seen_at,
            ),
        )
        return int(
            connection.execute("SELECT id FROM places WHERE place_key = ?", (place_key,)).fetchone()["id"]
        )

    @staticmethod
    def _upsert_listing(
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        place_id: int,
        raw_payload_id: int,
        seen_at: str,
    ) -> int:
        # v4 rows migrate as ``unknown``.  Historical replay must be able to
        # enrich this stable dimension even when a previously bad replay left
        # last_seen_at newer than the preserved observation timestamp.
        incoming_kind = str(record["kind"])
        if incoming_kind != "unknown":
            connection.execute(
                """
                UPDATE listings SET kind = ?
                WHERE source = ? AND external_id = ? AND kind = 'unknown'
                """,
                (incoming_kind, record["source"], record["external_id"]),
            )
        connection.execute(
            """
            INSERT INTO listings(
                place_id, source, external_id, url, title, kind, description, location_text,
                rating, review_count, price_from, currency, duration_text,
                location_scope, starts_in_budapest, latest_raw_payload_id,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                place_id = excluded.place_id,
                url = COALESCE(excluded.url, listings.url),
                title = excluded.title,
                kind = CASE WHEN excluded.kind = 'unknown'
                    THEN listings.kind ELSE excluded.kind END,
                description = COALESCE(excluded.description, listings.description),
                location_text = COALESCE(excluded.location_text, listings.location_text),
                rating = COALESCE(excluded.rating, listings.rating),
                review_count = COALESCE(excluded.review_count, listings.review_count),
                price_from = COALESCE(excluded.price_from, listings.price_from),
                currency = COALESCE(excluded.currency, listings.currency),
                duration_text = COALESCE(excluded.duration_text, listings.duration_text),
                location_scope = CASE WHEN excluded.location_scope = 'unknown'
                    THEN listings.location_scope ELSE excluded.location_scope END,
                starts_in_budapest = MAX(
                    listings.starts_in_budapest,
                    excluded.starts_in_budapest
                ),
                latest_raw_payload_id = excluded.latest_raw_payload_id,
                last_seen_at = excluded.last_seen_at,
                active = 1
            WHERE COALESCE(julianday(excluded.last_seen_at), 0) >=
                  COALESCE(julianday(listings.last_seen_at), 0)
            """,
            (
                place_id,
                record["source"],
                record["external_id"],
                _text(record.get("url")),
                record["title"],
                record["kind"],
                _text(record.get("description")),
                _text(record.get("location_text")),
                _float(record.get("rating")),
                _integer(record.get("review_count")),
                _float(record.get("price_from")),
                _text(record.get("currency")),
                _text(record.get("duration_text")),
                record["location_scope"],
                int(_boolean(record.get("starts_in_budapest"))),
                raw_payload_id,
                seen_at,
                seen_at,
            ),
        )
        row = connection.execute(
            "SELECT id FROM listings WHERE source = ? AND external_id = ?",
            (record["source"], record["external_id"]),
        ).fetchone()
        return int(row["id"])

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        listing_id: int,
        raw_payload_id: int,
        record: Mapping[str, Any],
        seen_at: str,
    ) -> int:
        connection.execute(
            """
            INSERT INTO listing_snapshots(
                listing_id, raw_payload_id, scraped_at, title, kind, description, url,
                rating, review_count, price_from, currency, location_scope,
                starts_in_budapest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id, raw_payload_id) DO UPDATE SET
                scraped_at = CASE
                    WHEN COALESCE(julianday(excluded.scraped_at), 0) >=
                         COALESCE(julianday(listing_snapshots.scraped_at), 0)
                    THEN excluded.scraped_at
                    ELSE listing_snapshots.scraped_at
                END,
                title = excluded.title,
                kind = excluded.kind,
                description = COALESCE(excluded.description, listing_snapshots.description),
                url = COALESCE(excluded.url, listing_snapshots.url),
                rating = COALESCE(excluded.rating, listing_snapshots.rating),
                review_count = COALESCE(excluded.review_count, listing_snapshots.review_count),
                price_from = COALESCE(excluded.price_from, listing_snapshots.price_from),
                currency = COALESCE(excluded.currency, listing_snapshots.currency),
                location_scope = CASE WHEN excluded.location_scope = 'unknown'
                    THEN listing_snapshots.location_scope ELSE excluded.location_scope END,
                starts_in_budapest = MAX(
                    listing_snapshots.starts_in_budapest,
                    excluded.starts_in_budapest
                )
            """,
            (
                listing_id,
                raw_payload_id,
                seen_at,
                record["title"],
                record["kind"],
                _text(record.get("description")),
                _text(record.get("url")),
                _float(record.get("rating")),
                _integer(record.get("review_count")),
                _float(record.get("price_from")),
                _text(record.get("currency")),
                record["location_scope"],
                int(_boolean(record.get("starts_in_budapest"))),
            ),
        )
        row = connection.execute(
            "SELECT id FROM listing_snapshots WHERE listing_id = ? AND raw_payload_id = ?",
            (listing_id, raw_payload_id),
        ).fetchone()
        return int(row["id"])

    @staticmethod
    def _sync_categories(
        connection: sqlite3.Connection,
        listing_id: int,
        categories: Iterable[Any],
        seen_at: str,
    ) -> None:
        # Research payloads vary in richness. Discovery rows often omit
        # categories that a later detail page supplied, so merge evidence
        # monotonically instead of treating absence as authoritative deletion.
        for category in categories:
            if isinstance(category, Mapping):
                name = _text(category.get("name") or category.get("title") or category.get("slug"))
                slug = _text(category.get("slug")) or (_slug(name) if name else None)
            else:
                name = _text(category)
                slug = _slug(name) if name else None
            if not name or not slug:
                continue
            connection.execute(
                """
                INSERT INTO categories(slug, name, created_at) VALUES (?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET name = excluded.name
                """,
                (slug, name, seen_at),
            )
            category_id = connection.execute(
                "SELECT id FROM categories WHERE slug = ?", (slug,)
            ).fetchone()["id"]
            connection.execute(
                "INSERT OR IGNORE INTO listing_categories(listing_id, category_id) VALUES (?, ?)",
                (listing_id, category_id),
            )

    @staticmethod
    def _sync_media(
        connection: sqlite3.Connection,
        listing_id: int,
        media_items: Iterable[Any],
        seen_at: str,
    ) -> None:
        # A shallow rediscovery may contain only one thumbnail. Keep previously
        # observed detail media active and upsert the evidence present here.
        for index, item in enumerate(media_items):
            if not isinstance(item, Mapping):
                item = {"url": item}
            url = _text(item.get("url"))
            if not url:
                continue
            external_id = _text(item.get("external_id") or item.get("id"))
            dedupe_key = external_id or hashlib.sha256(url.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO media(
                    listing_id, dedupe_key, external_id, media_type, url, caption,
                    width, height, sort_order, active, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(listing_id, dedupe_key) DO UPDATE SET
                    external_id = COALESCE(excluded.external_id, media.external_id),
                    media_type = excluded.media_type,
                    url = excluded.url,
                    caption = COALESCE(excluded.caption, media.caption),
                    width = COALESCE(excluded.width, media.width),
                    height = COALESCE(excluded.height, media.height),
                    sort_order = excluded.sort_order,
                    active = 1,
                    last_seen_at = excluded.last_seen_at
                WHERE COALESCE(julianday(excluded.last_seen_at), 0) >=
                      COALESCE(julianday(media.last_seen_at), 0)
                """,
                (
                    listing_id,
                    dedupe_key,
                    external_id,
                    _text(item.get("media_type") or item.get("type")) or "image",
                    url,
                    _text(item.get("caption")),
                    _integer(item.get("width")),
                    _integer(item.get("height")),
                    _integer(item.get("sort_order")) if item.get("sort_order") is not None else index,
                    seen_at,
                    seen_at,
                ),
            )

    @staticmethod
    def _sync_packages(
        connection: sqlite3.Connection,
        listing_id: int,
        package_items: Iterable[Any],
        seen_at: str,
    ) -> None:
        # Package absence on a shallow card means "not supplied", not "removed".
        for index, item in enumerate(package_items):
            if not isinstance(item, Mapping):
                item = {"name": item}
            name = _text(item.get("name") or item.get("title"))
            if not name:
                continue
            external_id = _text(item.get("external_id") or item.get("id"))
            natural_key = canonical_json(
                {
                    "name": name.casefold(),
                    "provider": (_text(item.get("provider")) or "").casefold(),
                    "category": (_text(item.get("category")) or "").casefold(),
                    "url": _text(item.get("url")),
                    "duration": _text(item.get("duration_text") or item.get("duration")),
                }
            )
            dedupe_key = external_id or hashlib.sha256(natural_key.encode("utf-8")).hexdigest()
            if not external_id:
                # v1-v4 initially included price in this fallback key, so a
                # refreshed price created another active option.  Re-key the
                # best matching legacy row to the stable option identity and
                # retire any older price variants before the upsert below.
                matches = connection.execute(
                    """
                    SELECT id, dedupe_key
                    FROM packages
                    WHERE listing_id = ? AND external_id IS NULL
                      AND name = ? COLLATE NOCASE
                      AND COALESCE(provider, '') = COALESCE(?, '')
                      AND COALESCE(category, '') = COALESCE(?, '')
                      AND COALESCE(url, '') = COALESCE(?, '')
                      AND COALESCE(duration_text, '') = COALESCE(?, '')
                    ORDER BY active DESC, last_seen_at DESC, id DESC
                    """,
                    (
                        listing_id,
                        name,
                        _text(item.get("provider")),
                        _text(item.get("category")),
                        _text(item.get("url")),
                        _text(item.get("duration_text") or item.get("duration")),
                    ),
                ).fetchall()
                current = next(
                    (row for row in matches if row["dedupe_key"] == dedupe_key),
                    None,
                )
                keeper = current or (matches[0] if matches else None)
                if keeper is not None and keeper["dedupe_key"] != dedupe_key:
                    connection.execute(
                        "UPDATE packages SET dedupe_key = ? WHERE id = ?",
                        (dedupe_key, keeper["id"]),
                    )
                if keeper is not None:
                    connection.executemany(
                        "UPDATE packages SET active = 0 WHERE id = ?",
                        [
                            (row["id"],)
                            for row in matches
                            if row["id"] != keeper["id"]
                        ],
                    )
            connection.execute(
                """
                INSERT INTO packages(
                    listing_id, dedupe_key, external_id, name, description, price,
                    original_price, currency, duration_text, availability_text,
                    url, provider, category, sort_order, active,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(listing_id, dedupe_key) DO UPDATE SET
                    external_id = COALESCE(excluded.external_id, packages.external_id),
                    name = excluded.name,
                    description = COALESCE(excluded.description, packages.description),
                    price = COALESCE(excluded.price, packages.price),
                    original_price = COALESCE(excluded.original_price, packages.original_price),
                    currency = COALESCE(excluded.currency, packages.currency),
                    duration_text = COALESCE(excluded.duration_text, packages.duration_text),
                    availability_text = COALESCE(excluded.availability_text, packages.availability_text),
                    url = COALESCE(excluded.url, packages.url),
                    provider = COALESCE(excluded.provider, packages.provider),
                    category = COALESCE(excluded.category, packages.category),
                    sort_order = excluded.sort_order,
                    active = 1,
                    last_seen_at = excluded.last_seen_at
                WHERE COALESCE(julianday(excluded.last_seen_at), 0) >=
                      COALESCE(julianday(packages.last_seen_at), 0)
                """,
                (
                    listing_id,
                    dedupe_key,
                    external_id,
                    name,
                    _text(item.get("description")),
                    _float(item.get("price")),
                    _float(item.get("original_price") or item.get("originalPrice")),
                    _text(item.get("currency")),
                    _text(item.get("duration_text") or item.get("duration")),
                    _text(item.get("availability_text") or item.get("availability")),
                    _text(item.get("url")),
                    _text(item.get("provider")),
                    _text(item.get("category")),
                    _integer(item.get("sort_order")) if item.get("sort_order") is not None else index,
                    seen_at,
                    seen_at,
                ),
            )

    @staticmethod
    def _upsert_review(
        connection: sqlite3.Connection,
        listing_id: int,
        raw_payload_id: int,
        source: str,
        review: Mapping[str, Any],
        seen_at: str,
    ) -> int:
        title = _text(review.get("title"))
        body = _text(review.get("body") or review.get("text") or review.get("content"))
        external_id = _text(review.get("external_id") or review.get("id"))
        if not external_id:
            identity = canonical_json(
                {
                    "listing_id": listing_id,
                    "rating": _float(review.get("rating")),
                    "title": title,
                    "body": body,
                    "review_date": _text(review.get("review_date") or review.get("date")),
                }
            )
            external_id = "derived:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO reviews(
                listing_id, raw_payload_id, source, external_id, rating, title,
                body, language, review_date, helpful_count, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                listing_id = excluded.listing_id,
                raw_payload_id = excluded.raw_payload_id,
                rating = COALESCE(excluded.rating, reviews.rating),
                title = COALESCE(excluded.title, reviews.title),
                body = COALESCE(excluded.body, reviews.body),
                language = COALESCE(excluded.language, reviews.language),
                review_date = COALESCE(excluded.review_date, reviews.review_date),
                helpful_count = COALESCE(excluded.helpful_count, reviews.helpful_count),
                last_seen_at = excluded.last_seen_at
            WHERE COALESCE(julianday(excluded.last_seen_at), 0) >=
                  COALESCE(julianday(reviews.last_seen_at), 0)
            """,
            (
                listing_id,
                raw_payload_id,
                source,
                external_id,
                _float(review.get("rating")),
                title,
                body,
                _text(review.get("language")),
                _text(review.get("review_date") or review.get("date")),
                _integer(review.get("helpful_count")),
                seen_at,
                seen_at,
            ),
        )
        row = connection.execute(
            "SELECT id FROM reviews WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        return int(row["id"])


if __name__ == "__main__":
    with ResearchStore() as store:
        print(json.dumps(store.stats(), indent=2, sort_keys=True))
