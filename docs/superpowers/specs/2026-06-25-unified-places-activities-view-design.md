# Unified "Places & Activities" view — design spec

**Date:** 2026-06-25
**Status:** approved (design); awaiting spec review → implementation plan
**Project:** Matija & Tündi trip site (`~/workspace/personal/projects/matija-and-tundi-trip-planing/`)

## 1. Problem & goal

Today there are two overlapping browse pages:

- **`saved-places.html`** — data-driven (`HU`/`UK` arrays), 118 places, photos/carousels, `src[]` source tags, Claude verdicts, and verdict+category+source filters. **No voting, no notes, no persistence.**
- **`activities.html`** — ~50 hand-authored experience cards, checkbox shortlist with `localStorage` persistence + a "view my picks" modal. Several cards are tagged `📍 saved` = duplicates of saved-places entries.

Seeing one place under "saved" and a related one under "activities" is confusing — they should be **one view**. The goal: a single, source-aware, votable, note-able grid where Matija & Tündi can react to every place/activity in one place.

## 2. Scope

### In scope (v1)
1. **Merge** the activity-picker items into the saved-places data model so there is one grid.
2. **Source as the primary filter axis** + a **"🔥 N sources" corroboration badge** + a **Sort** control.
3. **Dual voting** — two independent 5★ rows per card (Matija, Tündi), persisted.
4. **Notes** — one freeform note per card, persisted.
5. Retire `activities.html` (→ redirect) and update nav.

### Out of scope (explicit non-goals)
- **Per-source enrichment / scraping** (real per-source ratings, review counts, blurbs) — deferred to a separate v2.
- **Cross-device vote sync** — `localStorage` is per-browser (see §3). True sync needs a backend; v2.
- **Wiring `trip-plan.html` to read the votes** — nice follow-up, not this build.
- **Photographing the newly-merged activity cards** — they ship with emoji placeholders; photo grind is a later pass (same approach as the UK data pass).

## 3. Key constraint — persistence is per-browser

The site is static (GitHub Pages, no backend). Votes and notes save to `localStorage`, which is **scoped to one browser on one device**. Therefore:

- **v1 behaviour:** Matija & Tündi both tap stars on the **same device/browser** while planning together. Their two star-rows are independent fields in the same record — "separate votes" is satisfied; what is *not* satisfied is syncing those votes between two phones.
- This is an accepted limitation for v1, documented in-UI with a small footnote ("saved in this browser").
- v2 option (not now): a small serverless KV store keyed by a shared trip id.

## 4. Architecture

**Evolve `saved-places.html` in place.** It already owns the data arrays, the filter engine, and the photo-carousel code — the unified view is a superset of what it does. A from-scratch file would discard that working engine. The filename stays `saved-places.html` (keeps the live GitHub Pages URL stable); only the page title/heading changes to **"Places & Activities"**.

All logic remains a single self-contained HTML file (inline `<style>` + `<script>`), consistent with the rest of the site. No build step, no dependencies.

## 5. Data model

### 5.1 Entry shape (unchanged, additive)
Entries keep the existing shape:
```
{n, img?, e?, r?, v, cat:[...], src:[...], what, why}
```
- `n` name · `img` slug string or array (carousel) · `e` emoji fallback · `r` rating string · `v` verdict (`must|worth|maybe|skip`) · `cat[]` category keys · `src[]` source keys · `what`/`why` copy.
- No new required fields. Merged activities that lack a photo simply omit `img` and rely on `e` (exactly like the UK entries did before their photo grind).

### 5.2 Stable id (for persistence)
Computed at runtime, no data churn:
```
pid(p) = Array.isArray(p.img) ? p.img[0] : (p.img || slugify(p.n))
```
- Places already have an `img` slug → stable id.
- Img-less activities → `slugify(n)`. (Renaming an img-less item would orphan its saved votes — minor, acceptable for v1.)
- `slugify`: lowercase, strip accents, non-alphanumerics → `-`, collapse repeats, trim.

### 5.3 Merging the activities
Fold the `activities.html` items into the same `HU`/`UK` arrays (London activities → `UK`, Hungary activities → `HU`).

Rules:
- **Dedupe:** any activity tagged `📍 saved` already exists as a place → **drop the activity card** (the place already represents it). No double cards.
- **New experiences** (e.g. Quad/ATV tour, Hamilton, Crystal Maze LIVE, ABBA Voyage, Lee Valley rafting, axe-throwing, ruin bars, Danube cruise…) become **new entries**:
  - `cat`: mapped to existing `CATS` keys where possible (adrenaline, escape, shows, attractions, vr, bars, boats, food…). **Add new `CATS` keys only where needed** — at minimum `waterpark:{e:"🌊",l:"Water parks & slides"}` (currently has no home).
  - `src`: default **`["claude"]`** (they were curated suggestions). Upgrade to `["getyourguide"]` / `["fever"]` / `["web"]` where the original hook makes the source obvious (e.g. GetYourGuide tours).
  - `v` (verdict): assigned by Claude at implementation time (default `worth`, `maybe` for clearly-optional ones). **User reviews these verdicts** — same review step as the UK data pass.
  - `e`: a fitting emoji; no `img` yet.
  - `what`/`why`: ported/condensed from the existing activity hook text.
