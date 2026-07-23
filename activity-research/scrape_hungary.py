#!/usr/bin/env python3
"""Run, resume, and import the bounded Hungary activity-research pipeline.

The pipeline intentionally combines broad country discovery with a small set of
destination/theme queries.  Every Apify dataset item is stored losslessly in
SQLite before normalization, filtering, or ranking.  Actor runs are started
only after one preflight check reserves the hard-cap budget for the complete
configured pipeline.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Mapping, Sequence

from apify_client import (
    ApifyClient,
    ApifyConfigurationError,
    ApifyCostLimitError,
    ApifyError,
    ApifyHttpError,
    UsagePlan,
)
from normalizers import normalize_item
from store import DEFAULT_DB_PATH, ResearchStore, canonical_json
from terminal_safety import terminal_line


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "sources.json"
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})
GETYOURGUIDE_DETAIL_ENRICHMENT_VERSION = "piotrv1001-v1"


class PipelineConfigurationError(ValueError):
    """The local source plan is incomplete or internally unsafe."""


class PipelineBudgetError(RuntimeError):
    """The complete configured run does not fit a local or live hard cap."""


@dataclass(frozen=True)
class PlannedRun:
    actor_key: str
    actor_id: str
    source: str
    phase_label: str
    run_input: dict[str, Any]
    max_items: int
    max_total_charge_usd: Decimal
    query: str | None = None
    requires_tail_signal_from: str | None = None
    min_prior_new_listings: int = 1

    @property
    def display_label(self) -> str:
        return f"{self.phase_label}: {self.query}" if self.query else self.phase_label


@dataclass(frozen=True)
class IngestSummary:
    total: int
    stored: int
    failed: int
    sentinels: int
    unique_yield: int
    new_listing_yield: int
    tail_count: int
    strong_tail_count: int
    tail_quality_signal: bool


Print = Callable[[str], None]


NEW_LISTING_YIELD_SQL = """
    SELECT COUNT(DISTINCT item.listing_id)
    FROM scrape_run_items AS item
    WHERE item.run_id = ?
      AND item.status = 'stored'
      AND item.listing_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM scrape_run_items AS earlier
          WHERE earlier.listing_id = item.listing_id
            AND earlier.run_id < item.run_id
            AND earlier.status = 'stored'
      )
