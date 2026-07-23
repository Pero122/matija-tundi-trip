"""Provider-neutral normalization for cached Apify activity payloads.

The raw actor item is intentionally *not* returned from this module.  The
ingestion layer stores that exact item in ``raw_payloads`` before calling a
normalizer; this file produces only searchable, privacy-safe columns.

Supported actors:

* ``maxcopell/tripadvisor``
* ``maxcopell/tripadvisor-reviews``
* ``piotrv1001/getyourguide-listings-scraper``
* ``crawlerbros/getyourguide-scraper`` (fallback)

The actors have changed field names over time, so extraction is deliberately
alias-tolerant while remaining conservative: unknown fields stay in the raw
store instead of being guessed into normalized columns.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


TRIPADVISOR_ACTOR = "maxcopell/tripadvisor"
TRIPADVISOR_REVIEWS_ACTOR = "maxcopell/tripadvisor-reviews"
GETYOURGUIDE_ACTOR = "piotrv1001/getyourguide-listings-scraper"
GETYOURGUIDE_FALLBACK_ACTOR = "crawlerbros/getyourguide-scraper"

NORMALIZED_ITEM_KEYS = (
    "source",
    "external_id",
    "url",
    "kind",
    "title",
    "description",
    "rating",
    "review_count",
    "rank",
    "price",
    "currency",
    "duration",
    "cancellation",
    "language",
    "country",
    "locality",
    "region",
    "address",
    "lat",
    "lon",
    "location_scope",
    "starts_in_budapest",
    "categories",
    "media",
    "packages",
    "reviews",
)


_BLOCKED_MARKERS = (
    "access denied",
    "access blocked",
    "request blocked",
    "captcha",
    "cloudflare",
    "verify you are human",
    "robot check",
    "robots denied",
    "too many requests",
    "http 403",
    "http 429",
)

_COUNTRY_CODES = {
    "hungary": "HU",
    "magyarorszag": "HU",
    "hu": "HU",
    "hun": "HU",
    "austria": "AT",
    "at": "AT",
    "slovakia": "SK",
    "sk": "SK",
    "slovenia": "SI",
    "si": "SI",
    "croatia": "HR",
    "hr": "HR",
    "serbia": "RS",
    "rs": "RS",
    "romania": "RO",
    "ro": "RO",
    "czech republic": "CZ",
    "czechia": "CZ",
    "cz": "CZ",
    "france": "FR",
    "fr": "FR",
    "germany": "DE",
    "de": "DE",
    "italy": "IT",
    "it": "IT",
    "poland": "PL",
    "pl": "PL",
}

# Signals are intentionally destination-oriented.  They fix the common actor
# result where a product is geocoded to its Budapest pickup even though its
# title is "From Budapest: <outside destination>".
_HUNGARY_OUTSIDE_SIGNALS = (
    "aggtelek",
    "badacsonytomaj",
    "balaton",
    "balatonfured",
    "boldogko",
    "bukki",
    "bukk",
    "danube bend",
    "debrecen",
    "eger",
    "esztergom",
    "etyek",
    "fertod",
    "fuzer",
    "godollo",
    "gyor",
    "heviz",
    "holloko",
    "hortobagy",
    "kali basin",
    "kapolcs",
    "kecskemet",
    "keszthely",
    "leanyfalu",
    "lillafured",
    "matra",
    "miskolc",
    "mohacs",
    "nagymaros",
    "nyiregyhaza",
    "pannonhalma",
    "paty",
    "pecs",
    "puszta",
    "sarospatak",
    "sentendre",  # occasional machine-translated misspelling
    "szalajka",
    "szeged",
    "szentendre",
    "szekesfehervar",
    "szigetvar",
    "siofok",
    "sopron",
    "szilvasvarad",
    "szolnok",
    "tapolca",
    "tata",
    "tihany",
    "tokaj",
    "vac",
    "velence",
    "veszprem",
    "visegrad",
    "zebegeny",
    "zemplen",
)

_FOREIGN_DESTINATION_SIGNALS = (
    "bratislava",
    "dubrovnik",
    "kosice",
    "krakow",
    "ljubljana",
    "maribor",
    "nove zamky",
    "prague",
    "salzburg",
    "subotica",
    "timisoara",
    "vienna",
    "zagreb",
)


def _budapest_origin_destination(title: str | None) -> str | None:
    """Return the destination portion of a Budapest-origin product title.

    GetYourGuide uses both ``From Budapest: …`` and the less explicit
    ``Budapest: …``/``Budapest to …`` forms for day trips.  The latter must not
    turn every Budapest activity into an origin tour, though: a city cruise
    may merely serve Tokaj wine.  Only a named destination is accepted, and a
    lone Tokaj beverage reference is treated as an amenity unless the title
    also contains journey language.
    """

    folded = re.sub(r"\bbud(?:paest|epest)\b", "budapest", _fold(title))
    match = re.match(r"^budapest\s+to\s+(.+)$", folded)
    if match:
        return match.group(1).strip() or None
    match = re.match(r"^budapest\s*:\s*(.+)$", folded)
    if not match:
        return None
    destination = match.group(1).strip()
    has_hungarian_destination = _has_signal(
        destination, _HUNGARY_OUTSIDE_SIGNALS
    )
    has_foreign_destination = _has_signal(
        destination, _FOREIGN_DESTINATION_SIGNALS
    )
    if not (has_hungarian_destination or has_foreign_destination):
        return None
    # ``Tokaj Frizzante`` is a drink on a Budapest sightseeing cruise, not a
    # journey to Tokaj.  Conversely, "Tokaj wine-region day trip" is a real
    # destination product and contains explicit travel language.
    if (
        has_hungarian_destination
        and "tokaj" in destination
        and not _has_signal(
            destination,
            tuple(signal for signal in _HUNGARY_OUTSIDE_SIGNALS if signal != "tokaj"),
        )
        and re.search(r"\b(?:frizzante|wine|glass|drink|tasting)\b", destination)
        and not re.search(
            r"\b(?:day[ -]?trip|day[ -]?tour|excursion|visit|transfer|travel|journey)\b",
            destination,
        )
    ):
        return None
    return destination

_FOREIGN_LOCALITY_CODES = {
    "bratislava": "SK",
    "dubrovnik": "HR",
    "kosice": "SK",
    "krakow": "PL",
    "ljubljana": "SI",
    "maribor": "SI",
    "nove zamky": "SK",
    "prague": "CZ",
    "salzburg": "AT",
    "subotica": "RS",
    "timisoara": "RO",
    "vienna": "AT",
    "zagreb": "HR",
}

_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "Ft": "HUF", "HUF": "HUF"}


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def _has_signal(text: str, signals: Iterable[str]) -> bool:
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(signal)}(?![a-z0-9])", text)
        for signal in signals
    )


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _get(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _first(payload: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        value = _get(payload, path)
        if _present(value):
            return value
    return None


def _text(value: Any, *, joiner: str = "\n") -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = re.sub(r"[ \t]+", " ", value).strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        for key in (
            "text",
            "value",
            "name",
            "title",
            "label",
            "description",
            "formatted",
            "fullAddress",
        ):
            if key in value:
                result = _text(value[key], joiner=joiner)
                if result:
                    return result
        return None
    if isinstance(value, Iterable):
        values = [_text(item, joiner=joiner) for item in value]
        values = [item for item in values if item]
        return joiner.join(values) or None
    return None


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        for key in ("amount", "value", "price", "min", "from", "average", "rating"):
            if key in value:
                parsed = _as_number(value[key])
                if parsed is not None:
                    return parsed
        return None
    text = str(value).replace("\u00a0", " ").strip()
    match = re.search(r"-?\d[\d\s.,]*", text)
    if not match:
        return None
    token = re.sub(r"\s+", "", match.group(0))
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif token.count(",") == 1:
        left, right = token.split(",")
        token = left + ("." + right if len(right) != 3 else right)
    elif token.count(".") > 1:
        token = token.replace(".", "")
    try:
        return float(token)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if isinstance(value, Mapping):
        number = _as_number(value)
        return int(number) if number is not None and number >= 0 else None
    match = re.search(r"-?\d[\d\s.,]*", str(value).replace("\u00a0", " "))
    if not match or match.group(0).lstrip().startswith("-"):
        return None
    token = re.sub(r"\s+", "", match.group(0))
    # Counts commonly arrive localized as 17,699 or 17.699.  A final
    # three-digit group is a thousands separator, not a decimal fraction.
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token):
        return int(re.sub(r"[.,]", "", token))
    number = _as_number(token)
    return int(number) if number is not None and number >= 0 else None


def _rating(value: Any) -> float | None:
    number = _as_number(value)
    return number if number is not None and 0 <= number <= 5 else None


def _latitude(value: Any) -> float | None:
    number = _as_number(value)
    return number if number is not None and -90 <= number <= 90 else None


def _longitude(value: Any) -> float | None:
    number = _as_number(value)
    return number if number is not None and -180 <= number <= 180 else None


def _clean_id(value: Any) -> str | None:
    if value is None or isinstance(value, (bool, Mapping, list, tuple, set)):
        return None
    result = str(value).strip()
    if not result or _fold(result) in {"none", "null", "undefined", "n/a"}:
        return None
    return result


def _canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.strip()
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    if not re.match(r"^https?://", cleaned, flags=re.I):
        return cleaned
    parts = urlsplit(cleaned)
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, "", ""))


def _generated_id(source: str, *parts: Any, prefix: str = "generated") -> str:
    stable = json.dumps([source, *parts], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"


def _tripadvisor_id(payload: Mapping[str, Any], url: str | None) -> str:
    direct = _first(
        payload,
        "locationId",
        "location_id",
        "location.id",
        "detailId",
        "contentId",
        "placeId",
        "id",
    )
    cleaned = _clean_id(direct)
    if cleaned:
        match = re.fullmatch(r"[dD]?(\d+)", cleaned)
        return match.group(1) if match else cleaned
    match = re.search(r"(?:^|[-_/])d(\d+)(?=[-_.?/#]|$)", url or "", flags=re.I)
    if match:
        return match.group(1)
    return _generated_id(
        "tripadvisor",
        _canonical_url(url),
        _text(_first(payload, "name", "title")),
        _text(_first(payload, "address", "formattedAddress", "addressObj.city")),
        _first(payload, "latitude", "lat"),
        _first(payload, "longitude", "lon", "lng"),
    )


def _getyourguide_id(payload: Mapping[str, Any], url: str | None) -> str:
    direct = _first(
        payload,
        "activityId",
        "activity_id",
        "activity.id",
        "productId",
        "product_id",
        "tourId",
        "tour_id",
        "id",
    )
    cleaned = _clean_id(direct)
    if cleaned:
        match = re.fullmatch(r"[tT]?(\d+)", cleaned)
        return match.group(1) if match else cleaned
    for pattern in (r"-t(\d+)(?=[/?#-]|$)", r"/activities?/(\d+)(?=[/?#]|$)"):
        match = re.search(pattern, url or "", flags=re.I)
        if match:
            return match.group(1)
    return _generated_id(
        "getyourguide",
        _canonical_url(url),
        _text(_first(payload, "title", "activityTitle", "name")),
        _text(_first(payload, "location", "city", "destination.name")),
    )


def is_blocked_payload(payload: Any) -> bool:
    """Return True for actor sentinel/error items rather than real listings."""

    if not isinstance(payload, Mapping) or not payload:
        return True
    if any(payload.get(key) is True for key in ("blocked", "isBlocked", "accessDenied", "captcha")):
        return True
    status = _as_int(_first(payload, "statusCode", "responseStatusCode", "httpStatus"))
    identity = _first(payload, "name", "title", "activityTitle", "locationId", "activityId")
    if status is not None and status >= 400 and not identity:
        return True
    sentinel = " ".join(
        filter(
            None,
            (
                _text(payload.get("error")),
                _text(payload.get("message")),
                _text(payload.get("status")),
                _text(payload.get("title")),
                _text(payload.get("name")),
            ),
        )
    )
    folded = _fold(sentinel)
    if not any(marker in folded for marker in _BLOCKED_MARKERS):
        return False
    # A genuine listing can contain an incidental warning.  Treat it as a
    # sentinel only when it lacks normal listing content or its title itself is
    # the blocking message.
    title = _fold(_first(payload, "activityTitle", "title", "name"))
    has_listing_content = _present(_first(payload, "description", "rating", "reviews", "images", "address"))
    return not has_listing_content or any(marker in title for marker in _BLOCKED_MARKERS)


def _country_code(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    folded = _fold(text)
    if folded in _COUNTRY_CODES:
        return _COUNTRY_CODES[folded]
    if len(text) in (2, 3) and text.isalpha():
        return text.upper()
    return text


def _address(payload: Mapping[str, Any]) -> str | None:
    direct = _first(
        payload,
        "address",
        "formattedAddress",
        "fullAddress",
        "location.address",
        "addressObj.formatted",
        "addressObj.fullAddress",
    )
    if isinstance(direct, str):
        return _text(direct)
    address_obj = _first(payload, "addressObj", "address", "location.address")
    if isinstance(address_obj, Mapping):
        parts = [
            _text(address_obj.get(key))
            for key in ("street1", "street2", "addressLine1", "addressLine2", "postalcode", "postalCode", "city", "state", "country")
        ]
        parts = [part for part in parts if part]
        return ", ".join(dict.fromkeys(parts)) or None
    return _text(direct)


def _location(payload: Mapping[str, Any]) -> tuple[str | None, str | None, str | None, str | None, float | None, float | None]:
    country = _country_code(
        _first(
            payload,
            "countryCode",
            "country_code",
            "country.code",
            "country.name",
            "country",
            "addressObj.countryCode",
            "addressObj.country",
            "location.country.code",
            "location.country.name",
            "location.country",
            "destination.countryCode",
            "destination.country",
        )
    )
    locality = _text(
        _first(
            payload,
            "locality",
            "city",
            "addressObj.city",
            "location.city.name",
            "location.city",
            "location.locality",
            "destination.city",
            "destination.name",
            "location",
        )
    )
    # Shallow GetYourGuide results can omit ``location`` but retain the city
    # collection URL that produced the row (for example ``/eger-l2048/``).
    # It is useful only as a last-resort locality, never as proof of pickup.
    explicit_locality = locality
    source_city_url = _text(_first(payload, "sourceCityUrl", "source_city_url"))
    if source_city_url and "getyourguide." in _fold(urlsplit(source_city_url).netloc):
        match = re.search(r"/([^/?#]+)-l\d+(?:[/?#]|$)", source_city_url, flags=re.I)
        if match:
            source_slug = re.sub(r"[-_]+", " ", match.group(1)).strip()
            source_folded = _fold(source_slug)
            title_folded = _fold(
                _first(payload, "name", "title", "activityTitle", "activity.title")
            )
            explicit_foreign = _FOREIGN_LOCALITY_CODES.get(_fold(explicit_locality))
            source_is_hungary = (
                source_folded in {"hungary", "magyarorszag", "budapest"}
                or _has_signal(source_folded, _HUNGARY_OUTSIDE_SIGNALS)
            )
            if (
                explicit_foreign
                and source_is_hungary
                and _has_signal(title_folded, (source_folded,))
            ):
                # A collection can surface a neighboring-city locality even
                # when both the product title and requested collection name
                # identify the Hungarian destination (for example Esztergom
                # listed under Nové Zámky).
                locality = source_slug.title()
                explicit_locality = locality
                country = "HU"
            elif explicit_foreign:
                raw_title = _text(
                    _first(payload, "name", "title", "activityTitle", "activity.title")
                ) or ""
                title_prefix = raw_title.split(":", 1)[0].strip()
                prefix_folded = _fold(title_prefix)
                if (
                    0 < len(title_prefix.split()) <= 4
                    and _has_signal(prefix_folded, _HUNGARY_OUTSIDE_SIGNALS)
                    and not _has_signal(prefix_folded, _FOREIGN_DESTINATION_SIGNALS)
                ):
                    locality = title_prefix
                    explicit_locality = locality
                    country = "HU"
            if not country and not explicit_locality and (
                source_is_hungary
            ):
                country = "HU"
            if not locality and source_folded not in {"hungary", "magyarorszag"}:
                locality = source_slug.title() or None
    if locality:
        locality_folded = _fold(locality)
        foreign_code = _FOREIGN_LOCALITY_CODES.get(locality_folded)
        if foreign_code:
            # An explicit result locality is stronger than the collection URL
            # that happened to surface it. GYG city collections can cross-sell
            # Vienna, Bratislava, and other neighboring-country products.
            country = foreign_code
        elif not country and (
            locality_folded == "budapest" or _has_signal(
            locality_folded, _HUNGARY_OUTSIDE_SIGNALS
            )
        ):
            country = "HU"
    region = _text(
        _first(
            payload,
            "region",
            "state",
            "addressObj.state",
            "location.region.name",
            "location.region",
            "destination.region",
        )
    )
    lat = _latitude(
        _first(
            payload,
            "latitude",
            "lat",
            "coordinates.latitude",
            "coordinates.lat",
            "location.latitude",
            "location.lat",
            "location.coordinates.latitude",
            "location.coordinates.lat",
            "geo.latitude",
            "geo.lat",
        )
    )
    lon = _longitude(
        _first(
            payload,
            "longitude",
            "lon",
            "lng",
            "coordinates.longitude",
            "coordinates.lon",
            "coordinates.lng",
            "location.longitude",
            "location.lon",
            "location.lng",
            "location.coordinates.longitude",
            "location.coordinates.lon",
            "location.coordinates.lng",
            "geo.longitude",
            "geo.lon",
            "geo.lng",
        )
    )
    return country, locality, region, _address(payload), lat, lon


def _starts_in_budapest(payload: Mapping[str, Any], title: str | None, description: str | None) -> bool:
    explicit = _first(payload, "startsInBudapest", "starts_in_budapest", "departureFromBudapest")
    if isinstance(explicit, bool):
        return explicit
    if _fold(explicit) in {"true", "yes", "1"}:
        return True
    if _fold(explicit) in {"false", "no", "0"}:
        return False
    departure = _text(
        _first(
            payload,
            "departure",
            "departureLocation",
            "startingLocation",
            "startLocation",
            "meetingPoint",
            "pickupLocation",
            "pickup.location",
        )
    )
    text = _fold(" ".join(filter(None, (title, departure))))
    if re.search(
        r"\bfrom bud(?:apest|paest|epest)\b|\bbudapest departure\b|"
        r"\bpickup (?:in|from) budapest\b",
        text,
    ):
        return True
    if _budapest_origin_destination(title):
        return True
    # Description is a weaker signal and only accepts explicit journey terms.
    description_folded = _fold(description)
    return bool(re.search(r"\b(?:depart|departure|pickup|start|leav\w*) (?:in|from) budapest\b", description_folded))


def classify_location(
    *,
    title: str | None,
    description: str | None,
    country: str | None,
    locality: str | None,
    region: str | None,
    address: str | None,
    lat: float | None,
    lon: float | None,
    starts_in_budapest: bool,
) -> str:
    """Classify destination, not merely a Budapest pickup point."""

    title_folded = re.sub(r"\bbud(?:paest|epest)\b", "budapest", _fold(title))
    structured = _fold(" ".join(filter(None, (locality, region, address))))
    destination_text = title_folded
    title_origin_destination = _budapest_origin_destination(title)
    if title_origin_destination:
        if _has_signal(title_origin_destination, _FOREIGN_DESTINATION_SIGNALS):
            return "foreign"
        if _has_signal(title_origin_destination, _HUNGARY_OUTSIDE_SIGNALS):
            return "outside-budapest"
    if starts_in_budapest:
        match = re.search(r"\bfrom budapest\b\s*[:\-–—]?\s*(.*)", title_folded)
        if match:
            destination_text = match.group(1)
        if _has_signal(destination_text, _FOREIGN_DESTINATION_SIGNALS):
            return "foreign"
        if _has_signal(destination_text, _HUNGARY_OUTSIDE_SIGNALS):
            return "outside-budapest"

    title_prefix = title_folded.split(":", 1)[0].strip()
    if title_prefix == "budapest":
        return "budapest"
    if _has_signal(title_folded, _FOREIGN_DESTINATION_SIGNALS):
        return "foreign"
    if _has_signal(title_folded, _HUNGARY_OUTSIDE_SIGNALS):
        return "outside-budapest"
    if _has_signal(structured, _FOREIGN_DESTINATION_SIGNALS):
        return "foreign"

    country_folded = _fold(country)
    if country_folded and country_folded not in {"hu", "hun", "hungary", "magyarorszag"}:
        return "foreign"

    description_folded = _fold(description)
    independent_destination_text = " ".join((title_folded, description_folded))
    explicit_budapest_activity = bool(
        re.search(
            r"\b(?:around|across|through|within|in) budapest\b(?!['’]s)",
            description_folded,
        )
    )
    independent_outside_destination = bool(
        _has_signal(independent_destination_text, _HUNGARY_OUTSIDE_SIGNALS)
        or _has_signal(independent_destination_text, _FOREIGN_DESTINATION_SIGNALS)
    )
    if explicit_budapest_activity and not independent_outside_destination:
        # Destination collection pages can cross-sell a Budapest product while
        # stamping the collection locality onto the shallow row.  A detail
        # description that explicitly places the activity in/around Budapest
        # is stronger than that collection-derived locality unless the title
        # or description independently names a real outside destination.
        return "budapest"

    all_text = " ".join((title_folded, structured, description_folded))
    has_outside_signal = _has_signal(all_text, _HUNGARY_OUTSIDE_SIGNALS)
    if has_outside_signal and not re.search(r"\bbudapest\b", _fold(locality)):
        return "outside-budapest"

    if country_folded in {"hu", "hun", "hungary", "magyarorszag"}:
        mentions_budapest = bool(
            re.search(r"\bbudapest\b", structured)
            or (not structured and re.search(r"\bbudapest\b", title_folded))
        )
        if mentions_budapest and not (starts_in_budapest and has_outside_signal):
            return "budapest"
        return "outside-budapest"

    if lat is not None and lon is not None:
        if 47.30 <= lat <= 47.65 and 18.75 <= lon <= 19.40:
            return "budapest"
        # Hungary's neighbors overlap any useful bounding rectangle. Without
        # country, locality, or destination evidence, coordinates near the
        # border must remain unknown rather than silently becoming Hungarian.

    if re.search(r"\bbudapest\b", structured or title_folded):
        return "budapest"
    if (
        not structured
        and re.search(r"\bbudapest\b", description_folded)
        and not _has_signal(description_folded, _FOREIGN_DESTINATION_SIGNALS)
        and not _has_signal(description_folded, _HUNGARY_OUTSIDE_SIGNALS)
    ):
        return "budapest"
    if has_outside_signal:
        return "outside-budapest"
    return "unknown"


def _string_list(value: Any) -> list[str]:
    if not _present(value):
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in items:
        text = _text(item, joiner=", ")
        if text and text not in result:
            result.append(text)
    return result


def _categories(payload: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for path in (
        "categories",
        "category",
        "subcategories",
        "subCategories",
        "tags",
        "activityCategories",
        "activity.category",
        "breadcrumbs",
    ):
        for value in _string_list(_get(payload, path)):
            if value not in result:
                result.append(value)
    return result


def _media_item(value: Any, order: int) -> dict[str, Any] | None:
    if isinstance(value, str):
        url = value
        item: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        item = value
        url = _text(
            _first(
                item,
                "url",
                "imageUrl",
                "image_url",
                "large",
                "original",
                "src",
                "source",
                "image",
                "photoUrl",
                "videoUrl",
            )
        )
    else:
        return None
    if not url:
        return None
    kind = _text(_first(item, "mediaType", "type", "kind")) or ("video" if re.search(r"\.(?:mp4|webm)(?:\?|$)", url, re.I) else "image")
    normalized = {
        "external_id": _clean_id(_first(item, "id", "mediaId", "photoId")),
        "media_type": kind.casefold(),
        "url": url,
        "caption": _text(_first(item, "caption", "alt", "altText", "description", "title")),
        "width": _as_int(_first(item, "width", "dimensions.width")),
        "height": _as_int(_first(item, "height", "dimensions.height")),
        "sort_order": order,
    }
    return normalized


def _media(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[Any] = []
    for path in (
        "media",
        "images",
        "photos",
        "gallery",
        "thumbnailUrls",
        "activity.images",
        "activity.photos",
    ):
        value = _get(payload, path)
        if isinstance(value, list):
            values.extend(value)
        elif _present(value):
            values.append(value)
    for path in ("image", "imageUrl", "image_url", "mainImage", "heroImage", "coverImage"):
        value = _get(payload, path)
        if _present(value):
            values.append(value)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        item = _media_item(value, len(result))
        if item and item["url"] not in seen:
            seen.add(item["url"])
            result.append(item)
    return result


def _price_and_currency(payload: Mapping[str, Any]) -> tuple[float | None, str | None]:
    raw_price = _first(
        payload,
        "priceFrom",
        "price_from",
        "startingPrice",
        "starting_price",
        "fromPrice",
        "price.amount",
        "price.value",
        "price",
        "activity.price",
    )
    price = _as_number(raw_price)
    currency = _text(
        _first(
            payload,
            "currency",
            "currencyCode",
            "price.currency",
            "price.currencyCode",
            "activity.currency",
        )
    )
    price_text = _text(raw_price) or ""
    if not currency:
        for symbol, code in _CURRENCY_SYMBOLS.items():
            if symbol in price_text:
                currency = code
                break
    return (price if price is None or price >= 0 else None), (currency.upper() if currency and len(currency) <= 4 else currency)


def _duration(payload: Mapping[str, Any]) -> str | None:
    value = _first(payload, "duration", "durationText", "duration_text", "activity.duration")
    if isinstance(value, Mapping):
        text = _text(_first(value, "text", "label", "formatted"))
        if text:
            return text
        minimum = _first(value, "min", "minimum", "from", "value")
        maximum = _first(value, "max", "maximum", "to")
        unit = _text(_first(value, "unit", "units")) or "minutes"
        if minimum is not None and maximum is not None and minimum != maximum:
            return f"{_text(minimum)}–{_text(maximum)} {unit}"
        if minimum is not None:
            return f"{_text(minimum)} {unit}"
    return _text(value)


def _cancellation(payload: Mapping[str, Any]) -> str | None:
    value = _first(
        payload,
        "cancellation",
        "cancellationPolicy",
        "cancellation_policy",
        "freeCancellation",
        "activity.cancellationPolicy",
    )
    if value is True:
        return "Free cancellation"
    if value is False:
        return "Non-refundable"
    if _fold(value) in {"true", "yes", "1"}:
        return "Free cancellation"
    if _fold(value) in {"false", "no", "0"}:
        return "Non-refundable"
    return _text(value)


def _languages(payload: Mapping[str, Any]) -> list[str]:
    value = _first(payload, "languages", "language", "availableLanguages", "guideLanguages", "activity.languages")
    result = _string_list(value)
    alternate_urls = _first(payload, "alternateLanguageUrls", "alternate_language_urls")
    if isinstance(alternate_urls, Mapping):
        alternates: Iterable[Any] = alternate_urls.keys()
    elif isinstance(alternate_urls, list):
        alternates = [
            _first(item, "language", "languageCode", "locale") if isinstance(item, Mapping) else None
            for item in alternate_urls
        ]
    else:
        alternates = []
    for alternate in alternates:
        text = _text(alternate)
        if text and text not in result:
            result.append(text)
    return result


def _package(value: Any, order: int, default_currency: str | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        name = _text(value)
        return {
            "external_id": None,
            "name": name,
            "description": None,
            "price": None,
            "original_price": None,
            "currency": default_currency,
            "duration": None,
            "availability": None,
            "url": None,
            "provider": None,
            "category": None,
            "sort_order": order,
        } if name else None
    name = _text(_first(value, "name", "title", "label", "optionName", "packageName"))
    external_id = _clean_id(
        _first(value, "id", "optionId", "packageId", "productId", "productCode")
    )
    if not name:
        name = f"Option {order + 1}"
    price_value = _first(value, "price", "price.amount", "price.value", "amount", "priceFrom")
    currency = _text(_first(value, "currency", "currencyCode", "price.currency", "price.currencyCode")) or default_currency
    if not currency:
        price_text = _text(price_value) or ""
        for symbol, code in _CURRENCY_SYMBOLS.items():
            if symbol in price_text:
                currency = code
                break
    return {
        "external_id": external_id,
        "name": name,
        "description": _text(_first(value, "description", "details", "summary")),
        "price": _as_number(price_value),
        "original_price": _as_number(_first(value, "originalPrice", "original_price", "retailPrice", "price.original")),
        "currency": currency.upper() if currency and len(currency) <= 4 else currency,
        "duration": _duration(value),
        "availability": _text(_first(value, "availability", "availabilityText", "status", "schedule")),
        "url": _text(_first(value, "url", "bookingUrl", "productUrl")),
        "provider": _text(_first(value, "provider", "partner", "supplier")),
        "category": _text(_first(value, "category", "primaryCategory", "productCategory")),
        "sort_order": order,
    }


def _packages(payload: Mapping[str, Any], default_currency: str | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in (
        "packages",
        "options",
        "variants",
        "ticketOptions",
        "tourOptions",
        "priceOptions",
        "activity.options",
        "offerGroup.offerList",
    ):
        values = _get(payload, path)
        if not isinstance(values, list):
            continue
        for value in values:
            item = _package(value, len(result), default_currency)
            if not item:
                continue
            key = item["external_id"] or _fold(item["name"])
            if key not in seen:
                seen.add(key)
                result.append(item)
    return result


def _review_parent_id(payload: Mapping[str, Any]) -> str | None:
    direct = _first(
        payload,
        "locationId",
        "location_id",
        "placeId",
        "activityId",
        "activity_id",
        "productId",
        "activity.id",
        "location.id",
    )
    return _clean_id(direct)


def normalize_review(
    source: str,
    payload: Mapping[str, Any],
    *,
    activity_external_id: str | None = None,
) -> dict[str, Any] | None:
    """Normalize one review without copying reviewer identity fields."""

    if not isinstance(payload, Mapping) or is_blocked_payload(payload):
        return None
    provider = _provider(source)
    parent_id = activity_external_id or _review_parent_id(payload)
    translated_title = _text(
        _first(payload, "translatedTitle", "translation.title")
    )
    translated_body = _text(
        _first(payload, "translatedText", "translatedBody", "translation.text", "translation.body")
    )
    original_title = _text(_first(payload, "originalTitle", "original.title"))
    original_body = _text(_first(payload, "originalText", "originalBody", "original.text", "original.body"))
    ordinary_title = _text(_first(payload, "title", "reviewTitle", "headline"))
    ordinary_body = _text(_first(payload, "text", "body", "reviewText", "content", "comment"))
    title = translated_title or ordinary_title
    body = translated_body or ordinary_body
    if not title and not body and _rating(_first(payload, "rating", "score", "stars")) is None:
        return None
    language = _text(
        _first(payload, "translationLanguage", "translatedLanguage", "translation.language", "language", "languageCode")
    )
    original_language = _text(
        _first(payload, "originalLanguage", "originalLanguageCode", "original.language", "languageOriginal")
    )
    explicit_translated = _first(payload, "isTranslated", "translated")
    is_translated = bool(
        explicit_translated is True
        or translated_title
        or translated_body
        or original_title
        or original_body
        or (language and original_language and _fold(language) != _fold(original_language))
    )
    review_id = _clean_id(_first(payload, "reviewId", "review_id", "id", "uid"))
    review_date = _text(_first(payload, "reviewDate", "publishedDate", "published_at", "date", "createdAt"))
    if not review_id:
        review_id = _generated_id(
            provider,
            parent_id,
            _rating(_first(payload, "rating", "score", "stars")),
            original_title or ordinary_title or title,
            original_body or ordinary_body or body,
            review_date,
            original_language or language,
            prefix="review",
        )
    return {
        "source": provider,
        "external_id": review_id,
        "activity_external_id": parent_id,
        # Only an explicitly labelled review URL is safe.  A generic actor
        # ``url`` can be the reviewer's profile URL and must stay raw-only.
        "url": _text(_first(payload, "reviewUrl", "review_url")),
        "rating": _rating(_first(payload, "rating", "score", "stars", "bubbleRating")),
        "title": title,
        "body": body,
        "language": language,
        "original_language": original_language,
        "is_translated": is_translated,
        "original_title": original_title,
        "original_body": original_body,
        "review_date": review_date,
        "travel_date": _text(_first(payload, "travelDate", "tripDate", "dateOfExperience")),
        "helpful_count": _as_int(_first(payload, "helpfulCount", "helpfulVotes", "helpful", "votes")),
    }


def normalize_reviews(
    source: str,
    payload: Mapping[str, Any] | list[Any],
    *,
    activity_external_id: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize a single review, a review batch, or embedded review list."""

    if isinstance(payload, list):
        values = payload
        parent_id = activity_external_id
    elif isinstance(payload, Mapping):
        parent_id = activity_external_id or _review_parent_id(payload)
        candidate = _first(payload, "reviews", "sampleReviews", "data.reviews", "results", "items")
        values = candidate if isinstance(candidate, list) else [payload]
    else:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = normalize_review(source, value, activity_external_id=parent_id)
        if item and item["external_id"] not in seen:
            seen.add(item["external_id"])
            result.append(item)
    return result


