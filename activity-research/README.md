# Hungary activity research

This directory contains a durable, local research warehouse for Hungary activities. It combines broad country discovery with destination and theme gap-filling, preserves every provider payload before interpreting it, and exposes a read-only search CLI for ranking and review research.

The default database is `activity-research/data/activity-research.sqlite3`. It is private, local, and gitignored.

## What the pipeline does

```text
Tripadvisor country search ─┐
Tripadvisor place/themes ───┼─> capped Apify datasets
GetYourGuide country page ──┤             │
GetYourGuide destinations ──┘             v
                                  private raw_payloads
                                           │
                            conservative normalization
                                           │
             places / listings / snapshots / categories
                  media / packages / reviews / provenance
                                           │
                           FTS search + Bayesian ranking
```

The search strategy is deliberately hybrid:

1. Search Hungary at country level for broad coverage.
2. Query important destinations and activity themes to recover results that a country page under-exposes.
3. Classify the destination as `budapest`, `outside-budapest`, `foreign`, or `unknown`.
4. Rank outside-Budapest results by rating strength, not raw stars alone.
5. Enrich the highest-ranked GetYourGuide listings with detail payloads, packages, media, and available sample reviews.
6. Fill actor omissions from private Camoufox caches, using up to ten rendered reviews and the live headline price without paying to re-scrape the same URL.

Country-only crawling is not treated as complete coverage. Provider result pages can be dominated by Budapest, and provider ranking is discovery evidence rather than a statement of what is objectively best.

## Files

| File | Purpose |
| --- | --- |
| `sources.json` | Versioned source plan, actor inputs, search fan-out, ranking policy, and hard cost caps |
| `scrape_hungary.py` | Plan, run, resume, and existing-dataset ingestion commands |
| `headless_tripadvisor.py` | Export ranked Tripadvisor candidates and import locally cached browser evidence |
| `headless_getyourguide.py` | Render and import GetYourGuide URLs omitted by the paid detail actor |
| `apify_client.py` | Authenticated Apify API client with mandatory item and USD caps |
| `normalizers.py` | Alias-tolerant Tripadvisor and GetYourGuide adapters |
| `store.py` | Transactional SQLite ingestion, deduplication, migrations, and provenance |
| `schema.sql` | Versioned normalized/raw schema, indexes, FTS tables, and ranking view |
| `query.py` | Read-only listing, review, run, and database-stat queries |
| `test_*.py` | Unit tests for client, adapters, storage, and orchestration |

## Storage model

The SQLite schema is intentionally split between immutable evidence and convenient searchable projections.

### Lossless evidence and provenance

- `raw_payloads` stores canonical JSON for every dataset item, including blocked, malformed, or unrecognized rows. Unknown provider fields are never discarded.
- Raw payloads are deduplicated by `(source, sha256)`. Seeing the same payload again reuses the raw row without losing the new run provenance.
- `scrape_runs` records actor input, actor and dataset IDs, caps, status, statistics, resumable dataset offset, and metadata.
- `scrape_run_items` records the exact position, query or phase, result rank, destination, normalization status, raw payload, normalized listing, and per-occurrence `observed_at` time for each dataset item. A deduplicated raw row keeps its first fetch time while later identical occurrences keep their own chronology here.
- `listing_snapshots` preserves meaningful listing observations across time instead of overwriting all history.
- `listing_enrichments` records which version and transport successfully researched a listing. Any successful GetYourGuide detail transport prevents another paid detail request by default.
- `listing_enrichment_attempts` records success, no-result, and failure outcomes per listing so a missing detail is retried only within the configured bound.

### Searchable entities

- `places` holds stable geographic entities and coordinates.
- `listings` holds one provider-specific listing per `(source, external_id)`, including its normalized `kind` (`attraction`, `experience`, `lodging`, `restaurant`, or another provider-defined value).
- `categories` and `listing_categories` provide many-to-many classification.
- `media` holds image or media evidence.
- `packages` holds separate bookable options, prices, duration, and availability text.
- `reviews` holds normalized review evidence without reviewer identity fields.
- `listing_fts` and `review_fts` provide Unicode full-text search with diacritic folding.
- `listing_quality_ranking` exposes the current Bayesian score using a `4.0` rating prior with weight `50`.

Re-ingestion is monotonic where shallow result cards are incomplete: a later shallow row does not erase richer geography, kind, categories, media, package descriptions, or prices already learned from a detail page.

## Privacy

