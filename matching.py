"""Matching engine: filters and ranks trucks against loads."""
from __future__ import annotations

import math
from datetime import datetime

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


def estimate_price_ghs(distance_km, weight_kg=None) -> float:
    """Simple pricing model: GHS 5 per km, GHS 300 minimum."""
    if not distance_km:
        return 300.0
    return max(300.0, 5.0 * float(distance_km))


def _truck_anchor(truck):
    """Effective truck position: destination if set (truck heading home), else origin."""
    lat = truck.dest_lat if truck.dest_lat is not None else truck.origin_lat
    lng = truck.dest_lng if truck.dest_lng is not None else truck.origin_lng
    return lat, lng


def is_reverse_route(truck, load) -> bool:
    """True when hauling this load takes the truck back toward its origin."""
    if not truck.dest_city or not truck.origin_city:
        return False
    return (
        truck.dest_city.strip().lower() in load.dest_city.lower()
        and truck.origin_city.strip().lower() in load.origin_city.lower()
    )


def score_match(load, truck, distance_km: float) -> float:
    score = 100.0
    if truck.capacity_kg:
        margin = (truck.capacity_kg - load.weight_kg) / truck.capacity_kg
        score += max(0.0, margin) * 40.0
    if is_reverse_route(truck, load):
        score += 50.0
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
        tlat, tlng = _truck_anchor(truck)
        if tlat is None or tlng is None or load.origin_lat is None or load.origin_lng is None:
            continue
        km = haversine_km(tlat, tlng, load.origin_lat, load.origin_lng)
        if km > max_distance_km:
            continue
        results.append({"truck": truck, "distance_km": km, "score": score_match(load, truck, km)})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def run_marketplace_matching(db):
    """Match all open loads against available trucks. Returns {load_id: matches}."""
    import models

    loads = db.query(models.Load).filter(models.Load.status == "open").all()
    trucks = db.query(models.Truck).filter(models.Truck.status == "available").all()
    return {load.id: compatible_trucks(load, trucks) for load in loads}
