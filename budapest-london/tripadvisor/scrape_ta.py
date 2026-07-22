#!/usr/bin/env python3
"""
TripAdvisor category scraper for the Matija & Tündi trip site.

Discovery funnel: for each TripAdvisor parent category (c<NN>) per city, load
the first ~90 ranked listings, then keep paging only while the last 10 results
still contain several well-reviewed candidates. Pages are throttled via
Camoufox, parsed from the embedded GraphQL state blob, and deduplicated.

Run with the stealth venv:
  ~/workspace/scripts/stealth/.venv/bin/python scrape_ta.py --cities budapest

Output:
  raw/<city>_<cat>_oa<offset>.html   (gitignored, re-parse without re-fetch)
  listings_<city>.json               (gitignored structured data)

Per-listing fields: name, rating, reviews, rank, subtype, badge,
openStatus, closed(bool), url (webUrl), photos[] (1-2 from listing page),
city, cat (TA code), catLabel.
"""
import argparse
import re, json, time, html as H, urllib.parse, subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
CF = Path.home() / "workspace/scripts/stealth/.venv/bin/python"
CF_SCRAPE = Path.home() / "workspace/scripts/stealth/cf_scrape.py"

# --- config -----------------------------------------------------------------
CITIES = {
    "budapest": dict(geo="g274887", slug="Budapest_Central_Hungary"),
    "london":   dict(geo="g186338", slug="London_England"),
}
# TripAdvisor's parent category routes. Keep the original focused categories
# first so cross-category duplicates retain the most specific useful label.
CATS = {
    "c20": "Nightlife",
    "c36": "Food & Drink",
    "c40": "Spas & Wellness",
    "c41": "Classes & Workshops",
    "c55": "Boat Tours & Water Sports",
    "c56": "Fun & Games",
    "c57": "Nature & Parks",
    "c58": "Concerts & Shows",
    "c61": "Outdoor Activities",
    "c63": "Day Trips",
    "c42": "Tours",
    "c47": "Sights & Landmarks",
    "c49": "Museums",
    "c26": "Shopping",
    "c62": "Events",
    "c59": "Transportation",
    "c60": "Traveler Resources",
    # Small supplemental routes keep worthwhile niche results from being buried
    # deep inside their broader parent categories.
    "c48": "Zoos & Aquariums",
    "c52": "Water & Amusement Parks",
    "c53": "Casinos & Gambling",
}
PAGE_SIZE = 30
MIN_RESULTS = 90    # inspect at least 90 actual unique results when pages exist
MAX_PAGES = 20      # hard safety cap: at most 600 results per category
TAIL_SIZE = 10
TAIL_MIN_SCORE = 4.3
TAIL_MIN_REVIEWS = 50
TAIL_MIN_KEEPERS = 2
FETCH_RETRIES = 3
MIN_HTML_BYTES = 50_000
FETCH_DELAY = 5.5
CACHE_MAX_AGE_HOURS = 24
# Non-Budapest Hungarian detail-page geos currently returned by the Budapest
# category feeds. Unknown non-city geos are conservatively treated as foreign
# origin until reviewed, so Vienna/Bratislava departures do not leak in.
HUNGARY_GEOS = {"274887", "274890", "7923668"}
# ----------------------------------------------------------------------------


def bayesian_score(rating, reviews, prior_reviews=50, prior_rating=4.0):
    """Quality score that prevents tiny-review 5.0 listings driving paging."""
    if not rating:
        return 0.0
    reviews = reviews or 0
    return (
        (reviews / (reviews + prior_reviews)) * rating
        + (prior_reviews / (reviews + prior_reviews)) * prior_rating
    )


@dataclass(frozen=True)
class PageDecision:
    keep_going: bool
    tail_count: int
    keeper_count: int
    reason: str


@dataclass(frozen=True)
class PaginationPolicy:
    min_results: int = MIN_RESULTS
    max_pages: int = MAX_PAGES
    tail_size: int = TAIL_SIZE
    min_score: float = TAIL_MIN_SCORE
    min_reviews: int = TAIL_MIN_REVIEWS
    min_keepers: int = TAIL_MIN_KEEPERS

    def is_keeper(self, row):
        return (
            not row.get("closed")
            and
            (row.get("reviews") or 0) >= self.min_reviews
            and bayesian_score(row.get("rating"), row.get("reviews")) >= self.min_score
        )

    def decide(self, rows, page_number, new_count, total_unique, has_next):
        """Decide from the page tail; navigation/end-of-results wins first."""
        tail = rows[-self.tail_size:]
        keepers = sum(self.is_keeper(row) for row in tail)
        if not has_next:
            return PageDecision(False, len(tail), keepers, "TripAdvisor has no next page")
        if new_count == 0:
            return PageDecision(False, len(tail), keepers, "page added no new listing IDs")
        if page_number >= self.max_pages:
            suffix = " while the tail was still strong" if keepers >= self.min_keepers else ""
            return PageDecision(
                False,
                len(tail),
                keepers,
                f"reached {self.max_pages}-page safety cap{suffix}",
            )
        if total_unique < self.min_results:
            return PageDecision(
                True,
                len(tail),
                keepers,
                f"inspect at least {self.min_results} unique results ({total_unique} so far)",
            )
        if keepers >= self.min_keepers:
            return PageDecision(
                True,
                len(tail),
                keepers,
                f"{keepers}/{len(tail)} tail results are strong",
            )
        return PageDecision(
            False,
            len(tail),
            keepers,
            f"only {keepers}/{len(tail)} tail results are strong",
        )

