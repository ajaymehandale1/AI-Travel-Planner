import os
from datetime import datetime

import requests


BASE_URL = os.getenv("RAILRADAR_BASE_URL", "https://api.railradar.in/v1").rstrip("/")
API_KEY = os.getenv("RAILRADAR_API_KEY", "").strip()


def _headers():
    if not API_KEY:
        return {}
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    }


def _get(path, params=None):
    if not API_KEY:
        return None, "RAILRADAR_API_KEY is not configured"

    try:
        response = requests.get(
            f"{BASE_URL}{path}",
            headers=_headers(),
            params=params or {},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        message = ""
        try:
            message = response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        return None, message or str(exc)
    except Exception as exc:
        return None, str(exc)

    if not payload.get("success", False):
        return None, (payload.get("error") or {}).get("message", "Rail API request failed")

    return payload.get("data"), None


def search_station(query, limit=5):
    query = " ".join(str(query or "").strip().split())
    if not query:
        return [], "Station query is empty"

    data, error = _get(
        "/lookup/search/stations",
        params={"q": query, "limit": limit},
    )
    if error:
        return [], error
    return data if isinstance(data, list) else [], None


def choose_station(location):
    """
    Find the best railway station match for a city/place string.
    RailRadar's search endpoint returns station code, name and city.
    """
    location = " ".join(str(location or "").strip().split())
    if not location:
        return None, "Location is empty"

    # Try full value, then first comma-separated city name.
    queries = [location]
    city = location.split(",")[0].strip()
    if city and city.lower() != location.lower():
        queries.append(city)

    for query in queries:
        stations, error = search_station(query, 10)
        if error:
            continue
        if stations:
            city_lower = city.lower()
            for station in stations:
                station_city = str(station.get("city") or "").lower()
                station_name = str(station.get("name") or "").lower()
                if city_lower and (city_lower == station_city or city_lower in station_name):
                    return station, None
            return stations[0], None

    return None, "No railway station match found"


def trains_between(from_code, to_code, journey_date, live=True):
    params = {
        "date": journey_date,
        "byCity": "true",
        "live": "true" if live else "false",
    }
    data, error = _get(
        f"/trains/between/{from_code}/{to_code}",
        params=params,
    )
    if error:
        return [], error

    trains = []
    for item in (data or {}).get("trains", []):
        train = item.get("train") or {}
        source = item.get("from") or {}
        destination = item.get("to") or {}
        live_data = item.get("live") or {}

        duration_minutes = item.get("duration")
        if isinstance(duration_minutes, (int, float)):
            hours = int(duration_minutes // 60)
            minutes = int(duration_minutes % 60)
            duration_text = f"{hours}h {minutes:02d}m"
        else:
            duration_text = ""

        trains.append({
            "train_number": str(train.get("number") or ""),
            "train_name": train.get("name") or "",
            "train_type": train.get("type") or "",
            "departure_time": source.get("departure") or "",
            "arrival_time": destination.get("arrival") or "",
            "departure_day": source.get("day"),
            "arrival_day": destination.get("day"),
            "distance_km": item.get("distance"),
            "duration": duration_text,
            "halts": item.get("totalHaltsBetween"),
            "run_days": train.get("runDays") or [],
            "live_type": live_data.get("type") or "",
            "expected_departure": live_data.get("expectedDepartureTime")
                or live_data.get("expectedArrivalTime")
                or "",
            "platform": live_data.get("platform"),
            "delay_minutes": live_data.get("delayMinutes"),
        })

    return trains, None


def get_live_trains(start_location, destination, journey_date, max_results=8):
    if not API_KEY:
        return {
            "enabled": False,
            "error": "Add RAILRADAR_API_KEY to .env to show verified train timings.",
            "from_station": None,
            "to_station": None,
            "trains": [],
        }

    from_station, from_error = choose_station(start_location)
    to_station, to_error = choose_station(destination)

    if not from_station or not to_station:
        return {
            "enabled": True,
            "error": from_error or to_error or "Could not resolve railway stations.",
            "from_station": from_station,
            "to_station": to_station,
            "trains": [],
        }

    trains, error = trains_between(
        from_station.get("code"),
        to_station.get("code"),
        journey_date,
        live=True,
    )

    return {
        "enabled": True,
        "error": error,
        "from_station": from_station,
        "to_station": to_station,
        "trains": trains[:max_results],
        "journey_date": journey_date,
    }
