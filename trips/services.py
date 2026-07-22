import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

STATUSES = ("off_duty", "sleeper", "driving", "on_duty")
MILES_PER_KM = 0.621371


class TripError(Exception):
    pass


def _json_request(url, timeout, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TripError(f"External route service returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TripError("The map service is temporarily unavailable. Please retry.") from exc


def _geocode(place):
    query = urllib.parse.urlencode({"q": place.strip(), "format": "jsonv2", "limit": 1})
    data = _json_request(
        "https://nominatim.openstreetmap.org/search?" + query,
        timeout=12,
        headers={"User-Agent": "eld-assessment/1.0 (trip-planner)"},
    )
    if not data:
        raise TripError(f"Could not find location: {place}")
    item = data[0]
    return {"lat": float(item["lat"]), "lon": float(item["lon"]), "label": item.get("display_name", place.strip())}


def search_locations(query):
    query = query.strip()
    if len(query) < 3:
        return []
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1})
    data = _json_request(
        "https://nominatim.openstreetmap.org/search?" + params,
        timeout=12,
        headers={"User-Agent": "eld-assessment/1.0 (trip-planner)"},
    )
    results = []
    for item in data:
        address = item.get("address", {})
        primary = address.get("city") or address.get("town") or address.get("village") or address.get("municipality") or address.get("county")
        parts = [part for part in (primary, address.get("state"), address.get("country")) if part]
        compact = ", ".join(dict.fromkeys(parts))
        full_label = item.get("display_name", query)
        results.append({"lat": float(item["lat"]), "lon": float(item["lon"]), "label": compact or full_label, "full_label": full_label})
    return results


def _distance_km(first, second):
    """Return the great-circle distance between two lon/lat points."""
    lat1, lon1 = math.radians(first["lat"]), math.radians(first["lon"])
    lat2, lon2 = math.radians(second["lat"]), math.radians(second["lon"])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _route(points):
    coordinates = ";".join(f"{point['lon']},{point['lat']}" for point in points)
    url = f"https://router.project-osrm.org/route/v1/driving/{coordinates}?overview=full&geometries=geojson&steps=false"
    data = _json_request(url, timeout=20)
    if data.get("code") != "Ok" or not data.get("routes"):
        raise TripError("The routing service could not calculate this trip.")
    route = data["routes"][0]
    legs = route.get("legs", [])
    if len(legs) != len(points) - 1:
        raise TripError("The routing service returned an incomplete route.")
    snapped_waypoints = data.get("waypoints", [])
    if len(snapped_waypoints) != len(points):
        raise TripError("The routing service returned incomplete location data.")
    for point, waypoint in zip(points, snapped_waypoints):
        location = waypoint.get("location", [])
        if len(location) != 2:
            raise TripError("The routing service returned invalid location data.")
        snapped = {"lon": float(location[0]), "lat": float(location[1])}
        # OSRM can return an apparently successful route while snapping a
        # location across an ocean to the nearest supported road. Reject it
        # instead of drawing a misleading route on the map.
        if _distance_km(point, snapped) > 100:
            raise TripError(
                f"No drivable road route could be found to {point['label']}. "
                "Check the location or use locations connected by roads."
            )
    normalized_legs = [
        {
            "distance_miles": round(leg.get("distance", 0) / 1609.344, 2),
            "duration_hours": round(leg.get("duration", 0) / 3600, 4),
            "from": points[index]["label"],
            "to": points[index + 1]["label"],
        }
        for index, leg in enumerate(legs)
    ]
    return {
        "distance_miles": round(route["distance"] / 1609.344, 2),
        "duration_hours": round(route["duration"] / 3600, 4),
        "geometry": route.get("geometry", {}),
        "waypoints": points,
        "legs": normalized_legs,
    }


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _segment(status, start, end, location, remark, miles=0, progress_miles=0):
    return {
        "status": status,
        "start": _iso(start),
        "end": _iso(end),
        "location": location,
        "remark": remark,
        "miles": round(miles, 2),
        "progress_miles": round(progress_miles, 2),
    }


