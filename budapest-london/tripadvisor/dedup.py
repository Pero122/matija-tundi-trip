#!/usr/bin/env python3
"""
Dedup TA scrape vs existing trip-site cards + quality-score the NEW candidates.

Strict matching (a false "dup" hides a real find; a false "new" just gets skipped
during curation -> bias toward fewer dup-matches):
  - exact normalized match  -> dup
  - containment match ONLY if the shorter name is >=13 chars, >=2 words, and
    neither side is a generic activity-type (blocklist) -> dup

Quality score = Bayesian-weighted rating (pulls high-rating/low-review items down).

Output: candidates_<city>.json  (sorted, status=new|dup, score)
"""
import argparse
import json, re, unicodedata
from pathlib import Path

from build_review import classify

HERE = Path(__file__).resolve().parent
PRIOR_M, PRIOR_C = 50, 4.0   # Bayesian prior: 50 reviews of a 4.0 venue

# generic activity-type cards: never use for containment matching
GENERIC = {
    "go-karting", "bowling night", "axe-throwing bar", "bubble football",
    "indoor bouldering", "adventure mini-golf", "segway tour", "palinka tasting",
    "board-game cafe", "cat cafe", "karaoke bar", "trampoline park", "roller disco",
    "escape room", "escape rooms", "shooting range", "paintball", "go karts",
    "mini golf", "bowling", "spa", "thermal bath", "ruin bar", "ruin bars",
    "wine tasting", "beer tasting", "cooking class", "comedy club",
}

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(the|a|an|in|of|budapest|london|hungary|england)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()

EXPERIENCE_CATS = {"tours", "daytrip", "classes"}
CITY_KEYS = {"budapest": "HU", "london": "UK"}


def existing_kind(item):
    return "experience" if EXPERIENCE_CATS.intersection(item.get("cat", [])) else "venue"


def is_dup(ta_norm, ta_kind, existing):
    """Return an existing match without collapsing a product into a venue."""
    for orig, en, gen, kind in existing:
        if not en:
            continue
        if ta_norm == en:
            return orig
        # containment, guarded
        if gen or ta_kind != kind:
            continue
        short, long = sorted([ta_norm, en], key=len)
        if len(short) >= 13 and len(short.split()) >= 2 and short in long:
            return orig
    return None

def score(rating, reviews):
    if not rating:
        return 0.0
    v = reviews or 0
    return (v / (v + PRIOR_M)) * rating + (PRIOR_M / (v + PRIOR_M)) * PRIOR_C

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cities",
        nargs="+",
        choices=tuple(CITY_KEYS),
        default=["budapest"],
        help="cities to process (default: budapest)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with (HERE / "existing_names.json").open(encoding="utf-8") as handle:
        ex = json.load(handle)
    for city in args.cities:
        key = CITY_KEYS[city]
        existing = []
        for o in ex[key]:
            n = norm(o["n"])
            existing.append(
                (
                    o["n"],
                    n,
                    n in GENERIC or o["n"].lower() in GENERIC,
                    existing_kind(o),
                )
            )
        with (HERE / f"listings_{city}.json").open(encoding="utf-8") as handle:
            listings = json.load(handle)
        for L in listings:
            tn = norm(L["name"])
            m = is_dup(tn, classify(L), existing)
            L["status"] = "dup" if m else "new"
            L["matched"] = m
            L["score"] = round(score(L["rating"], L["reviews"]), 3)
        listings.sort(key=lambda L: L["score"], reverse=True)
        out = HERE / f"candidates_{city}.json"
        partial = out.with_name(out.name + ".part")
        partial.write_text(
            json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        partial.replace(out)
        new = [L for L in listings if L["status"] == "new"]
        dup = [L for L in listings if L["status"] == "dup"]
        # quality gates over NEW
        g1 = [L for L in new if L["rating"] and L["rating"] >= 4.3 and L["reviews"] >= 100]
        g2 = [L for L in new if L["rating"] and L["rating"] >= 4.0 and L["reviews"] >= 30]
        print(f"== {city}: {len(listings)} total | {len(new)} NEW | {len(dup)} dup")
        print(f"     NEW quality: >=4.3&100rev: {len(g1)}  |  >=4.0&30rev: {len(g2)}")
        print(f"     sample dups: {[d['matched'] for d in dup[:8]]}")
    print(f"\nWrote {', '.join(f'candidates_{city}.json' for city in args.cities)}")

if __name__ == "__main__":
    main()