def _provider(actor: str) -> str:
    folded = _fold(actor).replace("_", "-")
    if "tripadvisor" in folded:
        return "tripadvisor"
    if "getyourguide" in folded:
        return "getyourguide"
    raise ValueError(f"Unsupported actor/source: {actor!r}")


def _kind(payload: Mapping[str, Any], provider: str) -> str:
    value = _fold(_first(payload, "kind", "type", "listingType", "businessType", "category.name"))
    if provider == "getyourguide":
        return "experience"
    if "hotel" in value or "lodging" in value:
        return "lodging"
    if "restaurant" in value or "food" in value:
        return "restaurant"
    if "tour" in value or "experience" in value or "product" in value:
        return "experience"
    return "attraction"


def _description(payload: Mapping[str, Any]) -> str | None:
    value = _first(
        payload,
        "fullDescription",
        "activityDescription",
        "description",
        "shortDescription",
        "about",
        "overview",
        "activity.description",
    )
    description = _text(value)
    highlights = _string_list(_first(payload, "highlights", "activity.highlights"))
    if not highlights:
        return description
    highlight_text = "Highlights:\n" + "\n".join(f"• {highlight}" for highlight in highlights)
    return f"{description}\n\n{highlight_text}" if description else highlight_text


def _rank(payload: Mapping[str, Any], supplied_rank: int | None) -> int | None:
    if supplied_rank is not None:
        return supplied_rank if supplied_rank >= 0 else None
    direct = _as_int(_first(payload, "rank", "rankingPosition", "position", "searchRank", "index"))
    if direct is not None:
        return direct
    ranking_text = _text(_first(payload, "rankingString", "ranking", "rankText"))
    match = re.search(r"#?\s*(\d[\d,. ]*)", ranking_text or "")
    return _as_int(match.group(1)) if match else None