"""


def new_listing_yield(store: ResearchStore, run_id: int) -> int:
    """Count listings first observed globally in this persisted run."""

    return int(store.connection.execute(NEW_LISTING_YIELD_SQL, (run_id,)).fetchone()[0])


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineConfigurationError(f"cannot read source plan: {config_path}") from exc
    if not isinstance(value, dict):
        raise PipelineConfigurationError("source plan must be a JSON object")
    if not isinstance(value.get("actors"), dict) or not value["actors"]:
        raise PipelineConfigurationError("source plan has no actors")
    _configured_pipeline_charge(value)
    return value


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge JSON objects without mutating either input."""

    result = deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _decimal(value: Any, label: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PipelineConfigurationError(f"{label} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PipelineConfigurationError(f"{label} must be a finite number") from exc
    lower_ok = result >= 0 if allow_zero else result > 0
    if not result.is_finite() or not lower_ok:
        adjective = "non-negative" if allow_zero else "positive"
        raise PipelineConfigurationError(f"{label} must be {adjective}")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PipelineConfigurationError(f"{label} must be a positive integer")
    return value


def _source_for_actor(actor_key: str, actor: Mapping[str, Any]) -> str:
    explicit = str(actor.get("source") or "").strip().lower()
    if explicit:
        return explicit
    identity = f"{actor_key} {actor.get('actorId', '')}".lower()
    if "tripadvisor" in identity:
        return "tripadvisor"
    if "getyourguide" in identity:
        return "getyourguide"
    raise PipelineConfigurationError(f"cannot infer provider for actor {actor_key!r}")


def _actor_api_id(actor_key: str, actor: Mapping[str, Any]) -> str:
    value = str(actor.get("actorApiId") or actor.get("actorId") or "").strip()
    if not value:
        raise PipelineConfigurationError(f"actor {actor_key!r} has no actor id")
    return value


def _phase_plan(
    actor_key: str,
    actor: Mapping[str, Any],
    phase: Mapping[str, Any],
) -> list[PlannedRun]:
    actor_id = _actor_api_id(actor_key, actor)
    source = _source_for_actor(actor_key, actor)
    base_input = actor.get("baseInput") or {}
    phase_input = phase.get("input") or {}
    if not isinstance(base_input, Mapping) or not isinstance(phase_input, Mapping):
        raise PipelineConfigurationError(f"{actor_key} inputs must be objects")
    label = str(phase.get("label") or "unnamed-phase").strip()
    requires_tail_signal_from = str(
        phase.get("requiresTailSignalFrom") or ""
    ).strip() or None
    min_prior_new_listings = _positive_int(
        phase.get("minPriorNewListings", 1),
        f"{actor_key}/{label}.minPriorNewListings",
    )
    queries = phase.get("queries")
    if queries is not None:
        if not isinstance(queries, list) or not queries:
            raise PipelineConfigurationError(f"{actor_key}/{label} queries must be a list")
        per_items = _positive_int(
            phase.get("perQueryMaxItems"), f"{actor_key}/{label}.perQueryMaxItems"
        )
        per_charge = _decimal(
            phase.get("perQueryMaxTotalChargeUsd"),
            f"{actor_key}/{label}.perQueryMaxTotalChargeUsd",
        )
        plans: list[PlannedRun] = []
        for raw_query in queries:
            query = str(raw_query).strip()
            if not query:
                raise PipelineConfigurationError(f"{actor_key}/{label} has an empty query")
            # maxcopell/tripadvisor accepts one string `query`.  One actor run
            # per query makes each theme independently capped and resumable.
            run_input = deep_merge(base_input, phase_input)
            run_input["query"] = query
            run_input["maxItemsPerQuery"] = per_items
            plans.append(
                PlannedRun(
                    actor_key=actor_key,
                    actor_id=actor_id,
                    source=source,
                    phase_label=label,
                    run_input=run_input,
                    max_items=per_items,
                    max_total_charge_usd=per_charge,
                    query=query,
                    requires_tail_signal_from=requires_tail_signal_from,
                    min_prior_new_listings=min_prior_new_listings,
                )
            )
        return plans

    batch_size_raw = phase.get("batchSize")
    if batch_size_raw is not None:
        batch_size = _positive_int(batch_size_raw, f"{actor_key}/{label}.batchSize")
        batch_field = str(phase.get("batchField") or "cityUrls").strip()
        batch_values = phase_input.get(batch_field)
        if not batch_field or not isinstance(batch_values, list) or not batch_values:
            raise PipelineConfigurationError(
                f"{actor_key}/{label}.{batch_field or 'batchField'} must be a non-empty list"
            )
        per_items = _positive_int(
            phase.get("perBatchMaxItems"), f"{actor_key}/{label}.perBatchMaxItems"
        )
        per_charge = _decimal(
            phase.get("perBatchMaxTotalChargeUsd"),
            f"{actor_key}/{label}.perBatchMaxTotalChargeUsd",
        )
        per_destination = phase.get("perBatchMaxItemsPerDestination")
        if per_destination is not None:
            per_destination = _positive_int(
                per_destination,
                f"{actor_key}/{label}.perBatchMaxItemsPerDestination",
            )
        plans = []
        chunks = [
            batch_values[index : index + batch_size]
            for index in range(0, len(batch_values), batch_size)
        ]
        for index, chunk in enumerate(chunks, 1):
            run_input = deep_merge(base_input, phase_input)
            run_input.pop("batchSize", None)
            run_input[batch_field] = deepcopy(chunk)
            run_input["maxActivities"] = per_items
            if per_destination is not None:
                run_input["maxItemsPerCity"] = per_destination
            plans.append(
                PlannedRun(
                    actor_key=actor_key,
                    actor_id=actor_id,
                    source=source,
                    phase_label=f"{label}-batch-{index:02d}",
                    run_input=run_input,
                    max_items=per_items,
                    max_total_charge_usd=per_charge,
                    requires_tail_signal_from=requires_tail_signal_from,
                    min_prior_new_listings=min_prior_new_listings,
                )
            )
        return plans

    max_items = _positive_int(phase.get("maxItems"), f"{actor_key}/{label}.maxItems")
    max_charge = _decimal(
        phase.get("maxTotalChargeUsd"), f"{actor_key}/{label}.maxTotalChargeUsd"
    )
    return [
        PlannedRun(
            actor_key=actor_key,
            actor_id=actor_id,
            source=source,
            phase_label=label,
            run_input=deep_merge(base_input, phase_input),
            max_items=max_items,
            max_total_charge_usd=max_charge,
            requires_tail_signal_from=requires_tail_signal_from,
            min_prior_new_listings=min_prior_new_listings,
        )
    ]


def build_discovery_plans(config: Mapping[str, Any]) -> list[PlannedRun]:
    plans: list[PlannedRun] = []
    actors = config.get("actors")
    if not isinstance(actors, Mapping):
        raise PipelineConfigurationError("source plan has no actors object")
    for actor_key, actor_value in actors.items():
        if actor_key.endswith("-details"):
            continue
        if not isinstance(actor_value, Mapping):
            raise PipelineConfigurationError(f"actor {actor_key!r} must be an object")
        phases = actor_value.get("phases")
        if not isinstance(phases, list) or not phases:
            raise PipelineConfigurationError(f"actor {actor_key!r} has no phases")
        for phase in phases:
            if not isinstance(phase, Mapping):
                raise PipelineConfigurationError(f"actor {actor_key!r} has an invalid phase")
            plans.extend(_phase_plan(actor_key, actor_value, phase))
    return plans


def build_detail_plan(
    config: Mapping[str, Any], activity_urls: Sequence[str]
) -> PlannedRun | None:
    actors = config.get("actors")
    actor_key = "getyourguide-details"
    actor = actors.get(actor_key) if isinstance(actors, Mapping) else None
    if not isinstance(actor, Mapping):
        raise PipelineConfigurationError(f"source plan has no {actor_key!r} actor")
    urls = list(dict.fromkeys(url.strip() for url in activity_urls if url.strip()))
    limit = _positive_int(actor.get("topLimit") or actor.get("maxItems"), f"{actor_key}.topLimit")
    urls = urls[:limit]
    if not urls:
        return None
    raw_input = actor.get("input") or {}
    if not isinstance(raw_input, Mapping):
        raise PipelineConfigurationError(f"{actor_key}.input must be an object")
    run_input = deep_merge({}, raw_input)
    run_input["activityUrls"] = [{"url": url} for url in urls]
    run_input["maxActivities"] = len(urls)
    configured_max = _positive_int(actor.get("maxItems"), f"{actor_key}.maxItems")
    return PlannedRun(
        actor_key=actor_key,
        actor_id=_actor_api_id(actor_key, actor),
        source=_source_for_actor(actor_key, actor),
        phase_label="top-outside-budapest-details",
        run_input=run_input,
        max_items=min(configured_max, len(urls)),
        max_total_charge_usd=_decimal(
            actor.get("maxTotalChargeUsd"), f"{actor_key}.maxTotalChargeUsd"
        ),
    )


def _detail_charge(config: Mapping[str, Any]) -> Decimal:
    actors = config.get("actors")
    details = actors.get("getyourguide-details") if isinstance(actors, Mapping) else None
    if not isinstance(details, Mapping):
        raise PipelineConfigurationError("source plan has no getyourguide-details actor")
    return _decimal(
        details.get("maxTotalChargeUsd"),
        "getyourguide-details.maxTotalChargeUsd",
    )


def _selected_discovery_plans(
    config: Mapping[str, Any],
    *,
    only_provider: str | None = None,
    only_actor_key: str | None = None,
) -> tuple[list[PlannedRun], bool]:
    if only_provider and only_actor_key:
        raise PipelineConfigurationError(
            "choose either only_provider or only_actor_key, not both"
        )
    if only_provider not in {None, "tripadvisor", "getyourguide"}:
        raise PipelineConfigurationError(f"unknown provider filter {only_provider!r}")
    plans = build_discovery_plans(config)
    if only_provider:
        plans = [plan for plan in plans if plan.source == only_provider]
        include_details = only_provider == "getyourguide"
    elif only_actor_key:
        actors = config.get("actors")
        if not isinstance(actors, Mapping) or only_actor_key not in actors:
            raise PipelineConfigurationError(f"unknown actor key {only_actor_key!r}")
        plans = [plan for plan in plans if plan.actor_key == only_actor_key]
        include_details = only_actor_key == "getyourguide-details"
    else:
        include_details = True
    return plans, include_details


def _selection_charge(
    config: Mapping[str, Any],
    plans: Sequence[PlannedRun],
    *,
    include_details: bool,
) -> Decimal:
    total = sum((plan.max_total_charge_usd for plan in plans), Decimal("0"))
    if include_details:
        total += _detail_charge(config)
    budget = config.get("budget")
    if not isinstance(budget, Mapping):
        raise PipelineConfigurationError("source plan has no budget object")
    local_cap = _decimal(budget.get("maxPlannedChargeUsd"), "budget.maxPlannedChargeUsd")
    if total > local_cap:
        raise PipelineBudgetError(
            f"configured actor caps total ${total} but the local pipeline cap is ${local_cap}"
        )
    return total


def _configured_pipeline_charge(config: Mapping[str, Any]) -> Decimal:
    return _selection_charge(
        config,
        build_discovery_plans(config),
        include_details=True,
    )


def live_remaining_usd(usage: UsagePlan) -> tuple[Decimal, Decimal, Decimal]:
    try:
        account_cap = _decimal(
            usage.account_limits.get("maxMonthlyUsageUsd"),
            "limits.maxMonthlyUsageUsd",
            allow_zero=True,
        )
        used = _decimal(
            usage.current_usage.get("monthlyUsageUsd"),
            "current.monthlyUsageUsd",
            allow_zero=True,
        )
    except (AttributeError, PipelineConfigurationError) as exc:
        raise PipelineBudgetError(
            "live Apify usage did not contain reliable monthly USD limits; refusing to start"
        ) from exc
    return account_cap, used, max(Decimal("0"), account_cap - used)


def verify_live_budget(
    config: Mapping[str, Any],
    usage: UsagePlan,
    *,
    planned_charge: Decimal | None = None,
) -> dict[str, Decimal]:
    planned = _configured_pipeline_charge(config) if planned_charge is None else planned_charge
    if planned < 0 or not planned.is_finite():
        raise PipelineBudgetError("selected pipeline charge must be finite and non-negative")
    local_cap = _decimal(
        config.get("budget", {}).get("maxPlannedChargeUsd"),
        "budget.maxPlannedChargeUsd",
    )
    if planned > local_cap:
        raise PipelineBudgetError(
            f"selected actor caps total ${planned} but the local pipeline cap is ${local_cap}"
        )
    account_cap, used, remaining = live_remaining_usd(usage)
    if planned > remaining:
        raise PipelineBudgetError(
            f"pipeline reserves ${planned}, but only ${remaining} remains "
            f"(${used} used of ${account_cap})"
        )
    return {
        "planned": planned,
        "account_cap": account_cap,
        "used": used,
        "remaining": remaining,
    }


def select_getyourguide_detail_urls(
    db_path: str | Path,
    *,
    limit: int,
    enrichment_version: str = GETYOURGUIDE_DETAIL_ENRICHMENT_VERSION,
    max_not_returned_attempts: int = 2,
) -> list[str]:
    """Select the highest-quality listings missing this detail enrichment."""

    requested_limit = int(limit)
    if requested_limit <= 0:
        return []
    with closing(
        sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    ) as connection:
        rows = connection.execute(
            """
            SELECT l.url
            FROM listings AS l
            JOIN listing_quality_ranking AS q ON q.listing_id = l.id
            WHERE l.active = 1
              AND l.source = 'getyourguide'
              AND l.location_scope = 'outside-budapest'
              AND (l.url LIKE 'https://%' OR l.url LIKE 'http://%')
              AND NOT EXISTS (
                  SELECT 1
                  FROM listing_enrichments AS enrichment
                  WHERE enrichment.listing_id = l.id
                    AND enrichment.enrichment_kind = 'getyourguide-detail'
              )
              AND (
                  SELECT COUNT(*)
                  FROM listing_enrichment_attempts AS attempt
                  WHERE attempt.listing_id = l.id
                    AND attempt.enrichment_kind = 'getyourguide-detail'
                    AND attempt.enrichment_version = ?
                    AND attempt.status = 'not-returned'
              ) < ?
            ORDER BY
                q.bayesian_rating DESC,
                l.review_count DESC,
                l.rating DESC,
                l.title COLLATE NOCASE,
                l.id
            LIMIT ?
            """,
            (
                enrichment_version,
                max(0, int(max_not_returned_attempts)),
                requested_limit,
            ),
        ).fetchall()
    return [str(row[0]) for row in rows]


def record_getyourguide_detail_attempts(
    store: ResearchStore,
    *,
    run_id: int,
    plan: PlannedRun,
    terminal_status: str = "SUCCEEDED",
    attempted_at: str | None = None,
) -> dict[str, int]:
    """Record returned and omitted detail URLs per listing for bounded retries."""

    if plan.actor_key != "getyourguide-details":
        return {"succeeded": 0, "not-returned": 0, "failed": 0}
    values = plan.run_input.get("activityUrls")
    if not isinstance(values, list):
        return {"succeeded": 0, "not-returned": 0, "failed": 0}
    returned_ids = {
        int(row[0])
        for row in store.connection.execute(
            """
            SELECT DISTINCT listing_id
            FROM scrape_run_items
            WHERE run_id = ? AND status = 'stored' AND listing_id IS NOT NULL
            """,
            (run_id,),
        ).fetchall()
    }
    failed_payloads = [
        str(row[0])
        for row in store.connection.execute(
            """
            SELECT raw.canonical_json
            FROM scrape_run_items AS item
            JOIN raw_payloads AS raw ON raw.id = item.raw_payload_id
            WHERE item.run_id = ? AND item.status = 'failed'
            """,
            (run_id,),
        ).fetchall()
    ]
    counts = {"succeeded": 0, "not-returned": 0, "failed": 0}
    for value in values:
        url = str(value.get("url") if isinstance(value, Mapping) else value or "").strip()
        if not url:
            continue
        external_match = re.search(r"-t(\d+)(?:[/?#]|$)", url, flags=re.I)
        listing = None
        if external_match:
            listing = store.connection.execute(
                """
                SELECT id FROM listings
                WHERE source = 'getyourguide' AND external_id = ?
                """,
                (external_match.group(1),),
            ).fetchone()
        if listing is None:
            listing = store.connection.execute(
                "SELECT id FROM listings WHERE source = 'getyourguide' AND url = ?",
                (url,),
            ).fetchone()
        if listing is None:
            continue
        listing_id = int(listing["id"])
        failed_return = any(
            url in payload
            or (
                external_match is not None
                and re.search(
                    rf"(?:-t|activityId[^0-9]{{1,12}}){re.escape(external_match.group(1))}(?:[^0-9]|$)",
                    payload,
                    flags=re.I,
                )
            )
            for payload in failed_payloads
        )
        if terminal_status != "SUCCEEDED":
            attempt_status = "failed"
        elif listing_id in returned_ids:
            attempt_status = "succeeded"
        elif failed_return:
            attempt_status = "failed"
        else:
            attempt_status = "not-returned"
        store.record_enrichment_attempt(
            listing_id,
            kind="getyourguide-detail",
            version=GETYOURGUIDE_DETAIL_ENRICHMENT_VERSION,
            run_id=run_id,
            status=attempt_status,
            requested_url=url,
            attempted_at=attempted_at,
            error=None if terminal_status == "SUCCEEDED" else f"actor status {terminal_status}",
        )
        counts[attempt_status] += 1
    return counts


def _store_normalized(item: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the provider-neutral adapter to ResearchStore's stable model."""

    locality = item.get("locality")
    region = item.get("region")
    country = item.get("country")
    address = item.get("address")
    location_text = ", ".join(
        str(part).strip() for part in (locality, region, country) if str(part or "").strip()
    ) or (str(address).strip() if address else None)
    return {
        "source": item.get("source"),
        "external_id": item.get("external_id"),
        "url": item.get("url"),
        "title": item.get("title"),
        "kind": item.get("kind") or "unknown",
        "description": item.get("description"),
        "location_text": location_text,
        "rating": item.get("rating"),
        "review_count": item.get("review_count"),
        "price_from": item.get("price"),
        "currency": item.get("currency"),
        "duration_text": item.get("duration"),
        "location_scope": item.get("location_scope") or "unknown",
        "starts_in_budapest": bool(item.get("starts_in_budapest")),
        "place": {
            "canonical_name": locality or item.get("title"),
            "country_code": country,
            "region": region,
            "locality": locality,
            "address": address,
            "latitude": item.get("lat"),
            "longitude": item.get("lon"),
            "location_scope": item.get("location_scope") or "unknown",
            "starts_in_budapest": bool(item.get("starts_in_budapest")),
        },
        "categories": item.get("categories") or [],
        "media": item.get("media") or [],
        "packages": item.get("packages") or [],
        "reviews": item.get("reviews") or [],
    }


def _safe_error(exc: BaseException) -> str:
    detail = " ".join(terminal_line(exc).split())[:240]
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _destination(payload: Mapping[str, Any], normalized: Mapping[str, Any] | None) -> str | None:
    if normalized and normalized.get("locality"):
        return str(normalized["locality"])
    for key in ("sourceCityUrl", "source_city_url", "destination", "location"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def ingest_items(
    store: ResearchStore,
    *,
    run_id: int,
    source: str,
    actor_id: str,
    items: Sequence[Mapping[str, Any]],
    query_label: str,
    query: str | None,
    actor_run_id: str | None,
    dataset_id: str,
    ranking: Mapping[str, Any] | None = None,
    start_index: int = 0,
    enrichment_kind: str | None = None,
    enrichment_version: str | None = None,
    fetched_at: str | None = None,
) -> IngestSummary:
    """Persist and normalize a complete dataset idempotently."""

    stored = failed = sentinels = 0
    unique: set[tuple[str, str]] = set()
    normalized_by_position: list[Mapping[str, Any] | None] = []
    for local_index, payload in enumerate(items):
        item_index = start_index + local_index
        rank = item_index + 1
        provenance = {
            "actor_id": actor_id,
            "actor_run_id": actor_run_id,
            "dataset_id": dataset_id,
            "phase_label": query_label,
            "query": query,
        }
        # This is deliberately the first payload-dependent operation.  The
        # placeholder run-item is overwritten below after normalization, while
        # its raw_payload row remains lossless and private.
        store.record_unparsed_item(
            run_id,
            payload,
            source=source,
            status="skipped",
            error="pending normalization",
            item_index=item_index,
            query_label=query or query_label,
            destination=_destination(payload, None),
            result_rank=rank,
            item_metadata={**provenance, "classification": "pending-normalization"},
            fetched_at=fetched_at,
        )
        try:
            normalized = normalize_item(actor_id, payload, rank=rank)
            if normalized is None:
                sentinels += 1
                store.record_unparsed_item(
                    run_id,
                    payload,
                    source=source,
                    status="failed",
                    error="provider blocked/error sentinel",
                    item_index=item_index,
                    query_label=query or query_label,
                    destination=_destination(payload, None),
                    result_rank=rank,
                    item_metadata={**provenance, "classification": "provider-sentinel"},
                    fetched_at=fetched_at,
                )
                normalized_by_position.append(None)
                failed += 1
                continue
            stored_item = store.ingest_item(
                run_id,
                payload,
                source=source,
                normalized=_store_normalized(normalized),
                item_index=item_index,
                query_label=query or query_label,
                destination=_destination(payload, normalized),
                result_rank=int(normalized.get("rank") or rank),
                item_metadata=provenance,
                fetched_at=fetched_at,
            )
            if enrichment_kind and enrichment_version:
                store.mark_enrichment(
                    stored_item["listing_id"],
                    kind=enrichment_kind,
                    version=enrichment_version,
                    raw_payload_id=stored_item["raw_payload_id"],
                    enriched_at=fetched_at,
                )
            unique.add((str(normalized.get("source") or source), str(normalized["external_id"])))
            normalized_by_position.append(normalized)
            stored += 1
        except Exception as exc:  # one malformed provider row must not lose the dataset
            store.record_unparsed_item(
                run_id,
                payload,
                source=source,
                status="failed",
                error=_safe_error(exc),
                item_index=item_index,
                query_label=query or query_label,
                destination=_destination(payload, None),
                result_rank=rank,
                item_metadata={**provenance, "classification": "normalization-error"},
                fetched_at=fetched_at,
            )
            normalized_by_position.append(None)
            failed += 1
    policy = ranking or {}
    tail_window = int(policy.get("tailWindow", 10))
    prior_rating = Decimal(str(policy.get("priorRating", 4.0)))
    prior_reviews = Decimal(str(policy.get("priorReviews", 50)))
    min_reviews = int(policy.get("strongTailMinReviews", 50))
    min_bayesian = Decimal(str(policy.get("strongTailMinBayesianRating", 4.3)))
    min_count = int(policy.get("strongTailMinCount", 2))
    tail = normalized_by_position[-max(1, tail_window) :]
    strong_tail_count = 0
    for item in tail:
        if item is None or item.get("rating") is None:
            continue
        reviews = int(item.get("review_count") or 0)
        rating = Decimal(str(item["rating"]))
        bayesian = (
            Decimal(reviews) * rating + prior_reviews * prior_rating
        ) / (Decimal(reviews) + prior_reviews)
        if reviews >= min_reviews and bayesian >= min_bayesian:
            strong_tail_count += 1
    return IngestSummary(
        total=len(items),
        stored=stored,
        failed=failed,
        sentinels=sentinels,
        unique_yield=len(unique),
        new_listing_yield=new_listing_yield(store, run_id),
        tail_count=len(tail),
        strong_tail_count=strong_tail_count,
        tail_quality_signal=strong_tail_count >= min_count,
    )


def summarize_run(
    store: ResearchStore,
    run_id: int,
    *,
    ranking: Mapping[str, Any] | None = None,
) -> IngestSummary:
    """Compute bounded quality telemetry from persisted rows, not RAM payloads."""

    policy = ranking or {}
    tail_window = max(1, int(policy.get("tailWindow", 10)))
    prior_rating = Decimal(str(policy.get("priorRating", 4.0)))
    prior_reviews = Decimal(str(policy.get("priorReviews", 50)))
    min_reviews = int(policy.get("strongTailMinReviews", 50))
    min_bayesian = Decimal(str(policy.get("strongTailMinBayesianRating", 4.3)))
    min_count = int(policy.get("strongTailMinCount", 2))
    counts = store.connection.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(status = 'stored') AS stored,
            SUM(status = 'failed') AS failed,
            SUM(json_extract(metadata_json, '$.classification') = 'provider-sentinel')
                AS sentinels,
            COUNT(DISTINCT CASE WHEN status = 'stored' THEN listing_id END) AS unique_yield
        FROM scrape_run_items
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    tail = store.connection.execute(
        """
        SELECT snapshot.rating, snapshot.review_count
        FROM scrape_run_items AS item
        LEFT JOIN listing_snapshots AS snapshot
          ON snapshot.listing_id = item.listing_id
         AND snapshot.raw_payload_id = item.raw_payload_id
        WHERE item.run_id = ?
        ORDER BY item.item_index DESC
        LIMIT ?
        """,
        (run_id, tail_window),
    ).fetchall()
    strong = 0
    for row in tail:
        if row["rating"] is None:
            continue
        reviews = int(row["review_count"] or 0)
        rating = Decimal(str(row["rating"]))
        bayesian = (
            Decimal(reviews) * rating + prior_reviews * prior_rating
        ) / (Decimal(reviews) + prior_reviews)
        if reviews >= min_reviews and bayesian >= min_bayesian:
            strong += 1
    return IngestSummary(
        total=int(counts["total"] or 0),
        stored=int(counts["stored"] or 0),
        failed=int(counts["failed"] or 0),
        sentinels=int(counts["sentinels"] or 0),
        unique_yield=int(counts["unique_yield"] or 0),
        new_listing_yield=new_listing_yield(store, run_id),
        tail_count=len(tail),
        strong_tail_count=strong,
        tail_quality_signal=strong >= min_count,
    )


def _run_metadata(
    plan: PlannedRun,
    *,
    actor_run: Mapping[str, Any] | None = None,
    dataset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "orchestrator_version": 2,
        "plan_fingerprint": plan_fingerprint(plan),
        "actor_config_key": plan.actor_key,
        "actor_id": plan.actor_id,
        "phase_label": plan.phase_label,
        "query": plan.query,
        "requires_tail_signal_from": plan.requires_tail_signal_from,
        "min_prior_new_listings": plan.min_prior_new_listings,
        "hard_caps": {
            "max_items": plan.max_items,
            "max_total_charge_usd": str(plan.max_total_charge_usd),
        },
        "actor_run": dict(actor_run) if actor_run is not None else None,
        "dataset": dict(dataset) if dataset is not None else None,
    }


def plan_fingerprint(plan: PlannedRun) -> str:
    """Stable paid-plan identity used for cross-process claim/resume safety."""

    identity = {
        "actor_key": plan.actor_key,
        "actor_id": plan.actor_id,
        "source": plan.source,
        "phase_label": plan.phase_label,
        "query": plan.query,
        "requires_tail_signal_from": plan.requires_tail_signal_from,
        "min_prior_new_listings": plan.min_prior_new_listings,
        "run_input": plan.run_input,
        "max_items": plan.max_items,
        "max_total_charge_usd": str(plan.max_total_charge_usd),
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def _active_plan_row(store: ResearchStore, plan: PlannedRun) -> sqlite3.Row | None:
    return store.connection.execute(
        """
        SELECT *
        FROM scrape_runs
        WHERE plan_fingerprint = ? AND status = 'running'
        ORDER BY id DESC
        LIMIT 1
        """,
        (plan_fingerprint(plan),),
    ).fetchone()


_INPUT_LIMIT_KEYS = frozenset(
    {
        "limit",
        "maxActivities",
        "maxItems",
        "maxItemsPerCity",
        "maxItemsPerQuery",
        "maxResults",
    }
)


def _cached_input_covers(previous: Any, requested: Any, *, key: str | None = None) -> bool:
    """True when a previous paid input is equal or a known numeric superset."""

    if key in _INPUT_LIMIT_KEYS:
        try:
            return Decimal(str(previous)) >= Decimal(str(requested))
        except (InvalidOperation, TypeError, ValueError):
            return False
    if isinstance(previous, Mapping) and isinstance(requested, Mapping):
        return set(previous) == set(requested) and all(
            _cached_input_covers(previous[name], requested[name], key=str(name))
            for name in requested
        )
    if isinstance(previous, list) and isinstance(requested, list):
        return previous == requested
    return previous == requested


def _plan_is_materialized(store: ResearchStore, plan: PlannedRun) -> bool:
    """Return True when this exact paid input already has a cached dataset."""

    fingerprint = plan_fingerprint(plan)
    exact = store.connection.execute(
        """
        SELECT 1
        FROM scrape_runs
        WHERE plan_fingerprint = ?
          AND status IN ('complete', 'partial')
          AND dataset_id IS NOT NULL
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    if exact:
        return True

    # A completed run with identical semantics and larger numeric input/cost
    # bounds safely covers a smaller request.  Actor identity is mandatory so
    # replacing an actor under the same config key never reuses old evidence.
    rows = store.connection.execute(
        """
        SELECT input_json, metadata_json
        FROM scrape_runs
        WHERE source = ?
          AND status IN ('complete', 'partial')
          AND dataset_id IS NOT NULL
        ORDER BY started_at DESC
        """,
        (plan.source,),
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            previous_input = json.loads(row["input_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(metadata, Mapping):
            continue
        caps = metadata.get("hard_caps")
        if not isinstance(caps, Mapping):
            continue
        try:
            caps_cover = (
                int(caps.get("max_items")) >= plan.max_items
                and Decimal(str(caps.get("max_total_charge_usd")))
                >= plan.max_total_charge_usd
            )
        except (TypeError, ValueError, InvalidOperation):
            caps_cover = False
        if (
            metadata.get("actor_config_key") == plan.actor_key
            and metadata.get("actor_id") == plan.actor_id
            and metadata.get("phase_label") == plan.phase_label
            and metadata.get("query") == plan.query
            and _cached_input_covers(previous_input, plan.run_input)
            and caps_cover
        ):
            return True
    return False


def ingest_completed_dataset(
    store: ResearchStore,
    client: ApifyClient,
    *,
    plan: PlannedRun,
    actor_run: Mapping[str, Any],
    local_run_id: int | None = None,
    ranking: Mapping[str, Any] | None = None,
    print_fn: Print = print,
) -> IngestSummary:
    actor_run_id = str(actor_run.get("id") or "").strip() or None
    dataset_id = str(actor_run.get("defaultDatasetId") or "").strip()
    if actor_run.get("status") != "SUCCEEDED" or not dataset_id:
        raise RuntimeError("actor run is not a successful run with a default dataset")
    dataset = client.get_dataset(dataset_id)
    run_id = local_run_id or store.begin_run(
        plan.source,
        actor_run_id=actor_run_id,
        dataset_id=dataset_id,
        input_data=plan.run_input,
        metadata=_run_metadata(plan, actor_run=actor_run, dataset=dataset),
    )
    store.attach_actor_run(
        run_id,
        actor_run_id,
        dataset_id=dataset_id,
        metadata=_run_metadata(plan, actor_run=actor_run, dataset=dataset),
    )
    row = store.connection.execute(
        "SELECT next_offset FROM scrape_runs WHERE id = ?", (run_id,)
    ).fetchone()
    start_offset = int(row["next_offset"] or 0)
    for offset, page, _reported_total in client.iter_dataset_pages(
        dataset_id, page_size=100, start_offset=start_offset
    ):
        ingest_items(
            store,
            run_id=run_id,
            source=plan.source,
            actor_id=plan.actor_id,
            items=page,
            query_label=plan.phase_label,
            query=plan.query,
            actor_run_id=actor_run_id,
            dataset_id=dataset_id,
            ranking=ranking,
            start_index=offset,
            enrichment_kind=(
                "getyourguide-detail"
                if plan.actor_key == "getyourguide-details"
                else None
            ),
            enrichment_version=(
                GETYOURGUIDE_DETAIL_ENRICHMENT_VERSION
                if plan.actor_key == "getyourguide-details"
                else None
            ),
        )
        store.update_run_offset(run_id, offset + len(page))
    completed_offset = int(
        store.connection.execute(
            "SELECT next_offset FROM scrape_runs WHERE id = ?", (run_id,)
        ).fetchone()["next_offset"]
    )
    expected_count = dataset.get("itemCount")
    if isinstance(expected_count, int) and completed_offset < expected_count:
        raise RuntimeError(
            f"dataset {dataset_id} persisted {completed_offset}/{expected_count} items"
        )
    summary = summarize_run(store, run_id, ranking=ranking)
    detail_attempts = record_getyourguide_detail_attempts(
        store,
        run_id=run_id,
        plan=plan,
    )
    status = "complete" if summary.failed == 0 else "partial"
    store.finish_run(
        run_id,
        status=status,
        stats={
            "dataset_items": summary.total,
            "stored": summary.stored,
            "failed": summary.failed,
            "sentinels": summary.sentinels,
            "unique_yield": summary.unique_yield,
            "new_listing_yield": summary.new_listing_yield,
            "tail_count": summary.tail_count,
            "strong_tail_count": summary.strong_tail_count,
            "tail_quality_signal": summary.tail_quality_signal,
            "detail_attempts": detail_attempts,
        },
        metadata=_run_metadata(plan, actor_run=actor_run, dataset=dataset),
    )
    print_fn(
        f"  ingested {summary.stored}/{summary.total}; "
        f"{summary.unique_yield} unique in run, "
        f"{summary.new_listing_yield} globally new; "
        f"{summary.failed} failed/sentinel; "
        f"dataset {terminal_line(dataset_id)}"
    )
    print_fn(
        f"  tail quality: {summary.strong_tail_count}/{summary.tail_count} strong; "
        f"next-page signal={'yes' if summary.tail_quality_signal else 'no'}"
    )
    return summary


def execute_plan(
    store: ResearchStore,
    client: ApifyClient,
    plan: PlannedRun,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    ranking: Mapping[str, Any] | None = None,
    print_fn: Print = print,
) -> bool:
    fingerprint = plan_fingerprint(plan)
    local_run_id, claimed = store.claim_plan_run(
        plan.source,
        fingerprint,
        input_data=plan.run_input,
        metadata=_run_metadata(plan),
    )

    if claimed:
        print_fn(
            f"Starting {plan.actor_key} / {plan.display_label} "
            f"(max {plan.max_items} items, ${plan.max_total_charge_usd})"
        )
        try:
            started = client.start_actor(
                plan.actor_id,
                plan.run_input,
                max_items=plan.max_items,
                max_total_charge_usd=plan.max_total_charge_usd,
            )
        except (ApifyConfigurationError, ApifyCostLimitError, ValueError, TypeError) as exc:
            # These fail before a POST can create an actor run.
            store.finish_run(local_run_id, status="failed", error=str(exc)[:500])
            raise
        except ApifyHttpError as exc:
            if 400 <= exc.status_code < 500:
                # A documented client-error response means Apify rejected the
                # start request; releasing the claim cannot duplicate a run.
                store.finish_run(local_run_id, status="failed", error=str(exc)[:500])
            # 5xx/transport/protocol failures remain pending because a remote
            # run may have been created before the response was lost.
            raise
        actor_run_id = str(started.get("id") or "").strip()
        if not actor_run_id:
            raise RuntimeError(
                "actor start response had no id; the durable pending claim was retained "
                "to prevent an uncertain duplicate charge"
            )
        store.attach_actor_run(
            local_run_id,
            actor_run_id,
            dataset_id=str(started.get("defaultDatasetId") or "").strip() or None,
            metadata=_run_metadata(plan, actor_run=started),
        )
        print_fn(
            f"  actor run {terminal_line(actor_run_id)} started and attached "
            "to local provenance"
        )
        completed = client.wait_for_run(
            actor_run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            require_success=False,
        )
    else:
        row = store.connection.execute(
            "SELECT actor_run_id FROM scrape_runs WHERE id = ?", (local_run_id,)
        ).fetchone()
        actor_run_id = str(row["actor_run_id"] or "").strip() if row else ""
        if not actor_run_id or actor_run_id.startswith("pending:"):
            raise RuntimeError(
                "an exact plan has an unresolved pending claim; refusing to launch a "
                "possibly duplicate paid run until that claim is reconciled"
            )
        print_fn(
            f"Resuming exact active actor run {terminal_line(actor_run_id)} "
            f"({terminal_line(plan.display_label)})"
        )
        completed = client.get_run(actor_run_id)
        if completed.get("status") not in TERMINAL_STATUSES:
            completed = client.wait_for_run(
                actor_run_id,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                require_success=False,
            )

    if completed.get("status") != "SUCCEEDED":
        status = str(completed.get("status") or "unknown")
        message = str(completed.get("statusMessage") or "")[:240] or None
        record_getyourguide_detail_attempts(
            store,
            run_id=local_run_id,
            plan=plan,
            terminal_status=status,
        )
        store.finish_run(
            local_run_id,
            status="failed",
            stats={"dataset_items": 0, "stored": 0, "failed": 0, "sentinels": 0},
            metadata=_run_metadata(plan, actor_run=completed),
            error=f"actor status {status}" + (f": {message}" if message else ""),
        )
        print_fn(
            f"  actor run ended as {terminal_line(status)}; no dataset imported"
        )
        return False
    ingest_completed_dataset(
        store,
        client,
        plan=plan,
        actor_run=completed,
        local_run_id=local_run_id,
        ranking=ranking,
        print_fn=print_fn,
    )
    return True


def continuation_decision(
    store: ResearchStore, plan: PlannedRun
) -> tuple[bool, str]:
    """Evaluate an adaptive page against persisted tail quality and net-new yield."""

    prerequisite = plan.requires_tail_signal_from
    if not prerequisite:
        return True, "unconditional"
    rows = store.connection.execute(
        """
        SELECT id, metadata_json, stats_json
        FROM scrape_runs
        WHERE source = ? AND status IN ('complete', 'partial')
        ORDER BY id DESC
        """,
        (plan.source,),
    ).fetchall()
    prior = None
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if metadata.get("phase_label") == prerequisite:
            prior = row
            break
    if prior is None:
        return False, f"prerequisite {prerequisite!r} has no completed run"
    try:
        stats = json.loads(prior["stats_json"] or "{}")
    except json.JSONDecodeError:
        stats = {}
    if not bool(stats.get("tail_quality_signal")):
        return False, f"{prerequisite!r} tail was not strong"
    prior_new_listing_yield = new_listing_yield(store, int(prior["id"]))
    if prior_new_listing_yield < plan.min_prior_new_listings:
        return (
            False,
            f"{prerequisite!r} added only {prior_new_listing_yield} new listings "
            f"(< {plan.min_prior_new_listings})",
        )
    return (
        True,
        f"{prerequisite!r} had a strong tail and "
        f"{prior_new_listing_yield} new listings",
    )


def execute_pipeline(
    config: Mapping[str, Any],
    store: ResearchStore,
    client: ApifyClient,
    *,
    timeout_seconds: float = 1800,
    poll_interval_seconds: float = 5,
    only_provider: str | None = None,
    only_actor_key: str | None = None,
    rerun_completed: bool = False,
    print_fn: Print = print,
) -> bool:
    selected_plans, include_details = _selected_discovery_plans(
        config,
        only_provider=only_provider,
        only_actor_key=only_actor_key,
    )
    discovery_plans: list[PlannedRun] = []
    chargeable_discovery_plans: list[PlannedRun] = []
    for plan in selected_plans:
        if not rerun_completed and _plan_is_materialized(store, plan):
            print_fn(f"Skipping cached paid run: {plan.actor_key} / {plan.display_label}")
        else:
            discovery_plans.append(plan)
            if _active_plan_row(store, plan) is None:
                chargeable_discovery_plans.append(plan)
            else:
                print_fn(
                    f"Reusing active paid run: {plan.actor_key} / {plan.display_label}"
                )
    # Reserve every selected run that can still start, plus applicable details,
    # before the first actor starts.  Per-run Apify guards remain in force too.
    selected_charge = _selection_charge(
        config,
        chargeable_discovery_plans,
        include_details=include_details,
    )
    budget = verify_live_budget(
        config,
        client.get_current_usage_plan(),
        planned_charge=selected_charge,
    )
    print_fn(
        f"Budget preflight passed: reserving at most ${budget['planned']} "
        f"of ${budget['remaining']} live allowance"
    )
    success = True
    for index, plan in enumerate(discovery_plans, 1):
        print_fn(f"Discovery run {index}/{len(discovery_plans)}")
        should_run, decision_reason = continuation_decision(store, plan)
        if not should_run:
            print_fn(f"  adaptive stop: {plan.display_label} skipped — {decision_reason}")
            continue
        if plan.requires_tail_signal_from:
            print_fn(f"  adaptive continue: {decision_reason}")
        success = execute_plan(
            store,
            client,
            plan,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            ranking=config.get("ranking") if isinstance(config.get("ranking"), Mapping) else None,
            print_fn=print_fn,
        ) and success
        if not success:
            print_fn("Stopping before later paid runs because this actor did not succeed.")
            return False

    if include_details:
        details = config["actors"]["getyourguide-details"]
        top_limit = _positive_int(details.get("topLimit"), "getyourguide-details.topLimit")
        max_missing_attempts = _positive_int(
            details.get("maxNotReturnedAttempts", 2),
            "getyourguide-details.maxNotReturnedAttempts",
        )
        urls = select_getyourguide_detail_urls(
            store.db_path,
            limit=top_limit,
            max_not_returned_attempts=max_missing_attempts,
        )
        detail_plan = build_detail_plan(config, urls)
        if detail_plan is None:
            print_fn("No outside-Budapest GetYourGuide URLs qualified for detail enrichment.")
        else:
            # A non-empty selector means these URLs are neither enriched nor
            # exhausted.  Even an identical prior empty/partial dataset must
            # be retried until maxNotReturnedAttempts is reached.
            print_fn(f"Detail enrichment selected {len(urls)} quality-ranked outside-Budapest URLs.")
            success = execute_plan(
                store,
                client,
                detail_plan,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                ranking=config.get("ranking") if isinstance(config.get("ranking"), Mapping) else None,
                print_fn=print_fn,
            ) and success
    return success


def _plan_from_metadata(
    config: Mapping[str, Any], row: sqlite3.Row, metadata: Mapping[str, Any]
) -> PlannedRun:
    actor_key = str(metadata.get("actor_config_key") or "").strip()
    actor = config.get("actors", {}).get(actor_key)
    if not isinstance(actor, Mapping):
        raise PipelineConfigurationError(
            f"stored run {row['actor_run_id']} has unknown actor key {actor_key!r}"
        )
    try:
        run_input = json.loads(row["input_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise PipelineConfigurationError("stored run input is invalid JSON") from exc
    caps = metadata.get("hard_caps") if isinstance(metadata.get("hard_caps"), Mapping) else {}
    return PlannedRun(
        actor_key=actor_key,
        actor_id=str(metadata.get("actor_id") or _actor_api_id(actor_key, actor)),
        source=str(row["source"]),
        phase_label=str(metadata.get("phase_label") or "resumed-dataset"),
        query=str(metadata["query"]) if metadata.get("query") else None,
        run_input=run_input if isinstance(run_input, dict) else {},
        max_items=_positive_int(
            int(caps.get("max_items") or actor.get("maxItems") or 1), "stored max_items"
        ),
        max_total_charge_usd=_decimal(
            caps.get("max_total_charge_usd") or actor.get("maxTotalChargeUsd") or "0.01",
            "stored max_total_charge_usd",
        ),
        requires_tail_signal_from=(
            str(metadata.get("requires_tail_signal_from") or "").strip() or None
        ),
        min_prior_new_listings=_positive_int(
            int(metadata.get("min_prior_new_listings") or 1),
            "stored min_prior_new_listings",
        ),
    )


def resume_runs(
    config: Mapping[str, Any],
    store: ResearchStore,
    client: ApifyClient,
    *,
    actor_run_ids: Sequence[str] = (),
    timeout_seconds: float = 1800,
    poll_interval_seconds: float = 5,
    print_fn: Print = print,
) -> bool:
    params: list[Any] = []
    where = "status IN ('running', 'partial') AND actor_run_id IS NOT NULL"
    if actor_run_ids:
        where = f"actor_run_id IN ({','.join('?' for _ in actor_run_ids)})"
        params.extend(actor_run_ids)
    rows = store.connection.execute(
        f"SELECT * FROM scrape_runs WHERE {where} ORDER BY started_at", params
    ).fetchall()
    actor_keys = set(config.get("actors", {}))
    resumable_rows = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        actor_run_id = str(row["actor_run_id"] or "")
        if (
            metadata.get("actor_config_key") in actor_keys
            and not actor_run_id.startswith(("headless:", "dataset:", "pending:"))
        ):
            resumable_rows.append(row)
    rows = resumable_rows
    if actor_run_ids:
        found = {str(row["actor_run_id"]) for row in rows}
        missing = set(actor_run_ids) - found
        if missing:
            raise PipelineConfigurationError(
                "actor run is not in the local provenance DB: " + ", ".join(sorted(missing))
            )
    if not rows:
        print_fn("No resumable actor runs found.")
        return True

    success = True
    for row in rows:
        metadata_raw = row["metadata_json"] or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            metadata = {}
        plan = _plan_from_metadata(config, row, metadata)
        actor_run_id = str(row["actor_run_id"])
        print_fn(
            f"Resuming actor run {terminal_line(actor_run_id)} "
            f"({terminal_line(plan.display_label)})"
        )
        run = client.get_run(actor_run_id)
        if run.get("status") not in TERMINAL_STATUSES:
            run = client.wait_for_run(
                actor_run_id,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                require_success=False,
            )
        if run.get("status") != "SUCCEEDED":
            record_getyourguide_detail_attempts(
                store,
                run_id=int(row["id"]),
                plan=plan,
                terminal_status=str(run.get("status") or "unknown"),
            )
            store.finish_run(
                int(row["id"]),
                status="failed",
                metadata=_run_metadata(plan, actor_run=run),
                error=f"actor status {run.get('status', 'unknown')}",
            )
            print_fn(
                f"  actor ended as {terminal_line(run.get('status', 'unknown'))}"
            )
            success = False
            continue
        ingest_completed_dataset(
            store,
            client,
            plan=plan,
            actor_run=run,
            local_run_id=int(row["id"]),
            ranking=config.get("ranking") if isinstance(config.get("ranking"), Mapping) else None,
            print_fn=print_fn,
        )
    return success


def import_dataset(
    config: Mapping[str, Any],
    store: ResearchStore,
    client: ApifyClient,
    *,
    actor_key: str,
    dataset_id: str,
    actor_run_id: str | None = None,
    phase_label: str = "manual-dataset-import",
    query: str | None = None,
    print_fn: Print = print,
) -> IngestSummary:
    actor = config.get("actors", {}).get(actor_key)
    if not isinstance(actor, Mapping):
        raise PipelineConfigurationError(f"unknown actor key {actor_key!r}")
    source = _source_for_actor(actor_key, actor)
    actor_id = _actor_api_id(actor_key, actor)
    dataset = client.get_dataset(dataset_id)
    synthetic_run_id = actor_run_id or f"dataset:{dataset_id}"
    item_count = dataset.get("itemCount")
    if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0:
        raise RuntimeError(f"dataset {dataset_id} has no reliable non-negative itemCount")
    plan = PlannedRun(
        actor_key=actor_key,
        actor_id=actor_id,
        source=source,
        phase_label=phase_label,
        query=query,
        run_input={},
        max_items=max(1, item_count),
        max_total_charge_usd=Decimal("0.01"),
    )
    local_run_id = store.begin_run(
        source,
        actor_run_id=synthetic_run_id,
        dataset_id=dataset_id,
        input_data={},
        metadata=_run_metadata(plan, actor_run={"id": actor_run_id}, dataset=dataset),
    )
    summary = ingest_completed_dataset(
        store,
        client,
        plan=plan,
        actor_run={
            "id": synthetic_run_id,
            "status": "SUCCEEDED",
            "defaultDatasetId": dataset_id,
        },
        local_run_id=local_run_id,
        ranking=config.get("ranking") if isinstance(config.get("ranking"), Mapping) else None,
        print_fn=print_fn,
    )
    return summary


def replay_stored_payloads(
    config: Mapping[str, Any],
    store: ResearchStore,
    *,
    sources: Sequence[str] = (),
    run_ids: Sequence[int] = (),
    print_fn: Print = print,
) -> dict[str, int]:
    """Re-normalize private raw rows locally without fetching or paying again."""

    params: list[Any] = []
    where = ["item.raw_payload_id IS NOT NULL"]
    if sources:
        where.append(f"run.source IN ({','.join('?' for _ in sources)})")
        params.extend(source.strip().lower() for source in sources)
    if run_ids:
        where.append(f"run.id IN ({','.join('?' for _ in run_ids)})")
        params.extend(int(run_id) for run_id in run_ids)
    runs = store.connection.execute(
        f"""
        SELECT DISTINCT run.*
        FROM scrape_runs AS run
        JOIN scrape_run_items AS item ON item.run_id = run.id
        WHERE {' AND '.join(where)}
        ORDER BY run.id
        """,
        params,
    ).fetchall()
    totals = {"runs": 0, "items": 0, "stored": 0, "failed": 0, "sentinels": 0}
    actors = config.get("actors") if isinstance(config.get("actors"), Mapping) else {}
    for run in runs:
        try:
            metadata = json.loads(run["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        actor_key = str(metadata.get("actor_config_key") or "").strip()
        headless_key = actor_key if actor_key in {
            "tripadvisor-headless-details",
            "getyourguide-headless-details",
        } else None
        actor = actors.get(actor_key) if isinstance(actors, Mapping) else None
        if headless_key is None and not isinstance(actor, Mapping):
            if run["source"] == "tripadvisor":
                actor_key = "tripadvisor-discovery"
            elif "detail" in str(metadata.get("phase_label") or "").lower():
                actor_key = "getyourguide-details"
            else:
                actor_key = "getyourguide-discovery"
            actor = actors.get(actor_key) if isinstance(actors, Mapping) else None
        if headless_key is None and not isinstance(actor, Mapping):
            raise PipelineConfigurationError(
                f"cannot resolve actor adapter for stored run {run['id']}"
            )
        actor_id = (
            str(metadata.get("actor_id") or "repository-camoufox")
            if headless_key
            else str(metadata.get("actor_id") or _actor_api_id(actor_key, actor))
        )
        rows = store.connection.execute(
            """
            SELECT item.item_index, item.query_label, raw.canonical_json,
                   item.observed_at
            FROM scrape_run_items AS item
            JOIN raw_payloads AS raw ON raw.id = item.raw_payload_id
            WHERE item.run_id = ?
            ORDER BY item.item_index
            """,
            (run["id"],),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["canonical_json"])
            if not isinstance(payload, Mapping):
                raise RuntimeError(
                    f"stored raw payload for run {run['id']} item {row['item_index']} "
                    "is not a JSON object"
                )
            if headless_key:
                try:
                    if headless_key == "tripadvisor-headless-details":
                        import headless_tripadvisor as bridge

                        if payload.get("transport") == "tripadvisor-browser-html":
                            external_id = str(payload.get("externalId") or "")
                            enrichment_version = bridge.HTML_ENRICHMENT_VERSION
                        else:
                            evidence_meta = bridge.validate_graphql_evidence(payload)
                            external_id = str(evidence_meta["detail_id"])
                            enrichment_version = bridge.GRAPHQL_ENRICHMENT_VERSION
                        enrichment_kind = "tripadvisor-headless-detail"
                    else:
                        import headless_getyourguide as bridge

                        external_id = str(payload.get("externalId") or "")
                        enrichment_kind = bridge.ENRICHMENT_KIND
                        enrichment_version = bridge.ENRICHMENT_VERSION
                    listing_row = store.connection.execute(
                        """
                        SELECT id, external_id, title, kind, url, rating, review_count
                        FROM listings WHERE source = ? AND external_id = ?
                        """,
                        (str(run["source"]), external_id),
                    ).fetchone()
                    if listing_row is None:
                        raise ValueError(
                            f"headless evidence has no listing {run['source']}/{external_id}"
                        )
                    normalized = bridge.normalized_from_evidence(
                        payload, dict(listing_row)
                    )
                    stored = store.ingest_item(
                        int(run["id"]),
                        payload,
                        source=str(run["source"]),
                        normalized=normalized,
                        item_index=int(row["item_index"]),
                        query_label=str(
                            row["query_label"]
                            or metadata.get("phase_label")
                            or "headless-replay"
                        ),
                        item_metadata={
                            "transport": "private-headless-replay",
                            "locally_replayed": True,
                        },
                        fetched_at=str(row["observed_at"]),
                    )
                    store.mark_enrichment(
                        stored["listing_id"],
                        kind=enrichment_kind,
                        version=enrichment_version,
                        raw_payload_id=stored["raw_payload_id"],
                        enriched_at=str(row["observed_at"]),
                    )
                    if headless_key == "getyourguide-headless-details":
                        # The original headless importer records this success
                        # as retry-budget provenance.  A projection rebuild
                        # must reconstruct it along with the enrichment row.
                        store.record_enrichment_attempt(
                            stored["listing_id"],
                            kind=enrichment_kind,
                            version=enrichment_version,
                            run_id=int(run["id"]),
                            status="succeeded",
                            requested_url=(
                                str(listing_row["url"])
                                if listing_row["url"]
                                else None
                            ),
                            attempted_at=str(row["observed_at"]),
                        )
                    result = IngestSummary(
                        total=1,
                        stored=1,
                        failed=0,
                        sentinels=0,
                        unique_yield=1,
                        new_listing_yield=0,
                        tail_count=0,
                        strong_tail_count=0,
                        tail_quality_signal=False,
                    )
                except (KeyError, TypeError, ValueError, RuntimeError):
                    # Preserve the raw row and make the failed replay visible
                    # in run telemetry without silently fabricating a listing.
                    try:
                        store.ingest_item(
                            int(run["id"]),
                            payload,
                            source=str(run["source"]),
                            item_index=int(row["item_index"]),
                            query_label=str(row["query_label"] or "headless-replay"),
                            fetched_at=str(row["observed_at"]),
                        )
                    except (TypeError, ValueError):
                        pass
                    result = IngestSummary(
                        total=1,
                        stored=0,
                        failed=1,
                        sentinels=0,
                        unique_yield=0,
                        new_listing_yield=0,
                        tail_count=0,
                        strong_tail_count=0,
                        tail_quality_signal=False,
                    )
            else:
                result = ingest_items(
                    store,
                    run_id=int(run["id"]),
                    source=str(run["source"]),
                    actor_id=actor_id,
                    items=[payload],
                    query_label=str(row["query_label"] or metadata.get("phase_label") or "replay"),
                    query=str(metadata["query"]) if metadata.get("query") else None,
                    actor_run_id=str(run["actor_run_id"] or "") or None,
                    dataset_id=str(run["dataset_id"] or ""),
                    ranking=(
                        config.get("ranking")
                        if isinstance(config.get("ranking"), Mapping)
                        else None
                    ),
                    start_index=int(row["item_index"]),
                    enrichment_kind=(
                        "getyourguide-detail"
                        if actor_key == "getyourguide-details"
                        else None
                    ),
                    enrichment_version=(
                        GETYOURGUIDE_DETAIL_ENRICHMENT_VERSION
                        if actor_key == "getyourguide-details"
                        else None
                    ),
                    fetched_at=str(row["observed_at"]),
                )
            totals["items"] += result.total
            totals["stored"] += result.stored
            totals["failed"] += result.failed
            totals["sentinels"] += result.sentinels
        next_offset = max((int(row["item_index"]) for row in rows), default=-1) + 1
        store.update_run_offset(int(run["id"]), next_offset)
        summary = summarize_run(
            store,
            int(run["id"]),
            ranking=(
                config.get("ranking")
                if isinstance(config.get("ranking"), Mapping)
                else None
            ),
        )
        replay_stats = {
                "dataset_items": summary.total,
                "stored": summary.stored,
                "failed": summary.failed,
                "sentinels": summary.sentinels,
                "unique_yield": summary.unique_yield,
                "new_listing_yield": summary.new_listing_yield,
                "tail_count": summary.tail_count,
                "strong_tail_count": summary.strong_tail_count,
                "tail_quality_signal": summary.tail_quality_signal,
                "locally_replayed": True,
            }
        original_status = str(run["status"])
        if original_status == "running":
            # Local replay cannot prove that the remote actor/dataset reached
            # its terminal item count.  Keep it resumable.
            store.update_run_observation(
                int(run["id"]), stats=replay_stats, metadata=metadata
            )
        else:
            dataset_meta = metadata.get("dataset")
            expected_count = (
                dataset_meta.get("itemCount")
                if isinstance(dataset_meta, Mapping)
                else None
            )
            fully_materialized = (
                bool(headless_key)
                or (isinstance(expected_count, int) and next_offset >= expected_count)
            )
            target_status = original_status
            if original_status == "complete" or fully_materialized:
                target_status = "complete" if summary.failed == 0 else "partial"
            store.finish_run(
                int(run["id"]),
                status=target_status,
                stats=replay_stats,
                metadata=metadata,
                completed_at=(
                    str(run["completed_at"]) if run["completed_at"] else None
                ),
            )
        if actor_key == "getyourguide-details":
            replay_plan = _plan_from_metadata(config, run, metadata)
            record_getyourguide_detail_attempts(
                store,
                run_id=int(run["id"]),
                plan=replay_plan,
                attempted_at=str(
                    run["completed_at"] or run["started_at"]
                ),
            )
        totals["runs"] += 1
    print_fn(
        "Locally replayed "
        f"{totals['items']} payloads across {totals['runs']} runs: "
        f"{totals['stored']} stored, {totals['failed']} failed/sentinel; no network used."
    )
    return totals


def rebuild_normalized_projections(
    config: Mapping[str, Any],
    store: ResearchStore,
    *,
    print_fn: Print = print,
) -> dict[str, int]:
    """Atomically rebuild normalized tables from retained local evidence.

    This path never constructs an Apify client.  Raw payloads, run rows and
    run-item provenance remain exact; normalized foreign keys are rebound by
    stable ``(source, external_id)`` identity.  Enrichment and retry-attempt
    history is snapshotted before cleanup and restored byte-for-byte (apart
    from the intentionally rebound listing ID) after replay.
    """

    connection = store.connection
    orphan_listings = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM listings AS listing
            WHERE NOT EXISTS (
                SELECT 1 FROM scrape_run_items AS item
                WHERE item.listing_id = listing.id
            )
            """
        ).fetchone()[0]
    )
    if orphan_listings:
        raise RuntimeError(
            "projection rebuild refused: "
            f"{orphan_listings} listing(s) have no retained run-item provenance"
        )

    retained_counts = {
        "raw_payloads": int(connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0]),
        "scrape_runs": int(connection.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]),
        "scrape_run_items": int(
            connection.execute("SELECT COUNT(*) FROM scrape_run_items").fetchone()[0]
        ),
    }
    run_columns = (
        "source",
        "actor_run_id",
        "dataset_id",
        "plan_fingerprint",
        "next_offset",
        "status",
        "started_at",
        "completed_at",
        "input_json",
        "metadata_json",
        "stats_json",
        "error",
    )
    run_rows = [
        dict(row)
        for row in connection.execute(
            f"SELECT id, {', '.join(run_columns)} FROM scrape_runs ORDER BY id"
        ).fetchall()
    ]
    item_columns = (
        "run_id",
        "item_index",
        "external_id",
        "url",
        "query_label",
        "destination",
        "result_rank",
        "metadata_json",
        "status",
        "raw_payload_id",
        "error",
        "observed_at",
        "created_at",
    )
    item_rows = [
        dict(row)
        for row in connection.execute(
            f"SELECT id, {', '.join(item_columns)} "
            "FROM scrape_run_items ORDER BY id"
        ).fetchall()
    ]
    attempts = [
        dict(row)
        for row in connection.execute(
            """
            SELECT listing.source, listing.external_id,
                   attempt.enrichment_kind, attempt.enrichment_version,
                   attempt.run_id, attempt.status, attempt.requested_url,
                   attempt.attempted_at, attempt.error
            FROM listing_enrichment_attempts AS attempt
            JOIN listings AS listing ON listing.id = attempt.listing_id
            ORDER BY listing.source, listing.external_id,
                     attempt.enrichment_kind, attempt.enrichment_version,
                     attempt.run_id
            """
        ).fetchall()
    ]
    enrichments = [
        dict(row)
        for row in connection.execute(
            """
            SELECT listing.source, listing.external_id,
                   enrichment.enrichment_kind, enrichment.enrichment_version,
                   enrichment.raw_payload_id, enrichment.enriched_at
            FROM listing_enrichments AS enrichment
            JOIN listings AS listing ON listing.id = enrichment.listing_id
            ORDER BY listing.source, listing.external_id,
                     enrichment.enrichment_kind, enrichment.enrichment_version
            """
        ).fetchall()
    ]

    with store.transaction():
        connection.execute("DELETE FROM listings")
        connection.execute("DELETE FROM places")
        connection.execute("DELETE FROM categories")

        replay_totals = replay_stored_payloads(config, store, print_fn=print_fn)

        current_items = {
            int(row["id"]): row
            for row in connection.execute(
                "SELECT id, status, listing_id FROM scrape_run_items"
            ).fetchall()
        }
        for retained in item_rows:
            current = current_items.get(int(retained["id"]))
            if current is None:
                raise RuntimeError(
                    f"projection replay lost run item {retained['id']}"
                )
            if retained["status"] == "stored" and (
                current["status"] != "stored" or current["listing_id"] is None
            ):
                raise RuntimeError(
                    "projection replay could not restore stored run item "
                    f"{retained['run_id']}/{retained['item_index']}"
                )
            # Restore immutable observation/provenance fields exactly.  The
            # replayed listing_id remains, except historical non-stored rows
            # are deliberately kept unlinked.
            assignments = ", ".join(f"{column} = ?" for column in item_columns)
            connection.execute(
                f"UPDATE scrape_run_items SET {assignments}, listing_id = "
                "CASE WHEN ? = 'stored' THEN listing_id ELSE NULL END WHERE id = ?",
                tuple(retained[column] for column in item_columns)
                + (retained["status"], retained["id"]),
            )

        # Replay telemetry is useful to the caller but must not rewrite the
        # historical remote-run record.
        run_assignments = ", ".join(f"{column} = ?" for column in run_columns)
        for retained in run_rows:
            connection.execute(
                f"UPDATE scrape_runs SET {run_assignments} WHERE id = ?",
                tuple(retained[column] for column in run_columns) + (retained["id"],),
            )

        # A previously failed/unparsed row can become parseable under a newer
        # adapter.  Because its original provenance remains non-stored, remove
        # any projection that has no retained stored occurrence.
        connection.execute(
            """
            DELETE FROM listings
            WHERE NOT EXISTS (
                SELECT 1 FROM scrape_run_items AS item
                WHERE item.listing_id = listings.id AND item.status = 'stored'
            )
            """
        )
        connection.execute(
            "DELETE FROM places WHERE NOT EXISTS "
            "(SELECT 1 FROM listings WHERE listings.place_id = places.id)"
        )
        connection.execute(
            "DELETE FROM categories WHERE NOT EXISTS "
            "(SELECT 1 FROM listing_categories "
            " WHERE listing_categories.category_id = categories.id)"
        )

        listing_ids = {
            (str(row["source"]), str(row["external_id"])): int(row["id"])
            for row in connection.execute(
                "SELECT id, source, external_id FROM listings"
            ).fetchall()
        }
        connection.execute("DELETE FROM listing_enrichment_attempts")
        connection.execute("DELETE FROM listing_enrichments")
        for row in enrichments:
            identity = (str(row["source"]), str(row["external_id"]))
            listing_id = listing_ids.get(identity)
            if listing_id is None:
                raise RuntimeError(
                    "projection rebuild cannot rebind enrichment for "
                    f"{identity[0]}/{identity[1]}"
                )
            connection.execute(
                """
                INSERT INTO listing_enrichments(
                    listing_id, enrichment_kind, enrichment_version,
                    raw_payload_id, enriched_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    listing_id,
                    row["enrichment_kind"],
                    row["enrichment_version"],
                    row["raw_payload_id"],
                    row["enriched_at"],
                ),
            )
        for row in attempts:
            identity = (str(row["source"]), str(row["external_id"]))
            listing_id = listing_ids.get(identity)
            if listing_id is None:
                raise RuntimeError(
                    "projection rebuild cannot rebind attempt for "
                    f"{identity[0]}/{identity[1]}"
                )
            connection.execute(
                """
                INSERT INTO listing_enrichment_attempts(
                    listing_id, enrichment_kind, enrichment_version, run_id,
                    status, requested_url, attempted_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing_id,
                    row["enrichment_kind"],
                    row["enrichment_version"],
                    row["run_id"],
                    row["status"],
                    row["requested_url"],
                    row["attempted_at"],
                    row["error"],
                ),
            )

        final_counts = {
            "raw_payloads": int(connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0]),
            "scrape_runs": int(connection.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]),
            "scrape_run_items": int(
                connection.execute("SELECT COUNT(*) FROM scrape_run_items").fetchone()[0]
            ),
        }
        if final_counts != retained_counts:
            raise RuntimeError(
                f"projection rebuild changed retained evidence counts: "
                f"{retained_counts} -> {final_counts}"
            )
        if connection.execute(
            "SELECT COUNT(*) FROM scrape_run_items WHERE observed_at = '' OR observed_at IS NULL"
        ).fetchone()[0]:
            raise RuntimeError("projection rebuild found missing item chronology")
        if connection.execute(
            "SELECT COUNT(*) FROM scrape_run_items "
            "WHERE status = 'stored' AND listing_id IS NULL"
        ).fetchone()[0]:
            raise RuntimeError("projection rebuild left stored run items unlinked")
        if int(connection.execute(
            "SELECT COUNT(*) FROM listing_enrichment_attempts"
        ).fetchone()[0]) != len(attempts):
            raise RuntimeError("projection rebuild changed enrichment-attempt history")
        if int(connection.execute(
            "SELECT COUNT(*) FROM listing_enrichments"
        ).fetchone()[0]) != len(enrichments):
            raise RuntimeError("projection rebuild changed enrichment history")
        if int(connection.execute("SELECT COUNT(*) FROM listing_fts").fetchone()[0]) != int(
            connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        ):
            raise RuntimeError("projection rebuild left listing search index out of sync")
        if int(connection.execute("SELECT COUNT(*) FROM review_fts").fetchone()[0]) != int(
            connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        ):
            raise RuntimeError("projection rebuild left review search index out of sync")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("projection rebuild failed foreign-key validation")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"projection rebuild failed integrity check: {integrity}")

    result = {
        **replay_totals,
        "attempts_preserved": len(attempts),
        "enrichments_preserved": len(enrichments),
        "listings": int(connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]),
        "reviews": int(connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]),
    }
    print_fn(
        "Rebuilt normalized projections atomically from retained local evidence: "
        f"{result['listings']} listings, {result['reviews']} reviews, "
        f"{result['attempts_preserved']} exact attempts; no network used."
    )
    return result


