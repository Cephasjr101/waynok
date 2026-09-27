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


# ---- automatic pricing: distance x weight x road quality + bad-route delay ----
BASE_RATE_GHS_PER_KM_PER_TONNE = 1000.0  # 1 km, 1 ton, good road = GHS 1,000
MIN_PRICE_GHS = 300.0                    # floor for <0.5 km AND <0.5 ton jobs
MIN_DISTANCE_KM = 0.5
MIN_WEIGHT_TONNES = 0.5

# Bad-route time-delay surcharge: rough roads slow the truck down, so the
# haul costs 15% more. Routes are classed GOOD (tarred highway standard) or
# BAD (rough/unknown — assumed to cause delays).
BAD_ROUTE_DELAY_PCT = 0.15
GOOD_ROADS = {"accra", "tema", "kumasi"}


def road_factor(city: str | None) -> float:
    """1.0 on good roads; 1.15 on bad routes (15% time-delay surcharge)."""
    key = (city or "").split(",")[0].strip().lower()
    return 1.0 if key in GOOD_ROADS else 1.0 + BAD_ROUTE_DELAY_PCT


def estimate_price_ghs(distance_km, weight_kg=None, origin_city=None, dest_city=None) -> float:
    """Auto price: GHS 1,000 x km x tonnes x road factor, GHS 300 minimum.

    - 1 km, 1 ton on a good road (e.g. Accra) = GHS 1,000
    - under 0.5 km AND under 0.5 ton = flat GHS 300
    - below 0.5 km or 0.5 ton is billed at the 0.5 minimum anyway
    - bad routes (rough/unknown roads) add a 15% time-delay surcharge
    """
    d = max(float(distance_km or 0.0), 0.0)
    t = max(float(weight_kg or 0.0) / 1000.0, 0.0)  # kg -> tonnes
    if d < MIN_DISTANCE_KM and t < MIN_WEIGHT_TONNES:
        return MIN_PRICE_GHS
    d_eff = max(d, MIN_DISTANCE_KM)
    t_eff = max(t, MIN_WEIGHT_TONNES)
    factor = road_factor(origin_city)
    return max(MIN_PRICE_GHS, BASE_RATE_GHS_PER_KM_PER_TONNE * d_eff * t_eff * factor)


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