- A category section continues to group by `(p.cat||[])[0]` (first category = primary), so each merged activity needs a sensible first category.

### 5.4 Persistence schema
```
localStorage["matidi-picks-v1"] = JSON.stringify({
  [id]: { m: 0..5, t: 0..5, note: "" }
})
```
- Only non-empty records are stored (a record with `m=0,t=0,note=""` is pruned).
- The old `matidi-activities-v1` checkbox data is **not migrated** (different model, low value); the old picks simply don't carry over.

## 6. UI / behaviour

The **Hungary / UK country tabs stay** as the top-level switch (orthogonal to sort/filter); everything below operates within the active tab.

### 6.1 Source-primary filtering
- The filter bar reorders so the **source chips are the first, most prominent row** (📍 Your list / 🧠 Claude / ⭐ Google / 🦉 TripAdvisor / 🎡 GetYourGuide / 🎟️ Fever / …). Verdict + category chip rows move below.
- Multi-select behaviour and the existing "present sources only" chip-building are unchanged.

### 6.2 Corroboration badge
- Each card with `src.length >= 2` shows a **`🔥 N sources`** badge (N = number of sources). Single-source cards show no badge.

### 6.3 Sort control
A small segmented control / `<select>` above the results:
- **By category** (default) — current grouped-by-category layout with section headers.
- **Most sources** — flat grid, descending `src.length` (ties → rating, then name).
- **Top rated** — flat grid, descending `r` (entries without `r` sort last).

Category section headers appear only in "By category" mode; the other two render a single flat grid.

### 6.4 Card layout (additions to the existing card)
The existing card (photo/carousel + verdict badge + name + rating + category tags + what/why + source badges) gains, below the source badges:

```
┌─────────────────────────────────────────┐
│  [photo / emoji]            🟢 Must-see   │
│  🔥 3 sources                              │
│  Borough Market                    ★ 4.6  │
│  🛍️ Markets   🧀 Food                      │
│  London's best food market — …            │
│  Verdict: A foodie must. Go hungry…       │
│  📍 Your list   ⭐ Google   🦉 TripAdvisor │
│  ───────────────────────────────────────  │
│  Matija   ★ ★ ★ ☆ ☆                        │
│  Tündi    ★ ★ ★ ★ ☆                        │
│  📝 note ▾   (textarea when expanded)      │
└─────────────────────────────────────────┘
```
- **Two 5★ rows**, labelled `Matija` / `Tündi`. Tap star *k* → set that person's rating to *k*; tap the currently-set star → clear to 0. Filled vs hollow stars reflect stored value. Each change persists immediately.
- **Note:** a `📝 note ▾` toggle reveals a `<textarea>`; persists on `blur`/`input` (debounced). A filled dot/marker shows when a note exists so it's visible while collapsed.

### 6.5 "Rated by us" filter (replaces the old picks modal)
- A lightweight filter/sort entry so the couple can see what they've reacted to: a toggle **"⭐ Our picks"** that shows only cards where `m>=4 || t>=4`. This is the spiritual replacement for `activities.html`'s "view my picks" modal; the full modal/"copy list" is not reproduced in v1.

## 7. Cleanup & nav
- Page heading/title → **"Places & Activities"** (filename stays `saved-places.html`).
- **`activities.html`** is replaced by a redirect to `saved-places.html` (same pattern as `deploy/index.html`). Done **after** its data is migrated.
- Nav links in `trip-plan.html` and `saved-places.html` updated: the separate "🎯 Activity picker" link either points to the merged page or is removed (two nav items: Trip plan, Places & Activities).
- `trip-plan.html` content itself is unchanged in v1 (reading the votes is a later follow-up).

## 8. Verification
Local preview (`python3 -m http.server 8799`) opened in Chrome MCP, plus targeted checks:
1. **Render:** all merged cards render; no broken images; emoji fallback shows for img-less activities.
2. **Merge correctness:** total card count = (existing places) + (new activities) − (deduped `📍 saved`); no duplicate cards; London activities under UK tab, Hungary under Hungary tab.
3. **Source-primary:** source chips are the top filter row; filtering by a source narrows correctly.
4. **Corroboration:** `🔥 N sources` badge shows iff `src.length>=2` and N is correct.
5. **Sort:** all three sort modes order correctly; category headers only in "By category".
6. **Voting persists:** set M/T stars on several cards, reload → values intact; clear works.
7. **Notes persist:** type a note, reload → text intact; collapsed marker shows.
8. **"Our picks" filter:** shows only `m>=4||t>=4` cards.
9. **Redirect:** `activities.html` redirects to the unified page; nav links resolve.

## 9. Open items for the user (review step)
- **Page name** — "Places & Activities" OK, or prefer something else (e.g. "Our Shortlist")?
- **Verdicts on the new activity cards** — Claude assigns defaults; user adjusts to taste afterward.
- **"Our picks" threshold** — `>=4★` by either person; adjustable.