def _review_count(payload: Mapping[str, Any]) -> int | None:
    value = _first(
        payload,
        "numberOfReviews",
        "reviewCount",
        "reviewsCount",
        "ratingCount",
        "rating.reviewCount",
        "rating.reviewsCount",
        "rating.count",
        "reviews",
    )
    return None if isinstance(value, (list, Mapping)) else _as_int(value)


def _base_item(
    *,
    actor: str,
    payload: Mapping[str, Any],
    external_id: str,
    url: str | None,
    title: str | None,
    rank: int | None,
) -> dict[str, Any]:
    provider = _provider(actor)
    description = _description(payload)
    country, locality, region, address, lat, lon = _location(payload)
    starts = _starts_in_budapest(payload, title, description)
    price, currency = _price_and_currency(payload)
    reviews = normalize_reviews(provider, payload, activity_external_id=external_id) if isinstance(_first(payload, "reviews", "sampleReviews", "data.reviews"), list) else []
    scope = classify_location(
        title=title,
        description=description,
        country=country,
        locality=locality,
        region=region,
        address=address,
        lat=lat,
        lon=lon,
        starts_in_budapest=starts,
    )
    item = {
        "source": provider,
        "external_id": external_id,
        "url": url,
        "kind": _kind(payload, provider),
        "title": title,
        "description": description,
        "rating": _rating(_first(payload, "rating", "rating.value", "rating.average", "averageRating", "score")),
        "review_count": _review_count(payload),
        "rank": _rank(payload, rank),
        "price": price,
        "currency": currency,
        "duration": _duration(payload),
        "cancellation": _cancellation(payload),
        "language": _languages(payload),
        "country": country,
        "locality": locality,
        "region": region,
        "address": address,
        "lat": lat,
        "lon": lon,
        "location_scope": scope,
        "starts_in_budapest": starts,
        "categories": _categories(payload),
        "media": _media(payload),
        "packages": _packages(payload, currency),
        "reviews": reviews,
    }
    assert tuple(item) == NORMALIZED_ITEM_KEYS
    return item


