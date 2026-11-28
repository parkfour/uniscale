import json
import logging
import math
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uniscale.config import read_json, vault_db_path, vault_locked, write_atomic
from uniscale.day import yesterday as _yesterday, zone

logger = logging.getLogger(__name__)


def _vault_dir(db_path: Optional[str] = None) -> str:
    return os.path.dirname(os.path.abspath(db_path or vault_db_path()))


def _locations_file(db_path: Optional[str] = None) -> str:
    return os.path.join(_vault_dir(db_path), "locations.jsonl")


def _places_file(db_path: Optional[str] = None) -> str:
    return os.path.join(_vault_dir(db_path), "places.json")


MAX_ACCURACY_M = 250.0  # fixes vaguer than this (cell-tower guesses) are ignored


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great-circle distance between two GPS points in meters."""
    r = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


@vault_locked


def ingest_location(
    lat: float,
    lon: float,
    timestamp: Optional[str] = None,
    epoch: Optional[int] = None,
    accuracy: Optional[float] = None,
    source: str = "owntracks",
    db_path: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Appends a raw GPS location report (e.g. from OwnTracks) to locations.jsonl.
    """
    now = datetime.now(timezone.utc)
    if epoch is None and timestamp is not None:
        try:
            # Parse ISO timestamp to epoch
            clean_ts = timestamp.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_ts)
            epoch = int(dt.timestamp())
        except Exception:
            epoch = int(now.timestamp())
    elif epoch is None:
        epoch = int(now.timestamp())

    if timestamp is None:
        timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    record: Dict[str, Any] = {
        "timestamp": timestamp,
        "epoch": int(epoch),
        "lat": round(float(lat), 7),
        "lon": round(float(lon), 7),
        "accuracy": round(float(accuracy), 1) if accuracy is not None else 15.0,
        "source": source
    }
    if extra:
        for k in ("altitude", "battery", "velocity", "tid"):
            if k in extra and extra[k] is not None:
                record[k] = extra[k]

    path = _locations_file(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return record


@vault_locked


def ingest_owntracks_payload(payload: Any, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Parses and ingests OwnTracks JSON payload (single object or list of location objects).
    """
    items = payload if isinstance(payload, list) else [payload]
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # OwnTracks standard keys: _type=="location", lat, lon, tst, acc
        msg_type = item.get("_type")
        if msg_type and msg_type != "location":
            continue
        lat = item.get("lat")
        lon = item.get("lon")
        if lat is None or lon is None:
            continue
        tst = item.get("tst")
        acc = item.get("acc")
        extra = {
            "altitude": item.get("alt"),
            "battery": item.get("batt"),
            "velocity": item.get("vel"),
            "tid": item.get("tid")
        }
        res = ingest_location(
            lat=float(lat),
            lon=float(lon),
            epoch=int(tst) if tst is not None else None,
            accuracy=float(acc) if acc is not None else None,
            source="owntracks",
            db_path=db_path,
            extra=extra
        )
        results.append(res)
    return results


def _load_raw_locations(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Loads all raw GPS locations from locations.jsonl and measurements.jsonl."""
    locs: List[Dict[str, Any]] = []
    l_file = _locations_file(db_path)
    if os.path.exists(l_file):
        with open(l_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        locs.append(json.loads(line))
                    except Exception:
                        pass

    # Also check measurements.jsonl for any historical type='location' records
    m_file = db_path or vault_db_path()
    if os.path.exists(m_file):
        with open(m_file, "r", encoding="utf-8") as f:
            for line in f:
                if '"location"' in line:
                    try:
                        d = json.loads(line)
                        if d.get("type") == "location" and "lat" in d and "lon" in d:
                            locs.append(d)
                    except Exception:
                        pass

    return locs


def _load_places_state(db_path: Optional[str] = None) -> Dict[str, Any]:
    data = read_json(_places_file(db_path), None)
    if not (isinstance(data, dict) and "places" in data):
        return {"places": [], "known": []}
    if "known" not in data:  # older files: what was named is what we know
        data["known"] = []
        for p in data["places"]:
            if p.get("name") and (p.get("avg_lat") or p.get("avg_lon")):
                _remember(data, p["name"], p["avg_lat"], p["avg_lon"])
        _save_places_state(data, db_path)  # ids must stay put
    return data


def _save_places_state(state: Dict[str, Any], db_path: Optional[str] = None) -> None:
    write_atomic(_places_file(db_path), json.dumps(state, indent=2))


def _format_dwell(seconds: int) -> str:
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    if hrs > 0 and mins > 0:
        return f"{hrs}h {mins}m"
    elif hrs > 0:
        return f"{hrs}h"
    else:
        return f"{mins}m"


# --- Known places: a name picked for one stay is remembered and guessed for later stays there ----

PLACE_RADIUS_M = 150.0
HOME_RADIUS_M = 600.0       # GPS at home wanders a few hundred metres (indoors, wifi fixes)
LONG_STAY_S = 4 * 3600      # an unnamed stay this long ...
LONG_STAY_HOME_M = 2000.0   # ... this close to Home is taken to be Home


def _is_home(name: Optional[str]) -> bool:
    return (name or "").strip().lower() == "home"


def _remember(state: Dict[str, Any], name: str, lat: float, lon: float) -> None:
    """Adds a known place at (lat, lon) unless one of the same name already covers that spot."""
    import uuid
    known = state.setdefault("known", [])
    for k in known:
        if k["name"].lower() == name.lower() and haversine_distance(lat, lon, k["lat"], k["lon"]) <= k["radius_m"]:
            return
    known.append({"id": uuid.uuid4().hex[:8], "name": name, "lat": round(lat, 6), "lon": round(lon, 6),
                  "radius_m": HOME_RADIUS_M if _is_home(name) else PLACE_RADIUS_M})


def _guess_name(stay: Dict[str, Any], known: List[Dict[str, Any]]) -> Optional[str]:
    lat, lon = stay.get("avg_lat") or 0.0, stay.get("avg_lon") or 0.0
    if not (lat or lon):
        return None
    covering = [(haversine_distance(lat, lon, k["lat"], k["lon"]), k) for k in known]
    inside = [(d, k) for d, k in covering if d <= k["radius_m"]]
    if inside:
        return min(inside, key=lambda dk: dk[0])[1]["name"]
    if stay.get("duration_seconds", 0) >= LONG_STAY_S:
        homes = [k for d, k in covering if _is_home(k["name"]) and d <= LONG_STAY_HOME_M]
        if homes:
            return homes[0]["name"]
    return None


def _labelled(stay: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    """The stay with its name: the one the user picked, else a guess from the known places."""
    if stay.get("name"):
        return dict(stay, name_guessed=False)
    guess = _guess_name(stay, state.get("known", []))
    return dict(stay, name=guess, name_guessed=guess is not None)


@vault_locked
def detect_home(points: List[Dict[str, Any]], db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Where the phone sleeps: the busiest ~100 m cell of overnight (00-05 local) fixes, averaged.
    Adds it as the known place "Home" unless one already exists. Needs a couple of weeks of nights.
    """
    from collections import Counter
    state = _load_places_state(db_path)
    if any(_is_home(k["name"]) for k in state.get("known", [])):
        return None
    night = [p for p in points if float(p.get("accuracy", 15.0)) <= MAX_ACCURACY_M and p.get("epoch")
             and datetime.fromtimestamp(p["epoch"], tz=zone(db_path)).hour < 5]
    if len(night) < 20:
        return None
    (clat, clon), _ = Counter((round(p["lat"], 3), round(p["lon"], 3)) for p in night).most_common(1)[0]
    near = [p for p in night if haversine_distance(clat, clon, p["lat"], p["lon"]) <= 100.0]
    _remember(state, "Home", sum(p["lat"] for p in near) / len(near), sum(p["lon"] for p in near) / len(near))
    _save_places_state(state, db_path)
    return state["known"][-1]


@vault_locked
def forget_known_place(known_id: str, db_path: Optional[str] = None) -> bool:
    state = _load_places_state(db_path)
    kept = [k for k in state.get("known", []) if k.get("id") != known_id]
    if len(kept) == len(state.get("known", [])):
        return False
    state["known"] = kept
    _save_places_state(state, db_path)
    return True


def nearby_pois(lat: float, lon: float, radius_m: int = 200) -> List[Dict[str, Any]]:
    """Named things within radius_m from OpenStreetMap (Overpass), nearest first."""
    import urllib.parse
    import urllib.request
    q = (f"[out:json][timeout:15];(node[\"name\"](around:{radius_m},{lat},{lon});"
         f"way[\"name\"](around:{radius_m},{lat},{lon}););out center tags 40;")
    req = urllib.request.Request("https://overpass-api.de/api/interpreter",
                                 data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": "uniscale"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        elements = json.loads(resp.read().decode("utf-8")).get("elements", [])
    out: Dict[str, Dict[str, Any]] = {}
    for el in elements:
        tags = el.get("tags", {})
        plat = el.get("lat", el.get("center", {}).get("lat"))
        plon = el.get("lon", el.get("center", {}).get("lon"))
        if not tags.get("name") or plat is None:
            continue
        kind = next((tags[k] for k in ("amenity", "shop", "leisure", "tourism", "office", "building", "highway")
                     if tags.get(k)), None)
        poi = {"name": tags["name"], "type": kind, "distance_m": round(haversine_distance(lat, lon, plat, plon))}
        if poi["name"].lower() not in out or poi["distance_m"] < out[poi["name"].lower()]["distance_m"]:
            out[poi["name"].lower()] = poi
    return sorted(out.values(), key=lambda p: p["distance_m"])


def suggest_names(place_id: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Choices for naming a stay: known places nearby first, then OpenStreetMap POIs."""
    state = _load_places_state(db_path)
    stay = next((p for p in state.get("places", []) if p.get("id") == place_id), None)
    if stay is None:
        raise KeyError(place_id)
    lat, lon = stay.get("avg_lat") or 0.0, stay.get("avg_lon") or 0.0
    if not (lat or lon):
        return []
    known = sorted(({"name": k["name"], "type": "known", "distance_m": round(haversine_distance(lat, lon, k["lat"], k["lon"]))}
                    for k in state.get("known", [])), key=lambda k: k["distance_m"])
    known = [k for k in known if k["distance_m"] <= 2000]
    try:
        pois = nearby_pois(lat, lon, int(min(max(stay.get("confidence_range", 100.0), 100.0), 300.0)))
    except Exception as e:  # Overpass down or slow: the known places still help
        logger.warning(f"Overpass lookup failed: {e}")
        pois = []
    seen = {k["name"].lower() for k in known}
    return known + [p for p in pois if p["name"].lower() not in seen]


@vault_locked
def compute_dwell_places(
    db_path: Optional[str] = None,
    target_date: Optional[str] = None,
    min_dwell_seconds: int = 3600,
    max_radius_m: float = 100.0,
    points: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """
    Identifies places where the user spent more than min_dwell_seconds (default 1 hr / 3600 s)
    during target_date (default previous day).
    Calculates average GPS position, confidence range, and preserves existing POI assignments.
    """
    if not target_date or target_date == "previous":
        target_date = _yesterday(db_path)

    raw_points = points if points is not None else _load_raw_locations(db_path)
    if not raw_points:
        return []

    # OwnTracks reports on arrival and then stays quiet until you move, so a stay lasts from its
    # first report until the first report somewhere else. The day starts wherever the last report
    # before midnight put you, and its last stay runs to midnight (or now, for today).
    tz = zone(db_path)
    day_start = int(datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=tz).timestamp())
    day_end = min(int((datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz).timestamp()),
                  int(datetime.now(timezone.utc).timestamp()))
    raw_points = [p for p in raw_points if float(p.get("accuracy", 15.0)) <= MAX_ACCURACY_M]
    before = [p for p in raw_points if p.get("epoch") and day_start - 86400 <= p["epoch"] < day_start]

    # Filter points for target_date
    day_points: List[Dict[str, Any]] = []
    if before:
        carried = dict(max(before, key=lambda x: x["epoch"]))
        carried["epoch"] = day_start
        day_points.append(carried)
    for p in raw_points:
        p_epoch = p.get("epoch")
        p_ts = str(p.get("timestamp") or "")
        if p_epoch:
            dt = datetime.fromtimestamp(p_epoch, tz=tz)
            p_date = dt.strftime("%Y-%m-%d")
        elif p_ts:
            p_date = p_ts[:10]
        else:
            continue

        if p_date == target_date:
            day_points.append(p)

    if not day_points:
        return []

    # Sort points chronologically
    day_points.sort(key=lambda x: x.get("epoch") or 0)

    # Cluster points into dwell sessions
    clusters: List[List[Dict[str, Any]]] = []
    current_cluster: List[Dict[str, Any]] = [day_points[0]]

    def cluster_centroid(pts: List[Dict[str, Any]]) -> Tuple[float, float]:
        clat = sum(pt["lat"] for pt in pts) / len(pts)
        clon = sum(pt["lon"] for pt in pts) / len(pts)
        return clat, clon

    for pt in day_points[1:]:
        c_lat, c_lon = cluster_centroid(current_cluster)
        dist = haversine_distance(c_lat, c_lon, pt["lat"], pt["lon"])
        eff_radius = max(max_radius_m, float(pt.get("accuracy", 15.0)))

        if dist <= eff_radius:
            current_cluster.append(pt)
        else:
            clusters.append(current_cluster)
            current_cluster = [pt]

    if current_cluster:
        clusters.append(current_cluster)

    # Filter clusters by dwell duration >= min_dwell_seconds
    new_places: List[Dict[str, Any]] = []
    for idx, cluster in enumerate(clusters):
        start_epoch = cluster[0].get("epoch") or 0
        end_epoch = (clusters[idx + 1][0].get("epoch") or 0) if idx + 1 < len(clusters) else day_end
        duration = end_epoch - start_epoch

        if duration >= min_dwell_seconds:
            avg_lat, avg_lon = cluster_centroid(cluster)

            # Calculate confidence range: max distance from centroid + average accuracy, min 50m
            max_drift = max(haversine_distance(avg_lat, avg_lon, pt["lat"], pt["lon"]) for pt in cluster)
            avg_acc = sum(float(pt.get("accuracy", 15.0)) for pt in cluster) / len(cluster)
            confidence_range = max(50.0, round(max_drift + (avg_acc / 2.0), 1))

            start_dt = datetime.fromtimestamp(start_epoch, tz=tz)
            end_dt = datetime.fromtimestamp(end_epoch, tz=tz)

            place_id = f"place_{target_date}_{int(avg_lat*1000):+07d}_{int(avg_lon*1000):+07d}_{idx}"

            new_places.append({
                "id": place_id,
                "date": target_date,
                "avg_lat": round(avg_lat, 6),
                "avg_lon": round(avg_lon, 6),
                "confidence_range": confidence_range,
                "start_time": start_dt.isoformat(),
                "end_time": end_dt.isoformat(),
                "duration_seconds": int(duration),
                "dwell_formatted": _format_dwell(int(duration)),
                "points_count": len(cluster),
                "name": None,
                "poi_type": None,
                "candidate_pois": [],
                "is_deleted": False
            })

    # Merge / preserve existing state from places.json
    state = _load_places_state(db_path)
    existing_map = {p["id"]: p for p in state.get("places", [])}

    # A rescan of a day keeps what the user set on its stays (matched within 150 m)
    merged_places: List[Dict[str, Any]] = []
    for np in new_places:
        matched_existing = None
        for ep in existing_map.values():
            if ep.get("date") == target_date:
                d = haversine_distance(np["avg_lat"], np["avg_lon"], ep.get("avg_lat", 0), ep.get("avg_lon", 0))
                if d <= 150.0:
                    matched_existing = ep
                    break

        if matched_existing:
            np["id"] = matched_existing["id"]
            np["name"] = matched_existing.get("name")
            np["poi_type"] = matched_existing.get("poi_type")
            np["candidate_pois"] = matched_existing.get("candidate_pois", [])
            np["is_deleted"] = matched_existing.get("is_deleted", False)

        merged_places.append(np)

    # Keep places for other dates unchanged
    other_dates_places = [p for p in state.get("places", []) if p.get("date") != target_date]
    all_places = other_dates_places + merged_places

    state["places"] = all_places
    _save_places_state(state, db_path)

    return [_labelled(p, state) for p in merged_places]


def _shown(place: Dict[str, Any], include_home: bool = False) -> bool:
    """Deleted places and stays at a place labelled Home (any of them) are kept but not listed."""
    return not place.get("is_deleted", False) and (include_home or not _is_home(place.get("name")))


@vault_locked
def get_dwell_places(
    db_path: Optional[str] = None,
    target_date: Optional[str] = None,
    include_deleted: bool = False,
    include_home: bool = False
) -> Dict[str, Any]:
    """
    Returns dwell places for the specified date (default previous day), each named by the user or
    guessed from the known places (name_guessed), plus the known places themselves.
    If no places are indexed for that date yet, runs compute_dwell_places.
    """
    state = _load_places_state(db_path)

    if target_date == "all":
        try:
            sync_recorder(db_path)
        except Exception as e:  # recorder down or misconfigured: show what we have
            logger.warning(f"OwnTracks Recorder sync failed: {e}")
        state = _load_places_state(db_path)
        yesterday = _yesterday(db_path)
        if not any(p.get("date") == yesterday for p in state.get("places", [])):
            # ponytail: rescans locations.jsonl on each call while yesterday has no place; index by date if it grows large
            compute_dwell_places(db_path, yesterday)
            state = _load_places_state(db_path)
        places = [p for p in (_labelled(p, state) for p in state.get("places", []))
                  if include_deleted or _shown(p, include_home)]
        places.sort(key=lambda p: (p.get("date", ""), p.get("duration_seconds", 0)), reverse=True)
        return {
            "date": "all",
            "places": places,
            "count": len(places),
            "known": state.get("known", []),
            "min_dwell_minutes": 60
        }

    if not target_date or target_date == "previous":
        target_date = _yesterday(db_path)

    places = [p for p in state.get("places", []) if p.get("date") == target_date]

    if not places:
        places = compute_dwell_places(db_path, target_date)
        state = _load_places_state(db_path)
    places = [_labelled(p, state) for p in places]

    if not include_deleted:
        places = [p for p in places if _shown(p, include_home)]

    # Sort places by dwell duration descending
    places.sort(key=lambda p: p.get("duration_seconds", 0), reverse=True)

    return {
        "date": target_date,
        "places": places,
        "count": len(places),
        "known": state.get("known", []),
        "min_dwell_minutes": 60
    }


@vault_locked
def update_dwell_place(
    place_id: str,
    name: Optional[str] = None,
    poi_type: Optional[str] = None,
    candidate_pois: Optional[List[str]] = None,
    is_deleted: Optional[bool] = None,
    db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Updates place attributes (name, POI, candidate POIs, or deleted flag).
    """
    state = _load_places_state(db_path)
    updated_place = None
    for p in state.get("places", []):
        if p.get("id") == place_id:
            if name is not None:
                p["name"] = name.strip() or None
                if p["name"] and (p.get("avg_lat") or p.get("avg_lon")):
                    _remember(state, p["name"], p["avg_lat"], p["avg_lon"])
            if poi_type is not None:
                p["poi_type"] = poi_type.strip() if poi_type.strip() else None
            if candidate_pois is not None:
                p["candidate_pois"] = candidate_pois
            if is_deleted is not None:
                p["is_deleted"] = bool(is_deleted)
            p["updated_at"] = datetime.now(timezone.utc).isoformat()
            updated_place = p
            break

    if updated_place:
        _save_places_state(state, db_path)
        return _labelled(updated_place, state)

    return updated_place


@vault_locked
def delete_dwell_place(place_id: str, db_path: Optional[str] = None) -> bool:
    """Marks a dwell place as deleted."""
    res = update_dwell_place(place_id, is_deleted=True, db_path=db_path)
    return res is not None


@vault_locked
def add_custom_place(
    date_str: str,
    name: str,
    avg_lat: float = 0.0,
    avg_lon: float = 0.0,
    duration_seconds: int = 3600,
    confidence_range: float = 50.0,
    poi_type: Optional[str] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """Manually adds or registers a known dwell place."""
    import uuid
    import re
    state = _load_places_state(db_path)
    now = datetime.now(timezone.utc)
    slug = re.sub(r'[^a-zA-Z0-9]', '', name.lower())[:10] or "place"
    uid = uuid.uuid4().hex[:6]
    place_id = f"custom_{date_str}_{slug}_{uid}"

    place = {
        "id": place_id,
        "date": date_str,
        "avg_lat": round(float(avg_lat), 6),
        "avg_lon": round(float(avg_lon), 6),
        "confidence_range": round(float(confidence_range), 1),
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(seconds=duration_seconds)).isoformat(),
        "duration_seconds": int(duration_seconds),
        "dwell_formatted": _format_dwell(int(duration_seconds)),
        "points_count": 1,
        "name": name.strip(),
        "poi_type": poi_type or "custom",
        "candidate_pois": [name.strip()],
        "is_deleted": False
    }

    state.setdefault("places", []).append(place)
    _save_places_state(state, db_path)
    return place


# --- OwnTracks Recorder (pull) -------------------------------------------------------------

def _recorder_get(cfg, path: str, params: Dict[str, str]) -> Any:
    import base64
    import urllib.parse
    import urllib.request
    url = cfg.recorder_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if cfg.username:
        token = base64.b64encode(f"{cfg.username}:{cfg.password or ''}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _recorder_target(cfg) -> Tuple[str, str]:
    """The configured user/device, or the Recorder's only one when left empty."""
    user, device = cfg.user, cfg.device
    if not user:
        users = _recorder_get(cfg, "/api/0/list", {}).get("results", [])
        if len(users) != 1:
            raise ValueError(f"set [owntracks] user; the Recorder has {users}")
        user = users[0]
    if not device:
        devices = _recorder_get(cfg, "/api/0/list", {"user": user}).get("results", [])
        if len(devices) != 1:
            raise ValueError(f"set [owntracks] device; {user} has {devices}")
        device = devices[0]
    return user, device


def fetch_recorder_day(cfg, day: str, tz=None) -> List[Dict[str, Any]]:
    """One local calendar day of points from the Recorder, as {lat, lon, epoch, accuracy}."""
    user, device = _recorder_target(cfg)
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz or zone()) - timedelta(days=1)  # day before too: where the day began
    end = start + timedelta(days=2)
    fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    body = _recorder_get(cfg, "/api/0/locations", {"user": user, "device": device, "from": fmt(start), "to": fmt(end)})
    items = body.get("data", []) if isinstance(body, dict) else body
    pts = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("lat") is None or it.get("lon") is None or it.get("tst") is None:
            continue
        pts.append({"lat": float(it["lat"]), "lon": float(it["lon"]), "epoch": int(it["tst"]),
                    "accuracy": float(it.get("acc") or 15.0)})
    return pts


@vault_locked
def sync_recorder(db_path: Optional[str] = None, days: Optional[int] = None, cfg=None) -> List[str]:
    """
    Turns each finished day not yet read from the OwnTracks Recorder into dwell places.
    `days` reaches back that many days (re-reading them); otherwise it continues after the last
    synced day, starting with yesterday on the first run. Returns the days read.
    """
    if cfg is None:
        from uniscale.config import load_config
        cfg = load_config().owntracks
    if not cfg.recorder_url:
        return []
    yesterday = datetime.strptime(_yesterday(db_path), "%Y-%m-%d").date()
    state = _load_places_state(db_path)
    if days:
        first = yesterday - timedelta(days=days - 1)
    elif state.get("recorder_synced_through"):
        first = datetime.strptime(state["recorder_synced_through"], "%Y-%m-%d").date() + timedelta(days=1)
    else:
        first = yesterday
    done = []
    seen: List[Dict[str, Any]] = []
    d = first
    while d <= yesterday:
        day = d.strftime("%Y-%m-%d")
        pts = fetch_recorder_day(cfg, day, zone(db_path))
        seen += pts
        compute_dwell_places(db_path, day, points=pts)
        state = _load_places_state(db_path)
        if day > state.get("recorder_synced_through", ""):
            state["recorder_synced_through"] = day
            _save_places_state(state, db_path)
        done.append(day)
        d += timedelta(days=1)
    if seen:
        detect_home(seen, db_path)
    return done