Treat the entire database as private research material.

- `raw_payloads.is_private` defaults to `1`.
- Raw payloads can contain reviewer names, profile identifiers, free text, and provider metadata.
- Reviewer names, profile URLs, avatars, and user IDs are intentionally excluded from normalized `reviews`.
- Normalized review bodies can still be personal or copyrighted content, so they must not be published wholesale.
- The database and private export directory are gitignored; do not force-add them.
- The database, WAL/SHM sidecars, and browser evidence files are created as owner-only (`0600`) inside owner-only private data directories (`0700`).
- The database is not encrypted at rest. Local filesystem access is sufficient to read it.
- `scrape_hungary.py status` reports counts and provenance without printing raw payloads or review text.

Publish only compact derived summaries and source links. Do not publish the raw database, raw review text, or provider payload dumps.

## Authentication

Paid and remote commands need an Apify token. The client looks up:

1. macOS Keychain service `APIFY_TOKEN`, using the current macOS username as the account.
2. The `APIFY_TOKEN` environment variable as a fallback.

Never put the token in `sources.json`, source code, shell examples, or committed files.

## Safe command workflow

Run commands from the repository root.

### Inspect the plan

This is local and starts no actors:

```bash
python3 activity-research/scrape_hungary.py plan
```

This additionally reads the live Apify allowance but still starts no actors:

```bash
python3 activity-research/scrape_hungary.py plan --live
```

Provider-specific views are useful before incremental runs:

```bash
python3 activity-research/scrape_hungary.py plan --only-provider tripadvisor
python3 activity-research/scrape_hungary.py plan --only-provider getyourguide
```

### Run capped discovery

`run` can create paid actor runs. It performs one live allowance preflight, then each actor request also receives both `maxItems` and `maxTotalChargeUsd`.

```bash
python3 activity-research/scrape_hungary.py run --poll 10 --timeout 1800
```

Run only one provider when filling a known gap:

```bash
python3 activity-research/scrape_hungary.py run --only-provider getyourguide
```

The current complete configuration has a `$3.219` worst-case envelope inside a `$4.20` local safety cap. That is a ceiling, not an expected bill. Configuration loading fails if actor caps exceed the local envelope, and execution refuses to start if the selected envelope exceeds the account's live remaining allowance.

Do not use `--rerun-completed` casually. It deliberately bypasses the completed-input cache and can pay for identical actor inputs again:

```bash
python3 activity-research/scrape_hungary.py run --rerun-completed
```

### Resume without re-scraping

Resume every locally recorded `running` or `partial` run:

```bash
python3 activity-research/scrape_hungary.py resume
```

Or resume one known actor run:

```bash
python3 activity-research/scrape_hungary.py resume --actor-run-id ACTOR_RUN_ID
```

`resume` does not create a replacement actor. It checks the existing run and imports its existing dataset. Dataset pages are persisted in batches, and `next_offset` lets a later resume continue after the last committed page.

The general resume command operates only on real Apify actor IDs. Local headless imports and manual `dataset:` imports are deliberately excluded and are recovered through their corresponding import command.

An already-created Apify dataset can also be imported without starting an actor:

```bash
python3 activity-research/scrape_hungary.py ingest-dataset \
  --dataset-id DATASET_ID \
  --actor-key getyourguide-discovery \
  --label recovered-dataset
```

Re-run normalization over already stored private payloads without any network request or actor charge:

```bash
python3 activity-research/scrape_hungary.py replay-stored
```

`replay-stored` is an in-place merge. It is useful for targeted adapter checks, but it does **not** delete normalized rows that a newer parser no longer emits, so it is not a projection-cleanup command. It never re-scrapes a provider. Paid actor payloads, Tripadvisor GraphQL/HTML browser evidence, and GetYourGuide rendered evidence all have replay adapters; an incomplete `running` actor run stays running and resumable after replay.

For a complete cleanup, use the atomic no-network rebuild:

```bash
python3 activity-research/scrape_hungary.py rebuild-projections
```

This deletes only derived normalized tables, replays retained raw/run evidence, validates chronology, links and database integrity before commit, and then rebinds the exact successful enrichment and retry-attempt history by stable provider identity. Raw payloads, remote run records and run-item provenance are preserved. Make an online SQLite backup first when operating on the live warehouse.

### Enrich Tripadvisor with the local headless cache

Export the current quality-ranked outside-Budapest Tripadvisor candidates into the existing browser workflow:

