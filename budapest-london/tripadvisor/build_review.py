#!/usr/bin/env python3
"""Refresh selected cities in Discover without overwriting its application code.

The checked-in ``index.html`` is the UI source of truth. This builder replaces
only ``const DATA=...``. A Budapest-only refresh preserves the embedded London
archive, so ignored intermediate files are not required in a clean clone.
"""

import argparse
import json
import re
import subprocess
import urllib.parse
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent
CITIES = ("budapest", "london")
TOUR_SUB = re.compile(
    r"tour|cruise|crawl|safari|sightseeing|day trip|transfer|lesson|workshop|class",
    re.I,
)
TOUR_NAME = re.compile(
    r"\btour\b|\bcrawl\b|\bcruise\b|\bexperience\b|\btasting\b|\bclass\b|"
    r"transfer|sightseeing|guided|\bticket\b|admission|\bpass\b",
    re.I,
)
DATA_BLOCK = re.compile(r"const DATA=(\[.*?\]);\nconst SB_URL=", re.S)
TA_ID = re.compile(r"/(AttractionProductReview|Attraction_Review)-g\d+-d(\d+)-")
# Reviewed source-title repairs keyed by TripAdvisor identity. Keep these here,
# rather than in the UI, so every future rebuild emits the corrected title.
TITLE_OVERRIDES = {
    "AttractionProductReview:21032018": "The Puszta Horse Show",
}
# Reviewed one-time migrations where TripAdvisor changed both the route identity
# and product title, so neither identity nor exact-title matching is sufficient.
LEGACY_ALIAS_TARGETS = {
    "budapest|Budapest Centre Food Tour with 10+ Tastings, Wine & Street Food": "AttractionProductReview:14120801",
    "budapest|Bingo Bar Crawl|ta:20089459": "AttractionProductReview:20190065",
    "budapest|The Original Budapest Pub Crawl – Free Shots, Games & VIP Entry": "AttractionProductReview:11480515",
    "budapest|Budapest Nighttime Ruin Bars Crawl with Free Shots and VIP Entry": "AttractionProductReview:34168913",
    "budapest|Tokaj wineregion home to the worldfamous aszu, on a private tour!": "AttractionProductReview:19645432",
}


def classify(listing):
    """Keep the existing venue/experience distinction used by the filters."""
    return (
        "experience"
        if "AttractionProductReview" in listing["url"]
        or TOUR_SUB.search(listing.get("subtype", ""))
        or TOUR_NAME.search(listing.get("name", ""))
        else "venue"
    )


def listing_identity(listing):
    match = TA_ID.search(listing.get("url", ""))
    return f"{match.group(1)}:{match.group(2)}" if match else listing.get("url", "")


def listing_id(listing):
    match = TA_ID.search(listing.get("url", ""))
    return match.group(2) if match else ""


def listing_title(listing):
    """Return a reviewed title repair without mutating scraped source data."""
    return TITLE_OVERRIDES.get(listing_identity(listing), listing["name"])


def parse_embedded_data(html):
    match = DATA_BLOCK.search(html)
    if not match:
        raise RuntimeError("Could not find exactly one embedded DATA block in index.html")
    return json.loads(match.group(1))


def legacy_place_keys(data):
    """Reproduce the former city|name key ownership for cloud-data aliases."""
    groups = defaultdict(list)
    for item in data:
        groups[f"{item['city']}|{item['n']}"].append(item)
    result = {}
    for base, items in groups.items():
        primary = min(
            items,
            key=lambda item: int(listing_id(item)) if listing_id(item) else 2**63,
        )
        for item in items:
            suffix = listing_id(item) or urllib.parse.quote(item.get("url", ""), safe="")
            result[listing_identity(item)] = base if item is primary else f"{base}|ta:{suffix}"
    return result


def historical_aliases(snapshots, target_data):
    """Assign old keys once, bridging corrected operator URLs to product URLs."""
    target_by_name = defaultdict(list)
    for item in target_data:
        target_by_name[(item.get("city"), item.get("n"))].append(item)

    def snapshot_destinations(data):
        source_by_name = defaultdict(list)
        for item in data:
            source_by_name[(item.get("city"), item.get("n"))].append(item)
        destinations = {}
        for name_key, sources in source_by_name.items():
            targets = target_by_name.get(name_key, [])
            if len(targets) == 1:
                destination = listing_identity(targets[0])
                destinations.update(
                    (listing_identity(source), destination) for source in sources
                )
                continue
            if targets:
                remaining_sources = list(sources)
                remaining_targets = list(targets)
                for source in list(remaining_sources):
                    identity = listing_identity(source)
                    exact = next(
                        (target for target in remaining_targets if listing_identity(target) == identity),
                        None,
                    )
                    if exact is not None:
                        destinations[identity] = identity
                        remaining_sources.remove(source)
                        remaining_targets.remove(exact)
                if len(remaining_sources) == len(remaining_targets):
                    remaining_sources.sort(key=lambda item: int(listing_id(item) or 2**63))
                    remaining_targets.sort(key=lambda item: int(listing_id(item) or 2**63))
                    for source, target in zip(remaining_sources, remaining_targets):
                        destinations[listing_identity(source)] = listing_identity(target)
                    continue
            for source in sources:
                identity = listing_identity(source)
                destinations.setdefault(identity, identity)
        return destinations

    owners = {}
    for data in snapshots:
        old_keys = legacy_place_keys(data)
        destinations = snapshot_destinations(data)
        for item in data:
            old_identity = listing_identity(item)
            destination = destinations[old_identity]
            for alias in [*item.get("aliases", []), old_keys[old_identity]]:
                owners.setdefault(alias, destination)
    target_ids = {listing_identity(item) for item in target_data}
    for alias, destination in LEGACY_ALIAS_TARGETS.items():
        if destination in target_ids:
            owners[alias] = destination
    by_identity = defaultdict(list)
    for alias, identity in owners.items():
        by_identity[identity].append(alias)
    return by_identity