def normalize_tripadvisor_listing(
    payload: Mapping[str, Any], *, rank: int | None = None
) -> dict[str, Any] | None:
    if is_blocked_payload(payload):
        return None
    url = _text(_first(payload, "webUrl", "webpageUrl", "url", "link", "detailPageUrl"))
    title = _text(_first(payload, "name", "title", "locationName"))
    return _base_item(
        actor=TRIPADVISOR_ACTOR,
        payload=payload,
        external_id=_tripadvisor_id(payload, url),
        url=url,
        title=title,
        rank=rank,
    )


def normalize_getyourguide_listing(
    payload: Mapping[str, Any], *, rank: int | None = None, actor: str = GETYOURGUIDE_ACTOR
) -> dict[str, Any] | None:
    if is_blocked_payload(payload):
        return None
    url = _text(_first(payload, "activityUrl", "activity_url", "url", "link", "productUrl"))
    title = _text(_first(payload, "activityTitle", "title", "name", "activity.title"))
    return _base_item(
        actor=actor,
        payload=payload,
        external_id=_getyourguide_id(payload, url),
        url=url,
        title=title,
        rank=rank,
    )


def _normalize_tripadvisor_review_envelope(
    payload: Mapping[str, Any], *, rank: int | None = None
) -> dict[str, Any] | None:
    if is_blocked_payload(payload):
        return None
    reviews = normalize_reviews(TRIPADVISOR_REVIEWS_ACTOR, payload)
    if not reviews:
        return None
    external_id = _review_parent_id(payload) or reviews[0].get("activity_external_id")
    if not external_id:
        external_id = _generated_id("tripadvisor", *(review["external_id"] for review in reviews))
    url = _text(_first(payload, "locationUrl", "webUrl", "url"))
    title = _text(_first(payload, "locationName", "placeName", "activityTitle"))
    item = _base_item(
        actor=TRIPADVISOR_ACTOR,
        payload=payload,
        external_id=external_id,
        url=url,
        title=title,
        rank=rank,
    )
    item["kind"] = "review-batch"
    item["reviews"] = reviews
    return item