def _schedule(legs, start, cycle_used, current, pickup, dropoff):
    """Create a conservative property-carrying schedule for current assumptions.

    This intentionally uses a single 10-hour rest (no split sleeper) and treats
    every on-duty activity as consuming the 14-hour window and 70-hour cycle.
    """
    segments = []
    now = start
    drive_left = 11.0
    window_left = 14.0
    cycle_left = 70.0 - cycle_used
    driving_since_break = 0.0
    progress_miles = 0.0
    next_fuel = 1000.0
    total_miles = sum(leg["distance_miles"] for leg in legs)
    total_duration = sum(leg["duration_hours"] for leg in legs)
    if total_duration <= 0 or total_miles <= 0:
        raise TripError("The route has no drivable distance.")

    def activity(status, hours, location, remark, counts_window=False, resets_break=False):
        nonlocal now, window_left, cycle_left, driving_since_break
        end = now + timedelta(hours=hours)
        segments.append(_segment(status, now, end, location, remark, progress_miles=progress_miles))
        now = end
        if counts_window:
            window_left -= hours
            cycle_left -= hours
        elif status == "off_duty":
            window_left -= hours
        if resets_break and hours >= 0.5:
            driving_since_break = 0.0

    def rest_if_needed():
        nonlocal drive_left, window_left, cycle_left, driving_since_break
        if drive_left > 0.001 and window_left > 0.001 and cycle_left > 0.001:
            return
        restart = cycle_left <= 0.001
        hours = 34 if restart else 10
        remark = "34-hour restart" if restart else "10-hour sleeper berth"
        activity("sleeper", hours, "En route", remark)
        drive_left = 11.0
        window_left = 14.0
        driving_since_break = 0.0
        if restart:
            cycle_left = 70.0

    def drive_leg(distance, duration, destination):
        nonlocal now, drive_left, window_left, cycle_left, driving_since_break, progress_miles, next_fuel
        remaining_hours = duration
        remaining_miles = distance
        while remaining_hours > 0.001:
            rest_if_needed()
            if driving_since_break >= 8.0:
                activity("off_duty", 0.5, "En route", "30-minute rest break", resets_break=True)
                continue
            miles_until_fuel = min(next_fuel - progress_miles, remaining_miles)
            speed = distance / duration
            hours_to_fuel = miles_until_fuel / speed if miles_until_fuel > 0.001 else remaining_hours
            break_remaining = max(8.0 - driving_since_break, 0.001)
            chunk = min(remaining_hours, drive_left, window_left, cycle_left, hours_to_fuel, break_remaining)
            if chunk <= 0.001:
                rest_if_needed()
                continue
            miles = distance * (chunk / duration)
            end = now + timedelta(hours=chunk)
            segments.append(_segment("driving", now, end, destination if remaining_hours - chunk <= 0.001 else "En route", "Driving", miles=miles, progress_miles=progress_miles))
            now = end
            remaining_hours -= chunk
            remaining_miles -= miles
            drive_left -= chunk
            window_left -= chunk
            cycle_left -= chunk
            driving_since_break += chunk
            progress_miles += miles
            if progress_miles >= next_fuel - 0.001 and remaining_hours > 0.001:
                activity("on_duty", 0.5, "En route", "Fueling (30-minute break)", counts_window=True, resets_break=True)
                next_fuel += 1000.0

    activity("on_duty", 0.5, current, "Pre-trip inspection", counts_window=True)
    for index, leg in enumerate(legs):
        destination = pickup if index == 0 else dropoff
        drive_leg(leg["distance_miles"], leg["duration_hours"], destination)
        if index == 0:
            activity("on_duty", 1.0, pickup, "Pickup", counts_window=True, resets_break=True)
        else:
            activity("on_duty", 1.0, dropoff, "Drop-off", counts_window=True, resets_break=True)
    activity("on_duty", 0.5, dropoff, "Post-trip inspection", counts_window=True)
    return segments, progress_miles


