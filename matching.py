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


# Rate per km by cargo weight: (upper weight limit kg, GHS per km)
RATE_BY_WEIGHT = [
    (1000, 100.0),            # under 1 ton
    (5000, 160.0),            # 1 - 5 tons
    (15000, 240.0),           # 5 - 15 tons
    (float("inf"), 360.0),    # 15+ tons
]
MINIMUM_PRICE_GHS = 1000.0
LIGHT_WEIGHT_MAX_KG = 1000.0  # 'under 1 ton'
WITHIN_CITY_KM = 1.0          # trips this short are "within one city"
WITHIN_CITY_MIN_GHS = 300.0   # light-cargo intra-city minimum


def rate_for_weight(weight_kg) -> float:
    weight = float(weight_kg or 0)
    for cap, rate in RATE_BY_WEIGHT:
        if weight < cap:
            return rate
    return RATE_BY_WEIGHT[-1][1]


def estimate_price_ghs(distance_km, weight_kg=None) -> float:
    """Price = km x weight-tier rate. Under 1 ton within a city (~1 km): flat GHS 300.
    Otherwise min GHS 1,000 applies."""
    weight = float(weight_kg or 0)
    if (not distance_km or float(distance_km) <= WITHIN_CITY_KM) and weight < LIGHT_WEIGHT_MAX_KG:
        return WITHIN_CITY_MIN_GHS
    price = rate_for_weight(weight_kg) * float(distance_km or 0)
    return max(MINIMUM_PRICE_GHS, round(price, 2))


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