def print_plan(
    config: Mapping[str, Any],
    *,
    usage: UsagePlan | None = None,
    only_provider: str | None = None,
    only_actor_key: str | None = None,
) -> None:
    plans, include_details = _selected_discovery_plans(
        config,
        only_provider=only_provider,
        only_actor_key=only_actor_key,
    )
    print("Hybrid discovery: country coverage first, then destination/theme gap-fill queries.")
    for index, plan in enumerate(plans, 1):
        print(
            f"{index:>2}. {plan.actor_key} / {plan.display_label} — "
            f"max {plan.max_items}, ${plan.max_total_charge_usd}"
        )
    if include_details:
        detail = config["actors"]["getyourguide-details"]
        print(
            f" + quality-ranked GetYourGuide details — max {detail['maxItems']}, "
            f"${detail['maxTotalChargeUsd']}"
        )
    total = _selection_charge(config, plans, include_details=include_details)
    local_cap = Decimal(str(config["budget"]["maxPlannedChargeUsd"]))
    print(f"Total hard-cap envelope: ${total} / ${local_cap} local cap")
    if usage is not None:
        budget = verify_live_budget(config, usage, planned_charge=total)
        print(
            f"Live allowance: ${budget['remaining']} remaining; "
            f"the full pipeline fits with ${budget['remaining'] - budget['planned']} spare."
        )