def cat_url(geo, cat, slug, offset):
    oa = f"-oa{offset}" if offset else ""
    return f"https://www.tripadvisor.com/Attractions-{geo}-Activities-{cat}{oa}-{slug}.html"


def atomic_write_text(path, text):
    partial = path.with_name(path.name + ".part")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)


def listing_identity(row):
    """Stable identity: product and venue namespaces plus TripAdvisor detail ID."""
    match = re.search(
        r"/(AttractionProductReview|Attraction_Review)-g\d+-d(\d+)-",
        row.get("url", ""),
    )
    return (match.group(1), match.group(2)) if match else ("url", row.get("url", ""))


def origin_for(city, geo):
    """Classify where a result starts, separately from the feed it appeared in."""
    expected = CITIES[city]["geo"].removeprefix("g")
    if geo == expected:
        return city
    if city == "budapest" and geo in HUNGARY_GEOS:
        return "hungary-road-trip"
    return "foreign-origin"


def is_unavailable_status(status):
    """`Closed now` is a daily state; only explicit long-term closure counts."""
    return bool(re.search(r"\b(?:permanently|temporarily) closed\b", status or "", re.I))

def cache_looks_valid(path):
    if not path.exists() or path.stat().st_size <= MIN_HTML_BYTES:
        return False
    payload = path.read_bytes()
    return b"View%20details%20for" in payload or b'View details for' in payload


def cache_is_fresh(path, max_age_hours=CACHE_MAX_AGE_HOURS, now=None):
    if not cache_looks_valid(path):
        return False
    now = now or datetime.now(timezone.utc).timestamp()
    return now - path.stat().st_mtime <= max_age_hours * 3600


def fetch(
    url,
    dest: Path,
    refresh=False,
    offline_cache=False,
    cache_max_age_hours=CACHE_MAX_AGE_HOURS,
):
    """Fetch via Camoufox with retries; return (status, ok, fetched_live)."""
    if offline_cache:
        return (
            ("cached", True, False)
            if cache_looks_valid(dest)
            else ("FAIL (offline cache missing or invalid)", False, False)
        )
    if not refresh and cache_is_fresh(dest, cache_max_age_hours):
        return "cached", True, False

    errors = []
    partial = dest.with_name(dest.name + ".part")
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            partial.unlink(missing_ok=True)
            with partial.open("w") as handle:
                result = subprocess.run(
                    [str(CF), str(CF_SCRAPE), "html", url],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=180,
                )
            size = partial.stat().st_size
            if result.returncode == 0 and cache_looks_valid(partial):
                partial.replace(dest)
                suffix = f" after {attempt} attempts" if attempt > 1 else ""
                return f"fetched{suffix}", True, True
            errors.append(
                f"attempt {attempt}: rc={result.returncode} bytes={size} cards=no"
            )
        except subprocess.TimeoutExpired:
            errors.append(f"attempt {attempt}: timed out")
        if attempt < FETCH_RETRIES:
            time.sleep(attempt * 2)
    partial.unlink(missing_ok=True)
    return f"FAIL ({'; '.join(errors)})", False, True


def has_next_page(html_txt):
    """Rendered TA pages expose this stable accessibility/pagination marker."""
    return (
        'data-smoke-attr="pagination-next-arrow"' in html_txt
        or 'aria-label="Next page"' in html_txt
    )

def decode(txt):
    d = txt
    for _ in range(3):
        d = urllib.parse.unquote(d).replace("\\\\", "\\").replace('\\"', '"').replace("\\/", "/")
    return d

MARK = '"View details for '