def _daily_logs(segments, start):
    end = max(datetime.fromisoformat(segment["end"].replace("Z", "+00:00")) for segment in segments)
    logs = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        day_end = day + timedelta(days=1)
        daily = []
        totals = {status: 0.0 for status in STATUSES}
        for segment in segments:
            seg_start = datetime.fromisoformat(segment["start"].replace("Z", "+00:00"))
            seg_end = datetime.fromisoformat(segment["end"].replace("Z", "+00:00"))
            overlap_start, overlap_end = max(day, seg_start), min(day_end, seg_end)
            if overlap_end <= overlap_start:
                continue
            hours = (overlap_end - overlap_start).total_seconds() / 3600
            totals[segment["status"]] += hours
            original_hours = max((seg_end - seg_start).total_seconds() / 3600, 0.001)
            overlap_miles = segment.get("miles", 0) * (hours / original_hours)
            daily.append({**segment, "start": _iso(overlap_start), "end": _iso(overlap_end), "miles": round(overlap_miles, 2)})
        daily.sort(key=lambda item: item["start"])
        filled = []
        cursor = day
        for segment in daily:
            segment_start = datetime.fromisoformat(segment["start"].replace("Z", "+00:00"))
            if segment_start > cursor:
                gap_hours = (segment_start - cursor).total_seconds() / 3600
                filled.append(_segment("off_duty", cursor, segment_start, "", "Off duty"))
                totals["off_duty"] += gap_hours
            filled.append(segment)
            cursor = datetime.fromisoformat(segment["end"].replace("Z", "+00:00"))
        if cursor < day_end:
            gap_hours = (day_end - cursor).total_seconds() / 3600
            filled.append(_segment("off_duty", cursor, day_end, "", "Off duty"))
            totals["off_duty"] += gap_hours
        logs.append({
            "date": day.date().isoformat(),
            "segments": filled,
            "totals": {key: round(value, 2) for key, value in totals.items()},
            "total_driving_miles": round(sum(item.get("miles", 0) for item in filled if item["status"] == "driving"), 1),
            "total_hours": 24,
        })
        day = day_end
    return logs


def calculate_trip_plan(payload):
    required = ("current_location", "pickup_location", "dropoff_location")
    missing = [key for key in required if not str(payload.get(key, "")).strip()]
    if missing:
        raise ValueError("Missing location: " + ", ".join(missing))
    try:
        cycle_used = float(payload.get("cycle_used_hours", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Current cycle used must be a number.") from exc
    if cycle_used < 0 or cycle_used > 70:
        raise ValueError("Current cycle used must be between 0 and 70 hours.")
    start_value = payload.get("start_datetime") or datetime.now(timezone.utc).replace(hour=6, minute=30, second=0, microsecond=0).isoformat()
    try:
        start = datetime.fromisoformat(str(start_value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Start date/time must be a valid ISO date-time.") from exc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    current, pickup, dropoff = map(_geocode, [payload["current_location"], payload["pickup_location"], payload["dropoff_location"]])
    route = _route([current, pickup, dropoff])
    segments, miles = _schedule(route["legs"], start, cycle_used, current["label"], pickup["label"], dropoff["label"])
    stops = []
    for segment in segments:
        if segment["status"] == "driving":
            continue
        remark = segment["remark"]
        stop_type = "pickup" if remark == "Pickup" else "dropoff" if remark == "Drop-off" else "fuel" if remark.startswith("Fueling") else "rest" if "rest" in remark or "restart" in remark else "break"
        stops.append({"type": stop_type, "time": segment["start"], "location": segment["location"], "remark": remark, "progress_miles": segment["progress_miles"], "route_progress": round(segment["progress_miles"] / max(route["distance_miles"], 1), 4)})
    logs = _daily_logs(segments, start)
    return {
        "route": route,
        "stops": stops,
        "daily_logs": logs,
        "summary": {
            "distance_miles": round(route["distance_miles"], 1),
            "route_hours": round(route["duration_hours"], 2),
            "driving_miles": round(miles, 1),
            "days": len(logs),
            "cycle_hours_used": round(sum(log["totals"]["on_duty"] + log["totals"]["driving"] for log in logs), 2),
        },
    }