def print_status(store: ResearchStore) -> None:
    print("Database rows:")
    for label, count in store.stats().items():
        print(f"  {label}: {count}")
    print("Listings by provider and scope:")
    rows = store.connection.execute(
        """
        SELECT source, location_scope, COUNT(*) AS count
        FROM listings
        GROUP BY source, location_scope
        ORDER BY source, location_scope
        """
    ).fetchall()
    if not rows:
        print("  none")
    for row in rows:
        print(
            f"  {terminal_line(row['source'])} / "
            f"{terminal_line(row['location_scope'])}: {row['count']}"
        )
    recorded_cost = Decimal("0")
    cost_rows = 0
    for row in store.connection.execute(
        "SELECT metadata_json FROM scrape_runs WHERE metadata_json IS NOT NULL"
    ).fetchall():
        try:
            metadata = json.loads(row["metadata_json"])
            actor_run = metadata.get("actor_run") if isinstance(metadata, Mapping) else None
            raw_cost = actor_run.get("usageTotalUsd") if isinstance(actor_run, Mapping) else None
            if raw_cost is not None:
                recorded_cost += Decimal(str(raw_cost))
                cost_rows += 1
        except (json.JSONDecodeError, InvalidOperation, ValueError):
            continue
    if cost_rows:
        print(f"Recorded Apify usage across {cost_rows} runs: ${recorded_cost}")
    print("Recent runs:")
    runs = store.connection.execute(
        """
        SELECT r.actor_run_id, r.dataset_id, r.source, r.status,
               COUNT(i.id) AS items,
               SUM(CASE WHEN i.status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM scrape_runs AS r
        LEFT JOIN scrape_run_items AS i ON i.run_id = r.id
        GROUP BY r.id
        ORDER BY r.started_at DESC
        LIMIT 20
        """
    ).fetchall()
    if not runs:
        print("  none")
    for row in runs:
        print(
            f"  {terminal_line(row['source'])} {terminal_line(row['status'])}: "
            f"{row['items']} items, {row['failed'] or 0} failed; "
            f"run={terminal_line(row['actor_run_id'])}, "
            f"dataset={terminal_line(row['dataset_id'])}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="show capped runs without starting actors")
    plan.add_argument("--live", action="store_true", help="also verify current Apify allowance")
    plan_selection = plan.add_mutually_exclusive_group()
    plan_selection.add_argument(
        "--only-provider", choices=("tripadvisor", "getyourguide")
    )
    plan_selection.add_argument("--only-actor-key")

    run = subparsers.add_parser("run", help="run the complete capped hybrid pipeline")
    run.add_argument("--timeout", type=float, default=1800)
    run.add_argument("--poll", type=float, default=5)
    run_selection = run.add_mutually_exclusive_group()
    run_selection.add_argument(
        "--only-provider", choices=("tripadvisor", "getyourguide")
    )
    run_selection.add_argument("--only-actor-key")
    run.add_argument(
        "--rerun-completed",
        action="store_true",
        help="pay for exact inputs again instead of using cached completed datasets",
    )

    resume = subparsers.add_parser("resume", help="import completed datasets without rerunning actors")
    resume.add_argument("--actor-run-id", action="append", default=[])
    resume.add_argument("--timeout", type=float, default=1800)
    resume.add_argument("--poll", type=float, default=5)

    ingest = subparsers.add_parser(
        "ingest-dataset", help="import an existing dataset without starting an actor"
    )
    ingest.add_argument("--dataset-id", required=True)
    ingest.add_argument("--actor-key", required=True)
    ingest.add_argument("--actor-run-id")
    ingest.add_argument("--label", default="manual-dataset-import")
    ingest.add_argument("--query")

    replay = subparsers.add_parser(
        "replay-stored",
        help="re-normalize already stored raw payloads locally without network or actor costs",
    )
    replay.add_argument(
        "--source",
        action="append",
        choices=("tripadvisor", "getyourguide"),
        default=[],
    )
    replay.add_argument("--run-id", action="append", type=int, default=[])

    subparsers.add_parser(
        "rebuild-projections",
        help=(
            "atomically replace normalized projections from retained local raw/run "
            "evidence while preserving exact enrichment-attempt history"
        ),
    )

    subparsers.add_parser("status", help="show only privacy-safe local counts/provenance")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "plan":
            usage = ApifyClient().get_current_usage_plan() if args.live else None
            print_plan(
                config,
                usage=usage,
                only_provider=args.only_provider,
                only_actor_key=args.only_actor_key,
            )
            return 0
        with ResearchStore(args.db) as store:
            if args.command == "status":
                print_status(store)
                return 0
            if args.command == "replay-stored":
                replay_stored_payloads(
                    config,
                    store,
                    sources=args.source,
                    run_ids=args.run_id,
                )
                return 0
            if args.command == "rebuild-projections":
                rebuild_normalized_projections(config, store)
                return 0
            client = ApifyClient()
            if args.command == "run":
                return 0 if execute_pipeline(
                    config,
                    store,
                    client,
                    timeout_seconds=args.timeout,
                    poll_interval_seconds=args.poll,
                    only_provider=args.only_provider,
                    only_actor_key=args.only_actor_key,
                    rerun_completed=args.rerun_completed,
                ) else 1
            if args.command == "resume":
                return 0 if resume_runs(
                    config,
                    store,
                    client,
                    actor_run_ids=args.actor_run_id,
                    timeout_seconds=args.timeout,
                    poll_interval_seconds=args.poll,
                ) else 1
            import_dataset(
                config,
                store,
                client,
                actor_key=args.actor_key,
                dataset_id=args.dataset_id,
                actor_run_id=args.actor_run_id,
                phase_label=args.label,
                query=args.query,
            )
            return 0
    except (
        PipelineConfigurationError,
        PipelineBudgetError,
        ApifyError,
        OSError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"error: {_safe_error(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
