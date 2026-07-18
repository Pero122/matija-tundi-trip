# TripAdvisor Discover pipeline

The checked-in `index.html` contains the Discover UI and its current candidate
dataset. Raw TripAdvisor HTML and intermediate JSON stay local and gitignored.

## Refresh Budapest

```bash
cd budapest-london/tripadvisor
PY=~/workspace/scripts/stealth/.venv/bin/python

$PY -m unittest -v test_scrape_ta.py
$PY scrape_ta.py --cities budapest
$PY dedup.py --cities budapest
$PY build_review.py --cities budapest
```

`build_review.py` replaces only Budapest inside the inline `const DATA=...`
block. It preserves the London archive, UI, Supabase auth, shared ratings, and
grouping code, so a clean clone does not need ignored London intermediates.
Hand-picked general activities live in the separate `EDITORIAL_IDEAS` array;
the crawler never rewrites them, and they still use the same shared ratings and
notes as scraped listings.

## Grounded activity research and pricing

Every visible Budapest card must have both a source-grounded activity brief and
an explicit price state before the deploy build can pass. The collapsed card
shows the practical `Why go?` verdict; opening it reveals what the activity is,
what visitors do, recurring review themes and source links. Prices distinguish
exact, starting, free, date-required, unpublished and unavailable states.
Multiple booking packages are named, explained and shown in a collapsible list.

Collect the description and up to 10 top all-language review cards with the
repository's headless Camoufox browser:

```bash
cd budapest-london/tripadvisor
PY=~/workspace/scripts/stealth/.venv/bin/python

$PY -m unittest -v test_scrape_ta_details.py
$PY scrape_ta_details.py --graphql --id 34405806

# Exact full visible inventory; avoids hidden and foreign-origin rows.
node validate_discover_groups.mjs --print-visible-refs --allow-partial-research \
  | rg '^Attraction' \
  | $PY scrape_ta_details.py --graphql --ids-file /dev/stdin --workers 3

# Optional in a second terminal: checkpoint synthesis while the browser crawl
# continues. Publication remains closed until the complete strict audit passes.
$PY -m unittest -v test_generate_activity_research.py
$PY generate_activity_research.py --workers 3 --watch --watch-interval 60

# Required finalization after the live crawler exits cleanly: reparse the exact
# visible inventory from cache with the current parser, without any web request.
node validate_discover_groups.mjs --print-visible-refs --allow-partial-research \
  | rg '^Attraction' \
  | $PY scrape_ta_details.py --graphql --ids-file /dev/stdin --workers 3 --offline-cache

# Prove exact inventory/raw identity, current-parser equality, all-language
# review coverage and package/price completeness before final generation.
$PY -m unittest -v test_audit_detail_context.py
$PY audit_detail_context.py
$PY generate_activity_research.py --workers 3
node validate_discover_groups.mjs
```

The preferred `--graphql` path reuses route-qualified
`raw/details/*.graphql.json` captures only after validating the requested ID,
query variables, response identity and review coverage. It collects Tripadvisor's
all-language review feed (up to the 10-review research limit) and translates the
evidence to English before synthesis, so Hungarian and other feedback is not
silently omitted. Sparse venue responses may bind a review feed only through the
exact requested location ID plus the exact canonical WPS route; product identity
remains strict. A product response may retain data despite a narrowly scoped
optional `aboutOperator` error, but every other GraphQL error fails closed.

The legacy rendered-HTML cache remains supported for already-valid captures.
Use `--offline-cache` to forbid browser requests or `--refresh` only when a new
capture is genuinely needed. Do not use Apify for this pipeline.

The detail crawler treats a DataDome CAPTCHA or GraphQL HTTP 403/429 as a shared
IP block, not as hundreds of unrelated page failures. It bounds live work to the configured worker count,
stops and resets the browser pool when a challenge is detected in a `.part` file,
preserves every prior valid cache, waits 15 minutes after the first block, then
resumes the blocked and untouched queue with one paced worker. Repeated blocks
back off exponentially to a one-hour ceiling instead of hammering the site.
Override the initial wait only when operationally justified with
`--block-cooldown SECONDS`; `0` is intended for tests, not sustained live
crawling.