def parse(html_txt, city, cat):
    d = decode(html_txt)
    # split into FULL card segments (one "View details for NAME" per card; the
    # subtype/photo/openStatus fields live AFTER the cardLink, so the segment
    # must run to the next card boundary, not stop at the webLinkUrl).
    starts = [m.start() for m in re.finditer(re.escape(MARK), d)] + [len(d)]
    out, seen = [], set()
    rank = 0
    for k in range(len(starts) - 1):
        seg = d[starts[k]:starts[k + 1]]
        nm = re.match(re.escape(MARK) + r'([^"]+)"', seg)
        # The card's first link is the identity. Tour/product cards then contain
        # a later Attraction_Review operator link; accepting only that old venue
        # format collapsed several distinct products onto the same operator.
        um = re.search(
            r'"webLinkUrl":"(/(AttractionProductReview|Attraction_Review)-g(\d+)-d(\d+)-[^"]*?\.html)"',
            seg,
        )
        if not nm or not um:
            continue
        name = H.unescape(nm.group(1)).strip()
        url, route_kind, geo, did = um.group(1), um.group(2), um.group(3), um.group(4)
        identity = (route_kind, did)
        if identity in seen:
            continue
        seen.add(identity)
        rank += 1
        rm = re.search(r'"rating":([0-9.]+),"reviewCount":(\d+)', seg)
        sub = re.search(r'"primaryInfo":\{[^}]*?"text":"([^"]+)"', seg)
        op = re.search(r'"openStatus":\{[^}]*?"text":"([^"]*)"', seg)
        bm = re.search(r'"badge":\{[^}]*?"text":"([^"]+)"', seg)
        # venue photos live in the media zone BEFORE the review-contributor section;
        # cutting there avoids grabbing reviewer avatars. Also drop avatar/profile junk.
        photo_zone = re.split(r'"(?:reviewSnippet|FlexCardContributor|ReviewProfileBrief)"', seg)[0]
        photos = [p for p in dict.fromkeys(
            re.findall(r'"urlTemplate":"(https://[^"]+?\.jpg[^"]*)"', photo_zone))
            if "avatar" not in p and "/profile" not in p][:2]
        # Keep listing descriptions local for research, but never substitute a
        # review snippet. Review prose must not leak into the shipped discovery
        # inventory when a venue has no description.
        desc = re.search(r'"descriptiveText":\{[^}]*?"text":"([^"]+)"', seg)
        raw_blurb = desc.group(1) if desc else ""
        blurb = H.unescape(re.sub("[￹-￻]", "", raw_blurb))
        blurb = re.sub(r"\\[nrt]|\s+", " ", blurb).strip()
        if len(blurb) > 170:
            blurb = blurb[:168].rsplit(" ", 1)[0] + "…"
        openst = op.group(1) if op else ""
        closed = is_unavailable_status(openst)
        out.append(dict(
            name=name,
            rating=float(rm.group(1)) if rm else None,
            reviews=int(rm.group(2)) if rm else 0,
            rank=rank,
            subtype=sub.group(1) if sub else "",
            badge=bm.group(1) if bm else "",
            openStatus=openst,
            closed=closed,
            blurb=blurb,
            url="https://www.tripadvisor.com" + url,
            photos=[p.replace("{width}", "400").replace("{height}", "300") for p in photos],
            city=city,
            geo=geo,
            origin=origin_for(city, geo),
            cat=cat,
            catLabel=CATS[cat],
        ))
    return out

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cities",
        nargs="+",
        choices=tuple(CITIES),
        default=["budapest"],
        help="cities to crawl (default: budapest)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_PAGES,
        help=f"hard page cap per category (default: {MAX_PAGES})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore valid cached HTML and fetch every inspected page again",
    )
    parser.add_argument(
        "--offline-cache",
        action="store_true",
        help="allow valid cached HTML regardless of age (never use for a refresh)",
    )
    parser.add_argument(
        "--cache-max-age-hours",
        type=float,
        default=CACHE_MAX_AGE_HOURS,
        help=f"reuse fresh cache for this many hours (default: {CACHE_MAX_AGE_HOURS})",
    )
    args = parser.parse_args(argv)
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    if args.cache_max_age_hours < 0:
        parser.error("--cache-max-age-hours cannot be negative")
    if args.refresh and args.offline_cache:
        parser.error("--refresh and --offline-cache are mutually exclusive")
    return args


def adjust_category_decision(
    policy, cat, decision, weak_tail_streak, total_unique, has_next, new_count
):
    """Confirm one noisy weak Tours tail before stopping a featured feed."""
    quality_stop = (
        total_unique >= policy.min_results
        and decision.reason.startswith("only ")
        and has_next
        and new_count > 0
    )
    if quality_stop:
        weak_tail_streak += 1
        if cat == "c42" and weak_tail_streak < 2:
            decision = PageDecision(
                True,
                decision.tail_count,
                decision.keeper_count,
                "first weak Tours tail; confirm with one more page",
            )
    elif decision.keeper_count >= policy.min_keepers:
        weak_tail_streak = 0
    return decision, weak_tail_streak


