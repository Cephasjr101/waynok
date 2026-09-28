"""Matching engine: filters and ranks trucks against loads."""
from __future__ import annotations

import math

# Ghana city coordinates: name -> (lat, lng)
CITY_COORDS = {
    "accra": (5.6037, -0.1870),
    "kumasi": (6.6885, -1.6244),
    "tema": (5.6698, -0.0166),
    "takoradi": (4.8982, -1.7603),
    "tamale": (9.4034, -0.8424),
    "ho": (6.6000, 0.4667),
    "cape coast": (5.1053, -1.2466),
    "sunyani": (7.3399, -2.3268),
}

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    rlat1, rlng1, rlat2, rlng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = rlat2 - rlat1
    dlng = rlng2 - rlng1
    h = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


# ---- road distance via Google Routes API (server-side key) with fallback ----
import os as _os
import json as _json
import urllib.request as _urlreq

_GOOGLE_KEY = _os.getenv("GOOGLE_MAPS_API_KEY", "")
_road_cache: dict = {}


def road_km(origin_lat, origin_lng, dest_lat, dest_lng) -> float | None:
    """Road distance in km from Google Routes API, or None (caller falls back to haversine)."""
    if not _GOOGLE_KEY:
        return None
    cache_key = (round(origin_lat, 3), round(origin_lng, 3), round(dest_lat, 3), round(dest_lng, 3))
    if cache_key in _road_cache:
        return _road_cache[cache_key]
    payload = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
        "travelMode": "DRIVE",
    }
    req = _urlreq.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=_json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": _GOOGLE_KEY,
                 "X-Goog-FieldMask": "routes.distanceMeters"},
    )
    try:
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        meters = (data.get("routes") or [{}])[0].get("distanceMeters")
        km = round(meters / 1000.0, 1) if meters else None
    except Exception:
        km = None
    _road_cache[cache_key] = km
    return km


def haul_km(origin_lat, origin_lng, dest_lat, dest_lng) -> float:
    """Best available distance: road km when GOOGLE_MAPS_API_KEY is set, else straight-line."""
    return road_km(origin_lat, origin_lng, dest_lat, dest_lng) or \
        haversine_km(origin_lat, origin_lng, dest_lat, dest_lng)


# ---- market-calibrated pricing (Ghana, 2026 benchmarks) ----
# Full-truck hire rates per km by equipment class (GHS, ~11 GHS/USD).
# Reefer carries the cold-chain premium; artic is the 28t long-haul workhorse.
RATE_GHS_PER_KM = {
    "van": 5.0,        # 1-tonne pickup/van
    "box_truck": 9.5,  # 3-tonne
    "dry_van": 15.0,   # 7-tonne FMCG workhorse
    "flatbed": 20.0,   # 15-tonne rigid
    "tanker": 25.0,    # liquid bulk
    "reefer": 24.0,    # 7t cold chain (+55% premium baked in)
    "artic": 31.0,     # 28-tonne semi (container work: 20ft ~29, 40ft ~35)
}
DEFAULT_RATE = 20.0  # unknown equipment -> flatbed-class rate

MIN_PRICE_GHS = 300.0       # floor for small jobs (unchanged rule)
MIN_DISTANCE_KM = 0.5       # billed minimum distance
MIN_WEIGHT_TONNES = 0.5     # loads under half a tonne still pay the half-tonne minimum

# Bad-route time-delay surcharge: rough roads slow the truck down (unchanged rule).
BAD_ROUTE_DELAY_PCT = 0.15
GOOD_ROADS = {"accra", "tema", "kumasi"}


def road_factor(city: str | None) -> float:
    """1.0 on good roads; 1.15 on bad routes (15% time-delay surcharge)."""
    key = (city or "").split(",")[0].strip().lower()
    return 1.0 if key in GOOD_ROADS else 1.0 + BAD_ROUTE_DELAY_PCT


DRIVER_COMMISSION_PCT = 0.15   # platform commission on every trip — driver pays
PRICE_MARKUP_PCT = 0.50        # all market prices increased by 50% over the base market rate


def customer_price_ghs(market_price: float) -> float:
    """What the SHIPPER pays: market price + 15% markup."""
    return round(float(market_price) * (1 + PRICE_MARKUP_PCT), 2)


def driver_earning_ghs(market_price: float) -> float:
    """What the DRIVER nets per trip: increased price - 15% commission."""
    return round(customer_price_ghs(market_price) * (1 - DRIVER_COMMISSION_PCT), 2)


def estimate_price_ghs(distance_km, weight_kg=None, origin_city=None, dest_city=None,
                       equipment_type=None) -> float:
    """Market-calibrated auto price (GHS), floor of GHS 300.

    Price = per-km truck rate x distance x road factor. Weight is a capacity
    check, not a linear multiplier (a full artic is CHEAPER per tonne than a
    half-empty 3t truck). Loads under 0.5 km AND 0.5 t pay the flat GHS 300.
    """
    d = max(float(distance_km or 0.0), 0.0)
    t = max(float(weight_kg or 0.0) / 1000.0, 0.0)  # kg -> tonnes
    if d < MIN_DISTANCE_KM and t < MIN_WEIGHT_TONNES:
        return MIN_PRICE_GHS
    d_eff = max(d, MIN_DISTANCE_KM)
    rate = RATE_GHS_PER_KM.get((equipment_type or "").lower(), DEFAULT_RATE)
    factor = road_factor(origin_city)
    return max(MIN_PRICE_GHS, rate * d_eff * factor)


def score_match(load, truck, distance_km: float) -> float:
    score = 100.0
    if truck.capacity_kg:
        margin = (truck.capacity_kg - load.weight_kg) / truck.capacity_kg
        score += max(0.0, margin) * 40.0
    score += max(0.0, 100.0 * (1.0 - min(distance_km, 300.0) / 300.0))
    return score


def compatible_trucks(load, trucks, max_distance_km: float = 500.0):
    """Filter + rank trucks for a load. Returns [{'truck', 'distance_km', 'score'}] best-first."""
    results = []
    for truck in trucks:
        if truck.equipment_type.lower() != load.equipment_type.lower():
            continue
        if truck.capacity_kg and truck.capacity_kg < load.weight_kg:
            continue
        if truck.available_until and load.pickup_time and truck.available_until < load.pickup_time:
            continue
        if truck.origin_lat is None or truck.origin_lng is None:
            continue
        km = haversine_km(truck.origin_lat, truck.origin_lng, load.origin_lat, load.origin_lng)
        if km > max_distance_km:
            continue
        results.append({"truck": truck, "distance_km": km, "score": score_match(load, truck, km)})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results