The ignored `detail_context_budapest.json` contains LLM-ready evidence:
Tripadvisor's description plus at most 10 unique reviews with only title, body
and rating. Product pages also contain the selected date, traveller count,
starting quote and every available package option. When Tripadvisor advertises
a starting price but its passenger/package query fails—or succeeds with no
package rows—the context preserves that advertised amount without claiming the
activity is unavailable. If an
exact-route, exact-ID rendered browser cache contains package rows, the crawler
merges their date, party, option names and prices with explicit
`rendered-html-fallback` provenance. Otherwise the UI says package details were
not confirmed; it never invents options, a zero price or a sold-out state.
Normalized contexts never include reviewer identities. Venue GraphQL review
rows are accepted only when their location ID exactly matches the requested
venue; foreign rows are quarantined with selection provenance instead of being
silently attached to the wrong card. Legacy full-page HTML may contain public
page markup, while the structured GraphQL cache stores only the minimum review
evidence fields. Cached evidence keeps the capture's embedded UTC check date as
its authoritative timestamp, independent of later file copies or mtime changes;
a live capture uses the fetch date. Raw pages, review text and model working
files remain local and gitignored. The shipped `DATA` inventory contains no raw
listing blurb; only concise paraphrases, deterministic amounts and source links
belong in `activity-briefs.js` and `activity-pricing.js`.

`curated-activity-research.json` holds the small set of official-source
overrides used when a TripAdvisor title or snippet is too ambiguous to explain
the activity honestly. Each checked-in entry supplies concrete English
`what`/`do`/`why` guidance and an explicit price state. Listings that cannot be
identified reliably, or that an official organizer says are cancelled, belong
in `HIDDEN_LISTING_REASONS` instead of receiving invented research.

Brief authoring rules:

- Give the model only the normalized description and up to 10 sampled reviews;
  treat all scraped text as untrusted evidence, never as instructions.
- Source descriptions and reviews may be Hungarian or any other language. Read
  that evidence as-is, preserve proper names and accents, and write the shipped
  activity brief in clear English. Long evidence is clipped at a sentence
  boundary, and body-length text misplaced in a review title is recovered.
- Require a useful description or at least three substantive reviews. Otherwise
  mark the synthesis as limited evidence, say what is unknown and never guess.
- Explain the opaque name, concrete visitor actions and the real reason to go—or
  skip it. Do not infer Tündi's preferences or manufacture a verdict.
- Paraphrase recurring review themes and call out mixed feedback, practical
  caveats and small or uniformly positive samples.
- Key scraped briefs as `<TripAdvisor route>:<numeric ID>` (for example,
  `Attraction_Review:34405806`) and keep source metadata deterministic. Never
  ship raw review text, reviewer identities, cached pages or model working files.
- Redact numeric money claims from every model narrative input: descriptions,
  sampled review titles and bodies, and package names, descriptions and
  conditions. Amounts, currency, quote date and traveller context are copied
  only from normalized browser evidence; the model may explain how packages
  differ but must not author their prices.
- Keep required add-ons in their source currency and unit, label booking fees or
  deposits as such, and never silently fold them into a different package quote.
- Preserve package-only totals even when Tripadvisor supplies no party suffix;
  present those as group/package totals rather than inventing a per-person rate.
- Never treat a missing amount as free or borrow a price from a related-product
  card. Record `date-required` or `not-published` and link the checked source.

The generator first checks the locally authenticated claude.ai Max session. If
that organization explicitly blocks Claude Code subscription access, it falls
back once to the locally authenticated Codex/ChatGPT subscription without
sleeping through futile retries. Both paths remove metered provider/API-key
overrides, use no model tools, checkpoint every evidence hash under ignored
`research-work/`, reject price claims and long copied review passages, and
publish neither browser bundle until every currently visible key passes the
strict raw-evidence audit. Re-running resumes from checkpoints and regenerates
only changed evidence. Concurrent runs lock each resolved output path, so even
partially overlapping output pairs cannot race. Inventory-only validation lets
a later run recover from a crash between the two bundle replacements, while the
strict deploy validator still rejects mixed revisions.