def merge_failed_categories(previous, by_id, all_rows, failed_categories):
    """Restore failed category memberships without discarding fresh categories."""
    preserved = 0
    for prior in previous:
        failed_labels = [
            CATS[cat]
            for cat in failed_categories
            if prior.get("cat") == cat or CATS[cat] in prior.get("alsoCats", [])
        ]
        if not failed_labels:
            continue
        identity = listing_identity(prior)
        current = by_id.get(identity)
        if current is None:
            by_id[identity] = prior
            all_rows.append(prior)
            preserved += 1
            continue
        current.setdefault("alsoCats", [])
        for label in failed_labels:
            if label != current.get("catLabel") and label not in current["alsoCats"]:
                current["alsoCats"].append(label)
    return preserved


def main(argv=None):
    args = parse_args(argv)
    policy = PaginationPolicy(max_pages=args.max_pages)
    RAW.mkdir(exist_ok=True)
    live = 0
    had_failures = False
    for city in args.cities:
        c = CITIES[city]
        outp = HERE / f"listings_{city}.json"
        previous = json.loads(outp.read_text()) if outp.exists() else []
        all_rows, by_id = [], {}
        failed_categories = set()
        for cat in CATS:
            category_seen = set()
            weak_tail_streak = 0
            print(f"\n-- {city}: {CATS[cat]} ({cat}) --")
            for page_index in range(policy.max_pages):
                page_number = page_index + 1
                off = page_index * PAGE_SIZE
                url = cat_url(c["geo"], cat, c["slug"], off)
                dest = RAW / f"{city}_{cat}_oa{off}.html"
                status, ok, fetched_live = fetch(
                    url,
                    dest,
                    refresh=args.refresh,
                    offline_cache=args.offline_cache,
                    cache_max_age_hours=args.cache_max_age_hours,
                )
                print(f"  [{city} {cat} oa{off}] {status}")
                if fetched_live:
                    live += 1
                    time.sleep(FETCH_DELAY)
                if not ok:
                    failed_categories.add(cat)
                    print("    stop: fetch failed after retries; preserving prior rows for this category")
                    break

                html_txt = dest.read_text(errors="replace")
                rows = parse(html_txt, city, cat)
                next_page = has_next_page(html_txt)
                if not rows:
                    failed_categories.add(cat)
                    print("    stop: valid page returned no parsed cards; preserving prior rows")
                    break

                for row in rows:
                    row["rank"] = off + row["rank"]
                category_new = [
                    row for row in rows if listing_identity(row) not in category_seen
                ]
                category_seen.update(listing_identity(row) for row in category_new)
                added_to_city = 0
                # dedup within city (a venue can appear in 2 categories) - keep first/highest rank
                for r in category_new:
                    key = listing_identity(r)
                    if key not in by_id:
                        by_id[key] = r
                        all_rows.append(r)
                        added_to_city += 1
                    else:                          # already seen: record extra category
                        prev = by_id[key]
                        prev.setdefault("alsoCats", [])
                        if r["catLabel"] not in prev["alsoCats"] and r["catLabel"] != prev["catLabel"]:
                            prev["alsoCats"].append(r["catLabel"])

                decision = policy.decide(
                    rows,
                    page_number=page_number,
                    new_count=len(category_new),
                    total_unique=len(category_seen),
                    has_next=next_page,
                )
                decision, weak_tail_streak = adjust_category_decision(
                    policy,
                    cat,
                    decision,
                    weak_tail_streak,
                    total_unique=len(category_seen),
                    has_next=next_page,
                    new_count=len(category_new),
                )
                action = "continue" if decision.keep_going else "stop"
                print(
                    f"    {len(rows)} cards, {len(category_new)} new in category, "
                    f"{added_to_city} new in city; tail keepers "
                    f"{decision.keeper_count}/{decision.tail_count} -> {action}: {decision.reason}"
                )
                if not decision.keep_going:
                    break

        # A failed category must not erase its last known-good rows. Other
        # categories still refresh normally, so a single flaky page cannot
        # throw away the expansion completed in the same run.
        preserved = merge_failed_categories(
            previous, by_id, all_rows, failed_categories
        )
        if failed_categories:
            had_failures = True
            labels = ", ".join(CATS[cat] for cat in sorted(failed_categories))
            print(f"!! partial crawl: {labels}; preserved {preserved} prior rows")

        atomic_write_text(outp, json.dumps(all_rows, ensure_ascii=False, indent=2))
        rated = [r for r in all_rows if r["rating"]]
        print(f"== {city}: {len(all_rows)} unique listings "
              f"({len(rated)} rated, {sum(r['closed'] for r in all_rows)} closed) -> {outp.name}")
    print(f"\nDone. Live Camoufox fetches this run: {live}")
    return 2 if had_failures else 0

if __name__ == "__main__":
    raise SystemExit(main())
