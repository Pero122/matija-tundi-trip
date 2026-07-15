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

`validate_discover_groups.mjs` checks the current Budapest inventory for
duplicate curated assignments, unmatched “More finds,” and bath/spa regressions.
The deploy build runs it before replacing the generated static bundle.

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