```bash
python3 activity-research/headless_tripadvisor.py export
```

Fetch up to ten reviews plus the available description and price/package evidence with the repository's Camoufox browser. Existing valid evidence files are reused:

```bash
cd budapest-london/tripadvisor
/Users/matija/workspace/scripts/stealth/.venv/bin/python scrape_ta_details.py \
  --city hungary --all --graphql --workers 1 --block-cooldown 60
cd ../..
```

Import the exact cached browser evidence and compact normalized projections into SQLite:

```bash
python3 activity-research/headless_tripadvisor.py import
```

The candidate export, detail context, raw browser cache, and SQLite database are private and gitignored. Re-running this sequence reuses valid browser caches and retries only candidates whose evidence is missing or invalid.

Tripadvisor normally uses strict GraphQL evidence. A legacy page whose GraphQL request aborts can use the same crawler without `--graphql`; the importer validates and stores its exact rendered HTML instead. Both transports enforce the provider host, source URL, listing identity, canonical identity, and review-evidence contract.

### Fill GetYourGuide actor omissions with the browser

The paid detail actor is attempted at most twice for genuinely omitted URLs. If a valid activity still does not appear in its dataset, render only those remaining URLs with Camoufox:

```bash
/Users/matija/workspace/scripts/stealth/.venv/bin/python \
  activity-research/headless_getyourguide.py scrape
python3 activity-research/headless_getyourguide.py import
python3 activity-research/headless_getyourguide.py status
```

`status` reaches zero when every current outside-Budapest GetYourGuide listing has a successful paid or browser detail enrichment. Each browser evidence file contains the exact private rendered HTML and visible text; only identity-free review bodies and structured listing fields enter normalized tables.

### Inspect local state

```bash
python3 activity-research/scrape_hungary.py status
python3 activity-research/query.py stats
python3 activity-research/query.py runs --status partial --format json
```

Pass `--db PATH` before the subcommand to inspect a non-default database:

```bash
python3 activity-research/query.py --db /path/to/research.sqlite3 stats
```

## Cache and no-rescrape behavior

The normal `run` path skips a paid discovery input when a `complete` or `partial` local run already has a dataset and matches all of the following:

- provider and actor configuration key;
- phase label and query;
- complete actor input JSON;
- item cap and USD cap.

An older completed run also covers a newer plan when its actor input is otherwise identical and its item and USD caps are at least as large. Changing the substantive input intentionally creates a new research input. Raw payload SHA deduplication still prevents duplicate evidence rows, but it cannot undo the cost of a newly started actor.

The Tripadvisor country pass uses the actor's supported location-query input with one bounded 30-result cap. Live validation showed that both a larger query cap and manually offset `oa30`/`oa60` category URLs returned the same first 30 listings, so the plan does not pretend those inputs provide deeper pagination. Deeper coverage comes from the independently capped destination and theme fan-out. The generic continuation gate remains available for a future actor input that is first proven to advance and still requires both a strong tail and enough globally net-new listings.

Use this order after an interruption:

1. Run `status`.
2. Run `resume` to finish locally known datasets.
3. Run the normal `run` command; exact completed inputs will be skipped.
4. Use `--rerun-completed` only when fresh provider data is explicitly required.

## Search examples

The query CLI opens SQLite read-only.

Top outside-Budapest options by Bayesian quality:

```bash
python3 activity-research/query.py listings \
  --scope outside-budapest \
  --sort quality \
  --limit 40
```

High-consensus GetYourGuide activities:

```bash
python3 activity-research/query.py listings \
  --source getyourguide \
  --scope outside-budapest \
  --min-rating 4.5 \
  --min-reviews 50 \
  --sort quality \
  --format json
```

Tours that start in Budapest but visit an outside destination:

```bash
python3 activity-research/query.py listings \
  --scope outside-budapest \
  --starts-in-budapest yes
```

Full-text activity search:

```bash
python3 activity-research/query.py listings \
  --search "cave boat" \
  --scope outside-budapest
```

Search normalized review text and rank matches by relevance:

```bash
python3 activity-research/query.py reviews \
  --search "crowded weekend" \
  --scope outside-budapest \
  --sort relevance \
  --format jsonl
```

Search terms are combined with `AND`; `--search "cave boat"` requires both tokens. Repeat `--source`, `--kind`, or `--scope` to select multiple values.

## Direct SQL examples

SQLite remains the source of truth, so ad hoc research does not require another export format.