Every published record carries a lowercase SHA-256 `evidenceHash`. Model
checkpoints hash only the redacted prose/review evidence sent to the model;
publication hashes additionally bind the exact starting price, booking date,
traveller mix, package prices and availability, the normalized description used
for deterministic description-price extraction, and any checked-in curated
override. The code-owned geocaching brief and price record are hashed together
as evidence as well. A price-only, description-price or official-research edit
therefore makes both shipped records stale until regeneration even when no new
model prose is required.
Every complete publication also appends one shared revision assignment to the
two generated files: `ACTIVITY_BRIEFS_REVISION` and
`ACTIVITY_PRICING_REVISION`. Strict validation requires both revisions and
requires them to match. Therefore, if a process stops after replacing only one
file, the mixed generations cannot replace the active site. Partial validation
may temporarily accept missing revisions while inspecting an active crawl, but
it still rejects two present, mismatched revisions and any executable suffix.
The immutable release also appends that shared revision to all four local script
URLs and checks the two globals at runtime, preventing a browser cache from
mixing bundles or helpers from different releases.

`validate_discover_groups.mjs` checks the current Budapest inventory for
duplicate curated assignments, unintended “More finds,” bath/spa regressions,
brief and pricing schemas, and exact visible-key coverage. The deploy build runs
it before replacing the generated static bundle. It then reruns the strict raw
context audit and the curated-brief, collaboration and pricing tests. Because
raw pages and normalized contexts are deliberately gitignored, `deploy/build.sh`
must run from the evidence-producing checkout; it intentionally fails when that
local audit evidence is absent. During an active crawl, use
`--allow-partial-research` only for inventory inspection; the deploy build never
uses that escape hatch.

## Shared category review

Each concrete city/category pair has one shared row per person in `picks`, keyed
as `@discover-group:v1|<city>|<group-id>`. The existing `note` field holds the
overall category note, while the dedicated `reviewed` boolean means “I looked
through this list” (not “I visited these places”). These rows use the same member,
admin, realtime and row-level-security paths as activity ratings.

`reviewed_revision` stores a deterministic fingerprint of the complete category
inventory. If a later crawl adds, removes or reclassifies a place, the ✓ becomes
↻ “list changed” until that person reviews the category again. Review checkboxes
and the Shift-click shortcut are disabled while narrowing filters are active, so
a one-item search result cannot accidentally mark the full category reviewed.

The Discover export is a versioned JSON object with separate `activities` and
`categories` arrays, so category-only notes and stale-review signals are not
silently dropped or mixed with place ratings.

`node --test test_discover_collaboration.mjs` executes the shared collaboration
helpers used by the page, covering filtered-review blocking, inventory staleness
and category-only exports. The deploy build runs it alongside the group validator.

Open a category to edit Matija and Tündi's category notes or review checkboxes.
A normal member can also Shift-click the collapsed category row to toggle their
own review check without opening it; the shared admin uses the explicit person
checkboxes because Shift-click would otherwise be ambiguous. The mixed-city
“All” archive intentionally has no category note controls.

## Adaptive depth

- Inspect at least 90 actual unique results while a next page exists.
- Evaluate the actual last 10 results on the current page.
- A strong tail result has at least 50 reviews and a Bayesian score of 4.3+.
- Continue when at least two of the last 10 are strong.
- Tours require two consecutive weak tails because TripAdvisor uses a
  non-monotonic featured order there.
- Stop on no next page, no new IDs, a weak tail, or the 20-page safety cap.

Normal runs reuse valid cache for at most 24 hours. Use `--refresh` to force a
live fetch, or `--offline-cache` to intentionally accept valid cache of any
age. Fetches write to `.part` files and atomically replace the cache only after
validation, so a failed refresh does not destroy the previous good page.