def unresolved_legacy_aliases(snapshots, target_data, aliases):
    target_ids = {listing_identity(item) for item in target_data}
    emitted = {
        alias
        for identity, identity_aliases in aliases.items()
        if identity in target_ids
        for alias in identity_aliases
    }
    expected = {
        alias
        for snapshot in snapshots
        for alias in legacy_place_keys(snapshot).values()
    }
    return sorted(expected - emitted)


def load_git_head_snapshot():
    """Best-effort one-time migration source for pre-canonical cloud keys."""
    repo = HERE.parents[1]
    relative = (HERE / "index.html").relative_to(repo)
    result = subprocess.run(
        ["git", "show", f"HEAD:{relative.as_posix()}"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        return []
    try:
        return parse_embedded_data(result.stdout)
    except (RuntimeError, json.JSONDecodeError):
        return []


def load_candidates(cities):
    data = []
    for city in cities:
        source = HERE / f"candidates_{city}.json"
        with source.open(encoding="utf-8") as handle:
            listings = json.load(handle)
        for listing in listings:
            if listing["status"] != "new":
                continue
            data.append(
                {
                    "n": listing_title(listing),
                    "r": listing["rating"],
                    "rv": listing["reviews"],
                    "sub": listing["subtype"],
                    "badge": listing["badge"],
                    "url": listing["url"],
                    "img": listing["photos"][0] if listing["photos"] else "",
                    "city": city,
                    "geo": listing.get("geo", ""),
                    "origin": listing.get("origin", city),
                    "cat": listing["catLabel"],
                    "cats": list(
                        dict.fromkeys(
                            [listing["catLabel"], *listing.get("alsoCats", [])]
                        )
                    ),
                    "type": classify(listing),
                    "score": listing["score"],
                }
            )
    return data


def merge_city_data(existing, replacements, cities):
    selected = set(cities)
    return [item for item in existing if item.get("city") not in selected] + replacements


def script_safe_json(value):
    """Serialize inline-script JSON without allowing a literal closing tag."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def replace_data_block(html, data):
    replacement = f"const DATA={script_safe_json(data)};\nconst SB_URL="
    updated, replacements = DATA_BLOCK.subn(lambda _: replacement, html, count=1)
    if replacements != 1:
        raise RuntimeError("Could not find exactly one embedded DATA block in index.html")
    return updated


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cities",
        nargs="+",
        choices=CITIES,
        default=["budapest"],
        help="cities to replace (default: budapest; other embedded cities are preserved)",
    )
    return parser.parse_args(argv)


def build(cities=("budapest",), page=None):
    page = page or HERE / "index.html"
    html = page.read_text(encoding="utf-8")
    existing = parse_embedded_data(html)
    head = load_git_head_snapshot() if page == HERE / "index.html" else []
    snapshots = [snapshot for snapshot in (head, existing) if snapshot]
    data = merge_city_data(existing, load_candidates(cities), cities)
    aliases = historical_aliases(snapshots, data)
    unresolved = unresolved_legacy_aliases(snapshots, data, aliases)
    if unresolved:
        raise RuntimeError(
            f"{len(unresolved)} legacy cloud keys have no current listing; "
            f"first: {unresolved[0]}"
        )
    for item in data:
        item_aliases = aliases.get(listing_identity(item), [])
        if item_aliases:
            item["aliases"] = item_aliases
    data.sort(key=lambda item: item.get("score", 0), reverse=True)

    updated = replace_data_block(html, data)
    partial = page.with_name(page.name + ".part")
    partial.write_text(updated, encoding="utf-8")
    partial.replace(page)
    counts = ", ".join(
        f"{city}={sum(item.get('city') == city for item in data)}" for city in CITIES
    )
    print(
        f"index.html: {len(data)} candidates ({counts}; "
        f"{sum(item['type'] == 'venue' for item in data)} venue, "
        f"{sum(item['type'] == 'experience' for item in data)} experience)"
    )
    return data


if __name__ == "__main__":
    build(parse_args().cities)