def normalize_item(
    actor: str, payload: Mapping[str, Any], *, rank: int | None = None
) -> dict[str, Any] | None:
    """Normalize one actor item to the stable activity dictionary.

    ``rank`` is query provenance (the result's position for that search), not a
    provider-global property.  Passing it separately prevents actors' internal
    indexes from silently replacing the orchestrator's observed rank.
    """

    folded = _fold(actor)
    if "tripadvisor-reviews" in folded or ("tripadvisor" in folded and "review" in folded):
        return _normalize_tripadvisor_review_envelope(payload, rank=rank)
    if "tripadvisor" in folded:
        return normalize_tripadvisor_listing(payload, rank=rank)
    if "getyourguide" in folded:
        return normalize_getyourguide_listing(payload, rank=rank, actor=actor)
    raise ValueError(f"Unsupported actor/source: {actor!r}")


# Friendly aliases for orchestrators that prefer generic verbs.
normalize_listing = normalize_item
normalize_payload = normalize_item


__all__ = [
    "GETYOURGUIDE_ACTOR",
    "GETYOURGUIDE_FALLBACK_ACTOR",
    "NORMALIZED_ITEM_KEYS",
    "TRIPADVISOR_ACTOR",
    "TRIPADVISOR_REVIEWS_ACTOR",
    "classify_location",
    "is_blocked_payload",
    "normalize_getyourguide_listing",
    "normalize_item",
    "normalize_listing",
    "normalize_payload",
    "normalize_review",
    "normalize_reviews",
    "normalize_tripadvisor_listing",
]