```bash
sqlite3 -header -column activity-research/data/activity-research.sqlite3
```

Top quality-ranked outside-Budapest listings:

```sql
SELECT source, title, rating, review_count, bayesian_rating
FROM listing_quality_ranking
WHERE location_scope = 'outside-budapest'
ORDER BY bayesian_rating DESC, review_count DESC
LIMIT 30;
```

Package and price options for one listing:

```sql
SELECT l.title, p.name, p.price, p.original_price, p.currency,
       p.duration_text, p.availability_text
FROM packages AS p
JOIN listings AS l ON l.id = p.listing_id
WHERE l.source = 'getyourguide'
  AND l.external_id = 'PROVIDER_EXTERNAL_ID'
  AND p.active = 1
ORDER BY p.sort_order;
```

Trace a normalized listing back to every raw observation and search query:

```sql
SELECT r.started_at, i.query_label, i.result_rank, i.status,
       rp.id AS raw_payload_id, rp.sha256
FROM scrape_run_items AS i
JOIN scrape_runs AS r ON r.id = i.run_id
JOIN raw_payloads AS rp ON rp.id = i.raw_payload_id
JOIN listings AS l ON l.id = i.listing_id
WHERE l.source = 'tripadvisor'
  AND l.external_id = 'PROVIDER_EXTERNAL_ID'
ORDER BY r.started_at, i.item_index;
```

Inspect a provider field that has not yet been normalized:

```sql
SELECT id,
       json_extract(canonical_json, '$.name') AS name,
       json_extract(canonical_json, '$.rating') AS rating
FROM raw_payloads
WHERE source = 'tripadvisor'
ORDER BY id DESC
LIMIT 20;
```

Do not paste raw query results into public issues, commits, or generated pages.

## Source and interpretation caveats

- **Coverage is sampled, not exhaustive.** Country pages, destination collections, actor pagination, and provider ranking can all omit worthwhile activities.
- **Budapest can crowd out Hungary-wide results.** Destination and theme fan-out exists specifically to counter this; adding a genuinely missing region belongs in `sources.json` with its own bounded cap.
- **Location is evidence-based.** The normalizer separates destination from Budapest pickup where the payload supports it. Border coordinates or thin rows without reliable country/locality evidence remain `unknown` instead of being guessed as Hungarian.
- **Provider fields change.** Adapters accept known aliases conservatively. New or ambiguous fields remain available in `raw_payloads` until an adapter and tests explicitly support them.
- **Sentinels are retained.** Captcha, blocked, and malformed rows are stored for audit but do not become listings.
- **Ratings are snapshots.** Rating, review count, price, package availability, and hours can change after scraping.
- **A displayed review count is not the number of reviews stored locally.** It is the provider's aggregate count; `stored_reviews` reports local review evidence.
- **`price_from` is only the cheapest observed headline price.** Use `packages` for multiple ticket types and options, and verify live pricing before booking.
- **Bayesian quality reduces tiny-sample five-star noise.** It is still a ranking aid, not a recommendation by itself. Memorable or rare wildcard options need a separate qualitative pass.
- **Continuation requires proven pagination.** The orchestrator can gate a bounded continuation on tail quality and globally net-new yield, but the current Tripadvisor plan intentionally has no offset-page phases because that actor did not preserve the requested category offset.
- **GetYourGuide review enrichment is provider-dependent.** Detail payloads may include sample reviews, not a complete review corpus.
- **Tripadvisor review research is separate.** The configured repository strategy is the local headless Camoufox workflow with cached pages and up to ten reviews; `scrape_hungary.py` itself does not launch that browser enrichment.

Before making a recommendation, open the source listing and verify the distinctive payoff, effort, current price, opening status, and whether the experience is actually at the claimed destination.

## Database maintenance

- Schema version is tracked with `PRAGMA user_version`; `ResearchStore` currently supports schema version `6` and migrates older supported versions in order.
- SQLite uses WAL mode. While a writer is active, the `-wal` and `-shm` sidecars are part of the live database state.
- Prefer SQLite's online backup command over copying only the main file during ingestion:

  ```bash
  sqlite3 activity-research/data/activity-research.sqlite3 \
    ".backup 'activity-research/data/activity-research.backup.sqlite3'"
  ```

- Never delete the database merely to refresh results. Preserve it, resume incomplete imports, and add new snapshots.
- Keep schema, adapters, tests, and small derived public summaries versioned; keep raw databases, payloads, and private exports unversioned.
