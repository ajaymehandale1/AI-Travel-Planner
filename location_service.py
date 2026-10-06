import json
import os
import time
from pathlib import Path
from urllib.parse import quote_plus

import requests


NOMINATIM_URL = os.getenv(
    "NOMINATIM_URL",
    "https://nominatim.openstreetmap.org/search"
).strip()

APP_USER_AGENT = os.getenv(
    "GEOCODER_USER_AGENT",
    "AITravelPlanner/1.0 (student project; contact configured by developer)"
).strip()

CACHE_FILE = Path(__file__).with_name("location_cache.json")
_MIN_INTERVAL_SECONDS = 1.05
_last_request_at = 0.0


def _load_cache():
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cache(cache):
    try:
        CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _throttle():
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < _MIN_INTERVAL_SECONDS:
        time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.time()


def _clean(value):
    return " ".join(str(value or "").strip().split())


def get_exact_location(place_name, destination="", country="India"):
    """
    Resolve a user-visible itinerary place to one best geocoding result.

    This deliberately uses the public OpenStreetMap Nominatim endpoint only
    for user-triggered itinerary lookups. Results are cached locally and
    requests are rate-limited to respect the public service policy.
    """
    place_name = _clean(place_name)
    destination = _clean(destination)

    if not place_name:
        return None

    query_parts = [place_name]
    if destination and destination.lower() not in place_name.lower():
        query_parts.append(destination)
    if country and country.lower() not in " ".join(query_parts).lower():
        query_parts.append(country)

    query = ", ".join(query_parts)
    cache_key = query.lower()

    cache = _load_cache()
    if cache_key in cache:
        return cache[cache_key]

    _throttle()

    try:
        response = requests.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "addressdetails": 1,
            },
            headers={
                "User-Agent": APP_USER_AGENT,
                "Accept-Language": "en",
            },
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except Exception as exc:
        print("Geocoding warning:", exc)
        return None

    if not results:
        cache[cache_key] = None
        _save_cache(cache)
        return None

    item = results[0]
    lat = str(item.get("lat") or "").strip()
    lon = str(item.get("lon") or "").strip()
    display_name = _clean(item.get("display_name"))

    if not lat or not lon:
        return None

    result = {
        "query": query,
        "name": _clean(item.get("name")) or place_name,
        "display_name": display_name or place_name,
        "latitude": lat,
        "longitude": lon,
        "osm_type": item.get("type") or "",
        "category": item.get("category") or "",
        "google_maps_url": (
            "https://www.google.com/maps/search/?api=1&query="
            + quote_plus(f"{lat},{lon}")
        ),
        "openstreetmap_url": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}",
    }

    cache[cache_key] = result
    _save_cache(cache)
    return result


def enrich_itinerary_locations(plan, max_lookups=8):
    """
    Add exact_location to meaningful itinerary stops.
    max_lookups prevents a single page load from sending many public requests.
    Cached lookups do not consume the public request budget.
    """
    if not isinstance(plan, dict):
        return plan

    destination = _clean((plan.get("trip") or {}).get("destination"))
    lookup_count = 0

    for day in plan.get("itinerary") or []:
        for activity in day.get("activities") or []:
            kind = _clean(activity.get("activity_type")).lower()

            # Skip generic/non-place events.
            if kind in {
                "wake_up", "sleep", "rest", "breakfast", "lunch",
                "dinner", "train", "flight", "bus", "travel"
            }:
                continue

            place = _clean(activity.get("location") or activity.get("place"))
            if not place:
                continue

            exact = get_exact_location(place, destination)
            if exact:
                activity["exact_location"] = exact

            lookup_count += 1
            if lookup_count >= max_lookups:
                return plan

    return plan
