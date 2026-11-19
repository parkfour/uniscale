import base64
import binascii
import ipaddress
import json
import logging
import os
import secrets
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from uniscale.config import ProfileConfig, UniscaleConfig, current_user, load_config, load_profile, normalize_user, save_profile, vault_db_path
from uniscale.models import (
    ActivityWorkout,
    BloodPressureMeasurement,
    BloodTestMeasurement,
    DailyActivity,
    EcgRecording,
    GenomicProfile,
    GenomicVariant,
    SleepSession,
    SupplementIntake,
    SupplementItem,
    WeightMeasurement,
)

logger = logging.getLogger(__name__)

config: UniscaleConfig = load_config()

app = FastAPI(
    title="UniScale Health Service",
    description="Unified, offline-first personal health service for Scales, ECG, Blood Pressure, and Garmin watches.",
    version="0.4.0"
)

MAX_DNA_UPLOAD_CHARS = 50 * 1024 * 1024
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # per file; whole-genome Promethease report zips can be large
MAX_ARCHIVE_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024  # full Garmin / Google Takeout exports


def _presented_token(auth_header: str) -> str:
    """The secret from a Bearer header, or the password from a Basic one."""
    scheme, _, credential = auth_header.partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer":
        return credential.strip()
    if scheme == "basic":
        try:
            decoded = base64.b64decode(credential.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return ""
        return decoded.partition(":")[2]
    return ""


def _from_trusted_proxy(request: Request) -> bool:
    """Whether the TCP peer is one of auth.trusted_proxies (addresses or CIDRs)."""
    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        return False
    for net in config.auth.trusted_proxies:
        try:
            if peer in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            logger.warning(f"Ignoring invalid auth.trusted_proxies entry {net!r}")
    return False


def _token_user(presented: str) -> Optional[str]:
    """The user an [auth.tokens] token belongs to (constant-time compare against every token)."""
    match = None
    for token, user in config.auth.tokens.items():
        if secrets.compare_digest(presented.encode(), token.encode()):
            match = user
    return match


def _unauthorized() -> Response:
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="UniScale"'})


# Reachable without auth: the health check, and the installable-app (PWA) files, which hold no data.
# Browsers fetch the manifest and icons without credentials, so they must not need any.
PUBLIC_PATHS = {"/health", "/manifest.webmanifest", "/sw.js"}


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """
    Single-user mode: when server.api_token is set, every route except /health needs it, as a
    Bearer token (Android app, daemon) or as the HTTP Basic password, which browsers prompt for
    natively and then resend on the dashboard's fetches.

    Multi-user mode: every route except /health must resolve to a user, either from an
    [auth.tokens] token (Bearer or Basic password) or from auth.user_header, which is only
    believed when the request comes from a trusted proxy. The user scopes all data access for
    the request through config.current_user; there is no fallback user.
    """
    if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/icons/"):
        return await call_next(request)
    presented = _presented_token(request.headers.get("authorization", ""))

    if config.auth.mode != "multi":
        token = config.server.api_token
        if token and not (presented and secrets.compare_digest(presented.encode(), token.encode())):
            return _unauthorized()
        return await call_next(request)

    user = _token_user(presented) if presented else None
    if user is None and _from_trusted_proxy(request):
        user = request.headers.get(config.auth.user_header)
    if not user:
        return _unauthorized()
    try:
        user = normalize_user(user)
    except ValueError as e:
        return JSONResponse(status_code=403, content={"detail": str(e)})
    reset = current_user.set(user)
    try:
        return await call_next(request)
    finally:
        current_user.reset(reset)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    continue_reply: bool = False  # the last assistant message was cut off: carry on from there


class QuickLogSupplementRequest(BaseModel):
    name: str
    dosage: Optional[float] = None
    unit: Optional[str] = None
    category: Optional[str] = None
    frequency: Optional[str] = None
    notes: Optional[str] = None
    slot: Optional[str] = None


class SaveStackRequest(BaseModel):
    stack: List[SupplementItem]


class DeleteIntakeRequest(BaseModel):
    timestamp: str
    name: Optional[str] = None


class RawDnaUploadRequest(BaseModel):
    content: str


class CounterAdjustRequest(BaseModel):
    """Either item (+ delta, default 1) to tap one counter, or counts to set the day's values."""
    item: Optional[str] = None
    delta: int = 1
    counts: Optional[Dict[str, int]] = None
    date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    timestamp: Optional[str] = None


class CounterHistoryImportRequest(BaseModel):
    entries: List[Dict[str, Any]]


class UpdatePlaceRequest(BaseModel):
    name: Optional[str] = None
    poi_type: Optional[str] = None
    candidate_pois: Optional[List[str]] = None
    is_deleted: Optional[bool] = None


class AddCustomPlaceRequest(BaseModel):
    date: Optional[str] = None
    name: str
    avg_lat: Optional[float] = 0.0
    avg_lon: Optional[float] = 0.0
    duration_seconds: Optional[int] = 3600
    confidence_range: Optional[float] = 50.0
    poi_type: Optional[str] = "custom"


class LocationPoint(BaseModel):
    lat: float
    lon: float
    epoch: Optional[int] = None
    timestamp: Optional[str] = None
    acc: Optional[float] = None
    source: Optional[str] = "owntracks"


class BatchLocationsRequest(BaseModel):
    points: List[LocationPoint]




def get_db_path() -> str:
    return vault_db_path(config)


def load_all_records() -> List[Dict[str, Any]]:
    from uniscale.readings import load_vault
    return load_vault(get_db_path())


def append_record(record_dict: Dict[str, Any]):
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with open(db_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record_dict) + "\n")


@app.get("/api/config")
def get_public_config():
    """Returns safe, redacted configuration for UI inspection."""
    return {
        "auth_mode": config.auth.mode,
        "user": current_user.get(),
        "storage_backend": config.general.storage_backend,
        "external_url": config.server.external_url,
        "auto_scan": config.devices.auto_scan,
        "enabled_devices": {
            "yunmai": config.devices.enable_yunmai,
            "fitdays": config.devices.enable_fitdays,
            "vesync": config.devices.enable_vesync,
            "viatom_bp2": config.devices.enable_viatom_bp2,
            "omron": config.devices.enable_omron,
        },
        "known_devices_count": len(config.devices.known),
    }


@app.get("/api/profile")
def get_profile():
    """The signed-in user's profile (birth year, sex, ASCVD risk factors)."""
    return load_profile(get_db_path(), config).model_dump()


@app.put("/api/profile")
def put_profile(profile: ProfileConfig):
    """Replaces the signed-in user's profile; stored as profile.json next to their vault."""
    return save_profile(profile, get_db_path(), config).model_dump()


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "uniscale", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/summary")
def get_summary():
    records = load_all_records()
    latest_weight = None
    latest_bp = None
    latest_ecg = None
    latest_activity = None
    latest_lab = None
    latest_sleep = None
    latest_workout = None
    latest_supplements = None

    for r in reversed(records):
        if not latest_weight and ("weight_kg" in r or r.get("type") == "weight"):
            latest_weight = r
        if not latest_bp and ("systolic" in r or r.get("type") == "blood_pressure"):
            latest_bp = r
        if not latest_ecg and ("samples_mv" in r or r.get("type") == "ecg"):
            latest_ecg = r
        if not latest_activity and r.get("type") == "activity":
            latest_activity = r
        if not latest_lab and (r.get("type") == "blood_test" or "markers" in r):
            latest_lab = r
        if not latest_sleep and not r.get("nap") and (r.get("type") == "sleep" or "deep_sleep_seconds" in r):
            latest_sleep = r
        if not latest_workout and r.get("type") == "workout":
            latest_workout = r

    from uniscale.supplements import get_supplements_summary
    supp_summary = get_supplements_summary(get_db_path(), days=1)

    from uniscale.genomics import get_genomics_profile
    gen_profile = get_genomics_profile(get_db_path())

    from uniscale.counter import get_counter_state
    counter_state = get_counter_state(get_db_path())

    from uniscale.places import get_dwell_places
    places_state = get_dwell_places(get_db_path(), target_date="all")

    return {
        "latest_weight": latest_weight,
        "latest_bp": latest_bp,
        "latest_ecg": latest_ecg,
        "latest_activity": latest_activity,
        "latest_lab": latest_lab,
        "latest_sleep": latest_sleep,
        "latest_workout": latest_workout,
        "supplements": supp_summary,
        "genomics": gen_profile,
        "counter": counter_state,
        "places": places_state,
        "total_records": len(records),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


@app.get("/api/activity/today")
def get_activity_today():
    records = load_all_records()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    today_activity = None
    latest_activity = None

    for r in reversed(records):
        if r.get("type") == "activity":
            if not latest_activity:
                latest_activity = r
            if r.get("date") == today_str:
                today_activity = r
                break

    act = today_activity or latest_activity
    target_date = act.get("date") if act else today_str

    workouts_for_day = [
        r for r in records
        if (r.get("type") == "workout" or ("sport" in r and "duration_seconds" in r))
        and str(r.get("timestamp") or "").startswith(target_date)
    ]

    return {
        "date": target_date,
        "activity": act,
        "workouts": workouts_for_day
    }


@app.get("/api/history")
def get_history(
    record_type: Optional[str] = Query(None, description="weight, blood_pressure, ecg, activity, workout"),
    limit: int = Query(500, ge=1, le=5000)
):
    records = load_all_records()
    filtered = []
    for r in reversed(records):
        if record_type:
            if record_type == "weight" and ("weight_kg" in r or r.get("type") == "weight"):
                filtered.append(r)
            elif record_type in ("bp", "blood_pressure") and ("systolic" in r or r.get("type") == "blood_pressure"):
                filtered.append(r)
            elif record_type == "ecg" and ("samples_mv" in r or r.get("type") == "ecg"):
                filtered.append(r)
            elif record_type == "activity" and r.get("type") == "activity":
                filtered.append(r)
            elif record_type == "workout" and r.get("type") == "workout":
                filtered.append(r)
            elif record_type in ("blood_test", "lab", "labs") and (r.get("type") == "blood_test" or "markers" in r):
                filtered.append(r)
            elif record_type in ("sleep", "sleeps") and (r.get("type") == "sleep" or "deep_sleep_seconds" in r):
                filtered.append(r)
            elif record_type in ("supplement", "supplements") and (r.get("type") == "supplement" or "dosage" in r):
                filtered.append(r)
            elif record_type in ("genomics", "dna") and (r.get("type") == "genomics" or "variants" in r):
                filtered.append(r)
        else:
            filtered.append(r)

        if len(filtered) >= limit:
            break

    return {"count": len(filtered), "records": filtered}


@app.get("/api/insights/prompt")
def get_insights_prompt():
    from uniscale.ai import build_health_profile, generate_health_summary_prompt
    profile = build_health_profile(get_db_path())
    prompt = generate_health_summary_prompt(profile)
    return {"prompt": prompt, "profile": profile}


@app.get("/api/longevity")
def get_longevity():
    from uniscale.longevity import get_longevity_profile
    return get_longevity_profile(get_db_path())


@app.post("/api/sync/weight", status_code=status.HTTP_201_CREATED)
def sync_weight(data: WeightMeasurement):
    d = data.model_dump(mode="json")
    d["type"] = "weight"
    append_record(d)
    return {"status": "saved", "id": d.get("timestamp")}


@app.post("/api/sync/bp", status_code=status.HTTP_201_CREATED)
def sync_bp(data: BloodPressureMeasurement):
    d = data.model_dump(mode="json")
    d["type"] = "blood_pressure"
    append_record(d)
    return {"status": "saved", "id": d.get("timestamp")}


@app.post("/api/sync/ecg", status_code=status.HTTP_201_CREATED)
def sync_ecg(data: EcgRecording):
    d = data.model_dump(mode="json")
    d["type"] = "ecg"
    append_record(d)
    return {"status": "saved", "id": d.get("timestamp")}


@app.post("/api/sync/activity", status_code=status.HTTP_201_CREATED)
def sync_activity(data: DailyActivity):
    d = data.model_dump(mode="json")
    d["type"] = "activity"
    append_record(d)
    return {"status": "saved", "id": d.get("date")}


@app.post("/api/sync/workout", status_code=status.HTTP_201_CREATED)
def sync_workout(data: ActivityWorkout):
    d = data.model_dump(mode="json")
    d["type"] = "workout"
    append_record(d)
    return {"status": "saved", "id": d.get("timestamp")}


@app.post("/api/sync/lab", status_code=status.HTTP_201_CREATED)
def sync_lab(data: BloodTestMeasurement):
    d = data.model_dump(mode="json")
    d["type"] = "blood_test"
    append_record(d)
    return {"status": "saved", "id": d.get("timestamp")}


@app.get("/api/supplements")
def get_supplements(days: int = 7):
    from uniscale.supplements import get_supplements_summary
    return get_supplements_summary(get_db_path(), days=days)


@app.post("/api/supplements", status_code=status.HTTP_201_CREATED)
def record_supplement(data: SupplementIntake):
    from uniscale.supplements import log_supplement_intake
    res = log_supplement_intake(
        name=data.name,
        dosage=data.dosage,
        unit=data.unit,
        category=data.category,
        frequency=data.frequency,
        notes=data.notes,
        db_path=get_db_path(),
        timestamp=data.timestamp,
        slot=data.slot
    )
    return {"status": "saved", "intake": res}


@app.post("/api/supplements/quick-log", status_code=status.HTTP_201_CREATED)
def quick_log_supplement(req: QuickLogSupplementRequest):
    from uniscale.supplements import log_supplement_intake, active_stack
    dosage = req.dosage or 100.0
    unit = req.unit or "mg"
    cat = req.category or "supplement"
    freq = req.frequency or "daily"
    for item in active_stack(get_db_path()):
        if item["name"].lower() == req.name.lower():
            dosage = req.dosage or item["dosage"]
            unit = req.unit or item["unit"]
            cat = req.category or item["category"]
            freq = req.frequency or item["frequency"]
            break
    res = log_supplement_intake(
        name=req.name,
        dosage=dosage,
        unit=unit,
        category=cat,
        frequency=freq,
        notes=req.notes,
        db_path=get_db_path(),
        slot=req.slot
    )
    return {"status": "saved", "intake": res}


@app.get("/api/supplements/stack")
def get_supplements_stack():
    from uniscale.supplements import active_stack, DEFAULT_SUGGESTION_CATALOG
    return {
        "stack": active_stack(get_db_path()),
        "suggestion_catalog": DEFAULT_SUGGESTION_CATALOG
    }


@app.put("/api/supplements/stack")
def save_supplements_stack(req: SaveStackRequest):
    from uniscale.supplements import save_active_stack
    saved = save_active_stack([item.model_dump() for item in req.stack], get_db_path())
    return {"status": "saved", "stack": saved}


@app.post("/api/supplements/stack/item")
def add_supplement_to_stack(item: SupplementItem):
    from uniscale.supplements import add_to_stack
    updated = add_to_stack(item.model_dump(), get_db_path())
    return {"status": "added", "stack": updated}


@app.delete("/api/supplements/stack/item/{name:path}")
def delete_supplement_from_stack(name: str):
    from uniscale.supplements import remove_from_stack
    updated = remove_from_stack(name, get_db_path())
    if updated is None:
        raise HTTPException(status_code=404, detail=f"'{name}' is not in the active stack")
    return {"status": "deleted", "stack": updated}


@app.post("/api/supplements/intake/delete")
def delete_intake_record(req: DeleteIntakeRequest):
    from uniscale.supplements import delete_supplement_intake
    deleted = delete_supplement_intake(req.timestamp, req.name, get_db_path())
    return {"status": "deleted" if deleted else "not_found", "success": deleted}


@app.get("/api/workouts")
def get_workouts(per_sport: int = 5, sport: Optional[str] = None, offset: int = 0, limit: int = 50):
    """
    Device-tracked workouts: the latest one, and the last `per_sport` of each sport (most recent sport first).
    With `sport`: that sport's full history, newest first, one page (`offset`, `limit`) at a time, plus totals.
    """
    per_sport = max(1, min(per_sport, 50))
    workouts = [r for r in reversed(load_all_records()) if r.get("type") == "workout"]
    if sport is not None:
        mine = [w for w in workouts if str(w.get("sport") or "generic") == sport]
        offset, limit = max(0, offset), max(1, min(limit, 200))
        return {
            "sport": sport,
            "total": len(mine),
            "distance_km": round(sum(w.get("distance_km") or (w.get("distance_meters") or 0) / 1000 for w in mine), 1),
            "duration_seconds": round(sum(w.get("duration_seconds") or 0 for w in mine)),
            "first": mine[-1].get("timestamp") if mine else None,
            "workouts": mine[offset:offset + limit],
        }
    sports: Dict[str, Dict[str, Any]] = {}
    for w in workouts:
        entry = sports.setdefault(str(w.get("sport") or "generic"), {"count": 0, "recent": []})
        entry["count"] += 1
        if len(entry["recent"]) < per_sport:
            entry["recent"].append(w)
    return {
        "latest": workouts[0] if workouts else None,
        "sports": [{"sport": s, **e} for s, e in sports.items()],  # dicts keep newest-first insertion order
    }


@app.get("/api/sleep")
def get_sleep(days: int = 14):
    from uniscale.sleep import get_sleep_summary
    return get_sleep_summary(get_db_path(), days=days)


@app.post("/api/sync/sleep", status_code=status.HTTP_201_CREATED)
def sync_sleep(data: SleepSession):
    d = data.model_dump(mode="json")
    d["type"] = "sleep"
    # Derived fields are always computed here, never taken from the client.
    d["duration_hours"] = round(d["duration_seconds"] / 3600.0, 2)
    for stage in ("deep_sleep", "rem_sleep", "light_sleep", "awake"):
        d[f"{stage}_minutes"] = round(d[f"{stage}_seconds"] / 60.0)

    from uniscale.sleep import sleep_date
    db_path = get_db_path()
    if os.path.exists(db_path):
        with open(db_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (row.get("type") == "sleep" and sleep_date(row) == d["date"]
                        and row.get("duration_seconds") == d["duration_seconds"]
                        and row.get("device_name") == d["device_name"]):
                    return {"status": "already_exists", "id": d["date"]}

    append_record(d)
    return {"status": "saved", "id": d["date"]}


@app.post("/api/sync/sleep/fit", status_code=status.HTTP_201_CREATED)
def sync_sleep_fit(
    sleep_file: UploadFile = File(...),
    hrv_file: Optional[UploadFile] = File(None)
):
    from uniscale.importer import parse_garmin_sleep_fit
    with _upload_tmpdir() as tmp:
        sleep_path = _spool_upload(sleep_file, os.path.join(tmp, "sleep"))
        hrv_path = _spool_upload(hrv_file, os.path.join(tmp, "hrv")) if hrv_file else None
        session = parse_garmin_sleep_fit(sleep_path, hrv_path)
        if not session:
            raise HTTPException(status_code=422, detail="Could not parse sleep FIT file")
        return sync_sleep(session)


@app.get("/api/genomics")
def get_genomics():
    from uniscale.genomics import get_genomics_profile
    return get_genomics_profile(get_db_path())


@app.post("/api/genomics/import", status_code=status.HTTP_201_CREATED)
def import_genomics(req: RawDnaUploadRequest):
    from uniscale.genomics import parse_raw_dna_text, build_clinical_profile
    if len(req.content) > MAX_DNA_UPLOAD_CHARS:
        raise HTTPException(status_code=413, detail="Raw DNA file too large")
    extracted = parse_raw_dna_text(req.content)
    prof = build_clinical_profile(extracted)
    prof["type"] = "genomics"
    append_record(prof)
    return {"status": "imported", "snps_found": len(extracted), "profile": prof}


@app.get("/api/counter")
def get_counter(date: Optional[str] = None):
    from uniscale.counter import get_counter_state
    return get_counter_state(get_db_path(), target_date=date)


@app.post("/api/counter")
def post_counter(req: CounterAdjustRequest):
    from uniscale.counter import adjust_counter, set_counter
    try:
        if req.counts is not None:
            return set_counter(req.counts, db_path=get_db_path(), target_date=req.date)
        if not req.item:
            raise ValueError("Give item (to tap one counter) or counts (to set the day)")
        return adjust_counter(item=req.item, delta=req.delta, db_path=get_db_path(), target_date=req.date, timestamp=req.timestamp)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/counter/history")
def get_counter_history(days: int = 30):
    from uniscale.counter import get_daily_history
    return {"history": get_daily_history(get_db_path(), days=days)}


@app.post("/api/counter/history")
def post_counter_history(req: CounterHistoryImportRequest):
    from uniscale.counter import import_historic_records
    count = import_historic_records(req.entries, db_path=get_db_path())
    return {"status": "imported", "count": count}


@app.post("/api/owntracks")
async def owntracks_webhook(request: Request):
    """
    Ingests OwnTracks HTTP payloads (standard OwnTracks format or array of locations).
    Returns [] (HTTP 200) as expected by the OwnTracks mobile app.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    from uniscale.places import ingest_owntracks_payload
    ingest_owntracks_payload(body, db_path=get_db_path())
    return []


@app.post("/api/owntracks/points", status_code=status.HTTP_201_CREATED)
def post_owntracks_points(req: BatchLocationsRequest):
    from uniscale.places import ingest_location
    saved = []
    for pt in req.points:
        saved.append(ingest_location(
            lat=pt.lat,
            lon=pt.lon,
            epoch=pt.epoch,
            timestamp=pt.timestamp,
            accuracy=pt.acc,
            source=pt.source or "owntracks",
            db_path=get_db_path()
        ))
    return {"status": "saved", "count": len(saved)}


@app.get("/api/patterns")
def get_patterns():
    """Correlations between the counters, places and health metrics over the last 90 days (see uniscale.patterns)."""
    from uniscale.patterns import DAYS, build_patterns, describe
    from uniscale.readings import load_vault
    db = get_db_path()
    data = build_patterns(db, load_vault(db))
    return {
        "days": DAYS,
        "logged_days": len(data["rows"]),
        "patterns": [dict(p, text=describe(p)) for p in data["patterns"]],
    }


@app.get("/api/places")
def get_places(day: Optional[str] = "previous", date: Optional[str] = None, include_deleted: bool = False,
               include_home: bool = False):
    from uniscale.places import get_dwell_places
    target = date if date else day
    return get_dwell_places(get_db_path(), target_date=target, include_deleted=include_deleted,
                            include_home=include_home)


@app.get("/api/places/{place_id}/suggestions")
def place_suggestions(place_id: str):
    """Names to pick from for a stay: known places nearby, then OpenStreetMap points of interest."""
    from uniscale.places import suggest_names
    try:
        return {"suggestions": suggest_names(place_id, get_db_path())}
    except KeyError:
        raise HTTPException(status_code=404, detail="Place not found")


@app.delete("/api/places/known/{known_id}")
def forget_place(known_id: str):
    from uniscale.places import forget_known_place
    if not forget_known_place(known_id, get_db_path()):
        raise HTTPException(status_code=404, detail="Known place not found")
    return {"status": "forgotten", "id": known_id}


@app.post("/api/places", status_code=status.HTTP_201_CREATED)
def add_place(req: AddCustomPlaceRequest):
    from uniscale.places import add_custom_place
    date_str = req.date or (datetime.now(timezone.utc).astimezone().date() - timedelta(days=1)).strftime("%Y-%m-%d")
    lat = req.avg_lat if req.avg_lat is not None else 0.0
    lon = req.avg_lon if req.avg_lon is not None else 0.0
    dur = req.duration_seconds if req.duration_seconds is not None else 3600
    conf = req.confidence_range if req.confidence_range is not None else 50.0
    return add_custom_place(
        date_str=date_str,
        name=req.name,
        avg_lat=lat,
        avg_lon=lon,
        duration_seconds=dur,
        confidence_range=conf,
        poi_type=req.poi_type or "custom",
        db_path=get_db_path()
    )


@app.put("/api/places/{place_id}")
def update_place(place_id: str, req: UpdatePlaceRequest):
    from uniscale.places import update_dwell_place
    updated = update_dwell_place(
        place_id=place_id,
        name=req.name,
        poi_type=req.poi_type,
        candidate_pois=req.candidate_pois,
        is_deleted=req.is_deleted,
        db_path=get_db_path()
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Place not found")
    return updated


@app.delete("/api/places/{place_id}")
def delete_place(place_id: str):
    from uniscale.places import delete_dwell_place
    success = delete_dwell_place(place_id, db_path=get_db_path())
    if not success:
        raise HTTPException(status_code=404, detail="Place not found")
    return {"status": "deleted", "id": place_id}


@app.post("/api/places/rescan")
def rescan_places(date: Optional[str] = None):
    from uniscale.places import compute_dwell_places
    places = compute_dwell_places(db_path=get_db_path(), target_date=date or "previous")
    return {"status": "rescanned", "count": len(places), "places": places}



def _upload_tmpdir() -> tempfile.TemporaryDirectory:
    """
    Scratch space for uploads inside the data dir, not /tmp: /tmp is RAM-backed tmpfs on
    many distros (Debian 13), and Garmin/Takeout exports can be gigabytes.
    """
    base = os.path.dirname(os.path.abspath(get_db_path()))
    os.makedirs(base, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base, prefix=".import-")


def _spool_upload(upload: UploadFile, dest_dir: str, limit: int = MAX_UPLOAD_BYTES) -> str:
    """Copies an uploaded file into dest_dir under its own (sanitised) name, enforcing the size cap."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, os.path.basename(upload.filename or "") or "upload")
    size = 0
    with open(path, "wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(status_code=413, detail=f"{upload.filename} is larger than {limit // (1024 * 1024)} MB")
            out.write(chunk)
    return path


@app.post("/api/sync/workout/fit", status_code=status.HTTP_201_CREATED)
def sync_workout_fit(file: UploadFile = File(...)):
    """
    Direct upload endpoint for a Garmin .FIT activity file or GPX file.
    Extracts complete telemetry: running dynamics, splits, GPS track, HR series.
    """
    from uniscale import importer
    with _upload_tmpdir() as tmp:
        path = _spool_upload(file, tmp)
        fname = (file.filename or "workout.fit").lower()
        if fname.endswith(".gpx"):
            workout = importer.parse_gpx_file(path)
        else:
            workout = importer.parse_garmin_fit_file(path)
        if not workout:
            raise HTTPException(status_code=422, detail=f"Could not parse workout file {file.filename}")
        d = workout.model_dump(mode="json")
        d["type"] = "workout"
        append_record(d)
        return {"status": "saved", "id": d.get("timestamp"), "workout": d}


@app.post("/api/import/lab", status_code=status.HTTP_201_CREATED)
def import_lab_upload(file: UploadFile = File(...)):
    """Imports a Quest Diagnostics PDF report (needs pdftotext) or a BloodTestMeasurement JSON file."""
    from uniscale.importer import import_lab_file
    with _upload_tmpdir() as tmp:
        path = _spool_upload(file, tmp)
        try:
            res = import_lab_file(path, target_db=get_db_path())
        except FileNotFoundError:
            raise HTTPException(status_code=422, detail="pdftotext (poppler-utils) must be installed on the server to read PDF lab reports")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"Could not read lab report: {e}")
    return {"status": "imported", **res}


# source -> (importer function name in uniscale.importer, size cap)
_DEVICE_IMPORTERS = {
    "csv": ("import_csv_file", MAX_UPLOAD_BYTES),
    "vesync": ("import_vesync_file", MAX_UPLOAD_BYTES),
    "garmin": ("import_garmin_file", MAX_ARCHIVE_UPLOAD_BYTES),
    "google-fit": ("parse_google_fit_takeout", MAX_ARCHIVE_UPLOAD_BYTES),
}


@app.post("/api/import/dna", status_code=status.HTTP_201_CREATED)
def import_dna_upload(
    file: UploadFile = File(...),
    father: Optional[UploadFile] = File(None),
    mother: Optional[UploadFile] = File(None),
    ancestry: Optional[UploadFile] = File(None),
):
    """
    Imports a Promethease report zip or AncestryDNA / 23andMe / MyHeritage raw data (.zip/.txt/.csv),
    optionally phased with the parents' Promethease zips and merged with an AncestryDNA raw zip.
    """
    from uniscale.genomics import import_dna_file
    with _upload_tmpdir() as tmp:
        # One sub-directory per role, so identically named uploads (promethease.zip) don't collide.
        paths = {
            role: _spool_upload(up, os.path.join(tmp, role))
            for role, up in (("self", file), ("father", father), ("mother", mother), ("ancestry", ancestry))
            if up is not None and up.filename
        }
        try:
            prof = import_dna_file(
                paths["self"], paths.get("father"), paths.get("mother"),
                ancestry_zip=paths.get("ancestry"), db_path=get_db_path(),
            )
        except (ValueError, KeyError, OSError, zipfile.BadZipFile) as e:
            raise HTTPException(status_code=422, detail=f"Could not import DNA file: {e}")
    return {
        "status": "imported",
        "snps_found": prof.get("snps_found", prof.get("trio_status", {}).get("user_snps_total", 0)),
        "trio_status": prof.get("trio_status"),
        "key_highlights": prof.get("key_highlights"),
    }


@app.post("/api/import/{source}", status_code=status.HTTP_201_CREATED)
def import_device_upload(source: str, file: UploadFile = File(...)):
    """
    Imports a device/app export: Fitdays or Omron CSV (csv), VeSync JSON (vesync),
    Garmin .fit or data-export zip incl. sleep (garmin), Google Takeout zip (google-fit).
    """
    from uniscale import importer
    if source not in _DEVICE_IMPORTERS:
        raise HTTPException(status_code=404, detail=f"Unknown import source '{source}'")
    func_name, limit = _DEVICE_IMPORTERS[source]
    with _upload_tmpdir() as tmp:
        path = _spool_upload(file, tmp, limit)
        try:
            res = getattr(importer, func_name)(path, target_db=get_db_path())
        except (ValueError, KeyError, TypeError, AttributeError, OSError, zipfile.BadZipFile) as e:
            raise HTTPException(status_code=422, detail=f"Could not import {file.filename}: {e}")
    counts = {k: v for k, v in res.items() if isinstance(v, int) and not isinstance(v, bool)}
    if not any(counts.values()):
        raise HTTPException(status_code=422, detail=f"No records found in {file.filename}")
    return {"status": "imported", "source": source, **counts}


@app.get("/api/assistant")
def get_assistant():
    """Whether this hub has a chat assistant (an LLM set up in [ai]) and what it's called."""
    return {"available": config.ai.available, "name": config.ai.name}


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest):
    from uniscale.ai import chat_with_agent
    if not config.ai.available:
        raise HTTPException(status_code=404, detail="No chat assistant is set up on this hub ([ai] in the config)")
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    # The LLM endpoint comes only from config: the prompt carries the whole health profile.
    return chat_with_agent(msgs, endpoint=config.ai.endpoint, model=config.ai.model, db_path=get_db_path(),
                           continue_reply=req.continue_reply, thinking=config.ai.thinking,
                           context_tokens=config.ai.context_tokens)


ICONS_DIR = os.path.join(os.path.dirname(__file__), "static", "icons")
ICON_FILES = {"icon-192.png", "icon-512.png", "apple-touch-icon.png", "icon.svg"}

WEB_MANIFEST = {
    "name": "UniScale Health Hub",
    "short_name": "UniScale",
    "description": "Your self-hosted health vault",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0b0f19",
    "theme_color": "#0b0f19",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        {"src": "/icons/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
    ],
}

# Network first for everything. Only the page itself (no health data: that comes from /api, which is
# never cached) and the icons are kept, so the installed app still opens when the server is unreachable.
SERVICE_WORKER_JS = """
const CACHE = 'uniscale-shell-v1';
const SHELL = ['/', '/icons/icon-192.png', '/icons/apple-touch-icon.png'];

self.addEventListener('install', e => {
    e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
    e.waitUntil(caches.keys()
        .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
        .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
    const url = new URL(e.request.url);
    if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
    if (e.request.mode === 'navigate') {
        e.respondWith(fetch(e.request)
            .then(res => { if (res.ok) caches.open(CACHE).then(c => c.put('/', res.clone())); return res; })
            .catch(() => caches.match('/')));
    } else if (url.pathname.startsWith('/icons/')) {
        e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
    }
});
"""


@app.get("/manifest.webmanifest")
def web_manifest():
    return JSONResponse(WEB_MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return Response(SERVICE_WORKER_JS, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/icons/{name}")
def app_icon(name: str):
    if name not in ICON_FILES:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(os.path.join(ICONS_DIR, name), headers={"Cache-Control": "public, max-age=604800"})


@app.get("/", response_class=HTMLResponse)
def index_page():
    mtls_badge = '<span class="badge mtls">mTLS Protected</span>' if (getattr(config.server, "enable_mtls", False) or getattr(config.sync, "enable_mtls", False) or bool(getattr(config.sync, "client_cert", None))) else ""
    return HTML_CONTENT.replace("<!-- MTLS_BADGE -->", mtls_badge)


HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>UniScale Health Hub</title>
    <!-- Installable app (PWA): "Add to Home Screen" on iPhone, "Install app" on Android/desktop -->
    <link rel="manifest" href="/manifest.webmanifest">
    <link rel="icon" href="/icons/icon.svg" type="image/svg+xml">
    <link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="UniScale">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#0b0f19" media="(prefers-color-scheme: dark)">
    <meta name="theme-color" content="#f8fafc" media="(prefers-color-scheme: light)">
    <style>
        :root {
            --bg: #0b0f19;
            --surface: #111827;
            --surface-card: #1f2937;
            --border: #374151;
            --text-main: #f9fafb;
            --text-muted: #9ca3af;
            --accent-orange: #f59e0b;
            --accent-blue: #38bdf8;
            --accent-red: #ef4444;
            --accent-green: #10b981;
            --accent-purple: #8b5cf6;
        }
        [data-theme="light"] {
            --bg: #f8fafc;
            --surface: #ffffff;
            --surface-card: #f1f5f9;
            --border: #e2e8f0;
            --text-main: #0f172a;
            --text-muted: #64748b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background: var(--bg); color: var(--text-main); padding: 24px; min-height: 100vh; }
        /* Installed on a phone, the page runs under the status bar and home indicator: keep clear of them. */
        body {
            padding-top: max(24px, env(safe-area-inset-top));
            padding-bottom: max(24px, env(safe-area-inset-bottom));
            padding-left: max(24px, env(safe-area-inset-left));
            padding-right: max(24px, env(safe-area-inset-right));
        }
        .container { max-width: 1024px; margin: 0 auto; }
        header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
        .logo-area { display: flex; align-items: center; gap: 12px; }
        .logo { font-size: 26px; font-weight: 800; letter-spacing: -0.5px; }
        .badge { font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 999px; text-transform: uppercase; background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }
        .badge.mtls { background: rgba(56, 189, 248, 0.15); color: var(--accent-blue); border: 1px solid rgba(56, 189, 248, 0.3); }
        .badge.interactive { background: rgba(139, 92, 246, 0.15); color: var(--accent-purple); border: 1px solid rgba(139, 92, 246, 0.3); }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 18px; margin-bottom: 24px; }
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2); }
        .card.clickable { cursor: pointer; transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease; position: relative; }
        .card.clickable:hover { transform: translateY(-3px); box-shadow: 0 10px 24px -4px rgba(0, 0, 0, 0.45); border-color: rgba(255, 255, 255, 0.25); }
        .card.clickable .click-hint { font-size: 11px; color: var(--text-muted); opacity: 0.7; transition: opacity 0.15s; }
        .card.clickable:hover .click-hint { opacity: 1; color: var(--accent-blue); }
        .card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
        .card-title { font-size: 15px; font-weight: 600; color: var(--text-muted); display: flex; align-items: center; gap: 8px; }
        .metric-main { font-size: 40px; font-weight: 800; line-height: 1.1; margin-bottom: 4px; }
        .metric-sub { font-size: 14px; color: var(--text-muted); }
        .garmin-stats { display: flex; justify-content: space-around; margin-top: 12px; text-align: center; }
        /* Phone width (e.g. the installed app on an iPhone): logo on one line, the buttons in a row under it. */
        @media (max-width: 600px) {
            body { padding-left: max(14px, env(safe-area-inset-left)); padding-right: max(14px, env(safe-area-inset-right)); }
            header { flex-direction: column; align-items: stretch; gap: 12px; }
            header > div:last-child { justify-content: flex-start !important; gap: 8px !important; }
            .header-tagline { display: none; }
            .ai-header-btn { padding: 7px 12px; font-size: 12px; }
            .garmin-stats .metric-main { font-size: 26px; }
        }
        .canvas-container { position: relative; width: 100%; height: 140px; background: #070a13; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin-top: 10px; }
        canvas { width: 100%; height: 100%; display: block; }
        .history-table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        .history-table th { text-align: left; padding: 10px 14px; font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; border-bottom: 1px solid var(--border); }
        .history-table td { padding: 12px 14px; font-size: 13px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }
        .tag { font-size: 11px; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
        .tag-weight { background: rgba(139, 92, 246, 0.2); color: var(--accent-purple); }
        .tag-bp { background: rgba(239, 68, 68, 0.2); color: var(--accent-red); }
        .tag-ecg { background: rgba(56, 189, 248, 0.2); color: var(--accent-blue); }
        .tag-garmin { background: rgba(245, 158, 11, 0.2); color: var(--accent-orange); }
        .tag-lab { background: rgba(16, 185, 129, 0.2); color: #10b981; }

        /* Modal Backdrop & Dialog */
        .modal-backdrop { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.78); backdrop-filter: blur(8px); display: none; align-items: center; justify-content: center; z-index: 999; padding: 20px; }
        .modal-backdrop.open { display: flex; }
        .modal-container { background: var(--surface); border: 1px solid var(--border); border-radius: 20px; max-width: 820px; width: 100%; max-height: 90vh; overflow-y: auto; padding: 26px; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7); position: relative; transition: max-width 0.2s ease; }
        .modal-container.modal-wide { max-width: 980px; }
        .modal-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 20px; }
        .modal-title { font-size: 22px; font-weight: 700; color: var(--text-main); }
        .modal-subtitle { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
        .modal-close { background: transparent; border: none; font-size: 28px; color: var(--text-muted); cursor: pointer; line-height: 1; padding: 0 6px; transition: color 0.15s; }
        .modal-close:hover { color: #fff; }

        /* Filter period pill buttons */
        .filter-row { display: flex; gap: 8px; margin-bottom: 20px; background: #070a13; padding: 5px; border-radius: 12px; width: fit-content; border: 1px solid var(--border); }
        .filter-btn { background: transparent; border: none; color: var(--text-muted); padding: 6px 14px; font-size: 13px; font-weight: 600; border-radius: 8px; cursor: pointer; transition: all 0.15s; }
        .filter-btn:hover { color: var(--text-main); }
        .filter-btn.active { background: var(--surface-card); color: var(--text-main); box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3); }

        /* Modal Stats Grid */
        .modal-stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .stat-box { background: var(--surface-card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; }
        .stat-box.clickable, tr.clickable { cursor: pointer; }
        .stat-box.clickable:hover { border-color: #f97316; }
        tr.clickable:hover td { background: rgba(249, 115, 22, 0.08); }
        .modal-back { color: var(--accent-blue); cursor: pointer; text-decoration: none; margin-right: 8px; }
        .stat-label { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; margin-bottom: 4px; }
        .stat-value { font-size: 22px; font-weight: 700; }
        .stat-sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

        /* Modal Chart */
        .modal-chart-card { background: #070a13; border: 1px solid var(--border); border-radius: 14px; padding: 16px; margin-bottom: 22px; height: 260px; position: relative; }
        .chart-legend { display: flex; gap: 16px; font-size: 12px; margin-bottom: 8px; justify-content: flex-end; }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-dot { width: 10px; height: 10px; border-radius: 50%; }

        /* Telemetry Cards */
        .telemetry-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 20px; }
        .telemetry-card { background: var(--surface-card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
        .telemetry-val { font-size: 26px; font-weight: 800; margin: 4px 0; }

        /* AI & Health Hub Extensions */
        .ai-header-btn {
            background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
            color: #fff;
            border: 1px solid rgba(255, 255, 255, 0.2);
            padding: 8px 16px;
            border-radius: 999px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
            box-shadow: 0 4px 12px rgba(124, 58, 237, 0.3);
        }
        .ai-header-btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 16px rgba(124, 58, 237, 0.5);
        }
        .ai-header-btn.plain { background: var(--surface-card); border-color: var(--border); color: var(--text-main); box-shadow: none; }
        dialog.import-dialog {
            background: var(--surface);
            color: var(--text-main);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 22px;
            width: min(480px, calc(100vw - 32px));
            margin: auto;
        }
        dialog.import-dialog::backdrop { background: rgba(0, 0, 0, 0.55); }
        .import-dialog h3 { font-size: 17px; margin-bottom: 4px; }
        .import-dialog p, .import-dialog summary { font-size: 12px; color: var(--text-muted); line-height: 1.45; }
        .import-dialog summary { cursor: pointer; margin-top: 14px; }
        .import-dialog label { display: block; font-size: 13px; margin: 12px 0 4px; color: var(--text-muted); }
        .import-dialog input[type="file"] { width: 100%; font-size: 13px; color: var(--text-main); }
        .import-dialog select, .import-dialog input[type="number"], .import-dialog input[type="text"], .import-dialog input[type="date"] { width: 100%; font-size: 13px; padding: 8px 10px; border-radius: 8px; background: var(--surface-card); color: var(--text-main); border: 1px solid var(--border); }
        .import-status { font-size: 13px; margin-top: 12px; min-height: 18px; }
        .import-status.ok { color: var(--accent-green); }
        .import-status.err { color: var(--accent-red); }
        .import-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
        .sleep-bar-container {
            width: 100%;
            height: 12px;
            background: #070a13;
            border-radius: 6px;
            overflow: hidden;
            display: flex;
            margin: 10px 0 8px 0;
            border: 1px solid var(--border);
        }
        .sleep-seg-deep { background: #8b5cf6; }
        .sleep-seg-rem { background: #06b6d4; }
        .sleep-seg-light { background: #38bdf8; }
        .sleep-seg-awake { background: #ef4444; }
        .supp-pill {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 12px;
            background: var(--surface-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            margin-bottom: 6px;
            font-size: 13px;
            transition: background 0.15s;
        }
        .supp-pill.taken {
            border-color: rgba(16, 185, 129, 0.4);
            background: rgba(16, 185, 129, 0.08);
        }
        .supp-check-btn {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-muted);
            width: 24px;
            height: 24px;
            border-radius: 6px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            transition: all 0.15s;
        }
        .supp-check-btn.checked {
            background: #10b981;
            color: #fff;
            border-color: #10b981;
        }
        .supp-action-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 14px;
        }
        .supp-btn {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            border: 1px solid var(--border);
            background: var(--surface-card);
            color: var(--text-main);
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .supp-btn:hover {
            border-color: #f59e0b;
            color: #f59e0b;
        }
        .supp-btn.primary {
            background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
            border: none;
            color: #111;
            font-weight: 700;
        }
        .supp-btn.ai-btn {
            background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
            border: none;
            color: #fff;
            font-weight: 700;
        }
        .supp-edit-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            margin-top: 10px;
        }
        .supp-edit-table th {
            text-align: left;
            padding: 8px 10px;
            color: var(--text-muted);
            font-size: 11px;
            text-transform: uppercase;
            border-bottom: 1px solid var(--border);
        }
        .supp-edit-table td {
            padding: 8px 10px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            vertical-align: middle;
        }
        .supp-edit-table th:last-child,
        .supp-edit-table td:last-child {
            position: sticky;
            right: 0;
            background: var(--surface-card);
            text-align: center;
            z-index: 2;
            box-shadow: -4px 0 8px -2px rgba(0, 0, 0, 0.4);
        }
        .supp-input, .supp-select {
            background: var(--surface);
            border: 1px solid var(--border);
            color: #fff;
            padding: 6px 10px;
            border-radius: 6px;
            font-size: 13px;
            width: 100%;
            box-sizing: border-box;
            outline: none;
        }
        .supp-input:focus, .supp-select:focus {
            border-color: #f59e0b;
        }
        .supp-badge-freq {
            font-size: 11px;
            padding: 2px 7px;
            border-radius: 4px;
            background: rgba(245, 158, 11, 0.15);
            color: #f59e0b;
            text-transform: capitalize;
            font-weight: 600;
        }
        .supp-badge-freq.morning {
            background: rgba(59, 130, 246, 0.15);
            color: #3b82f6;
        }
        .supp-badge-freq.evening {
            background: rgba(139, 92, 246, 0.15);
            color: #a78bfa;
        }
        .chat-bubble {
            padding: 12px 16px;
            border-radius: 14px;
            font-size: 14px;
            line-height: 1.5;
            max-width: 88%;
            margin-bottom: 12px;
            word-wrap: break-word;
        }
        .chat-bubble.user {
            background: #2563eb;
            color: #fff;
            margin-left: auto;
            border-bottom-right-radius: 4px;
        }
        .chat-bubble.ai {
            background: #0f172a;
            border: 1px solid var(--border);
            color: var(--text-main);
            margin-right: auto;
            border-bottom-left-radius: 4px;
        }
        .chip-container {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 14px;
        }
        .prompt-chip {
            background: #070a13;
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 6px 12px;
            font-size: 12px;
            color: var(--accent-blue);
            cursor: pointer;
            transition: all 0.15s;
        }
        .prompt-chip:hover {
            border-color: var(--accent-blue);
            background: rgba(56, 189, 248, 0.1);
        }
        .tag-sleep { background: rgba(56, 189, 248, 0.2); color: #38bdf8; }
        .tag-supp { background: rgba(245, 158, 11, 0.2); color: #f59e0b; }
        .tag-gen { background: rgba(139, 92, 246, 0.2); color: #8b5cf6; }
            body.no-assistant .ai-only { display: none !important; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-area">
                <div class="logo">uniscale</div>
                <span class="badge">Offline Vault</span>
                <!-- MTLS_BADGE -->
                <span class="badge interactive" id="user-badge" title="Signed-in user" hidden></span>
            </div>
            <div style="display: flex; align-items: center; gap: 14px; flex-wrap: wrap; justify-content: flex-end;">
                <button id="theme-btn" onclick="cycleTheme()" class="ai-header-btn plain" title="Toggle Display Theme">
                    <span>🌓</span>
                    <span id="theme-text">Theme</span>
                </button>
                <button onclick="openProfileDialog()" class="ai-header-btn plain" title="Birth year, sex and risk factors used for biological age and ASCVD">
                    <span>👤</span>
                    <span>Profile</span>
                </button>
                <button onclick="openImportDialog()" class="ai-header-btn plain" title="Import lab results, DNA, Garmin, scale/BP, VeSync or Google Fit data">
                    <span>📥</span>
                    <span>Import</span>
                </button>
                <button onclick="openChatModal()" class="ai-header-btn ai-only">
                    <span>✨</span>
                    <span>Consult <span class="assistant-name">Dr. Gemma</span></span>
                </button>
                <div class="header-tagline" style="font-size: 13px; color: var(--text-muted);">
                    100% Local Health Cloud
                </div>
            </div>
        </header>

        <dialog id="profileDialog" class="import-dialog">
            <form onsubmit="saveProfile(event)">
                <h3>👤 Your Profile</h3>
                <p>Used to greet you, and for biological age (PhenoAge) and 10-year ASCVD risk. Stored only in your own vault.</p>
                <label for="profName">Name</label>
                <input id="profName" name="name" type="text" maxlength="80" autocomplete="given-name" placeholder="What should we call you?">
                <label for="profDob">Date of birth</label>
                <input id="profDob" name="date_of_birth" type="date" min="1900-01-01">
                <small id="profDobHint" class="metric-sub" hidden></small>
                <label for="profSex">Sex (the ASCVD equations differ by sex)</label>
                <select id="profSex" name="sex">
                    <option value="">Not set</option>
                    <option value="female">Female</option>
                    <option value="male">Male</option>
                </select>
                <label style="display: flex; gap: 8px; align-items: center;"><input type="checkbox" id="profSmoker"> Current smoker</label>
                <label style="display: flex; gap: 8px; align-items: center;"><input type="checkbox" id="profDiabetic"> Diabetic</label>
                <label style="display: flex; gap: 8px; align-items: center;"><input type="checkbox" id="profBpTreated"> Taking blood-pressure medication</label>
                <label for="profCounterTitle">Daily counter tile (leave blank for none)</label>
                <input id="profCounterTitle" type="text" maxlength="40" placeholder="Title, e.g. D Tracker">
                <input id="profCounterElements" type="text" placeholder="Buttons, comma-separated, e.g. D(out), D(in)" aria-label="Counter buttons, comma-separated">
                <div class="import-status" role="status"></div>
                <div class="import-actions">
                    <button type="button" class="ai-header-btn plain" onclick="this.closest('dialog').close()">Close</button>
                    <button type="submit" class="ai-header-btn">Save</button>
                </div>
            </form>
        </dialog>

        <dialog id="importDialog" class="import-dialog">
            <form onsubmit="submitImport(event)">
                <h3>📥 Import Health Data</h3>
                <p>Files are parsed locally on this server; nothing leaves your vault. Re-importing the same file is safe: duplicates are skipped.</p>
                <label for="importSource">What are you importing?</label>
                <select id="importSource" onchange="updateImportSource()">
                    <option value="lab">🧪 Lab results: Quest Diagnostics</option>
                    <option value="dna">🧬 DNA: Promethease, AncestryDNA, 23andMe, MyHeritage</option>
                    <option value="garmin">⌚ Garmin: data export or .fit workout</option>
                    <option value="csv">⚖️ Scale / blood pressure CSV: Fitdays, Omron</option>
                    <option value="vesync">⚖️ VeSync scale history (JSON)</option>
                    <option value="google-fit">🏃 Google Fit: Google Takeout</option>
                </select>
                <p id="importHint" style="margin-top: 8px;"></p>
                <label for="importFile" id="importFileLabel">File</label>
                <input id="importFile" type="file" name="file" required>
                <details id="importDnaExtras">
                    <summary>Family trio &amp; raw data (optional)</summary>
                    <label for="dnaFather">Father's Promethease report (.zip)</label>
                    <input id="dnaFather" type="file" name="father" accept=".zip">
                    <label for="dnaMother">Mother's Promethease report (.zip)</label>
                    <input id="dnaMother" type="file" name="mother" accept=".zip">
                    <label for="dnaAncestry">Your AncestryDNA raw data (.zip), merged with a Promethease report</label>
                    <input id="dnaAncestry" type="file" name="ancestry" accept=".zip">
                </details>
                <div class="import-status" role="status"></div>
                <div class="import-actions">
                    <button type="button" class="ai-header-btn plain" onclick="this.closest('dialog').close()">Close</button>
                    <button type="submit" class="ai-header-btn">Import</button>
                </div>
            </form>
        </dialog>

        <!-- Top Status Card: Garmin Watch (as requested at the top) -->
        <div class="grid">
            <div class="card clickable" style="border-top: 3px solid var(--accent-orange); grid-column: 1 / -1;" onclick="openDeviceModal()">
                <div class="card-header">
                    <span class="card-title">
                        <span style="color: var(--accent-orange); font-size: 18px;">●</span>
                        Watch Activity
                    </span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for daily telemetry ↗</span>
                        <span class="badge" id="garmin-badge" style="background: rgba(245, 158, 11, 0.15); color: var(--accent-orange); border-color: rgba(245, 158, 11, 0.3);">Active Watch Link</span>
                    </div>
                </div>
                <div class="garmin-stats">
                    <div>
                        <div class="metric-main" id="garmin-steps">--</div>
                        <div class="metric-sub">Steps Today</div>
                    </div>
                    <div>
                        <div class="metric-main" style="color: var(--accent-blue);" id="garmin-dist">-- km</div>
                        <div class="metric-sub">Distance</div>
                    </div>
                    <div>
                        <div class="metric-main" style="color: var(--accent-green);">Direct BLE</div>
                        <div class="metric-sub">Zero Garmin Cloud</div>
                    </div>
                </div>
            </div>

            <!-- Last Workout Card -->
            <div class="card clickable" style="border-top: 3px solid #f97316;" onclick="openWorkoutModal()">
                <div class="card-header">
                    <span class="card-title">Last Workout</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for history ↗</span>
                        <span class="badge" id="workout-sport" style="background: rgba(249, 115, 22, 0.15); color: #f97316;">--</span>
                    </div>
                </div>
                <div class="metric-main" id="workout-main">-- <span style="font-size: 18px; font-weight: 500;">km</span></div>
                <div class="metric-sub" id="workout-detail" style="color: var(--text-main); font-weight: 500;">Time -- • Pace --</div>
                <div class="metric-sub" id="workout-hr">Avg HR -- • Calories --</div>
                <div class="metric-sub" style="margin-top: 8px;" id="workout-ts">No workouts recorded yet</div>
            </div>

            <!-- Weight Card -->
            <div class="card clickable" style="border-top: 3px solid var(--accent-purple);" onclick="openWeightModal()">
                <div class="card-header">
                    <span class="card-title">Weight & Body Composition</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for trends ↗</span>
                        <span class="badge" id="weight-dev">Yunmai / Scale</span>
                    </div>
                </div>
                <div class="metric-main" id="weight-lb">--.- <span style="font-size: 18px; font-weight: 500;">lb</span></div>
                <div class="metric-sub" id="weight-kg">(--.- kg)</div>
                <div class="metric-sub" style="margin-top: 8px;" id="weight-ts">Awaiting measurement</div>
            </div>

            <!-- Blood Pressure Card -->
            <div class="card clickable" style="border-top: 3px solid var(--accent-red);" onclick="openBpModal()">
                <div class="card-header">
                    <span class="card-title">Blood Pressure & Pulse</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for trends ↗</span>
                        <span class="badge" id="bp-dev">Viatom / Omron</span>
                    </div>
                </div>
                <div class="metric-main" id="bp-val">---/-- <span style="font-size: 18px; font-weight: 500;">mmHg</span></div>
                <div class="metric-sub" style="color: var(--accent-red); font-weight: 600;" id="bp-pulse">Pulse: -- bpm</div>
                <div class="metric-sub" style="margin-top: 8px;" id="bp-ts">Awaiting cuff reading</div>
            </div>

            <!-- Lead-I ECG Card -->
            <div class="card" style="border-top: 3px solid var(--accent-blue);">
                <div class="card-header">
                    <span class="card-title">Lead-I ECG (Viatom BP2)</span>
                    <span class="badge" id="ecg-hr">125 Hz Lead-I</span>
                </div>
                <div class="canvas-container">
                    <canvas id="ecgCanvas"></canvas>
                </div>
                <div class="metric-sub" style="margin-top: 8px; display: flex; justify-content: space-between;">
                    <span id="ecg-status">Normal Sinus Rhythm</span>
                    <span id="ecg-ts">Saved trace</span>
                </div>
            </div>

            <!-- Clinical Labs & Blood Biomarkers Card -->
            <div class="card clickable" style="border-top: 3px solid #10b981;" onclick="openLabModal()">
                <div class="card-header">
                    <span class="card-title">Clinical Labs & Biomarkers</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for panel ↗</span>
                        <span class="badge" id="lab-provider" style="background: rgba(16, 185, 129, 0.15); color: #10b981;">--</span>
                    </div>
                </div>
                <div class="metric-main" id="lab-main">-- <span style="font-size: 18px; font-weight: 500;">Biomarkers</span></div>
                <div class="metric-sub" id="lab-highlights" style="color: var(--text-main); font-weight: 500; margin-top: 4px;">Awaiting lab results</div>
                <div class="metric-sub" style="margin-top: 8px;" id="lab-ts">Awaiting report import</div>
            </div>

            <!-- Biological Age & Longevity Card -->
            <div class="card clickable" style="border-top: 3px solid #06b6d4;" onclick="openLongevityModal()">
                <div class="card-header">
                    <span class="card-title">Biological Age & Longevity</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for trajectory ↗</span>
                        <span class="badge" id="longevity-badge" style="background: rgba(6, 182, 212, 0.15); color: #06b6d4; border-color: rgba(6, 182, 212, 0.3);">--</span>
                    </div>
                </div>
                <div class="metric-main" id="pheno-age-val">-- <span style="font-size: 18px; font-weight: 500;">PhenoAge</span></div>
                <div class="metric-sub" id="pheno-age-sub" style="color: var(--accent-green); font-weight: 600;">Awaiting lab results</div>
                <div class="metric-sub" style="margin-top: 4px; opacity: 0.75;" id="pheno-age-basis"></div>
                <div class="metric-sub" style="margin-top: 8px;" id="ascvd-summary">10-Yr ASCVD Risk: --</div>
            </div>

            <!-- Sleep Architecture & Recovery Card -->
            <div class="card clickable" style="border-top: 3px solid #8b5cf6;" onclick="openSleepModal()">
                <div class="card-header">
                    <span class="card-title">Sleep Architecture & Recovery</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for sleep stages ↗</span>
                        <span class="badge" id="sleep-badge" style="background: rgba(139, 92, 246, 0.15); color: #8b5cf6;">-- Score</span>
                    </div>
                </div>
                <div class="metric-main" id="sleep-duration">-- <span style="font-size: 18px; font-weight: 500;">hrs</span></div>
                <div class="sleep-bar-container" id="sleep-stages-bar">
                    <div class="sleep-seg-deep" style="width: 20%;" title="Deep Sleep"></div>
                    <div class="sleep-seg-rem" style="width: 25%;" title="REM Sleep"></div>
                    <div class="sleep-seg-light" style="width: 50%;" title="Light Sleep"></div>
                    <div class="sleep-seg-awake" style="width: 5%;" title="Awake"></div>
                </div>
                <div class="metric-sub" id="sleep-breakdown" style="color: var(--text-main); font-weight: 500; font-size: 12px; margin-top: 4px;">Deep: -- • REM: -- • Light: --</div>
                <div class="metric-sub" style="margin-top: 6px; display: flex; justify-content: space-between;" id="sleep-vitals">
                    <span id="sleep-hrv">HRV: -- ms</span>
                    <span id="sleep-spo2">SpO2: --%</span>
                    <span id="sleep-date">Awaiting sleep data</span>
                </div>
            </div>

            <!-- Daily Protocol & Supplements Tracker Card -->
            <div class="card clickable" style="border-top: 3px solid #f59e0b;" onclick="openSupplementsModal()">
                <div class="card-header">
                    <span class="card-title">Daily Protocol & Supplements</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click to edit & review ↗</span>
                        <span class="badge" id="supp-badge" style="background: rgba(245, 158, 11, 0.15); color: #f59e0b;">--</span>
                    </div>
                </div>
                <div class="metric-main" id="supp-progress-val">--% <span style="font-size: 18px; font-weight: 500;">Adherence</span></div>
                <div id="supp-quick-checklist" style="margin-top: 8px;">
                    <div style="font-size: 12px; color: var(--text-muted);">Loading active stack...</div>
                </div>
                <div class="metric-sub" style="margin-top: 8px;" id="supp-footer">Personalized Regimen • Click to edit or consult Dr. Gemma</div>
            </div>

            <!-- Sovereign Genomics & Pharmacogenomics Card -->
            <div class="card clickable" style="border-top: 3px solid #ec4899;" onclick="openGenomicsModal()">
                <div class="card-header">
                    <span class="card-title">Genomics & Pharmacogenomics</span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for profile ↗</span>
                        <span class="badge" id="gen-badge" style="background: rgba(236, 72, 153, 0.15); color: #ec4899;">DNA</span>
                    </div>
                </div>
                <div class="metric-main" id="gen-main" style="font-size: 28px;">--</div>
                <div class="metric-sub" id="gen-highlights" style="color: var(--text-main); font-weight: 500; font-size: 12px; margin-top: 6px;">
                    Awaiting raw DNA import
                </div>
                <div class="metric-sub" style="margin-top: 8px;" id="gen-footer">100% Offline Sovereign DNA Screening</div>
            </div>

            <!-- Daily tap counter card: title and buttons come from the profile's counter set -->
            <div class="card clickable" id="counter-card" style="border-top: 3px solid #0ea5e9; display: none;" onclick="openCounterModal()">
                <div class="card-header">
                    <span class="card-title">
                        <span style="color: #0ea5e9; font-size: 18px;">●</span>
                        <span id="counter-title">Counter</span>
                    </span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for controls ↗</span>
                        <span class="badge" id="counter-badge" style="background: rgba(14, 165, 233, 0.15); color: #0ea5e9; border-color: rgba(14, 165, 233, 0.3);">Daily</span>
                    </div>
                </div>
                <div id="counter-elements" style="display: flex; flex-wrap: wrap; justify-content: space-around; align-items: center; gap: 8px; margin-top: 10px;"></div>
                <div id="counter-week" style="display: flex; gap: 6px; justify-content: space-between; margin-top: 12px;"></div>
                <div class="metric-sub" style="margin-top: 12px; text-align: center;" id="counter-footer"></div>
            </div>

            <!-- Patterns: correlations the hub found between counters, places and health (hidden until there's a log) -->
            <div class="card" id="patterns-card" style="border-top: 3px solid #14b8a6; display: none;">
                <div class="card-header">
                    <span class="card-title">
                        <span style="color: #14b8a6; font-size: 18px;">●</span>
                        Patterns
                    </span>
                    <span class="badge" id="patterns-badge" style="background: rgba(20, 184, 166, 0.15); color: #14b8a6; border-color: rgba(20, 184, 166, 0.3);">90 days</span>
                </div>
                <ul id="patterns-list" style="margin: 10px 0 0; padding-left: 18px; font-size: 13px; line-height: 1.5;"></ul>
                <div class="metric-sub" style="margin-top: 8px;">Correlations, not causes. Ask <span class="assistant-name">Dr. Gemma</span> what they mean.</div>
            </div>

            <!-- Dwell Places (>1 hr Yesterday) Card -->
            <div class="card clickable" style="border-top: 3px solid #6366f1;" onclick="openPlacesModal()">
                <div class="card-header">
                    <span class="card-title">
                        <span style="color: #6366f1; font-size: 18px;">●</span>
                        Dwell Places (>1 hr)
                    </span>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="click-hint">Click for places & map ↗</span>
                        <span class="badge" id="places-badge" style="background: rgba(99, 102, 241, 0.15); color: #818cf8; border-color: rgba(99, 102, 241, 0.3);">OwnTracks</span>
                    </div>
                </div>
                <div class="metric-main" id="places-count-val">-- <span style="font-size: 18px; font-weight: 500;">Places</span></div>
                <div id="places-summary-list" style="margin-top: 8px; display: flex; flex-direction: column; gap: 4px;">
                    <div style="font-size: 12px; color: var(--text-muted);">Awaiting dwell places...</div>
                </div>
                <div class="metric-sub" style="margin-top: 10px;" id="places-footer">Newest first • 1 hr or more in one spot</div>
            </div>
        </div>

        <!-- History Feed -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">Imported Health History (Google Fit, Garmin, Scales, BP2)</span>
                <span class="metric-sub" id="record-count">Loading...</span>
            </div>
            <table class="history-table">
                <thead>
                    <tr>
                        <th>Date & Time</th>
                        <th>Type</th>
                        <th>Measurement</th>
                        <th>Device / Origin</th>
                    </tr>
                </thead>
                <tbody id="history-body">
                    <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Fetching records from vault...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- Modal Dialog for Trends and Device Telemetry -->
    <div id="detailModal" class="modal-backdrop" onclick="onBackdropClick(event)">
        <div class="modal-container" onclick="event.stopPropagation()">
            <div class="modal-header">
                <div>
                    <h2 id="modalTitle" class="modal-title">Analytics</h2>
                    <div id="modalSubtitle" class="modal-subtitle">Historical trends & breakdown</div>
                </div>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>

            <!-- Filter buttons (Weight & BP) -->
            <div id="modalFilterRow" class="filter-row">
                <button class="filter-btn" data-period="today" onclick="setPeriod('today')">Today</button>
                <button class="filter-btn active" data-period="week" onclick="setPeriod('week')">Week</button>
                <button class="filter-btn" data-period="month" onclick="setPeriod('month')">Month</button>
                <button class="filter-btn" data-period="year" onclick="setPeriod('year')">Year</button>
                <button class="filter-btn" data-period="all" onclick="setPeriod('all')">All Time</button>
            </div>

            <!-- Stats Grid -->
            <div id="modalStatsGrid" class="modal-stats-grid"></div>

            <!-- Chart Canvas Container -->
            <div id="modalChartCard" class="modal-chart-card">
                <div id="chartLegend" class="chart-legend"></div>
                <div style="position: relative; width: 100%; height: calc(100% - 24px);">
                    <canvas id="modalChartCanvas"></canvas>
                </div>
            </div>

            <!-- Device Telemetry View (Garmin) -->
            <div id="modalDeviceTelemetry" style="display: none;"></div>

            <!-- Mini Log Table -->
            <div style="margin-top: 24px;">
                <h3 id="modalTableTitle" style="font-size: 13px; font-weight: 600; text-transform: uppercase; color: var(--text-muted); margin-bottom: 10px;">Recordings</h3>
                <div id="modalTableScroll" style="max-height: 240px; overflow-y: auto; border: 1px solid var(--border); border-radius: 10px;">
                    <table class="history-table" style="margin-top: 0;">
                        <thead id="modalTableHead"></thead>
                        <tbody id="modalTableBody"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- Dedicated Modal for Dr. Gemma Clinical AI -->
    <div id="chatModal" class="modal-backdrop" onclick="onChatBackdropClick(event)">
        <div class="modal-container" style="max-width: 780px; display: flex; flex-direction: column; height: 85vh;" onclick="event.stopPropagation()">
            <div class="modal-header" style="margin-bottom: 12px;">
                <div style="display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 28px;">🩺</span>
                    <div>
                        <h2 class="modal-title">Dr. Gemma Clinical AI</h2>
                        <div class="modal-subtitle">Synthesized with your lab results, sleep stages, and telemetry</div>
                    </div>
                </div>
                <button class="modal-close" onclick="closeChatModal()">&times;</button>
            </div>

            <!-- Suggested Prompt Chips -->
            <div class="chip-container">
                <button class="prompt-chip" onclick="sendPromptChip('Explain my biological age (PhenoAge) compared to my calendar age')">🧬 Explain my PhenoAge</button>
                <button class="prompt-chip" onclick="sendPromptChip('Analyze my HDL and cardiovascular risk')">❤️ Interpret my HDL & ASCVD</button>
                <button class="prompt-chip" onclick="sendPromptChip('Review my Garmin sleep stages and overnight HRV')">🌙 Sleep stages & HRV recovery</button>
                <button class="prompt-chip" onclick="sendPromptChip(SUPP_REVIEW_PROMPT)">💊 Review my supplements & additions</button>
                <button class="prompt-chip" onclick="sendPromptChip('Summarize my pharmacogenomics and ApoE status')">🔬 Pharmacogenomics & ApoE</button>
            </div>

            <!-- Conversation Body -->
            <div id="chatMessages" style="flex: 1; overflow-y: auto; padding: 14px; background: #070a13; border: 1px solid var(--border); border-radius: 14px; margin-bottom: 14px;">
                <div class="chat-bubble ai">
                    <strong style="color: var(--accent-purple);">Dr. Gemma:</strong><br/>
                    Hello<span id="chat-greeting-name"></span>. I have loaded your health vault: lab results, biological age, sleep and HRV telemetry, body composition, and genomic markers.
                    <br/><br/>
                    What aspect of your preventive health, recovery, or longevity would you like to review today?
                </div>
            </div>

            <!-- Input area -->
            <div style="display: flex; gap: 10px; align-items: center;">
                <input type="text" id="chatInput" placeholder="Ask Dr. Gemma about your health telemetry, labs, or recovery..." 
                       style="flex: 1; background: var(--surface-card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 16px; color: #fff; font-size: 14px; outline: none;"
                       onkeydown="if(event.key==='Enter') sendChatMessage()"/>
                <button onclick="sendChatMessage()" id="chatSendBtn" 
                        style="background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); color: #fff; border: none; padding: 12px 20px; border-radius: 12px; font-weight: 700; cursor: pointer;">
                    Send
                </button>
            </div>
        </div>
    </div>


    <script>
        // Every API response is HTML-escaped (esc) before rendering: the page
        // builds its markup with innerHTML, and record fields come from clients.
        function esc(v) {
            if (typeof v === 'string') {
                return v.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
            }
            if (Array.isArray(v)) return v.map(esc);
            if (v && typeof v === 'object') {
                return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, esc(x)]));
            }
            return v;
        }

        let currentModalType = null; // 'weight', 'bp', or 'device'
        let currentPeriod = 'week';   // 'today', 'week', 'month', 'year', 'all'
        let cachedRecords = {
            weight: [],
            bp: [],
            activity: []
        };

        async function loadSummary() {
            try {
                const res = await fetch('/api/summary');
                const data = esc(await res.json());
                
                if (data.latest_weight) {
                    const w = data.latest_weight;
                    document.getElementById('weight-lb').innerHTML = `${w.weight_lb || (w.weight_kg * 2.20462).toFixed(1)} <span style="font-size: 18px; font-weight: 500;">lb</span>`;
                    document.getElementById('weight-kg').innerText = `(${w.weight_kg} kg)`;
                    document.getElementById('weight-dev').innerText = w.device_name || 'Scale';
                    document.getElementById('weight-ts').innerText = (w.timestamp || '').substring(0, 19).replace('T', ' ');
                }

                if (data.latest_bp) {
                    const bp = data.latest_bp;
                    document.getElementById('bp-val').innerHTML = `${bp.systolic}/${bp.diastolic} <span style="font-size: 18px; font-weight: 500;">mmHg</span>`;
                    document.getElementById('bp-pulse').innerText = `Pulse: ${bp.pulse ?? "--"} bpm`;
                    document.getElementById('bp-dev').innerText = bp.device_name || 'Cuff';
                    document.getElementById('bp-ts').innerText = (bp.timestamp || '').substring(0, 19).replace('T', ' ');
                }

                if (data.latest_activity) {
                    const act = data.latest_activity;
                    if (act.steps !== undefined && act.steps !== null) {
                        document.getElementById('garmin-steps').innerText = Number(act.steps).toLocaleString();
                    }
                    if (act.distance_km !== undefined && act.distance_km !== null) {
                        document.getElementById('garmin-dist').innerText = `${act.distance_km} km`;
                    }
                    if (act.source) {
                        const badge = document.getElementById('garmin-badge');
                        if (badge) badge.innerText = act.source;
                    }
                }

                if (data.latest_ecg && data.latest_ecg.samples_mv) {
                    drawEcg(data.latest_ecg.samples_mv);
                    document.getElementById('ecg-hr').innerText = `${data.latest_ecg.heart_rate || 75} bpm`;
                    document.getElementById('ecg-status').innerText = data.latest_ecg.rhythm_status || 'Normal Sinus Rhythm';
                    document.getElementById('ecg-ts').innerText = (data.latest_ecg.timestamp || '').substring(0, 19).replace('T', ' ');
                } else {
                    drawGridOnly();
                }

                if (data.latest_lab && data.latest_lab.markers) {
                    const lab = data.latest_lab;
                    const m = lab.markers;
                    const mCount = Object.keys(m).length;
                    document.getElementById('lab-main').innerHTML = `${mCount} <span style="font-size: 18px; font-weight: 500;">Biomarkers</span>`;
                    const chol = m.total_cholesterol ? `Chol: ${m.total_cholesterol.value}` : '';
                    const hdl = m.hdl_cholesterol ? `HDL: ${m.hdl_cholesterol.value}` : '';
                    const a1c = m.hba1c ? `A1c: ${m.hba1c.value}%` : '';
                    const egfr = m.egfr ? `eGFR: ${m.egfr.value}` : '';
                    const parts = [chol, hdl, a1c, egfr].filter(Boolean);
                    document.getElementById('lab-highlights').innerText = parts.join(' | ') || 'Comprehensive Panel';
                    document.getElementById('lab-provider').innerText = lab.lab_name || 'Lab';
                    document.getElementById('lab-ts').innerText = 'Collected: ' + (lab.timestamp || '').substring(0, 10);
                }

                // Longevity & Biological Age
                try {
                    const lRes = await fetch('/api/longevity');
                    const lData = esc(await lRes.json());
                    if (lData && lData.current) {
                        const cur = lData.current;
                        document.getElementById('pheno-age-val').innerHTML = `${cur.phenotypic_age} <span style="font-size: 18px; font-weight: 500;">PhenoAge</span>`;
                        document.getElementById('pheno-age-sub').innerText = `${Math.abs(cur.biological_age_delta)} years ${cur.biological_age_delta <= 0 ? 'younger' : 'older'} than calendar age (${cur.chronological_age})`;
                        document.getElementById('longevity-badge').innerText = `${cur.biological_age_delta} Yrs`;
                        const names = {lymphocytes: 'lymphocyte %', crp: 'CRP'};
                        let basis = `${cur.markers_measured}/9 PhenoAge markers measured`;
                        if (cur.assumed_markers && cur.assumed_markers.length) {
                            basis += `; ${cur.assumed_markers.map(k => names[k] || k).join(', ')} assumed`;
                            if (cur.phenotypic_age_range) basis += ` (could be ${cur.phenotypic_age_range[0]}–${cur.phenotypic_age_range[1]})`;
                        }
                        const b = cur.body;
                        if (b) basis += ` · BMI ${b.bmi}${b.body_fat_percent ? `, body fat ${b.body_fat_percent}%` : ''} (${b.date})`;
                        document.getElementById('pheno-age-basis').innerText = basis;
                        document.getElementById('ascvd-summary').innerText = `10-Yr ASCVD Risk: ${pct(cur.ten_year_ascvd_risk)} (${cur.ascvd_category})`;
                    } else if (lData && lData.error) {
                        document.getElementById('pheno-age-sub').innerText = lData.error;
                    }
                } catch (le) {
                    console.error("Longevity metric error:", le);
                }

                // Sleep Architecture & Recovery Card
                if (data.latest_workout) {
                    const w = data.latest_workout;
                    document.getElementById('workout-sport').innerText = sportName(w.sport);
                    document.getElementById('workout-main').innerHTML = workoutKm(w)
                        ? `${workoutKm(w).toFixed(2)} <span style="font-size: 18px; font-weight: 500;">km</span>`
                        : fmtDuration(w.duration_seconds);
                    document.getElementById('workout-detail').innerText = `Time ${fmtDuration(w.duration_seconds)} • ${fmtPace(w)}`;
                    document.getElementById('workout-hr').innerText =
                        `Avg HR ${w.avg_heart_rate ?? '--'} bpm • ${w.calories ?? '--'} kcal` +
                        (w.aerobic_training_effect ? ` • TE ${w.aerobic_training_effect}` : '');
                    document.getElementById('workout-ts').innerText = `${fmtWhen(w.timestamp)} • ${w.device_name || 'device'}`;
                }

                if (data.latest_sleep) {
                    const sl = data.latest_sleep;
                    const durHrs = sl.duration_hours || (sl.duration_seconds / 3600).toFixed(1);
                    document.getElementById('sleep-duration').innerHTML = `${durHrs} <span style="font-size: 18px; font-weight: 500;">hrs</span>`;
                    if (sl.sleep_score) {
                        document.getElementById('sleep-badge').innerText = `${sl.sleep_score}/100 Score`;
                    } else if (sl.estimated_sleep_score) {
                        document.getElementById('sleep-badge').innerText = `~${sl.estimated_sleep_score}/100 Est.`;
                    }
                    const deepM = sl.deep_sleep_minutes || Math.round((sl.deep_sleep_seconds || 0)/60);
                    const remM = sl.rem_sleep_minutes || Math.round((sl.rem_sleep_seconds || 0)/60);
                    const lightM = sl.light_sleep_minutes || Math.round((sl.light_sleep_seconds || 0)/60);
                    const totM = deepM + remM + lightM || 1;
                    const deepPct = Math.round((deepM / totM) * 100);
                    const remPct = Math.round((remM / totM) * 100);
                    const lightPct = Math.max(0, 100 - deepPct - remPct);

                    const bar = document.getElementById('sleep-stages-bar');
                    if (bar) {
                        bar.innerHTML = `
                            <div class="sleep-seg-deep" style="width: ${deepPct}%;" title="Deep: ${deepM}m (${deepPct}%)"></div>
                            <div class="sleep-seg-rem" style="width: ${remPct}%;" title="REM: ${remM}m (${remPct}%)"></div>
                            <div class="sleep-seg-light" style="width: ${lightPct}%;" title="Light: ${lightM}m (${lightPct}%)"></div>
                        `;
                    }
                    document.getElementById('sleep-breakdown').innerText = `Deep: ${deepM}m (${deepPct}%) • REM: ${remM}m (${remPct}%) • Light: ${lightM}m`;
                    document.getElementById('sleep-hrv').innerText = sl.hrv_rmssd_ms ? `HRV: ${sl.hrv_rmssd_ms} ms` : 'HRV: --';
                    document.getElementById('sleep-spo2').innerText = sl.avg_spo2 ? `SpO2: ${sl.avg_spo2}%` : 'SpO2: --';
                    document.getElementById('sleep-date').innerText = (sl.date || (sl.timestamp || '').substring(0, 10));
                }

                // Daily Protocol & Supplements Tracker Card
                if (data.supplements) {
                    const sup = data.supplements;
                    document.getElementById('supp-badge').innerText = `${sup.completed_count}/${sup.total_count} Taken`;
                    document.getElementById('supp-progress-val').innerHTML = `${sup.completion_rate_percent}% <span style="font-size: 18px; font-weight: 500;">Adherence</span>`;
                    const checkContainer = document.getElementById('supp-quick-checklist');
                    if (checkContainer && sup.checklist) {
                        checkContainer.innerHTML = '';
                        // Open doses first (in day order), so the tile shows what's still to take.
                        [...sup.checklist].sort((a, b) => a.taken_today - b.taken_today).slice(0, 3).forEach(item => {
                            const taken = item.taken_today;
                            const div = document.createElement('div');
                            div.className = `supp-pill ${taken ? 'taken' : ''}`;
                            div.innerHTML = `
                                <div>
                                    <strong>${item.name}</strong> <span style="color: var(--text-muted); font-size: 12px;">(${item.dosage}${item.unit}${item.slot ? ', ' + item.slot : ''})</span>
                                </div>
                                <button class="supp-check-btn ${taken ? 'checked' : ''}" data-name="${item.name}" data-slot="${item.slot || ''}" onclick="event.stopPropagation(); quickLog(this.dataset.name, this.dataset.slot)" title="${taken ? 'Logged today' : 'Click to log intake'}">
                                    ${taken ? '✓' : '+'}
                                </button>
                            `;
                            checkContainer.appendChild(div);
                        });
                    }
                }

                // Sovereign Genomics & Pharmacogenomics Card
                if (data.genomics && data.genomics.key_highlights) {
                    const gen = data.genomics.key_highlights;
                    const ts = data.genomics.trio_status || {};
                    const short = v => (v && v !== 'Not genotyped') ? v.split(' ')[0] : '--';
                    document.getElementById('gen-badge').innerText = ts.is_phased_trio ? 'Trio Phased' : 'DNA';
                    document.getElementById('gen-main').innerText = short(gen.apoe_isoform) === '--' ? 'No DNA imported' : `ApoE ${short(gen.apoe_isoform)}`;
                    document.getElementById('gen-highlights').innerText = `MTHFR: ${short(gen.mthfr_efficiency)} • Caffeine: ${short(gen.caffeine_metabolism)} • Statin: ${short(gen.statin_sensitivity)}`;
                    if (ts.total_family_snps) {
                        document.getElementById('gen-footer').innerText = `${ts.total_family_snps.toLocaleString()} SNPs analyzed${ts.is_phased_trio ? ' across self, father & mother' : ''}`;
                    }
                }

                // Daily tap counter card (hidden when the profile has no counter set)
                if (data.counter) renderCounterCard(data.counter);

                // Dwell Places Card
                if (data.places) {
                    const pl = data.places;
                    const places = pl.places || [];
                    const countEl = document.getElementById('places-count-val');
                    if (countEl) countEl.innerHTML = `${places.length} <span style="font-size: 18px; font-weight: 500;">${places.length === 1 ? 'Place' : 'Places'}</span>`;
                    const badge = document.getElementById('places-badge');
                    if (badge) badge.innerText = `Logged Places (${places.length})`;
                    const container = document.getElementById('places-summary-list');
                    if (container) {
                        if (places.length === 0) {
                            container.innerHTML = '<div style="font-size: 12px; color: var(--text-muted);">No places recorded yet.</div>';
                        } else {
                            container.innerHTML = places.slice(0, 4).map(p => {
                                const name = p.name || `${p.avg_lat.toFixed(3)}, ${p.avg_lon.toFixed(3)}`;
                                const dateSub = p.date ? `<span style="font-size: 11px; color: var(--text-muted); font-weight: normal;"> (${p.date})</span>` : '';
                                return `<div class="clickable" onclick="event.stopPropagation(); openPlacesModal('${p.id}')" style="font-size: 13px; display: flex; justify-content: space-between; padding: 2px 4px; border-radius: 4px; transition: background 0.15s;" onmouseover="this.style.background='rgba(99,102,241,0.1)'" onmouseout="this.style.background='transparent'">
                                    <span style="font-weight: 500;">📍 ${name}${dateSub}</span>
                                    <span style="color: #818cf8; font-weight: 600;">${p.dwell_formatted}</span>
                                </div>`;
                            }).join('');
                        }
                    }
                }

                document.getElementById('record-count').innerText = `${data.total_records} records stored`;
            } catch (e) {
                console.error("Summary error:", e);
            }
        }

        async function loadHistory() {
            try {
                const res = await fetch('/api/history?limit=100');
                const data = esc(await res.json());
                const tbody = document.getElementById('history-body');
                tbody.innerHTML = '';

                data.records.forEach(r => {
                    const tr = document.createElement('tr');
                    let tagClass = 'tag-weight';
                    let typeName = 'Weight';
                    let val = '';
                    let origin = r.device_name || r.source || 'Local';

                    if (r.weight_kg !== undefined || r.type === 'weight') {
                        tagClass = 'tag-weight';
                        typeName = 'Weight';
                        val = `${r.weight_lb || (r.weight_kg * 2.20462).toFixed(1)} lb (${r.weight_kg} kg)`;
                    } else if (r.systolic !== undefined || r.type === 'blood_pressure') {
                        tagClass = 'tag-bp';
                        typeName = 'Blood Pressure';
                        val = `${r.systolic}/${r.diastolic} mmHg (P: ${r.pulse ?? "--"} bpm)`;
                    } else if (r.samples_mv !== undefined || r.type === 'ecg') {
                        tagClass = 'tag-ecg';
                        typeName = 'Lead-I ECG';
                        val = `${r.samples_mv.length} samples (${r.duration_seconds || 30}s @ 125Hz)`;
                    } else if (r.type === 'activity' || (r.steps !== undefined && r.sport === undefined)) {
                        tagClass = 'tag-garmin';
                        typeName = 'Daily Activity';
                        val = `${(r.steps || 0).toLocaleString()} steps (${r.distance_km || 0} km)`;
                    } else if (r.sport !== undefined || r.type === 'workout') {
                        tagClass = 'tag-garmin';
                        typeName = `Workout (${r.sport})`;
                        val = `${(r.distance_km || (r.distance_meters/1000)).toFixed(2)} km in ${(r.duration_seconds/60).toFixed(1)} min`;
                    } else if (r.markers !== undefined || r.type === 'blood_test') {
                        tagClass = 'tag-lab';
                        typeName = 'Blood Test';
                        const mCount = Object.keys(r.markers || {}).length;
                        const chol = r.markers?.total_cholesterol ? `Chol ${r.markers.total_cholesterol.value}` : '';
                        const hdl = r.markers?.hdl_cholesterol ? `HDL ${r.markers.hdl_cholesterol.value}` : '';
                        const a1c = r.markers?.hba1c ? `A1c ${r.markers.hba1c.value}%` : '';
                        const summary = [chol, hdl, a1c].filter(Boolean).join(', ');
                        val = `${mCount} Biomarkers (${summary})`;
                        origin = r.lab_name || 'Lab';
                    } else if (r.deep_sleep_seconds !== undefined || r.type === 'sleep') {
                        tagClass = 'tag-sleep';
                        typeName = 'Sleep & Recovery';
                        const scoreStr = r.sleep_score ? `Score: ${r.sleep_score}/100, ` : '';
                        const hrvStr = r.hrv_rmssd_ms ? `HRV: ${r.hrv_rmssd_ms} ms` : '';
                        val = `${r.duration_hours || (r.duration_seconds/3600).toFixed(1)} hrs (${scoreStr}${hrvStr})`;
                        origin = r.device_name || 'Garmin';
                    } else if (r.dosage !== undefined || r.type === 'supplement') {
                        tagClass = 'tag-supp';
                        typeName = 'Supplement';
                        val = `${r.name} (${r.dosage}${r.unit} - ${r.frequency || 'daily'})`;
                        origin = 'Protocol Log';
                    } else if (r.type === 'counter') {
                        tagClass = 'tag-supp';
                        typeName = r.title || 'Counter';
                        val = `${r.date}: ${r.display_name || r.item} ${r.delta > 0 ? '+' : ''}${r.delta}`;
                        origin = 'Counter';
                    } else if (r.variants !== undefined || r.type === 'genomics') {
                        tagClass = 'tag-gen';
                        typeName = 'Genomics';
                        val = `Sovereign DNA Profile: ApoE ${r.key_highlights?.apoe_isoform || ''}`;
                        origin = 'Sovereign Profiler';
                    }

                    tr.innerHTML = `
                        <td style="color: var(--text-muted);">${(r.timestamp || r.date || '').substring(0, 19).replace('T', ' ')}</td>
                        <td><span class="tag ${tagClass}">${typeName}</span></td>
                        <td style="font-weight: 600;">${val}</td>
                        <td style="color: var(--text-muted);">${origin}</td>
                    `;
                    tbody.appendChild(tr);
                });
            } catch (e) {
                console.error("History error:", e);
            }
        }

        /* Modal Dialog Management */
        function openModal() {
            // Every open* renders after this, so stale content from the last modal never shows.
            ['modalStatsGrid', 'modalTableHead', 'modalTableBody', 'modalDeviceTelemetry']
                .forEach(id => { document.getElementById(id).innerHTML = ''; });
            const scroll = document.getElementById('modalTableScroll');
            scroll.style.maxHeight = '240px';
            scroll.onscroll = null;
            document.getElementById('detailModal').classList.add('open');
        }

        function closeModal() {
            document.getElementById('detailModal').classList.remove('open');
            document.querySelector('#detailModal .modal-container')?.classList.remove('modal-wide');
            currentModalType = null;
        }

        function onBackdropClick(e) {
            if (e.target.id === 'detailModal') {
                closeModal();
            }
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeModal();
        });

        function setPeriod(period) {
            currentPeriod = period;
            document.querySelectorAll('.filter-btn').forEach(btn => {
                if (btn.dataset.period === period) btn.classList.add('active');
                else btn.classList.remove('active');
            });

            if (currentModalType === 'weight') {
                renderWeightModal();
            } else if (currentModalType === 'bp') {
                renderBpModal();
            }
        }

        /* Filter Records by Period */
        function filterByPeriod(records, dateKey = 'timestamp') {
            const now = new Date();
            let cutoff = new Date(0);

            if (currentPeriod === 'today') {
                cutoff = new Date(now.getFullYear(), now.getMonth(), now.getDate());
            } else if (currentPeriod === 'week') {
                cutoff = new Date(now.getTime() - 7 * 24 * 3600 * 1000);
            } else if (currentPeriod === 'month') {
                cutoff = new Date(now.getTime() - 30 * 24 * 3600 * 1000);
            } else if (currentPeriod === 'year') {
                cutoff = new Date(now.getTime() - 365 * 24 * 3600 * 1000);
            } else if (currentPeriod === 'all') {
                cutoff = new Date(0);
            }

            return records.filter(r => {
                const rawDate = r[dateKey] || r.date || r.timestamp;
                if (!rawDate) return false;
                const d = new Date(rawDate);
                return d >= cutoff;
            }).sort((a, b) => new Date(a[dateKey] || a.date || a.timestamp) - new Date(b[dateKey] || b.date || b.timestamp));
        }

        /* 1. Weight Modal */
        async function openWeightModal() {
            currentModalType = 'weight';
            currentPeriod = 'week';
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.period === 'week'));
            document.getElementById('modalTitle').innerText = 'Weight & Body Composition';
            document.getElementById('modalSubtitle').innerText = 'Trends for Weight (lb/kg), BMI, Body Fat %, and Impedance';
            document.getElementById('modalFilterRow').style.display = 'flex';
            document.getElementById('modalChartCard').style.display = 'block';
            document.getElementById('modalDeviceTelemetry').style.display = 'none';

            openModal();

            // Fetch history
            const res = await fetch('/api/history?record_type=weight&limit=2000');
            const data = esc(await res.json());
            cachedRecords.weight = data.records;
            renderWeightModal();
        }

        function renderWeightModal() {
            let filtered = filterByPeriod(cachedRecords.weight);
            const statsGrid = document.getElementById('modalStatsGrid');
            const thead = document.getElementById('modalTableHead');
            const tbody = document.getElementById('modalTableBody');

            // If empty in this range but records exist overall, show a helper note
            const latestOverall = cachedRecords.weight[0] || null;

            if (filtered.length === 0) {
                statsGrid.innerHTML = `
                    <div class="stat-box" style="grid-column: 1 / -1; border-color: rgba(245, 158, 11, 0.4);">
                        <div class="stat-label" style="color: var(--accent-orange);">No weigh-ins in this period (${currentPeriod})</div>
                        <div class="stat-value" style="font-size: 16px; margin: 4px 0;">
                            ${latestOverall ? `Latest measurement on file: <strong>${latestOverall.weight_lb || (latestOverall.weight_kg * 2.20462).toFixed(1)} lb</strong> on ${(latestOverall.timestamp || '').substring(0, 10)}.` : 'No weigh-ins recorded yet.'}
                        </div>
                        <div class="stat-sub">Switch to <strong>All Time</strong> or step on your scale to view trends.</div>
                    </div>
                `;
                drawEmptyChart("No weigh-in data for selected period");
                thead.innerHTML = '';
                tbody.innerHTML = '<tr><td style="text-align: center; color: var(--text-muted); padding: 16px;">No entries in this timeframe.</td></tr>';
                return;
            }

            // Stats
            const weightsLb = filtered.map(r => r.weight_lb || (r.weight_kg * 2.20462));
            const latest = filtered[filtered.length - 1];
            const first = filtered[0];
            const minW = Math.min(...weightsLb).toFixed(1);
            const maxW = Math.max(...weightsLb).toFixed(1);
            const delta = (latest.weight_lb || (latest.weight_kg * 2.20462)) - (first.weight_lb || (first.weight_kg * 2.20462));
            const deltaStr = delta === 0 ? '0.0 lb' : (delta > 0 ? `+${delta.toFixed(1)} lb` : `${delta.toFixed(1)} lb`);

            statsGrid.innerHTML = `
                <div class="stat-box">
                    <div class="stat-label">Latest Weight</div>
                    <div class="stat-value" style="color: var(--accent-purple);">${(latest.weight_lb || (latest.weight_kg * 2.20462)).toFixed(1)} <span style="font-size: 13px;">lb</span></div>
                    <div class="stat-sub">${latest.weight_kg.toFixed(1)} kg</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">BMI</div>
                    <div class="stat-value" style="color: var(--accent-orange);">${latest.bmi ? latest.bmi.toFixed(1) : (latest.weight_kg / 3.06).toFixed(1)}</div>
                    <div class="stat-sub">Normal (18.5 - 24.9)</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Body Fat %</div>
                    <div class="stat-value" style="color: var(--accent-blue);">${latest.body_fat_percent ? `${latest.body_fat_percent.toFixed(1)}%` : '--'}</div>
                    <div class="stat-sub">${latest.impedance ? `${latest.impedance} Ω impedance` : 'Bioimpedance'}</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Period Change / Range</div>
                    <div class="stat-value" style="color: ${delta <= 0 ? 'var(--accent-green)' : 'var(--accent-orange)'};">${deltaStr}</div>
                    <div class="stat-sub">Min: ${minW} • Max: ${maxW} lb</div>
                </div>
            `;

            // Chart
            document.getElementById('chartLegend').innerHTML = `
                <div class="legend-item"><div class="legend-dot" style="background: var(--accent-purple);"></div> Weight (lb)</div>
                <div class="legend-item"><div class="legend-dot" style="background: var(--accent-orange);"></div> BMI</div>
            `;

            drawTimeSeriesChart(
                filtered.map(r => ({
                    time: new Date(r.timestamp),
                    val1: r.weight_lb || (r.weight_kg * 2.20462),
                    val2: r.bmi || (r.weight_kg / 3.06)
                })),
                '#8b5cf6',
                '#f59e0b',
                'lb',
                'BMI'
            );

            // Table
            thead.innerHTML = `<tr><th>Date & Time</th><th>Weight</th><th>BMI</th><th>Body Fat</th><th>Device</th></tr>`;
            tbody.innerHTML = '';
            [...filtered].reverse().forEach(r => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td style="color: var(--text-muted);">${(r.timestamp || '').substring(0, 19).replace('T', ' ')}</td>
                    <td style="font-weight: 700; color: var(--accent-purple);">${(r.weight_lb || (r.weight_kg * 2.20462)).toFixed(1)} lb (${r.weight_kg.toFixed(1)} kg)</td>
                    <td>${r.bmi ? r.bmi.toFixed(1) : '--'}</td>
                    <td>${r.body_fat_percent ? `${r.body_fat_percent.toFixed(1)}%` : (r.impedance ? `${r.impedance} Ω` : '--')}</td>
                    <td style="color: var(--text-muted);">${r.device_name || 'Scale'}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        /* 2. Blood Pressure Modal */
        async function openBpModal() {
            currentModalType = 'bp';
            currentPeriod = 'week';
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.period === 'week'));
            document.getElementById('modalTitle').innerText = 'Blood Pressure & Heart Rate';
            document.getElementById('modalSubtitle').innerText = 'Trends for Systolic, Diastolic, and Pulse';
            document.getElementById('modalFilterRow').style.display = 'flex';
            document.getElementById('modalChartCard').style.display = 'block';
            document.getElementById('modalDeviceTelemetry').style.display = 'none';

            openModal();

            const res = await fetch('/api/history?record_type=bp&limit=2000');
            const data = esc(await res.json());
            cachedRecords.bp = data.records;
            renderBpModal();
        }

        function renderBpModal() {
            let filtered = filterByPeriod(cachedRecords.bp);
            const statsGrid = document.getElementById('modalStatsGrid');
            const thead = document.getElementById('modalTableHead');
            const tbody = document.getElementById('modalTableBody');
            const latestOverall = cachedRecords.bp[0] || null;

            if (filtered.length === 0) {
                statsGrid.innerHTML = `
                    <div class="stat-box" style="grid-column: 1 / -1; border-color: rgba(245, 158, 11, 0.4);">
                        <div class="stat-label" style="color: var(--accent-orange);">No readings in this period (${currentPeriod})</div>
                        <div class="stat-value" style="font-size: 16px; margin: 4px 0;">
                            ${latestOverall ? `Latest BP on file: <strong>${latestOverall.systolic}/${latestOverall.diastolic} mmHg</strong> on ${(latestOverall.timestamp || '').substring(0, 10)}.` : 'No blood pressure recorded yet.'}
                        </div>
                        <div class="stat-sub">Switch to <strong>All Time</strong> or take a reading on your cuff.</div>
                    </div>
                `;
                drawEmptyChart("No blood pressure data for selected period");
                thead.innerHTML = '';
                tbody.innerHTML = '<tr><td style="text-align: center; color: var(--text-muted); padding: 16px;">No entries in this timeframe.</td></tr>';
                return;
            }

            const latest = filtered[filtered.length - 1];
            const avgSys = Math.round(filtered.reduce((a, b) => a + b.systolic, 0) / filtered.length);
            const avgDia = Math.round(filtered.reduce((a, b) => a + b.diastolic, 0) / filtered.length);
            const pulses = filtered.map(r => r.pulse).filter(p => p > 0);
            const avgPulse = pulses.length ? Math.round(pulses.reduce((a, b) => a + b, 0) / pulses.length) : '--';

            let bpCategory = 'Normal';
            let catColor = 'var(--accent-green)';
            if (latest.systolic >= 140 || latest.diastolic >= 90) {
                bpCategory = 'Stage 2 HTN';
                catColor = 'var(--accent-red)';
            } else if (latest.systolic >= 130 || latest.diastolic >= 80) {
                bpCategory = 'Stage 1 HTN';
                catColor = 'var(--accent-orange)';
            } else if (latest.systolic >= 120 && latest.diastolic < 80) {
                bpCategory = 'Elevated';
                catColor = 'var(--accent-orange)';
            }

            statsGrid.innerHTML = `
                <div class="stat-box">
                    <div class="stat-label">Latest Blood Pressure</div>
                    <div class="stat-value" style="color: var(--accent-red);">${latest.systolic}/${latest.diastolic} <span style="font-size: 13px;">mmHg</span></div>
                    <div class="stat-sub">Pulse: ${latest.pulse ?? "--"} bpm</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">AHA Classification</div>
                    <div class="stat-value" style="color: ${catColor};">${bpCategory}</div>
                    <div class="stat-sub">Based on latest cuff reading</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Period Average BP</div>
                    <div class="stat-value" style="color: var(--accent-purple);">${avgSys}/${avgDia} <span style="font-size: 13px;">mmHg</span></div>
                    <div class="stat-sub">${filtered.length} readings recorded</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Average Pulse</div>
                    <div class="stat-value" style="color: var(--accent-blue);">${avgPulse} <span style="font-size: 13px;">bpm</span></div>
                    <div class="stat-sub">Resting cardiovascular</div>
                </div>
            `;

            // Chart
            document.getElementById('chartLegend').innerHTML = `
                <div class="legend-item"><div class="legend-dot" style="background: var(--accent-red);"></div> Systolic</div>
                <div class="legend-item"><div class="legend-dot" style="background: var(--accent-orange);"></div> Diastolic</div>
                <div class="legend-item"><div class="legend-dot" style="background: var(--accent-blue);"></div> Pulse</div>
            `;

            drawTimeSeriesChart(
                filtered.map(r => ({
                    time: new Date(r.timestamp),
                    val1: r.systolic,
                    val2: r.diastolic,
                    val3: r.pulse
                })),
                '#ef4444',
                '#f59e0b',
                'mmHg (Sys)',
                'mmHg (Dia)',
                '#38bdf8'
            );

            // Table
            thead.innerHTML = `<tr><th>Date & Time</th><th>Blood Pressure</th><th>Pulse</th><th>Device</th></tr>`;
            tbody.innerHTML = '';
            [...filtered].reverse().forEach(r => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td style="color: var(--text-muted);">${(r.timestamp || '').substring(0, 19).replace('T', ' ')}</td>
                    <td style="font-weight: 700; color: var(--accent-red);">${r.systolic}/${r.diastolic} mmHg</td>
                    <td style="color: var(--accent-blue);">${r.pulse ?? "--"} bpm</td>
                    <td style="color: var(--text-muted);">${r.device_name || 'Cuff'}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        /* 3. Garmin / Device Telemetry Modal */
        async function openDeviceModal() {
            currentModalType = 'device';
            document.getElementById('modalTitle').innerText = 'Watch – Daily Telemetry';
            document.getElementById('modalSubtitle').innerText = 'Full breakdown of daily metrics collected from the watch';
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'none';
            document.getElementById('modalDeviceTelemetry').style.display = 'block';

            openModal();

            const res = await fetch('/api/activity/today');
            const data = esc(await res.json());
            const act = data.activity || {};
            const workouts = data.workouts || [];

            const statsGrid = document.getElementById('modalStatsGrid');
            const telemContainer = document.getElementById('modalDeviceTelemetry');
            const thead = document.getElementById('modalTableHead');
            const tbody = document.getElementById('modalTableBody');

            const steps = act.steps || 0;
            const distKm = act.distance_km || 0;
            const cal = act.calories || Math.round(steps * 0.045);
            const activeMin = act.active_minutes || Math.round(steps / 110);
            const rhr = act.resting_heart_rate || '--';

            statsGrid.innerHTML = `
                <div class="stat-box">
                    <div class="stat-label">Steps Today</div>
                    <div class="stat-value" style="color: var(--accent-orange);">${steps.toLocaleString()}</div>
                    <div class="stat-sub">${Math.round(steps / 10000 * 100)}% of 10k goal</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Total Distance</div>
                    <div class="stat-value" style="color: var(--accent-blue);">${distKm} <span style="font-size: 13px;">km</span></div>
                    <div class="stat-sub">${(distKm * 0.621371).toFixed(2)} miles</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Energy Burned</div>
                    <div class="stat-value" style="color: var(--accent-red);">${cal} <span style="font-size: 13px;">kcal</span></div>
                    <div class="stat-sub">Active & basal energy</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Resting Heart Rate</div>
                    <div class="stat-value" style="color: var(--accent-green);">${rhr} <span style="font-size: 13px;">bpm</span></div>
                    <div class="stat-sub">Watch sensor</div>
                </div>
            `;

            telemContainer.innerHTML = `
                <div class="telemetry-grid">
                    <div class="telemetry-card">
                        <div class="stat-label">Active Intensity Time</div>
                        <div class="telemetry-val" style="color: var(--accent-orange);">${activeMin} min</div>
                        <div class="stat-sub">Brisk walking & active workouts</div>
                    </div>
                    <div class="telemetry-card">
                        <div class="stat-label">Average Stride / Cadence</div>
                        <div class="telemetry-val" style="color: var(--accent-blue);">${steps ? (distKm * 1000 / steps).toFixed(2) : '--'} m</div>
                        <div class="stat-sub">Distance ÷ steps</div>
                    </div>
                    <div class="telemetry-card">
                        <div class="stat-label">Sync Pipeline</div>
                        <div class="telemetry-val" style="color: var(--accent-green); font-size: 18px;">Direct Local Link</div>
                        <div class="stat-sub">Watch → Gadgetbridge → UniScale Hub</div>
                    </div>
                </div>
            `;

            thead.innerHTML = `<tr><th>Time / Date</th><th>Sport / Activity</th><th>Distance & Duration</th><th>Device Source</th></tr>`;
            tbody.innerHTML = '';

            if (workouts.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td style="color: var(--text-muted);">${act.date || 'Today'}</td>
                        <td><span class="tag tag-garmin">All-Day Pedometer</span></td>
                        <td style="font-weight: 600;">${steps.toLocaleString()} steps (${distKm} km)</td>
                        <td style="color: var(--text-muted);">${act.source || 'Garmin'}</td>
                    </tr>
                `;
            } else {
                workouts.forEach(w => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td style="color: var(--text-muted);">${(w.timestamp || '').substring(0, 19).replace('T', ' ')}</td>
                        <td><span class="tag tag-garmin">${(w.sport || 'Workout').toUpperCase()}</span></td>
                        <td style="font-weight: 600;">${(w.distance_km || (w.distance_meters/1000)).toFixed(2)} km in ${(w.duration_seconds/60).toFixed(1)} min (HR: ${w.avg_heart_rate || '--'} bpm)</td>
                        <td style="color: var(--text-muted);">${w.device_name || 'Garmin'}</td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        }

        /* 4. Clinical Labs Modal */
        async function openLabModal() {
            currentModalType = 'lab';
            document.getElementById('modalTitle').innerText = 'Clinical Labs & Blood Chemistry';
            document.getElementById('modalSubtitle').innerText = 'Diagnostic blood panels, reference intervals & multi-year progression';
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'none';
            document.getElementById('modalDeviceTelemetry').style.display = 'block';
            document.getElementById('modalTableTitle').innerText = 'Report History';
            openModal();
            await renderLabModal();
        }

        async function renderLabModal() {
            try {
                const res = await fetch('/api/history?record_type=blood_test&limit=10');
                const data = esc(await res.json());
                const records = data.records || [];
                if (!records.length) {
                    document.getElementById('modalTableTitle').innerText = '';
                    document.getElementById('modalStatsGrid').innerHTML = `
                        <div class="stat-box" style="grid-column: 1 / -1;">
                            <div class="stat-label">Notice</div>
                            <div class="stat-value" style="font-size: 15px; color: var(--text-muted);">No clinical lab reports recorded yet.</div>
                        </div>
                    `;
                    return;
                }

                // Index reports by year
                const yearReports = {};
                records.forEach(r => {
                    const yr = (r.timestamp || '').substring(0, 4);
                    if (yr && !yearReports[yr]) yearReports[yr] = r;
                });
                const sortedYears = Object.keys(yearReports).sort().reverse();
                const latestYear = sortedYears[0];
                const latest = yearReports[latestYear] || records[0];
                const mLatest = latest.markers || {};

                // 1. Stats grid
                const mCount = Object.keys(mLatest).length;
                let flaggedCount = 0;
                Object.values(mLatest).forEach(v => {
                    if (v.flag && v.flag !== 'Normal') flaggedCount++;
                });

                const cholHdl = mLatest.chol_hdl_ratio ? mLatest.chol_hdl_ratio.value : (mLatest.total_cholesterol && mLatest.hdl_cholesterol ? (mLatest.total_cholesterol.value / mLatest.hdl_cholesterol.value).toFixed(1) : '--');
                const a1cVal = mLatest.hba1c ? `${mLatest.hba1c.value}%` : '--';

                const statsGrid = document.getElementById('modalStatsGrid');
                statsGrid.innerHTML = `
                    <div class="stat-box">
                        <div class="stat-label">Total Biomarkers (${latestYear})</div>
                        <div class="stat-value" style="color: #10b981;">${mCount}</div>
                        <div class="stat-sub">${latest.lab_name || 'Lab'}</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Out of Range Flags</div>
                        <div class="stat-value" style="color: ${flaggedCount > 0 ? 'var(--accent-red)' : 'var(--accent-green)'};">${flaggedCount}</div>
                        <div class="stat-sub">${flaggedCount === 0 ? 'All markers optimal' : 'Mild clinical elevations'}</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Chol / HDL Ratio</div>
                        <div class="stat-value" style="color: var(--accent-green);">${cholHdl}</div>
                        <div class="stat-sub">Ideal target: &lt; 5.0</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Hemoglobin A1c</div>
                        <div class="stat-value" style="color: var(--accent-green);">${a1cVal}</div>
                        <div class="stat-sub">Normal: &lt; 5.7% (Optimal)</div>
                    </div>
                `;

                // 2. Render categorized multi-year biomarkers in modalDeviceTelemetry
                const cats = [
                    { key: 'lipid', label: 'Lipid & Cardiovascular Panel' },
                    { key: 'metabolic', label: 'Comprehensive Metabolic & Renal Panel' },
                    { key: 'thyroid', label: 'Thyroid & Endocrine Function' },
                    { key: 'cbc', label: 'Complete Blood Count (CBC) & Hematology' }
                ];

                let html = '<div style="display: flex; flex-direction: column; gap: 20px;">';
                cats.forEach(c => {
                    const markersInCat = Object.entries(mLatest).filter(([k, v]) => v.category === c.key);
                    if (!markersInCat.length) return;

                    html += `
                        <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 12px; padding: 14px 18px; overflow-x: auto;">
                            <h4 style="font-size: 14px; font-weight: 700; color: var(--accent-blue); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px;">${c.label}</h4>
                            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                                <thead>
                                    <tr style="color: var(--text-muted); border-bottom: 1px solid var(--border); text-align: left;">
                                        <th style="padding: 6px 8px;">Marker</th>
                                        ${sortedYears.map(yr => `<th style="padding: 6px 6px; text-align: right;">${yr}</th>`).join('')}
                                        <th style="padding: 6px 8px; text-align: center;">Status</th>
                                        <th style="padding: 6px 8px; text-align: right;">Reference Interval</th>
                                    </tr>
                                </thead>
                                <tbody>
                    `;

                    markersInCat.forEach(([k, v]) => {
                        let badgeStyle = 'background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3);';
                        let badgeText = 'Normal';
                        if (v.flag === 'H') {
                            badgeStyle = 'background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3);';
                            badgeText = 'High';
                        } else if (v.flag === 'L') {
                            badgeStyle = 'background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3);';
                            badgeText = 'Low';
                        }

                        const yearCells = sortedYears.map(yr => {
                            const mYr = yearReports[yr]?.markers?.[k];
                            if (!mYr) return `<td style="padding: 6px 6px; text-align: right; color: var(--text-muted);">--</td>`;
                            const isLatest = yr === sortedYears[0];
                            const valColor = mYr.flag === 'H' ? 'var(--accent-red)' : (mYr.flag === 'L' ? 'var(--accent-orange)' : (isLatest ? '#fff' : 'var(--text-muted)'));
                            const weight = isLatest ? '700' : '500';
                            return `<td style="padding: 6px 6px; text-align: right; font-weight: ${weight}; color: ${valColor};">${mYr.value} <span style="font-size: 10px; color: var(--text-muted);">${v.unit}</span></td>`;
                        }).join('');

                        html += `
                            <tr style="border-bottom: 1px solid rgba(255,255,255,0.03);">
                                <td style="padding: 8px 8px; font-weight: 600;">${v.name}</td>
                                ${yearCells}
                                <td style="padding: 8px 8px; text-align: center;"><span style="font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; ${badgeStyle}">${badgeText}</span></td>
                                <td style="padding: 8px 8px; text-align: right; color: var(--text-muted); font-size: 12px;">${v.reference_range || '--'}</td>
                            </tr>
                        `;
                    });

                    html += `
                                </tbody>
                            </table>
                        </div>
                    `;
                });
                html += '</div>';

                document.getElementById('modalDeviceTelemetry').innerHTML = html;

                // 3. Mini table of all reports
                document.getElementById('modalTableHead').innerHTML = `
                    <tr>
                        <th>Date</th>
                        <th>Lab Provider</th>
                        <th>Panel Name</th>
                        <th>Markers Count</th>
                        <th>Fasting</th>
                    </tr>
                `;
                const tBody = document.getElementById('modalTableBody');
                tBody.innerHTML = '';
                records.forEach(r => {
                    const row = document.createElement('tr');
                    const cnt = Object.keys(r.markers || {}).length;
                    row.innerHTML = `
                        <td style="font-weight: 600;">${(r.timestamp || '').substring(0, 10)}</td>
                        <td>${r.lab_name || 'Lab'}</td>
                        <td style="color: var(--text-muted);">${r.panel_name || 'Comprehensive Panel'}</td>
                        <td><span class="tag tag-lab">${cnt} markers</span></td>
                        <td>${r.fasting ? 'Yes' : 'No'}</td>
                    `;
                    tBody.appendChild(row);
                });

            } catch (err) {
                console.error("renderLabModal error:", err);
            }
        }

        /* 5. Biological Age & Longevity Modal */
        async function openLongevityModal() {
            currentModalType = 'longevity';
            document.getElementById('modalTitle').innerText = 'Biological Age & Longevity Matrix';
            document.getElementById('modalSubtitle').innerText = 'Morgan Levine PhenoAge (9 blood biomarkers) & ACC/AHA 10-Year ASCVD Risk Profile';
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'none';
            document.getElementById('modalDeviceTelemetry').style.display = 'block';
            document.getElementById('modalTableTitle').innerText = 'Annual Longevity Progression';
            openModal();
            await renderLongevityModal();
        }

        async function renderLongevityModal() {
            try {
                const res = await fetch('/api/longevity');
                const data = esc(await res.json());
                if (!data || !data.current) {
                    document.getElementById('modalTableTitle').innerText = '';
                    if (data && data.error) {
                        document.getElementById('modalStatsGrid').innerHTML = `
                            <div class="stat-box" style="grid-column: 1 / -1;">
                                <div class="stat-label">Configuration Required</div>
                                <div class="stat-value" style="font-size: 15px; color: var(--accent-orange);">${data.error}</div>
                            </div>
                        `;
                    }
                    return;
                }

                const cur = data.current;
                const traj = data.multi_year_trajectory || [];

                // 1. Stats grid
                const statsGrid = document.getElementById('modalStatsGrid');
                statsGrid.innerHTML = `
                    <div class="stat-box">
                        <div class="stat-label">Phenotypic Biological Age</div>
                        <div class="stat-value" style="color: #06b6d4;">${cur.phenotypic_age} <span style="font-size: 13px;">Years</span></div>
                        <div class="stat-sub">Calendar Age: ${cur.chronological_age}</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Biological Age Delta</div>
                        <div class="stat-value" style="color: ${cur.biological_age_delta <= 0 ? 'var(--accent-green)' : 'var(--accent-orange)'};">${cur.biological_age_delta} <span style="font-size: 13px;">Years</span></div>
                        <div class="stat-sub">${Math.abs(cur.biological_age_delta)} years ${cur.biological_age_delta <= 0 ? 'younger' : 'older'} cellular age</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">10-Year ASCVD Risk</div>
                        <div class="stat-value" style="color: var(--accent-green);">${pct(cur.ten_year_ascvd_risk)}</div>
                        <div class="stat-sub">${cur.ascvd_category}</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Longevity Health Status</div>
                        <div class="stat-value" style="color: var(--accent-purple); font-size: 18px;">${cur.status}</div>
                        <div class="stat-sub">From imported lab reports</div>
                    </div>
                `;

                // 2. Longevity narrative & biomarker breakdown
                let html = `
                    <div style="display: flex; flex-direction: column; gap: 16px;">
                        <div style="background: rgba(6, 182, 212, 0.05); border: 1px solid rgba(6, 182, 212, 0.2); border-radius: 12px; padding: 16px;">
                            <h4 style="font-size: 15px; font-weight: 700; color: #06b6d4; margin-bottom: 8px;">The Science of Your Biological Age</h4>
                            <p style="font-size: 13px; line-height: 1.5; color: var(--text-main); margin-bottom: 12px;">
                                Developed by Dr. Morgan Levine (Yale/Columbia), <strong>PhenoAge</strong> is a clinically validated multi-system biomarker of biological aging. It utilizes 9 standard blood markers to quantify physiological wear, systemic inflammation, and metabolic reserves.
                            </p>
                        </div>
                    </div>
                `;
                document.getElementById('modalDeviceTelemetry').innerHTML = html;

                // 3. Trajectory table
                document.getElementById('modalTableHead').innerHTML = `
                    <tr>
                        <th>Year</th>
                        <th>Calendar Age</th>
                        <th>Phenotypic Age</th>
                        <th>Biological Delta</th>
                        <th>10-Yr ASCVD Risk</th>
                        <th>Total Chol / HDL</th>
                        <th>Fasting Glucose / A1c</th>
                    </tr>
                `;
                const tBody = document.getElementById('modalTableBody');
                tBody.innerHTML = '';
                [...traj].reverse().forEach(row => {
                    const tr = document.createElement('tr');
                    const km = row.key_markers || {};
                    const a1cStr = km.hba1c ? ' (' + km.hba1c + '%)' : '';
                    tr.innerHTML = `
                        <td style="font-weight: 700;">${row.year} <span style="font-size: 11px; color: var(--text-muted); font-weight: 400;">(${row.date})</span></td>
                        <td>${row.chronological_age} yrs</td>
                        <td style="color: #06b6d4; font-weight: 700;">${row.phenotypic_age} yrs</td>
                        <td><span style="font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3);">${row.delta_years} yrs (${row.acceleration_percent}%)</span></td>
                        <td style="font-weight: 600;">${pct(row.ascvd_risk_percent)}</td>
                        <td style="color: var(--text-muted);">${km.total_cholesterol} / ${km.hdl} (Ratio: ${km.chol_hdl_ratio})</td>
                        <td style="color: var(--text-muted);">${km.glucose} mg/dL${a1cStr}</td>
                    `;
                    tBody.appendChild(tr);
                });

            } catch (err) {
                console.error("renderLongevityModal error:", err);
            }
        }

        /* 6. Sleep Architecture & Recovery Modal */
        // ---- Workouts (tile + modal) ----
        const SPORTS = {
            running: ['🏃', 'Run'], walking: ['🚶', 'Walk'], hiking: ['🥾', 'Hike'],
            cycling: ['🚴', 'Ride'], swimming: ['🏊', 'Swim'], generic: ['💪', 'Workout'],
        };
        function sportName(s) {
            const k = SPORTS[s];
            if (k) return `${k[0]} ${k[1]}`;
            return '💪 ' + (s ? s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ') : 'Workout');
        }
        function workoutKm(w) { return w.distance_km || (w.distance_meters || 0) / 1000; }
        function pad2(n) { return String(n).padStart(2, '0'); }
        function fmtDuration(sec) {
            const t = Math.round(sec || 0), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
            return h ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
        }
        function fmtPace(w) {
            const km = workoutKm(w);
            if (!km || !w.duration_seconds) return 'Pace --';
            if (w.sport === 'cycling') return (km / (w.duration_seconds / 3600)).toFixed(1) + ' km/h';
            const p = Math.round(w.duration_seconds / km);
            return `${Math.floor(p / 60)}:${pad2(p % 60)} /km`;
        }
        function fmtWhen(ts) {
            const d = new Date(ts);
            if (isNaN(d)) return ts || '';
            const year = d.getFullYear() !== new Date().getFullYear() ? { year: 'numeric' } : {};
            return d.toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', ...year, hour: 'numeric', minute: '2-digit' });
        }

        // Workouts modal, three levels: by type -> one type's full history (scrolls, pages in) -> one workout.
        // Rows point into workoutList by index (data-i), never by inline strings.
        const WORKOUT_PAGE = 50;
        let workoutList = [];       // workouts the current table shows
        let workoutSport = null;    // the type being browsed, or null on the "by type" view
        let workoutTotal = 0;
        let workoutTotals = null;
        let workoutLoading = false;
        const WORKOUT_HEAD = `<tr><th>Type</th><th>When</th><th>Distance</th><th>Time</th><th>Pace / Speed</th>
            <th>Avg / Max HR</th><th>Calories</th><th>Training Effect</th><th>Device</th></tr>`;

        function workoutRow(w, i) {
            const km = workoutKm(w);
            return `
                <tr class="clickable" data-i="${i}" onclick="openWorkoutDetail(+this.dataset.i)">
                    <td style="font-weight: 700;">${sportName(w.sport)}</td>
                    <td>${fmtWhen(w.timestamp)}</td>
                    <td style="color: var(--accent-blue); font-weight: 600;">${km ? km.toFixed(2) + ' km' : '--'}</td>
                    <td>${fmtDuration(w.duration_seconds)}</td>
                    <td>${fmtPace(w)}</td>
                    <td style="color: var(--accent-red); font-weight: 600;">${w.avg_heart_rate ?? '--'} / ${w.max_heart_rate ?? '--'}</td>
                    <td>${w.calories ?? '--'}</td>
                    <td>${w.aerobic_training_effect ?? '--'}</td>
                    <td style="color: var(--text-muted);">${w.device_name || '--'}</td>
                </tr>`;
        }

        function workoutModalFrame(title, subtitleHtml, tableTitle) {
            document.querySelector('#detailModal .modal-container')?.classList.add('modal-wide');
            document.getElementById('modalTitle').innerHTML = title;
            document.getElementById('modalSubtitle').innerHTML = subtitleHtml;
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'none';
            document.getElementById('modalDeviceTelemetry').style.display = 'none';
            document.getElementById('modalTableTitle').innerText = tableTitle;
            openModal();
        }

        async function uploadWorkoutFile(file) {
            if (!file) return;
            const form = new FormData();
            form.append('file', file);
            try {
                const res = await fetch('/api/sync/workout/fit', { method: 'POST', body: form });
                if (!res.ok) {
                    const err = await res.json();
                    alert('Upload error: ' + (err.detail || 'Could not import workout'));
                    return;
                }
                await openWorkoutModal();
            } catch (e) {
                alert('Upload failed: ' + e.message);
            }
        }

        async function openWorkoutModal() {
            currentModalType = 'workouts';
            workoutSport = null;
            workoutModalFrame('Workouts',
                'Workouts tracked by your devices: tap a type for full history, a row for details ' +
                `<label style="cursor: pointer; display: inline-flex; align-items: center; gap: 4px; font-size: 11px; padding: 2px 8px; margin-left: 10px; background: rgba(59, 130, 246, 0.15); color: var(--accent-blue); border-radius: 4px; border: 1px solid rgba(59, 130, 246, 0.3); font-weight: 500;">
                    <input type="file" accept=".fit,.gpx" style="display: none;" onchange="uploadWorkoutFile(this.files[0])">
                    + Import .FIT / .GPX
                </label>`,
                'Recent Workouts by Type');
            try {
                const data = esc(await (await fetch('/api/workouts?per_sport=5')).json());
                const sports = data.sports || [];
                document.getElementById('modalStatsGrid').innerHTML = sports.map(s => `
                    <div class="stat-box clickable" data-sport="${s.sport}" onclick="openWorkoutSport(this.dataset.sport)">
                        <div class="stat-label">${sportName(s.sport)}</div>
                        <div class="stat-value" style="color: #f97316;">${s.count}</div>
                        <div class="stat-sub">Last: ${fmtWhen(s.recent[0].timestamp)} ›</div>
                    </div>`).join('');
                workoutList = sports.flatMap(s => s.recent);
                document.getElementById('modalTableHead').innerHTML = WORKOUT_HEAD;
                document.getElementById('modalTableBody').innerHTML = workoutList.map(workoutRow).join('')
                    || '<tr><td colspan="9" style="text-align: center; color: var(--text-muted);">No workouts recorded yet</td></tr>';
            } catch (err) {
                document.getElementById('modalTableBody').innerHTML =
                    `<tr><td colspan="9" style="color: var(--accent-red);">Could not load workouts: ${esc(err.message)}</td></tr>`;
            }
        }

        // `sport` comes from a data attribute (already unescaped by the browser): only ever used via esc() or as a URL param.
        async function openWorkoutSport(sport) {
            workoutSport = sport;
            workoutList = [];
            workoutTotal = 0;
            workoutTotals = null;
            renderWorkoutSport(0);
            await loadMoreWorkouts();
        }

        function renderWorkoutSport(scrollTop) {
            currentModalType = 'workout-sport';
            workoutModalFrame(sportName(esc(workoutSport)),
                '<a class="modal-back" onclick="openWorkoutModal()">‹ All workouts</a>Scroll down for older ones; tap a row for details',
                'History');
            const t = workoutTotals;
            if (t) {
                const avgKm = t.total ? t.distance_km / t.total : 0;
                document.getElementById('modalStatsGrid').innerHTML = `
                    <div class="stat-box"><div class="stat-label">Workouts</div>
                        <div class="stat-value" style="color: #f97316;">${t.total}</div>
                        <div class="stat-sub">Since ${t.first ? fmtWhen(t.first) : '--'}</div></div>
                    <div class="stat-box"><div class="stat-label">Total Distance</div>
                        <div class="stat-value" style="color: var(--accent-blue);">${t.distance_km.toLocaleString()} <span style="font-size: 13px;">km</span></div>
                        <div class="stat-sub">Avg ${avgKm.toFixed(2)} km</div></div>
                    <div class="stat-box"><div class="stat-label">Total Time</div>
                        <div class="stat-value">${Math.round(t.duration_seconds / 3600).toLocaleString()} <span style="font-size: 13px;">hrs</span></div>
                        <div class="stat-sub">Avg ${fmtDuration(t.total ? t.duration_seconds / t.total : 0)}</div></div>
                    <div class="stat-box"><div class="stat-label">Average Pace</div>
                        <div class="stat-value" style="font-size: 20px;">${fmtPace({ sport: t.sport, distance_km: t.distance_km, duration_seconds: t.duration_seconds })}</div>
                        <div class="stat-sub">Over all of them</div></div>`;
            }
            document.getElementById('modalTableHead').innerHTML = WORKOUT_HEAD;
            document.getElementById('modalTableBody').innerHTML = workoutList.map(workoutRow).join('');
            const scroll = document.getElementById('modalTableScroll');
            scroll.style.maxHeight = '55vh';
            scroll.scrollTop = scrollTop;
            scroll.onscroll = () => {
                if (scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 120) loadMoreWorkouts();
            };
        }

        async function loadMoreWorkouts() {
            if (workoutLoading || currentModalType !== 'workout-sport') return;
            if (workoutTotals && workoutList.length >= workoutTotal) return;
            workoutLoading = true;
            const sport = workoutSport;
            try {
                const url = `/api/workouts?sport=${encodeURIComponent(sport)}&offset=${workoutList.length}&limit=${WORKOUT_PAGE}`;
                const data = esc(await (await fetch(url)).json());
                if (sport !== workoutSport || currentModalType !== 'workout-sport') return;  // user moved on
                const start = workoutList.length;
                workoutList.push(...data.workouts);
                workoutTotal = data.total;
                if (!workoutTotals) {
                    workoutTotals = data;
                    renderWorkoutSport(0);
                } else {
                    document.getElementById('modalTableBody')
                        .insertAdjacentHTML('beforeend', data.workouts.map((w, i) => workoutRow(w, start + i)).join(''));
                }
            } catch (err) {
                document.getElementById('modalTableBody').insertAdjacentHTML('beforeend',
                    `<tr><td colspan="9" style="color: var(--accent-red);">Could not load workouts: ${esc(err.message)}</td></tr>`);
            } finally {
                workoutLoading = false;
            }
        }

        // Every non-empty field the record has is listed, so data the devices add later shows up without UI changes.
        const WORKOUT_FIELD_LABELS = {
            sub_sport: 'Sub-type', avg_cadence: 'Avg cadence (spm)', elevation_gain_m: 'Ascent (m)', steps: 'Steps',
            avg_power: 'Avg power (W)', max_power: 'Max power (W)', aerobic_training_effect: 'Aerobic training effect',
            vo2max: 'VO2max', device_name: 'Device', source_file: 'Source file',
            anaerobic_training_effect: 'Anaerobic training effect', training_load: 'Training load',
            recovery_time_hours: 'Recovery time (h)', body_battery_start: 'Body Battery at start',
            body_battery_end: 'Body Battery at end', sweat_loss_ml: 'Sweat loss (ml)',
            avg_temperature_c: 'Avg temperature (°C)', ground_contact_ms: 'Ground contact (ms)',
            ground_contact_balance: 'GCT balance (%)', vertical_oscillation_mm: 'Vertical oscillation (mm)',
            vertical_ratio_percent: 'Vertical ratio (%)', stride_length_m: 'Stride length (m)',
            max_cadence: 'Max cadence (spm)', elevation_loss_m: 'Descent (m)',
            normalized_power: 'Normalized power (W)', active_calories: 'Active calories',
            start_lat: 'Start latitude', start_lon: 'Start longitude',
            end_lat: 'End latitude', end_lon: 'End longitude',
        };
        const ZONE_COLORS = ['#94a3b8', '#3b82f6', '#10b981', '#f59e0b', '#ef4444'];

        function workoutDynamicsHtml(w) {
            const hasDynamics = w.stride_length_m || w.ground_contact_ms || w.ground_contact_balance
                || w.vertical_oscillation_mm || w.vertical_ratio_percent || (w.sport === 'running' && (w.avg_cadence || w.avg_power));
            if (!hasDynamics) return '';

            const cards = [];
            if (w.stride_length_m) {
                cards.push(`<div class="stat-box" style="padding: 10px 14px;"><div class="stat-label">Stride Length</div><div class="stat-value" style="font-size: 19px; color: var(--accent-blue);">${w.stride_length_m.toFixed(2)} <span style="font-size: 12px; font-weight: normal;">m</span></div></div>`);
            }
            if (w.avg_cadence || w.max_cadence) {
                const cadVal = `${w.avg_cadence ?? '--'}${w.max_cadence ? ` <span style="font-size: 13px; font-weight: normal; color: var(--text-muted);">/ ${w.max_cadence} max</span>` : ''}`;
                cards.push(`<div class="stat-box" style="padding: 10px 14px;"><div class="stat-label">Cadence</div><div class="stat-value" style="font-size: 19px;">${cadVal} <span style="font-size: 12px; font-weight: normal;">spm</span></div></div>`);
            }
            if (w.ground_contact_ms) {
                cards.push(`<div class="stat-box" style="padding: 10px 14px;"><div class="stat-label">Ground Contact Time</div><div class="stat-value" style="font-size: 19px; color: #10b981;">${Math.round(w.ground_contact_ms)} <span style="font-size: 12px; font-weight: normal;">ms</span></div></div>`);
            }
            if (w.ground_contact_balance) {
                const balLeft = Number(w.ground_contact_balance).toFixed(1);
                const balRight = (100 - balLeft).toFixed(1);
                cards.push(`<div class="stat-box" style="padding: 10px 14px;"><div class="stat-label">GCT Balance</div><div class="stat-value" style="font-size: 19px;">${balLeft}% L <span style="font-size: 13px; font-weight: normal; color: var(--text-muted);">/ ${balRight}% R</span></div></div>`);
            }
            if (w.vertical_oscillation_mm) {
                const cm = (w.vertical_oscillation_mm / 10).toFixed(1);
                cards.push(`<div class="stat-box" style="padding: 10px 14px;"><div class="stat-label">Vertical Oscillation</div><div class="stat-value" style="font-size: 19px; color: #f59e0b;">${cm} <span style="font-size: 12px; font-weight: normal;">cm (${Math.round(w.vertical_oscillation_mm)} mm)</span></div></div>`);
            }
            if (w.vertical_ratio_percent) {
                cards.push(`<div class="stat-box" style="padding: 10px 14px;"><div class="stat-label">Vertical Ratio</div><div class="stat-value" style="font-size: 19px;">${Number(w.vertical_ratio_percent).toFixed(1)} <span style="font-size: 12px; font-weight: normal;">%</span></div></div>`);
            }
            if (w.avg_power || w.max_power) {
                const pwrVal = `${w.avg_power ?? '--'}${w.max_power ? ` <span style="font-size: 13px; font-weight: normal; color: var(--text-muted);">/ ${w.max_power} max</span>` : ''}`;
                cards.push(`<div class="stat-box" style="padding: 10px 14px;"><div class="stat-label">Running Power</div><div class="stat-value" style="font-size: 19px; color: #8b5cf6;">${pwrVal} <span style="font-size: 12px; font-weight: normal;">W</span></div></div>`);
            }

            return `<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 6px;">${cards.join('')}</div>`;
        }

        function workoutRouteHtml(w) {
            const track = w.gps_track;
            const hasCoords = (track && track.length > 1) || (w.start_lat && w.start_lon);
            if (!hasCoords) return '';

            const startLat = w.start_lat || (track && track[0] ? track[0][0] : null);
            const startLon = w.start_lon || (track && track[0] ? track[0][1] : null);
            const mapUrl = startLat && startLon
                ? `https://www.openstreetmap.org/?mlat=${startLat}&mlon=${startLon}#map=15/${startLat}/${startLon}`
                : 'https://www.openstreetmap.org/';

            let svgRoute = '';
            if (track && track.length > 1) {
                const lats = track.map(p => p[0]);
                const lons = track.map(p => p[1]);
                const minLat = Math.min(...lats), maxLat = Math.max(...lats);
                const minLon = Math.min(...lons), maxLon = Math.max(...lons);
                const avgLat = (minLat + maxLat) / 2;
                const cosLat = Math.cos(avgLat * Math.PI / 180.0) || 1.0;

                const W = 600, H = 200, pad = 24;
                const dLat = (maxLat - minLat) || 0.0001;
                const dLon = ((maxLon - minLon) * cosLat) || 0.0001;

                const scale = Math.min((W - 2 * pad) / dLon, (H - 2 * pad) / dLat);
                const cx = (minLon + maxLon) / 2;
                const cy = (minLat + maxLat) / 2;

                const x = lon => (W / 2) + (lon - cx) * cosLat * scale;
                const y = lat => (H / 2) - (lat - cy) * scale;

                const pts = track.map(p => `${x(p[1]).toFixed(1)},${y(p[0]).toFixed(1)}`).join(' ');
                const startPt = track[0], endPt = track[track.length - 1];

                svgRoute = `<div style="background: rgba(0,0,0,0.25); border: 1px solid var(--border-color); border-radius: 8px; padding: 10px; margin-top: 6px;">
                    <svg viewBox="0 0 ${W} ${H}" style="width: 100%; height: auto; max-height: 220px;" role="img" aria-label="Workout GPS track">
                        <polyline points="${pts}" fill="none" stroke="var(--accent-blue)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
                        <circle cx="${x(startPt[1]).toFixed(1)}" cy="${y(startPt[0]).toFixed(1)}" r="6" fill="#10b981" stroke="#fff" stroke-width="2"/>
                        <circle cx="${x(endPt[1]).toFixed(1)}" cy="${y(endPt[0]).toFixed(1)}" r="6" fill="#ef4444" stroke="#fff" stroke-width="2"/>
                    </svg>
                    <div style="display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: var(--text-muted); margin-top: 6px;">
                        <div>
                            <span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #10b981; margin-right: 4px;"></span> Start: ${startPt[0].toFixed(5)}, ${startPt[1].toFixed(5)}
                            <span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #ef4444; margin: 0 4px 0 12px;"></span> Finish: ${endPt[0].toFixed(5)}, ${endPt[1].toFixed(5)}
                        </div>
                        <a href="${mapUrl}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-blue); text-decoration: none; font-weight: 500;">OpenStreetMap ↗</a>
                    </div>
                </div>`;
            } else {
                svgRoute = `<div style="padding: 10px; background: rgba(0,0,0,0.2); border-radius: 8px; margin-top: 6px; font-size: 13px;">
                    Coordinates: <b>${startLat.toFixed(5)}, ${startLon.toFixed(5)}</b>
                    <a href="${mapUrl}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-blue); margin-left: 12px; text-decoration: none;">View Map in OpenStreetMap ↗</a>
                </div>`;
            }

            return svgRoute;
        }

        // Heart rate over the workout: hr_samples = [[seconds from start, bpm], ...]
        function workoutHrSvg(samples) {
            if (!samples || samples.length < 2) return '';
            const W = 600, H = 160, L = 34, B = 20, T = 8;
            const t1 = samples[samples.length - 1][0] || 1;
            const bpms = samples.map(s => s[1]);
            const lo = Math.min(...bpms) - 5, hi = Math.max(...bpms) + 5;
            const x = t => L + (t / t1) * (W - L - 8), y = b => T + (1 - (b - lo) / (hi - lo)) * (H - T - B);
            const pts = samples.map(([t, b]) => `${x(t).toFixed(1)},${y(b).toFixed(1)}`).join(' ');
            const label = (tx, ty, s, anchor) => `<text x="${tx}" y="${ty}" font-size="11" fill="var(--text-muted)" text-anchor="${anchor}">${s}</text>`;
            return `<svg viewBox="0 0 ${W} ${H}" style="width: 100%; height: auto;" role="img" aria-label="Heart rate during the workout">
                <polyline points="${pts}" fill="none" stroke="var(--accent-red)" stroke-width="2" stroke-linejoin="round"/>
                ${label(L - 6, y(hi - 5) + 4, Math.round(hi - 5), 'end')}${label(L - 6, y(lo + 5) + 4, Math.round(lo + 5), 'end')}
                ${label(L, H - 4, '0:00', 'start')}${label(W - 8, H - 4, fmtDuration(t1), 'end')}
            </svg>`;
        }

        function workoutZonesHtml(zones) {
            const total = (zones || []).reduce((a, z) => a + (z.seconds || 0), 0);
            if (!total) return '';
            const bar = zones.map(z => `<div title="${z.name}: ${fmtDuration(z.seconds)}"
                style="width: ${(100 * z.seconds / total).toFixed(2)}%; background: ${ZONE_COLORS[z.zone - 1] || '#64748b'};"></div>`).join('');
            const legend = zones.map(z => `<span style="margin-right: 14px; white-space: nowrap;">
                <span style="display: inline-block; width: 10px; height: 10px; border-radius: 2px; background: ${ZONE_COLORS[z.zone - 1] || '#64748b'};"></span>
                Z${z.zone} ${z.name}${z.min_bpm ? ` (${z.min_bpm}+)` : ''}: <b>${fmtDuration(z.seconds)}</b></span>`).join('');
            return `<div style="display: flex; height: 14px; border-radius: 7px; overflow: hidden; margin: 6px 0 8px;">${bar}</div>
                <div style="font-size: 12px; color: var(--text-muted); line-height: 1.9;">${legend}</div>`;
        }

        function workoutSplitsHtml(splits) {
            if (!splits || !splits.length) return '';
            const rows = splits.map((s, i) => {
                const pace = s.distance_m ? Math.round(s.seconds / (s.distance_m / 1000)) : 0;
                return `<tr><td>${i + 1}</td><td>${(s.distance_m / 1000).toFixed(2)} km</td><td>${fmtDuration(s.seconds)}</td>
                    <td style="font-weight: 600;">${pace ? `${Math.floor(pace / 60)}:${pad2(pace % 60)} /km` : '--'}</td>
                    <td style="color: var(--accent-red);">${s.avg_hr ?? '--'}</td></tr>`;
            }).join('');
            return `<table class="history-table" style="margin-top: 0;"><thead><tr><th>#</th><th>Distance</th><th>Time</th>
                <th>Pace</th><th>Avg HR</th></tr></thead><tbody>${rows}</tbody></table>`;
        }

        function workoutSection(title, html) {
            return html ? `<h3 style="font-size: 13px; font-weight: 600; text-transform: uppercase; color: var(--text-muted);
                margin: 22px 0 8px;">${title}</h3>${html}` : '';
        }
        const WORKOUT_FIELDS_SHOWN = ['type', 'sport', 'timestamp', 'distance_km', 'distance_meters', 'duration_seconds',
            'avg_heart_rate', 'max_heart_rate', 'calories'];

        function openWorkoutDetail(i) {
            const w = workoutList[i];
            if (!w) return;
            const fromSport = currentModalType === 'workout-sport';
            const scrollTop = document.getElementById('modalTableScroll').scrollTop;
            currentModalType = 'workout-detail';
            window.workoutBack = fromSport ? () => renderWorkoutSport(scrollTop) : openWorkoutModal;
            const backLabel = fromSport ? `‹ ${sportName(esc(workoutSport))}` : '‹ All workouts';
            workoutModalFrame(`${sportName(w.sport)} <span style="font-weight: 400; font-size: 15px;">${fmtWhen(w.timestamp)}</span>`,
                `<a class="modal-back" onclick="workoutBack()">${backLabel}</a>${w.device_name || ''}`, 'All Recorded Fields');
            const km = workoutKm(w);
            const box = (label, value, color) => `<div class="stat-box"><div class="stat-label">${label}</div>
                <div class="stat-value" style="color: ${color || 'var(--text-main)'};">${value}</div></div>`;
            document.getElementById('modalStatsGrid').innerHTML = [
                box('Distance', km ? km.toFixed(2) + ' <span style="font-size: 13px;">km</span>' : '--', 'var(--accent-blue)'),
                box('Time', fmtDuration(w.duration_seconds)),
                box('Pace / Speed', fmtPace(w)),
                box('Avg / Max HR', `${w.avg_heart_rate ?? '--'} / ${w.max_heart_rate ?? '--'}`, 'var(--accent-red)'),
                box('Calories', w.calories ?? '--', '#f97316'),
            ].join('');
            const telemetry = document.getElementById('modalDeviceTelemetry');
            telemetry.innerHTML = workoutSection('Running Dynamics', workoutDynamicsHtml(w))
                + workoutSection('Heart Rate', workoutHrSvg(w.hr_samples))
                + workoutSection('Time in Heart-Rate Zones', workoutZonesHtml(w.hr_zones))
                + workoutSection('Splits', workoutSplitsHtml(w.splits))
                + workoutSection('GPS Route & Location', workoutRouteHtml(w));
            telemetry.style.display = telemetry.innerHTML ? 'block' : 'none';
            const rows = Object.entries(w)
                .filter(([k, v]) => !WORKOUT_FIELDS_SHOWN.includes(k) && v !== null && v !== '' && v !== 0 && typeof v !== 'object')
                .map(([k, v]) => `<tr><td style="color: var(--text-muted);">${WORKOUT_FIELD_LABELS[k]
                    || k.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase())}</td><td style="font-weight: 600;">${v}</td></tr>`);
            document.getElementById('modalTableHead').innerHTML = '';
            document.getElementById('modalTableBody').innerHTML = rows.join('')
                || '<tr><td style="color: var(--text-muted);">No other data recorded for this workout</td></tr>';
        }

        async function openSleepModal() {
            currentModalType = 'sleep';
            document.getElementById('modalTitle').innerText = 'Sleep Architecture & Autonomic Recovery';
            document.getElementById('modalSubtitle').innerText = 'Garmin PPG sleep stages (Deep, REM, Light), nocturnal SpO2 & HRV rMSSD';
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'none';
            document.getElementById('modalDeviceTelemetry').style.display = 'block';
            document.getElementById('modalTableTitle').innerText = '14-Day Sleep & Recovery Records';
            openModal();
            await renderSleepModal();
        }

        async function renderSleepModal() {
            try {
                const res = await fetch('/api/sleep?days=14');
                const data = esc(await res.json());
                if (!data) return;

                const avg = data.averages_7d || {};
                const latest = data.latest || {};

                // 1. Stats grid
                const statsGrid = document.getElementById('modalStatsGrid');
                statsGrid.innerHTML = `
                    <div class="stat-box">
                        <div class="stat-label">7-Day Avg Sleep Score</div>
                        <div class="stat-value" style="color: #8b5cf6;">${avg.avg_sleep_score || '--'}/100</div>
                        <div class="stat-sub">Autonomic Recovery</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">7-Day Avg Duration</div>
                        <div class="stat-value" style="color: var(--accent-blue);">${avg.avg_duration_hours || '--'} <span style="font-size: 13px;">hrs</span></div>
                        <div class="stat-sub">Nightly rest volume</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Deep Slow-Wave Sleep</div>
                        <div class="stat-value" style="color: #8b5cf6;">${avg.deep_sleep_percent || '--'}%</div>
                        <div class="stat-sub">Target: 15–25% (Physical restoration)</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Overnight HRV (rMSSD)</div>
                        <div class="stat-value" style="color: var(--accent-green);">${latest.hrv_rmssd_ms || avg.avg_hrv_rmssd_ms || '--'} <span style="font-size: 13px;">ms</span></div>
                        <div class="stat-sub">${data.recovery_status || 'Balanced'}</div>
                    </div>
                `;

                // 2. Telemetry Architecture Narrative
                let html = `
                    <div style="background: rgba(139, 92, 246, 0.05); border: 1px solid rgba(139, 92, 246, 0.2); border-radius: 12px; padding: 16px; margin-bottom: 16px;">
                        <h4 style="font-size: 15px; font-weight: 700; color: #8b5cf6; margin-bottom: 8px;">Understanding Your Sleep Architecture</h4>
                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; font-size: 12px;">
                            <div style="background: var(--surface); padding: 10px; border-radius: 8px; border: 1px solid var(--border);">
                                <strong style="color: #8b5cf6;">● Deep (Slow-Wave) Sleep</strong><br/>
                                Triggers growth hormone release, physical cellular repair, and glymphatic brain metabolic waste clearance.
                            </div>
                            <div style="background: var(--surface); padding: 10px; border-radius: 8px; border: 1px solid var(--border);">
                                <strong style="color: #06b6d4;">● REM Sleep</strong><br/>
                                Critical for neuroplasticity, memory consolidation, emotional resilience, and executive cognitive functioning.
                            </div>
                            <div style="background: var(--surface); padding: 10px; border-radius: 8px; border: 1px solid var(--border);">
                                <strong style="color: var(--accent-green);">● Overnight HRV & SpO2</strong><br/>
                                Nocturnal SpO2 averages ${avg.avg_spo2_percent || 95}% with healthy autonomic parasympathetic tone during sleep.
                            </div>
                        </div>
                    </div>
                `;
                document.getElementById('modalDeviceTelemetry').innerHTML = html;

                // 3. Table
                document.getElementById('modalTableHead').innerHTML = `
                    <tr>
                        <th>Date</th>
                        <th>Duration</th>
                        <th>Sleep Score</th>
                        <th>Deep Sleep</th>
                        <th>REM Sleep</th>
                        <th>Light Sleep</th>
                        <th>SpO2</th>
                        <th>HRV (rMSSD)</th>
                    </tr>
                `;
                const tBody = document.getElementById('modalTableBody');
                tBody.innerHTML = '';
                const trend = data.trend || [];
                [...trend].reverse().forEach(row => {
                    const tr = document.createElement('tr');
                    const deepM = row.deep_sleep_minutes || Math.round((row.deep_sleep_seconds || 0)/60);
                    const remM = row.rem_sleep_minutes || Math.round((row.rem_sleep_seconds || 0)/60);
                    const lightM = row.light_sleep_minutes || Math.round((row.light_sleep_seconds || 0)/60);
                    const hrvVal = row.hrv_rmssd_ms ? `${row.hrv_rmssd_ms} ms` : '--';
                    const spo2Val = row.avg_spo2 ? `${row.avg_spo2}%` : '--';
                    tr.innerHTML = `
                        <td style="font-weight: 700;">${row.date || (row.timestamp || '').substring(0,10)}</td>
                        <td style="color: var(--accent-blue); font-weight: 600;">${row.duration_hours || (row.duration_seconds/3600).toFixed(1)} hrs</td>
                        <td><span style="font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; background: rgba(139, 92, 246, 0.15); color: #8b5cf6;">${row.sleep_score || '--'}/100</span></td>
                        <td>${deepM} min</td>
                        <td>${remM} min</td>
                        <td style="color: var(--text-muted);">${lightM} min</td>
                        <td style="color: var(--accent-green); font-weight: 600;">${spo2Val}</td>
                        <td style="font-weight: 600;">${hrvVal}</td>
                    `;
                    tBody.appendChild(tr);
                });

            } catch (err) {
                console.error("renderSleepModal error:", err);
            }
        }

        /* 7. Daily Protocol & Supplements Modal */
        const SUPP_REVIEW_PROMPT = 'Review the supplements and dosages I am currently taking in my stack. Based on my biomarkers, sleep architecture, blood labs, and genetics: what is your clinical assessment? Are there any dosages to adjust, timing/synergies to optimize, interactions to watch out for, and what additional compounds would you consider adding to optimize my health and longevity?';
        const SUPP_UNITS = ['mg', 'g', 'mcg', 'IU', 'ml', 'capsules', 'drops'];
        const SUPP_CATEGORIES = ['supplement', 'vitamin', 'mineral', 'longevity', 'nootropic', 'medication'];
        let suppEditMode = false;
        let cachedSupplementsData = null;
        let editableStack = [];

        // Options for a stack-editor <select>; a value outside the list is kept as an extra
        // selected option so saving never silently rewrites it to the first choice.
        const SUPP_SLOTS = ['wake', 'breakfast', 'lunch', 'dinner', 'bed'];
        const SUPP_SLOT_SHORT = {wake: 'W', breakfast: 'B', lunch: 'L', dinner: 'D', bed: 'Bed'};
        function suppSlotBoxes(slots) {
            return SUPP_SLOTS.map(sl => `<label title="${sl}" style="display: inline-flex; align-items: center; gap: 2px; margin-right: 6px; font-size: 11px; text-transform: capitalize;">
                <input type="checkbox" class="row-slot" value="${sl}" ${(slots || []).includes(sl) ? 'checked' : ''} />${SUPP_SLOT_SHORT[sl]}</label>`).join('');
        }

        function suppSelectOptions(choices, current) {
            const list = current && !choices.includes(current) ? [...choices, current] : choices;
            return list.map(v => `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(v)}</option>`).join('');
        }

        async function openSupplementsModal() {
            currentModalType = 'supplements';
            suppEditMode = false;
            document.querySelector('#detailModal .modal-container')?.classList.add('modal-wide');
            document.getElementById('modalTitle').innerText = 'Daily Protocol & Supplements Tracker';
            document.getElementById('modalSubtitle').innerText = 'Personalized longevity stack, intake adherence, and dosage customization';
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'none';
            document.getElementById('modalDeviceTelemetry').style.display = 'block';
            document.getElementById('modalTableTitle').innerText = 'Recent Intake Logs & Adherence';
            openModal();
            await renderSupplementsModal();
        }

        function toggleSuppEditMode() {
            suppEditMode = !suppEditMode;
            if (suppEditMode && cachedSupplementsData) {
                editableStack = JSON.parse(JSON.stringify(cachedSupplementsData.active_stack || []));
            }
            renderSupplementsModalView();
        }

        function addStackItemRow(preset) {
            syncEditableStackFromInputs();
            if (preset) {
                editableStack.push({
                    name: preset.name || '',
                    dosage: preset.dosage || 100,
                    unit: preset.unit || 'mg',
                    category: preset.category || 'supplement',
                    frequency: preset.frequency || 'daily',
                    slots: preset.slots || ['breakfast'],
                    purpose: preset.purpose || ''
                });
            } else {
                editableStack.push({
                    name: '',
                    dosage: 100,
                    unit: 'mg',
                    category: 'supplement',
                    frequency: 'daily',
                    slots: ['breakfast'],
                    purpose: ''
                });
            }
            renderSupplementsModalView();
        }

        function removeStackItemRow(index) {
            syncEditableStackFromInputs();
            editableStack.splice(index, 1);
            renderSupplementsModalView();
        }

        function syncEditableStackFromInputs() {
            editableStack = [];
            const rows = document.querySelectorAll('.supp-edit-row');
            rows.forEach(r => {
                const name = r.querySelector('.row-name')?.value?.trim();
                const dosage = parseFloat(r.querySelector('.row-dosage')?.value) || 0;
                const unit = r.querySelector('.row-unit')?.value?.trim() || 'mg';
                const frequency = r.dataset.freq || 'daily';
                const slots = [...r.querySelectorAll('.row-slot:checked')].map(c => c.value);
                const category = r.querySelector('.row-cat')?.value?.trim() || 'supplement';
                const purpose = r.querySelector('.row-purpose')?.value?.trim() || '';
                // Keep blank rows while editing; they are dropped on save.
                editableStack.push({ name: name || '', dosage, unit, frequency, slots, category, purpose });
            });
        }

        async function saveStackChanges() {
            syncEditableStackFromInputs();
            editableStack = editableStack.filter(s => s.name);
            if (editableStack.length === 0) {
                if (!confirm("Your regimen will be empty. Continue?")) return;
            }
            try {
                const res = await fetch('/api/supplements/stack', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ stack: editableStack })
                });
                if (res.ok) {
                    suppEditMode = false;
                    await loadSummary();
                    await renderSupplementsModal();
                } else {
                    const err = await res.json();
                    alert("Error saving regimen: " + (err.detail || JSON.stringify(err)));
                }
            } catch (err) {
                console.error("saveStackChanges error:", err);
                alert("Error saving regimen: " + err);
            }
        }

        function consultDrGemmaAboutSupplements() {
            closeModal();
            openChatModal();
            sendPromptChip(SUPP_REVIEW_PROMPT);
        }

        async function deleteIntakeLog(timestampStr, name) {
            if (!confirm(`Delete intake record for ${name || 'supplement'}?`)) return;
            try {
                const res = await fetch('/api/supplements/intake/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ timestamp: timestampStr, name: name })
                });
                if (res.ok) {
                    await loadSummary();
                    await renderSupplementsModal();
                }
            } catch (e) {
                console.error("deleteIntakeLog error:", e);
            }
        }

        async function quickLog(name, slot) {
            try {
                const res = await fetch('/api/supplements/quick-log', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: name, slot: slot || null })
                });
                if (res.ok) {
                    await loadSummary();
                    if (currentModalType === 'supplements') {
                        await renderSupplementsModal();
                    }
                }
            } catch (e) {
                console.error("quickLog error:", e);
            }
        }

        async function renderSupplementsModal() {
            try {
                const res = await fetch('/api/supplements?days=7');
                const data = await res.json();
                if (!data) return;
                cachedSupplementsData = data;
                if (suppEditMode && editableStack.length === 0) {
                    editableStack = JSON.parse(JSON.stringify(data.active_stack || []));
                }
                renderSupplementsModalView();
            } catch (err) {
                console.error("renderSupplementsModal error:", err);
            }
        }

        function renderSupplementsModalView() {
            const data = cachedSupplementsData;
            if (!data) return;

            const activeStack = data.active_stack || [];
            const morningItems = activeStack.filter(s => {
                const f = (s.frequency || '').toLowerCase();
                return f.includes('morn') || f.includes('am');
            });
            const eveningItems = activeStack.filter(s => {
                const f = (s.frequency || '').toLowerCase();
                return f.includes('even') || f.includes('night') || f.includes('pm') || f.includes('bed');
            });
            const morningSummary = morningItems.map(s => esc(s.name)).slice(0, 3).join(', ') || 'None scheduled';
            const eveningSummary = eveningItems.map(s => esc(s.name)).slice(0, 3).join(', ') || 'None scheduled';

            // 1. Stats grid
            const statsGrid = document.getElementById('modalStatsGrid');
            statsGrid.innerHTML = `
                <div class="stat-box">
                    <div class="stat-label">Today's Protocol Progress</div>
                    <div class="stat-value" style="color: #f59e0b;">${esc(data.completed_count)}/${esc(data.total_count)}</div>
                    <div class="stat-sub">${esc(data.completion_rate_percent)}% protocol adherence</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Active Regimen</div>
                    <div class="stat-value" style="color: var(--accent-green);">${activeStack.length} <span style="font-size: 13px;">Compounds</span></div>
                    <div class="stat-sub">Personalized longevity stack</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Morning Protocol</div>
                    <div class="stat-value" style="color: var(--accent-blue); font-size: 18px;">${morningItems.length} Compounds</div>
                    <div class="stat-sub" title="${morningSummary}">${morningSummary}</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Evening Protocol</div>
                    <div class="stat-value" style="color: #8b5cf6; font-size: 18px;">${eveningItems.length} Compounds</div>
                    <div class="stat-sub" title="${eveningSummary}">${eveningSummary}</div>
                </div>
            `;

            // 2. Action bar & banner
            let html = `
                <div style="background: rgba(245, 158, 11, 0.04); border: 1px solid rgba(245, 158, 11, 0.2); border-radius: 12px; padding: 14px 16px; margin-bottom: 16px;">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px;">
                        <div style="max-width: 580px;">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <strong style="font-size: 14px; color: #f59e0b;">💡 Collaborative, Adaptive Regimen</strong>
                                <span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(245, 158, 11, 0.15); color: #f59e0b;">Customizable</span>
                            </div>
                            <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px; line-height: 1.4;">
                                Supplement suggestions are starting hypotheses tailored to your telemetry, not rigid dogma. Edit compounds and dosages to reflect what you actually take, then consult Dr. Gemma to review interactions and suggest personalized additions.
                            </div>
                        </div>
                        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                            <button class="supp-btn ai-btn ai-only" onclick="consultDrGemmaAboutSupplements()" title="Consult Dr. Gemma on your current stack & get personalized suggestions">
                                🩺 Review with Dr. Gemma
                            </button>
                            <button class="supp-btn" id="toggleEditStackBtn" onclick="toggleSuppEditMode()">
                                ${suppEditMode ? '✓ View Checklist' : '✏️ Edit Regimen'}
                            </button>
                            ${!suppEditMode ? `<button class="supp-btn primary" onclick="toggleSuppEditMode(); addStackItemRow();">➕ Add Compound</button>` : ''}
                        </div>
                    </div>
                </div>
            `;

            // 3. Render either Edit Mode or Checklist Mode
            if (suppEditMode) {
                html += `
                    <div style="background: var(--surface-card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                            <div>
                                <h4 style="font-size: 15px; font-weight: 700; color: #f59e0b;">Edit Your Regimen & Dosages</h4>
                                <div style="font-size: 12px; color: var(--text-muted);">Customize what you actually take. Save changes to update your checklist and Dr. Gemma dossier.</div>
                            </div>
                            <div style="display: flex; gap: 8px;">
                                <button class="supp-btn primary" onclick="saveStackChanges()">💾 Save Regimen</button>
                                <button class="supp-btn" onclick="toggleSuppEditMode()">Cancel</button>
                            </div>
                        </div>

                        <!-- Suggestions Quick Add Chips -->
                        <div style="margin-bottom: 16px; padding: 12px; background: rgba(255,255,255,0.02); border-radius: 8px; border: 1px dashed var(--border);">
                            <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 8px;">
                                Evidence-Based Starting Suggestions (Click to Add):
                            </div>
                            <div style="display: flex; flex-wrap: wrap; gap: 6px;">
                `;

                const catalog = data.suggestion_catalog || [];
                catalog.forEach((catItem, idx) => {
                    html += `
                        <button class="prompt-chip" style="margin: 0; font-size: 12px; padding: 4px 10px;"
                                onclick="addStackItemRow(cachedSupplementsData.suggestion_catalog[${idx}])">
                            + ${esc(catItem.name)} (${esc(catItem.dosage)}${esc(catItem.unit)})
                        </button>
                    `;
                });

                html += `
                            </div>
                        </div>

                        <!-- Editable Table -->
                        <div style="overflow-x: auto;">
                            <table class="supp-edit-table">
                                <thead>
                                    <tr>
                                        <th style="min-width: 140px;">Compound Name</th>
                                        <th style="min-width: 75px;">Dosage</th>
                                        <th style="min-width: 70px;">Unit</th>
                                        <th style="min-width: 150px;" title="When in the day (none = as needed)">When</th>
                                        <th style="min-width: 95px;">Category</th>
                                        <th style="min-width: 140px;">Purpose / Target</th>
                                        <th style="width: 44px; text-align: center;"></th>
                                    </tr>
                                </thead>
                                <tbody>
                `;

                if (editableStack.length === 0) {
                    html += `
                        <tr>
                            <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">
                                No supplements in regimen. Click below or pick from suggestions above to add what you take.
                            </td>
                        </tr>
                    `;
                }

                editableStack.forEach((item, idx) => {
                    html += `
                        <tr class="supp-edit-row" data-freq="${esc(item.frequency || 'daily')}">
                            <td><input type="text" class="supp-input row-name" value="${esc(item.name)}" placeholder="e.g. Creatine" /></td>
                            <td><input type="number" step="any" class="supp-input row-dosage" value="${esc(String(item.dosage))}" /></td>
                            <td><select class="supp-select row-unit">${suppSelectOptions(SUPP_UNITS, item.unit)}</select></td>
                            <td style="white-space: nowrap;">${suppSlotBoxes(item.slots)}</td>
                            <td><select class="supp-select row-cat" style="text-transform: capitalize;">${suppSelectOptions(SUPP_CATEGORIES, item.category)}</select></td>
                            <td><input type="text" class="supp-input row-purpose" value="${esc(item.purpose || '')}" placeholder="e.g. Cardiovascular & sleep support" /></td>
                            <td style="text-align: center;">
                                <button type="button" onclick="removeStackItemRow(${idx})" title="Remove compound" 
                                        style="background: transparent; border: none; color: var(--accent-red); cursor: pointer; font-size: 16px;">
                                    🗑️
                                </button>
                            </td>
                        </tr>
                    `;
                });

                html += `
                                </tbody>
                            </table>
                        </div>

                        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 14px;">
                            <button class="supp-btn" onclick="addStackItemRow()">+ Add Another Compound</button>
                            <div style="display: flex; gap: 8px;">
                                <button class="supp-btn primary" onclick="saveStackChanges()">💾 Save Regimen</button>
                                <button class="supp-btn" onclick="toggleSuppEditMode()">Cancel</button>
                            </div>
                        </div>
                    </div>
                `;
            } else {
                // Checklist Mode
                html += `
                    <div style="background: rgba(245, 158, 11, 0.05); border: 1px solid rgba(245, 158, 11, 0.2); border-radius: 12px; padding: 16px; margin-bottom: 16px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                            <h4 style="font-size: 15px; font-weight: 700; color: #f59e0b;">Today's Protocol Checklist (${esc(data.today_date)})</h4>
                            <span style="font-size: 12px; color: var(--text-muted);">${data.completed_count}/${data.total_count} Completed</span>
                        </div>
                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px;">
                `;

                if (data.checklist.length === 0) {
                    html += `
                        <div style="grid-column: 1 / -1; text-align: center; padding: 24px; color: var(--text-muted);">
                            No supplements configured yet. Click <strong>✏️ Edit Regimen</strong> above to add what you take.
                        </div>
                    `;
                }

                data.checklist.forEach(item => {
                    const taken = item.taken_today;
                    const freqClass = ['wake', 'breakfast', 'lunch'].includes(item.slot) ? 'morning' : (['dinner', 'bed'].includes(item.slot) ? 'evening' : '');
                    html += `
                        <div style="background: var(--surface); padding: 12px; border-radius: 10px; border: 1px solid ${taken ? 'rgba(16, 185, 129, 0.4)' : 'var(--border)'}; display: flex; align-items: center; justify-content: space-between; gap: 10px;">
                            <div style="flex: 1; min-width: 0;">
                                <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                                    <strong style="font-size: 14px;">${esc(item.name)}</strong>
                                    <span style="font-size: 12px; color: #f59e0b; font-weight: 600;">${esc(item.dosage)}${esc(item.unit)}</span>
                                    <span class="supp-badge-freq ${freqClass}" style="text-transform: capitalize;">${esc(item.slot || 'as needed')}</span>
                                </div>
                                <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${esc(item.purpose)}">
                                    ${esc(item.purpose || 'Longevity protocol')}
                                </div>
                                ${taken ? `<div style="font-size: 10px; color: var(--accent-green); margin-top: 3px;">✓ Taken today</div>` : ''}
                            </div>
                            <div style="display: flex; align-items: center; gap: 6px;">
                                <button class="supp-check-btn ${taken ? 'checked' : ''}" data-name="${esc(item.name)}" data-slot="${esc(item.slot || '')}" onclick="quickLog(this.dataset.name, this.dataset.slot)" title="${taken ? 'Logged today. Click to log again' : 'Click to log intake'}">
                                    ${taken ? '✓' : '+'}
                                </button>
                            </div>
                        </div>
                    `;
                });

                html += `
                        </div>
                    </div>
                `;
            }

            document.getElementById('modalDeviceTelemetry').innerHTML = html;

            // 4. Table of recent logs
            document.getElementById('modalTableHead').innerHTML = `
                <tr>
                    <th>Date & Time</th>
                    <th>Compound Name</th>
                    <th>Dosage</th>
                    <th>Category</th>
                    <th>Frequency</th>
                    <th style="width: 60px;">Action</th>
                </tr>
            `;
            const tBody = document.getElementById('modalTableBody');
            tBody.innerHTML = '';
            const recs = data.recent_intakes || [];
            if (recs.length === 0) {
                tBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 16px;">No recent supplement intakes logged.</td></tr>`;
            } else {
                recs.forEach(r => {
                    const tr = document.createElement('tr');
                    const tsStr = (r.timestamp || '');
                    const displayTs = tsStr.substring(0, 16).replace('T', ' ');
                    tr.innerHTML = `
                        <td style="color: var(--text-muted);">${esc(displayTs)}</td>
                        <td style="font-weight: 700;">${esc(r.name)}</td>
                        <td style="color: var(--accent-orange); font-weight: 600;">${esc(r.dosage)} ${esc(r.unit || 'mg')}</td>
                        <td><span style="font-size: 11px; text-transform: uppercase; padding: 2px 6px; border-radius: 4px; background: rgba(255,255,255,0.08);">${esc(r.category || 'supplement')}</span></td>
                        <td style="color: var(--text-muted);">${esc(r.frequency || 'daily')}</td>
                        <td>
                            <button data-ts="${esc(tsStr)}" data-name="${esc(r.name || '')}" onclick="deleteIntakeLog(this.dataset.ts, this.dataset.name)" title="Delete intake log" 
                                    style="background: transparent; border: none; color: var(--text-muted); cursor: pointer; font-size: 14px;">
                                🗑️
                            </button>
                        </td>
                    `;
                    tBody.appendChild(tr);
                });
            }
        }

        /* 8. Sovereign Genomics & Pharmacogenomics Modal */
        async function openGenomicsModal() {
            currentModalType = 'genomics';
            document.getElementById('modalTitle').innerText = 'Sovereign Genomics';
            document.getElementById('modalSubtitle').innerText = 'Offline raw DNA analysis & SNPedia insights';
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'none';
            document.getElementById('modalDeviceTelemetry').style.display = 'block';
            document.getElementById('modalTableTitle').innerText = 'Curated Clinical Variants & Top Promethease Findings';
            openModal();
            await renderGenomicsModal();
        }

        async function renderGenomicsModal() {
            try {
                const res = await fetch('/api/genomics');
                const data = esc(await res.json());
                if (!data) return;

                const high = data.key_highlights || {};
                const variants = data.variants || [];
                const trioInsights = data.trio_insights || [];
                const topFindings = data.top_promethease_findings || [];
                const isTrio = data.trio_status && data.trio_status.is_phased_trio;

                if (isTrio) {
                    const ts = data.trio_status;
                    const tot = (ts.total_family_snps || 0).toLocaleString();
                    document.getElementById('modalSubtitle').innerText = `Self (${ts.user_snps_total?.toLocaleString()}) + Father (${ts.father_snps_total?.toLocaleString()}) + Mother (${ts.mother_snps_total?.toLocaleString()}) • ${tot} Total SNPs`;
                }

                // 1. Stats grid
                const statsGrid = document.getElementById('modalStatsGrid');
                const label = k => k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
                statsGrid.innerHTML = Object.entries(high).map(([k, v]) => `
                    <div class="stat-box">
                        <div class="stat-label">${label(k)}</div>
                        <div class="stat-value" style="font-size: 16px;">${v}</div>
                    </div>
                `).join('');

                // 2. Telemetry Section: Trio Pedigree & Curated Variants
                let html = '';

                if (trioInsights.length > 0) {
                    html += `
                        <div style="background: rgba(14, 165, 233, 0.05); border: 1px solid rgba(14, 165, 233, 0.25); border-radius: 12px; padding: 16px; margin-bottom: 16px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                                <h4 style="font-size: 15px; font-weight: 700; color: #38bdf8; margin: 0;">Family Pedigree Phasing & Inheritance Insights</h4>
                                <span style="font-size: 11px; padding: 2px 8px; border-radius: 999px; background: rgba(56, 189, 248, 0.15); color: #38bdf8; font-weight: 600;">Trio Validated</span>
                            </div>
                            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px;">
                    `;
                    trioInsights.forEach(ti => {
                        let badgeColor = '#38bdf8';
                        let bgBadge = 'rgba(56, 189, 248, 0.15)';
                        if (ti.trait.includes('ApoE') || ti.trait.includes('Zero')) {
                            badgeColor = '#10b981';
                            bgBadge = 'rgba(16, 185, 129, 0.15)';
                        } else if (ti.trait.includes('CYP2C19')) {
                            badgeColor = '#f59e0b';
                            bgBadge = 'rgba(245, 158, 11, 0.15)';
                        }
                        html += `
                            <div style="background: var(--surface); padding: 14px; border-radius: 10px; border: 1px solid var(--border);">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                    <strong style="color: #38bdf8; font-size: 13px;">${ti.trait}</strong>
                                    <span style="font-size: 10px; padding: 2px 6px; border-radius: 4px; background: ${bgBadge}; color: ${badgeColor}; font-weight: 600;">Verified</span>
                                </div>
                                <div style="font-weight: 700; font-size: 13px; color: var(--text-main); margin-bottom: 4px;">${ti.finding}</div>
                                <div style="font-size: 12px; color: var(--text-muted); line-height: 1.4;">${ti.detail}</div>
                            </div>
                        `;
                    });
                    html += `
                            </div>
                        </div>
                    `;
                }

                // Curated Variant Cards
                html += `
                    <div style="background: rgba(236, 72, 153, 0.05); border: 1px solid rgba(236, 72, 153, 0.2); border-radius: 12px; padding: 16px; margin-bottom: 16px;">
                        <h4 style="font-size: 15px; font-weight: 700; color: #ec4899; margin-bottom: 10px;">Curated Pharmacogenomics & Longevity Variants</h4>
                        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px;">
                `;

                variants.forEach(v => {
                    const inhTag = v.inheritance ? `<div style="font-size: 11px; color: #38bdf8; margin-top: 4px;"><strong>Pedigree:</strong> ${v.inheritance}</div>` : '';
                    html += `
                        <div style="background: var(--surface); padding: 14px; border-radius: 10px; border: 1px solid var(--border);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                <strong style="color: #ec4899; font-size: 14px;">${v.gene} (${v.rsid})</strong>
                                <span style="font-size: 11px; padding: 2px 8px; border-radius: 999px; background: rgba(255,255,255,0.08); font-weight: 600;">${v.badge}</span>
                            </div>
                            <div style="font-weight: 700; font-size: 13px; color: var(--text-main); margin-bottom: 4px;">${v.status}</div>
                            <div style="font-size: 12px; color: var(--text-muted); line-height: 1.4; margin-bottom: 6px;">${v.significance}</div>
                            <div style="font-size: 12px; color: var(--accent-green); line-height: 1.4;"><strong>Action:</strong> ${v.recommendation}</div>
                            ${inhTag}
                        </div>
                    `;
                });

                html += `
                        </div>
                    </div>
                `;
                document.getElementById('modalDeviceTelemetry').innerHTML = html;

                // 3. Table: Top Promethease High-Magnitude Findings if present, else standard variants
                if (topFindings.length > 0) {
                    document.getElementById('modalTableTitle').innerText = 'Top Promethease High-Magnitude Findings (Magnitude ≥ 2.5)';
                    document.getElementById('modalTableHead').innerHTML = `
                        <tr>
                            <th>Gene / Locus</th>
                            <th>rsID</th>
                            <th>Magnitude</th>
                            <th>Repute</th>
                            <th>Self</th>
                            <th>Father</th>
                            <th>Mother</th>
                            <th>Summary Finding</th>
                        </tr>
                    `;
                    const tBody = document.getElementById('modalTableBody');
                    tBody.innerHTML = '';
                    topFindings.forEach(f => {
                        let reputeBadge = `<span style="padding: 2px 6px; border-radius: 4px; background: rgba(148, 163, 184, 0.2); color: #94a3b8; font-weight: 600; font-size: 11px;">Neutral</span>`;
                        if (f.repute && f.repute.toLowerCase() === 'good') {
                            reputeBadge = `<span style="padding: 2px 6px; border-radius: 4px; background: rgba(16, 185, 129, 0.2); color: #10b981; font-weight: 600; font-size: 11px;">Good</span>`;
                        } else if (f.repute && f.repute.toLowerCase() === 'bad') {
                            reputeBadge = `<span style="padding: 2px 6px; border-radius: 4px; background: rgba(239, 68, 68, 0.2); color: #ef4444; font-weight: 600; font-size: 11px;">Bad</span>`;
                        }

                        const tr = document.createElement('tr');
                        tr.innerHTML = `
                            <td style="font-weight: 700; color: #ec4899;">${f.gene || 'Locus'}</td>
                            <td><a href="https://www.snpedia.com/index.php/${f.rsid}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-blue); text-decoration: underline; font-weight: 600;">${f.rsid}</a></td>
                            <td style="font-weight: 700; color: ${f.magnitude >= 4 ? '#ef4444' : '#f59e0b'};">${f.magnitude.toFixed(1)}</td>
                            <td>${reputeBadge}</td>
                            <td style="font-family: monospace; font-weight: 700; color: var(--text-main);">${f.genotype}</td>
                            <td style="font-family: monospace; color: var(--text-muted);">${f.father_genotype || 'N/A'}</td>
                            <td style="font-family: monospace; color: var(--text-muted);">${f.mother_genotype || 'N/A'}</td>
                            <td style="font-size: 12px; color: var(--text-main); line-height: 1.4;">${f.summary}</td>
                        `;
                        tBody.appendChild(tr);
                    });
                } else {
                    document.getElementById('modalTableTitle').innerText = 'Clinically Validated Variants & Recommendations';
                    document.getElementById('modalTableHead').innerHTML = `
                        <tr>
                            <th>Gene</th>
                            <th>rsID & Genotype</th>
                            <th>Category</th>
                            <th>Status</th>
                            <th>Clinical Significance</th>
                        </tr>
                    `;
                    const tBody = document.getElementById('modalTableBody');
                    tBody.innerHTML = '';
                    variants.forEach(v => {
                        const tr = document.createElement('tr');
                        tr.innerHTML = `
                            <td style="font-weight: 700; color: #ec4899;">${v.gene}</td>
                            <td style="color: var(--accent-blue); font-weight: 600;">${v.rsid} (${v.genotype})</td>
                            <td style="text-transform: uppercase; font-size: 11px; color: var(--text-muted);">${v.category}</td>
                            <td style="font-weight: 600;">${v.status}</td>
                            <td style="font-size: 12px; color: var(--text-muted);">${v.significance}</td>
                        `;
                        tBody.appendChild(tr);
                    });
                }

            } catch (err) {
                console.error("renderGenomicsModal error:", err);
            }
        }

        /* Counter & Places Modals */
        const COUNTER_COLORS = ['#38bdf8', '#10b981', '#f59e0b', '#a78bfa', '#f472b6', '#f87171', '#2dd4bf', '#facc15'];
        const counterColor = i => COUNTER_COLORS[i % COUNTER_COLORS.length];
        let counterState = null;  // last /api/counter response, escaped

        async function fetchCounter() {
            const res = await fetch('/api/counter');
            return esc(await res.json());
        }

        function renderCounterCard(c) {
            counterState = c;
            const card = document.getElementById('counter-card');
            const els = c.elements || [];
            card.style.display = els.length ? '' : 'none';
            if (!els.length) return;
            document.getElementById('counter-title').innerHTML = c.title;
            document.getElementById('counter-badge').innerText = `Today (${c.date})`;
            document.getElementById('counter-elements').innerHTML = els.map((e, i) => `
                <div style="text-align: center; min-width: 64px;">
                    <div style="font-size: 13px; font-weight: 600; color: var(--text-muted); margin-bottom: 2px;">${e.label}</div>
                    <div class="metric-main" style="color: ${counterColor(i)};">${c.counts[e.key] ?? 0}</div>
                    <div style="display: flex; gap: 6px; justify-content: center; margin-top: 6px;">
                        <button class="ai-header-btn plain" style="padding: 2px 10px; font-size: 14px; font-weight: bold;" onclick="event.stopPropagation(); adjustCounterWeb('${e.key}', -1)" aria-label="${e.label} minus one">-</button>
                        <button class="ai-header-btn" style="padding: 2px 10px; font-size: 14px; font-weight: bold;" onclick="event.stopPropagation(); adjustCounterWeb('${e.key}', 1)" aria-label="${e.label} plus one">+</button>
                    </div>
                </div>`).join('');
            document.getElementById('counter-footer').innerHTML = 'All-time: ' + els.map(e => `${e.label} ${c.totals[e.key] ?? 0}`).join(' • ');
            renderCounterWeek(c);
        }

        async function loadPatterns() {
            try {
                const res = await fetch('/api/patterns');
                if (!res.ok) return;
                const d = esc(await res.json());
                const card = document.getElementById('patterns-card');
                card.style.display = d.logged_days ? '' : 'none';
                document.getElementById('patterns-badge').innerText = `${d.days} days`;
                const shown = (d.patterns || []).slice(0, 6);
                document.getElementById('patterns-list').innerHTML = shown.length
                    ? shown.map(p => `<li>${p.text}</li>`).join('')
                    : '<li style="list-style: none; margin-left: -18px; color: var(--text-muted);">Nothing strong enough yet: patterns need at least 8 days of data.</li>';
            } catch (e) { /* the card stays as it was */ }
        }

        async function postCounter(body) {
            const res = await fetch('/api/counter', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const updated = esc(await res.json());
            // The response is for the day posted; refetch so the card shows today.
            const today = body.date ? await fetchCounter() : updated;
            renderCounterCard(today);
            if (currentModalType === 'counter') renderCounterModal(today);
        }

        async function adjustCounterWeb(item, delta, date) {
            try {
                await postCounter(date ? { item, delta, date } : { item, delta });
            } catch (e) {
                console.error("Counter update failed", e);
            }
        }

        function editPastRow(dateStr) {
            const row = (counterState?.daily_history || []).find(d => d.date === dateStr);
            document.getElementById('counter-past-date').value = dateStr;
            (counterState?.elements || []).forEach(e => {
                const input = document.getElementById(`counter-past-${e.key}`);
                if (input) input.value = row ? (row.counts[e.key] ?? 0) : 0;
            });
            document.getElementById('modalDeviceTelemetry')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }

        async function savePastCounterWeb() {
            const date = document.getElementById('counter-past-date')?.value;
            if (!date) return;
            const counts = Object.fromEntries((counterState?.elements || []).map(e =>
                [e.key, parseInt(document.getElementById(`counter-past-${e.key}`)?.value || '0', 10) || 0]));
            try {
                await postCounter({ date, counts });
            } catch (e) {
                console.error("savePastCounterWeb failed", e);
            }
        }

        // Last 7 days on the card: one row per counter, oldest left, days with no entry show "–"
        function renderCounterWeek(c) {
            const el = document.getElementById('counter-week');
            const byDate = Object.fromEntries((c.daily_history || []).map(d => [d.date, d]));
            const cells = [];
            for (let i = 6; i >= 0; i--) {
                const d = new Date(); d.setDate(d.getDate() - i);
                const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
                const r = byDate[key];
                const label = d.toLocaleDateString(undefined, { weekday: 'narrow' });
                cells.push(`<div style="flex: 1; text-align: center; font-size: 11px; line-height: 1.4;">
                    <div style="color: var(--text-muted);">${label}</div>
                    ${c.elements.map((e, j) => `<div style="color: ${counterColor(j)}; font-weight: 600;">${r ? (r.counts[e.key] ?? 0) : '–'}</div>`).join('')}
                </div>`);
            }
            el.innerHTML = cells.join('');
        }

        async function openCounterModal() {
            currentModalType = 'counter';
            openModal();
            document.getElementById('modalTitle').innerHTML = counterState?.title || 'Counter';
            document.getElementById('modalSubtitle').innerText = 'Daily counts and 30-day history';
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'block';
            document.getElementById('modalDeviceTelemetry').style.display = 'block';
            try {
                const c = await fetchCounter();
                renderCounterCard(c);
                renderCounterModal(c);
            } catch (e) {
                console.error("Failed to load counter", e);
            }
        }

        function renderCounterModal(data) {
            const els = data.elements || [];
            const daily = data.daily_history || [];
            const past7 = daily.slice(0, 7);
            const avg7 = k => past7.length ? (past7.reduce((acc, d) => acc + (d.counts[k] || 0), 0) / past7.length).toFixed(1) : '0.0';

            document.getElementById('modalStatsGrid').innerHTML = els.map((e, i) => `
                <div class="stat-card">
                    <div class="stat-label">Today ${e.label}</div>
                    <div class="stat-value" style="color: ${counterColor(i)};">${data.counts[e.key] ?? 0}</div>
                    <div style="display: flex; gap: 8px; margin-top: 8px;">
                        <button class="ai-header-btn plain" onclick="adjustCounterWeb('${e.key}', -1)">- 1</button>
                        <button class="ai-header-btn" onclick="adjustCounterWeb('${e.key}', 1)">+ 1</button>
                    </div>
                    <div class="stat-sub">7-day avg ${avg7(e.key)} • all-time ${data.totals[e.key] ?? 0}</div>
                </div>`).join('');

            // Two counters chart side by side; otherwise the daily total.
            if (daily.length > 0) {
                const two = els.length === 2;
                const points = daily.slice().reverse().map(d => {
                    const p = { time: new Date(d.date + "T12:00:00Z"), val1: two ? (d.counts[els[0].key] || 0) : d.total };
                    if (two) p.val2 = d.counts[els[1].key] || 0;
                    return p;
                });
                const series = two ? [[els[0].label, counterColor(0)], [els[1].label, counterColor(1)]] : [['Total', counterColor(0)]];
                document.getElementById('chartLegend').innerHTML = series.map(([l, col]) =>
                    `<div class="legend-item"><div class="legend-dot" style="background: ${col};"></div> ${l}</div>`).join('');
                drawTimeSeriesChart(points, counterColor(0), counterColor(1), series[0][0], two ? series[1][0] : '');
            } else {
                drawEmptyChart("No counter history yet");
            }

            // Past day entry bar
            const yDate = new Date();
            yDate.setDate(yDate.getDate() - 1);
            const yesterdayStr = yDate.toISOString().substring(0, 10);
            const inputStyle = 'width: 55px; background: var(--surface-1, #1e293b); border: 1px solid var(--border, #334155); color: #f8fafc; border-radius: 6px; padding: 4px 6px; font-size: 13px; text-align: center;';
            const container = document.getElementById('modalDeviceTelemetry');
            container.style.display = 'block';
            container.innerHTML = `
                <div style="background: rgba(14, 165, 233, 0.08); border: 1px solid rgba(14, 165, 233, 0.25); border-radius: 8px; padding: 12px 16px; margin: 14px 0 6px 0; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between;">
                    <div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: center;">
                        <span style="font-size: 13px; font-weight: 600; color: #38bdf8;">📅 Log / Edit Past Day:</span>
                        <input type="date" id="counter-past-date" value="${yesterdayStr}" style="background: var(--surface-1, #1e293b); border: 1px solid var(--border, #334155); color: #f8fafc; border-radius: 6px; padding: 4px 8px; font-size: 13px;">
                        ${els.map((e, i) => `
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <label for="counter-past-${e.key}" style="font-size: 12px; color: ${counterColor(i)}; font-weight: bold;">${e.label}:</label>
                            <input type="number" id="counter-past-${e.key}" min="0" value="0" style="${inputStyle}">
                        </div>`).join('')}
                    </div>
                    <button class="ai-header-btn" style="font-size: 12px; padding: 6px 14px;" onclick="savePastCounterWeb()">Save Count</button>
                </div>
            `;

            document.getElementById('modalTableTitle').innerHTML = `<span>Daily History (Past 30 Days)</span>`;
            document.getElementById('modalTableHead').innerHTML = `
                <tr>
                    <th>Date</th>
                    ${els.map((e, i) => `<th style="color: ${counterColor(i)};">${e.label}</th>`).join('')}
                    <th>Total</th>
                    <th style="text-align: right;">Adjust</th>
                </tr>
            `;

            const tbody = document.getElementById('modalTableBody');
            if (daily.length === 0) {
                tbody.innerHTML = `<tr><td colspan="${els.length + 3}" style="text-align: center; color: var(--text-muted); padding: 16px;">No daily history recorded yet. Use + on the card, the app, or Log / Edit Past Day above.</td></tr>`;
                return;
            }
            tbody.innerHTML = daily.map(d => `
                <tr>
                    <td><strong>${d.date}</strong></td>
                    ${els.map((e, i) => `<td style="color: ${counterColor(i)}; font-weight: 600;">${d.counts[e.key] ?? 0}</td>`).join('')}
                    <td style="font-weight: 600;">${d.total}</td>
                    <td style="text-align: right; white-space: nowrap;">
                        ${els.map(e => `<button class="ai-header-btn plain" style="font-size: 11px; padding: 2px 7px; margin-right: 4px;" onclick="adjustCounterWeb('${e.key}', 1, '${d.date}')" title="Add 1 to ${e.label}">+${e.label}</button>`).join('')}
                        <button class="ai-header-btn plain" style="font-size: 11px; padding: 2px 8px;" onclick="editPastRow('${d.date}')">Edit</button>
                    </td>
                </tr>`).join('');
        }

        async function addManualPlaceWeb() {
            const name = document.getElementById('manual-place-name')?.value?.trim();
            const dateStr = document.getElementById('manual-place-date')?.value;
            const mins = parseInt(document.getElementById('manual-place-mins')?.value || '60', 10);
            const coordsStr = document.getElementById('manual-place-coords')?.value?.trim() || '';
            if (!name) {
                alert("Please enter a place name.");
                return;
            }
            let lat = 0.0;
            let lon = 0.0;
            if (coordsStr.includes(',')) {
                const parts = coordsStr.split(',');
                lat = parseFloat(parts[0]) || 0.0;
                lon = parseFloat(parts[1]) || 0.0;
            }
            try {
                const res = await fetch('/api/places', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: name,
                        date: dateStr,
                        duration_seconds: mins * 60,
                        avg_lat: lat,
                        avg_lon: lon
                    })
                });
                if (res.ok) await reloadPlaces();
            } catch (e) {
                console.error("addManualPlaceWeb failed", e);
            }
        }

        async function deletePlaceWeb(placeId) {
            if (!confirm("Delete this place?")) return;
            try {
                const res = await fetch(`/api/places/${placeId}`, { method: 'DELETE' });
                if (res.ok) await reloadPlaces();
            } catch (e) {
                console.error("deletePlaceWeb failed", e);
            }
        }

        async function openPlacesModal(focusId) {
            currentModalType = 'places';
            window._placesMapMode = 'local';
            if (focusId) window._placesMapFocusId = focusId;
            openModal();
            document.getElementById('modalTitle').innerText = 'Dwell Places (>1 hr)';
            document.getElementById('modalSubtitle').innerText = 'Places where you spent more than an hour';
            document.getElementById('modalFilterRow').style.display = 'none';
            document.getElementById('modalChartCard').style.display = 'none';
            document.getElementById('modalDeviceTelemetry').style.display = 'block';
            document.getElementById('modalTableTitle').innerText = 'All Recorded Places';

            await reloadPlaces();
        }

        async function reloadPlaces() {
            try {
                const res = await fetch('/api/places?date=all' + (window._placesShowHome ? '&include_home=true' : ''));
                renderPlacesModal(await res.json());
            } catch (e) {
                console.error("Failed to load places", e);
            }
        }

        function togglePlacesHome(on) { window._placesShowHome = on; reloadPlaces(); }

        // Pick a name for a stay: known places nearby, then OpenStreetMap POIs. The pick is remembered
        // and guessed for later stays there.
        async function openNamePicker(id) {
            showPlaceOnMap(id, 'local', false);  // stay with the picker, don't scroll up to the map
            const row = document.getElementById('name-row-' + id);
            if (!row) return;
            if (row.style.display !== 'none') { row.style.display = 'none'; return; }
            row.style.display = '';
            const box = row.querySelector('.name-choices');
            box.innerHTML = '<span style="color: var(--text-muted); font-size: 12px;">Looking up nearby places…</span>';
            let sugg = [];
            try {
                const res = await fetch(`/api/places/${encodeURIComponent(id)}/suggestions`);
                if (res.ok) sugg = (await res.json()).suggestions || [];
            } catch (e) { console.error("suggestions failed", e); }
            if (!sugg.some(s => s.name.toLowerCase() === 'home')) sugg.unshift({ name: 'Home', type: 'known' });
            (window._nameChoices = window._nameChoices || {})[id] = sugg;
            box.innerHTML = sugg.map((s, i) => `<button class="ai-header-btn plain" style="font-size: 11px; padding: 3px 9px; margin: 2px;" onclick="namePlace('${id}', window._nameChoices['${id}'][${i}].name)">${esc(s.name)}${s.type === 'known' ? ' ★' : ''}${s.distance_m != null ? ` <span style="color: var(--text-muted);">${s.distance_m} m</span>` : ''}</button>`).join('');
        }

        async function namePlace(id, name) {
            name = (name || '').trim();
            try {
                const res = await fetch(`/api/places/${encodeURIComponent(id)}`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name })
                });
                if (res.ok) await reloadPlaces();
            } catch (e) { console.error("namePlace failed", e); }
        }

        async function forgetKnownPlace(id) {
            if (!confirm("Forget this place? Stays you named yourself keep their names.")) return;
            try {
                const res = await fetch(`/api/places/known/${encodeURIComponent(id)}`, { method: 'DELETE' });
                if (res.ok) await reloadPlaces();
            } catch (e) { console.error("forgetKnownPlace failed", e); }
        }

        // OpenStreetMap embed. Default mode: 'local' (starts at a fairly local neighborhood zoom ~1 km around focused place).
        // Mode 'all' frames all valid coordinates across places.
        function placesMapHtml(places, focus, mode) {
            if (mode) window._placesMapMode = mode;
            const currentMode = window._placesMapMode || 'local';
            const pts = (places || []).filter(p => typeof p.avg_lat === 'number' && typeof p.avg_lon === 'number'
                && !isNaN(p.avg_lat) && !isNaN(p.avg_lon) && Math.abs(p.avg_lat) > 0.0001 && Math.abs(p.avg_lon) > 0.0001);
            if (!pts.length) return '<div style="font-size: 12px; color: var(--text-muted); margin: 8px 0;">No places with coordinates yet: OwnTracks places have them, manual ones only if you enter Lat, Lon.</div>';
            const f = focus || (window._placesMapFocusId ? pts.find(x => x.id === window._placesMapFocusId) : null) || pts[0];
            window._placesMapFocusId = f.id;

            let bbox = '';
            if (currentMode === 'all' && pts.length > 1) {
                const validLats = pts.map(p => p.avg_lat);
                const validLons = pts.map(p => p.avg_lon);
                const pad = 0.02;
                bbox = [
                    (Math.min(...validLons) - pad).toFixed(6),
                    (Math.min(...validLats) - pad).toFixed(6),
                    (Math.max(...validLons) + pad).toFixed(6),
                    (Math.max(...validLats) + pad).toFixed(6)
                ].join(',');
            } else {
                // Fairly local neighborhood zoom (~1.1 km box around focused place)
                const padLat = 0.005;
                const cosLat = Math.cos(f.avg_lat * Math.PI / 180.0) || 1.0;
                const padLon = padLat / Math.abs(cosLat);
                bbox = [
                    (f.avg_lon - padLon).toFixed(6),
                    (f.avg_lat - padLat).toFixed(6),
                    (f.avg_lon + padLon).toFixed(6),
                    (f.avg_lat + padLat).toFixed(6)
                ].join(',');
            }

            const src = `https://www.openstreetmap.org/export/embed.html?bbox=${bbox}&layer=mapnik&marker=${f.avg_lat},${f.avg_lon}`;
            const osmUrl = `https://www.openstreetmap.org/?mlat=${f.avg_lat}&mlon=${f.avg_lon}#map=17/${f.avg_lat}/${f.avg_lon}`;
            const placeTitle = f.name ? `<strong>${f.name}</strong>` : '<span style="color: var(--text-muted);">Unnamed Place</span>';
            const dwellStr = f.dwell_formatted ? ` • ${f.dwell_formatted}` : '';

            return `
                <div style="background: rgba(0,0,0,0.25); border: 1px solid var(--border-color); border-radius: 8px; padding: 10px; margin-top: 10px;">
                    <div style="display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 12px;">
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #6366f1;"></span>
                            <span>Viewing: ${placeTitle}${dwellStr} <span style="color: var(--text-muted); font-family: monospace; font-size: 11px;">(${f.avg_lat.toFixed(4)}, ${f.avg_lon.toFixed(4)})</span></span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <button class="ai-header-btn ${currentMode === 'local' ? '' : 'plain'}" style="font-size: 11px; padding: 2px 8px;" onclick="setPlacesMapMode('local')">📍 Local Zoom</button>
                            <button class="ai-header-btn ${currentMode === 'all' ? '' : 'plain'}" style="font-size: 11px; padding: 2px 8px;" onclick="setPlacesMapMode('all')">🗺️ All Places (${pts.length})</button>
                            <a href="${osmUrl}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-blue); text-decoration: none; font-weight: 500; margin-left: 6px;">Open in OSM ↗</a>
                        </div>
                    </div>
                    <iframe id="places-map" title="Places map" src="${src}" style="width: 100%; height: 320px; border: 1px solid var(--border-color); border-radius: 6px;" loading="lazy"></iframe>
                </div>
            `;
        }

        function setPlacesMapMode(mode) {
            window._placesMapMode = mode;
            const holder = document.getElementById('places-map-holder');
            if (holder) {
                const f = (window._placesShown || []).find(x => x.id === window._placesMapFocusId);
                holder.innerHTML = placesMapHtml(window._placesShown, f, mode);
            }
        }

        function showPlaceOnMap(id, mode, scroll = true) {
            const p = (window._placesShown || []).find(x => x.id === id);
            const holder = document.getElementById('places-map-holder');
            if (p && holder) {
                window._placesMapFocusId = id;
                holder.innerHTML = placesMapHtml(window._placesShown, p, mode || 'local');
                if (scroll) holder.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }

        function renderPlacesModal(data) {
            const places = esc(data.places || []);
            window._placesShown = places;
            const stats = document.getElementById('modalStatsGrid');
            const totalMins = places.reduce((acc, p) => acc + (p.duration_seconds || 0), 0) / 60;
            const hrs = Math.floor(totalMins / 60);
            const mins = Math.floor(totalMins % 60);
            const totalDwellStr = hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`;

            stats.innerHTML = `
                <div class="stat-card">
                    <div class="stat-label">Total Logged Places</div>
                    <div class="stat-value" style="color: #818cf8;">${places.length}</div>
                    <div class="stat-sub">Across all dates</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Total Dwell Time</div>
                    <div class="stat-value">${totalDwellStr}</div>
                    <div class="stat-sub">Across qualified dwell places</div>
                </div>
            `;

            // Manual Place Entry Form
            const yDate = new Date();
            yDate.setDate(yDate.getDate() - 1);
            const defaultDate = yDate.toISOString().substring(0, 10);
            const telemetryContainer = document.getElementById('modalDeviceTelemetry');
            telemetryContainer.style.display = 'block';
            telemetryContainer.innerHTML = `
                <div style="background: rgba(99, 102, 241, 0.08); border: 1px solid rgba(99, 102, 241, 0.25); border-radius: 8px; padding: 12px 16px; margin: 14px 0 6px 0; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between;">
                    <div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: center;">
                        <span style="font-size: 13px; font-weight: 600; color: #818cf8;">📍 Add Place:</span>
                        <input type="text" id="manual-place-name" placeholder="Name (e.g. Gym, Office)" style="background: var(--surface-1, #1e293b); border: 1px solid var(--border, #334155); color: #f8fafc; border-radius: 6px; padding: 4px 8px; font-size: 13px; min-width: 150px;">
                        <input type="date" id="manual-place-date" value="${defaultDate}" style="background: var(--surface-1, #1e293b); border: 1px solid var(--border, #334155); color: #f8fafc; border-radius: 6px; padding: 4px 8px; font-size: 13px;">
                        <div style="display: flex; align-items: center; gap: 4px;">
                            <label style="font-size: 12px; color: var(--text-dim, #94a3b8);">Duration:</label>
                            <input type="number" id="manual-place-mins" min="15" step="15" value="60" style="width: 55px; background: var(--surface-1, #1e293b); border: 1px solid var(--border, #334155); color: #f8fafc; border-radius: 6px; padding: 4px 6px; font-size: 13px; text-align: center;">
                            <span style="font-size: 12px; color: var(--text-dim, #94a3b8);">mins</span>
                        </div>
                        <input type="text" id="manual-place-coords" placeholder="Lat, Lon (optional)" style="background: var(--surface-1, #1e293b); border: 1px solid var(--border, #334155); color: #f8fafc; border-radius: 6px; padding: 4px 8px; font-size: 13px; width: 140px;">
                    </div>
                    <button class="ai-header-btn" style="font-size: 12px; padding: 6px 14px; background: #6366f1;" onclick="addManualPlaceWeb()">Add Place</button>
                </div>
                <div style="display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 10px 0 4px 0; font-size: 12px;">
                    <span style="color: var(--text-muted);">Known places:</span>
                    ${(esc(data.known || [])).map(k => `<span style="border: 1px solid var(--border-color); border-radius: 12px; padding: 2px 4px 2px 9px;">${k.name} <span style="color: var(--text-muted);">${Math.round(k.radius_m)} m</span> <button class="ai-header-btn plain" style="font-size: 11px; padding: 0 6px;" title="Forget" onclick="forgetKnownPlace('${k.id}')">×</button></span>`).join('') || '<span style="color: var(--text-muted);">none yet: name a stay below</span>'}
                    <label style="margin-left: auto; color: var(--text-muted);"><input type="checkbox" ${window._placesShowHome ? 'checked' : ''} onchange="togglePlacesHome(this.checked)"> Show Home stays</label>
                </div>
                <div id="places-map-holder">${placesMapHtml(places)}</div>
            `;

            document.getElementById('modalTableHead').innerHTML = `
                <tr>
                    <th>Date</th>
                    <th>Place / POI Name</th>
                    <th>Dwell Time</th>
                    <th>Coordinates</th>
                    <th>Map</th>
                    <th style="text-align: right;">Action</th>
                </tr>
            `;

            const tbody = document.getElementById('modalTableBody');
            tbody.innerHTML = '';
            if (places.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 16px;">No places recorded yet. Use the form above to add one.</td></tr>';
                return;
            }

            places.forEach(p => {
                const tr = document.createElement('tr');
                const displayName = !p.name ? `<span style="color: var(--text-muted);">Unnamed</span>`
                    : p.name_guessed ? `<em>${p.name}</em> <span style="color: var(--text-muted); font-size: 11px;">(guess)</span>`
                    : `<strong>${p.name}</strong>`;
                const hasCoords = p.avg_lat !== 0 || p.avg_lon !== 0;
                const mapUrl = hasCoords
                    ? `https://www.openstreetmap.org/?mlat=${p.avg_lat}&mlon=${p.avg_lon}#map=17/${p.avg_lat}/${p.avg_lon}`
                    : `https://www.openstreetmap.org/search?query=${encodeURIComponent(p.name || '')}`;
                const coordsText = hasCoords
                    ? `${p.avg_lat.toFixed(5)}, ${p.avg_lon.toFixed(5)}`
                    : '<span style="color: var(--text-muted);">Manual</span>';
                const mapLabel = hasCoords ? 'View Map ↗' : 'Search ↗';

                tr.innerHTML = `
                    <td><strong>${p.date || ''}</strong></td>
                    <td>${displayName}</td>
                    <td><strong style="color: #818cf8;">${p.dwell_formatted}</strong></td>
                    <td style="font-family: monospace; font-size: 12px;">${coordsText}</td>
                    <td style="white-space: nowrap;">${hasCoords ? `<button class="ai-header-btn plain" style="font-size: 11px; padding: 2px 8px; margin-right: 6px;" onclick="showPlaceOnMap('${p.id}')">Show</button>` : ''}<a href="${mapUrl}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-blue); text-decoration: none;">${mapLabel}</a></td>
                    <td style="text-align: right; white-space: nowrap;">
                        ${hasCoords ? `<button class="ai-header-btn plain" style="font-size: 11px; padding: 2px 8px; margin-right: 4px;" onclick="openNamePicker('${p.id}')">${p.name && !p.name_guessed ? 'Rename' : 'Name'}</button>` : ''}
                        <button class="ai-header-btn plain" style="font-size: 11px; padding: 2px 8px; color: #ef4444;" onclick="deletePlaceWeb('${p.id}')">Delete</button>
                    </td>
                `;
                tbody.appendChild(tr);
                if (hasCoords) {
                    const nr = document.createElement('tr');
                    nr.id = 'name-row-' + p.id;
                    nr.style.display = 'none';
                    nr.innerHTML = `<td colspan="6"><div class="name-choices" style="margin-bottom: 6px;"></div>
                        <input type="text" id="name-input-${p.id}" placeholder="Or type a name" value="${p.name || ''}" style="background: var(--surface-1, #1e293b); border: 1px solid var(--border, #334155); color: #f8fafc; border-radius: 6px; padding: 4px 8px; font-size: 13px; min-width: 180px;">
                        <button class="ai-header-btn" style="font-size: 12px; padding: 4px 12px;" onclick="namePlace('${p.id}', document.getElementById('name-input-${p.id}').value)">Save</button></td>`;
                    tbody.appendChild(nr);
                }
            });
        }

        /* 9. Interactive AI Health Agent ("Dr. Gemma") */
        let chatHistory = [];

        function openChatModal() {
            document.getElementById('chatModal').classList.add('open');
            setTimeout(() => {
                document.getElementById('chatInput').focus();
            }, 100);
        }

        function closeChatModal() {
            document.getElementById('chatModal').classList.remove('open');
        }

        function onChatBackdropClick(e) {
            if (e.target.id === 'chatModal') {
                closeChatModal();
            }
        }

        function sendPromptChip(text) {
            document.getElementById('chatInput').value = text;
            sendChatMessage();
        }

        function formatAiReply(text) {
            let html = esc(text)
                .replace(/### (.*?)\n/g, '<h4 style="color: var(--accent-blue); font-size: 14px; margin: 8px 0 4px 0;">$1</h4>')
                .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                .replace(/\*(.*?)\*/g, '<em>$1</em>')
                .replace(/^- (.*?)\n/gm, '<li style="margin-left: 16px; margin-bottom: 4px;">$1</li>')
                .replace(/\n\n/g, '<br/><br/>');
            return html;
        }

        async function sendChatMessage() {
            const input = document.getElementById('chatInput');
            const q = input.value.trim();
            if (!q) return;

            input.value = '';
            input.disabled = true;
            document.getElementById('chatSendBtn').disabled = true;

            const msgContainer = document.getElementById('chatMessages');

            // Append user bubble
            const uDiv = document.createElement('div');
            uDiv.className = 'chat-bubble user';
            uDiv.innerText = q;
            msgContainer.appendChild(uDiv);

            chatHistory.push({ role: 'user', content: q });

            // Append thinking bubble
            const thinkDiv = document.createElement('div');
            thinkDiv.className = 'chat-bubble ai';
            thinkDiv.id = 'typingBubble';
            thinkDiv.innerHTML = '<em style="color: var(--text-muted);">Dr. Gemma is analyzing clinical telemetry...</em>';
            msgContainer.appendChild(thinkDiv);
            msgContainer.scrollTop = msgContainer.scrollHeight;

            thinkDiv.removeAttribute('id');
            await askGemma(thinkDiv, false);
        }

        // Fills `bubble` with Dr. Gemma's answer to chatHistory. With `cont`, the last assistant message was
        // cut off: the continuation is appended to it, in the same bubble.
        async function askGemma(bubble, cont) {
            const input = document.getElementById('chatInput');
            const msgContainer = document.getElementById('chatMessages');
            input.disabled = true;
            document.getElementById('chatSendBtn').disabled = true;
            bubble.querySelector('.chat-continue')?.remove();
            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ messages: chatHistory, continue_reply: cont })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || res.status);
                let replyText = data.reply || 'No response returned.';
                if (cont) {
                    const last = chatHistory[chatHistory.length - 1];
                    last.content = last.content + (/^[\s.,;:!?)]/.test(replyText) ? '' : ' ') + replyText;
                    replyText = last.content;
                } else {
                    chatHistory.push({ role: 'assistant', content: replyText });
                }
                bubble.innerHTML = `
                    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;">
                        <strong style="color: var(--accent-purple);">Dr. Gemma:</strong>
                        <span style="font-size: 11px; color: var(--text-muted);">${esc(data.engine || 'Local')}</span>
                    </div>
                    <div>${formatAiReply(replyText)}</div>
                `;
                if (data.truncated) {
                    const btn = document.createElement('button');
                    btn.className = 'prompt-chip chat-continue';
                    btn.style.marginTop = '8px';
                    btn.textContent = '… cut off. Continue';
                    btn.onclick = () => {
                        btn.disabled = true;
                        btn.textContent = 'Continuing...';
                        askGemma(bubble, true);
                    };
                    bubble.appendChild(btn);
                }
            } catch (err) {
                const errDiv = document.createElement('div');
                errDiv.style.color = 'var(--accent-red)';
                errDiv.textContent = 'Error consulting Dr. Gemma: ' + String(err.message || err);
                if (cont) bubble.appendChild(errDiv); else bubble.replaceChildren(errDiv);
            } finally {
                input.disabled = false;
                document.getElementById('chatSendBtn').disabled = false;
                input.focus();
                msgContainer.scrollTop = msgContainer.scrollHeight;
            }
        }


        /* Clean High-DPI Canvas Chart Renderer */
        function drawTimeSeriesChart(dataPoints, color1, color2, label1, label2, color3 = null) {
            const canvas = document.getElementById('modalChartCanvas');
            const ctx = canvas.getContext('2d');
            const dpr = window.devicePixelRatio || 1;
            canvas.width = canvas.parentElement.clientWidth * dpr;
            canvas.height = canvas.parentElement.clientHeight * dpr;
            ctx.scale(dpr, dpr);

            const w = canvas.parentElement.clientWidth;
            const h = canvas.parentElement.clientHeight;

            ctx.clearRect(0, 0, w, h);

            if (!dataPoints || dataPoints.length === 0) {
                drawEmptyChart("No data points available");
                return;
            }

            const padLeft = 45;
            const padRight = 20;
            const padTop = 20;
            const padBottom = 30;
            const plotW = w - padLeft - padRight;
            const plotH = h - padTop - padBottom;

            // Background grid
            ctx.strokeStyle = '#1a2234';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {
                const y = padTop + (plotH / 4) * i;
                ctx.beginPath(); ctx.moveTo(padLeft, y); ctx.lineTo(w - padRight, y); ctx.stroke();
            }

            // Calculate min/max for primary series
            const vals1 = dataPoints.map(d => d.val1);
            let min1 = Math.min(...vals1);
            let max1 = Math.max(...vals1);
            if (min1 === max1) { min1 -= 5; max1 += 5; }
            const range1 = max1 - min1;

            // Y-axis labels
            ctx.fillStyle = '#64748b';
            ctx.font = '11px sans-serif';
            ctx.textAlign = 'right';
            for (let i = 0; i <= 4; i++) {
                const y = padTop + (plotH / 4) * i;
                const v = max1 - (range1 / 4) * i;
                ctx.fillText(v.toFixed(0), padLeft - 8, y + 4);
            }

            // Single Data Point handling
            if (dataPoints.length === 1) {
                const pt = dataPoints[0];
                const y1 = padTop + plotH / 2;
                const x = padLeft + plotW / 2;

                // Baseline
                ctx.strokeStyle = color1;
                ctx.setLineDash([4, 4]);
                ctx.beginPath(); ctx.moveTo(padLeft, y1); ctx.lineTo(w - padRight, y1); ctx.stroke();
                ctx.setLineDash([]);

                // Marker
                ctx.fillStyle = color1;
                ctx.beginPath(); ctx.arc(x, y1, 6, 0, Math.PI * 2); ctx.fill();
                ctx.strokeStyle = '#fff';
                ctx.lineWidth = 2; ctx.stroke();

                // Value callout
                ctx.fillStyle = '#f8fafc';
                ctx.font = 'bold 12px sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText(`${pt.val1.toFixed(1)} ${label1}`, x, y1 - 14);

                // Date label
                ctx.fillStyle = '#94a3b8';
                ctx.font = '11px sans-serif';
                ctx.fillText(pt.time.toISOString().substring(0, 10), x, h - 8);
                return;
            }

            // Multi-point time mapping
            const tStart = dataPoints[0].time.getTime();
            const tEnd = dataPoints[dataPoints.length - 1].time.getTime();
            const tRange = Math.max(tEnd - tStart, 1);

            const getX = (t) => padLeft + ((t.getTime() - tStart) / tRange) * plotW;
            const getY1 = (v) => padTop + plotH - ((v - min1) / range1) * plotH;

            // Plot primary series curve
            ctx.strokeStyle = color1;
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            dataPoints.forEach((d, i) => {
                const x = getX(d.time);
                const y = getY1(d.val1);
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });
            ctx.stroke();

            // Draw markers
            dataPoints.forEach(d => {
                const x = getX(d.time);
                const y = getY1(d.val1);
                ctx.fillStyle = color1;
                ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
                ctx.strokeStyle = '#070a13';
                ctx.lineWidth = 2; ctx.stroke();
            });

            // Plot secondary series if present (val2)
            if (dataPoints[0].val2 !== undefined) {
                const vals2 = dataPoints.map(d => d.val2);
                let min2 = Math.min(...vals2);
                let max2 = Math.max(...vals2);
                if (min2 === max2) { min2 -= 2; max2 += 2; }
                const range2 = max2 - min2;
                const getY2 = (v) => padTop + plotH - ((v - min2) / range2) * plotH;

                ctx.strokeStyle = color2;
                ctx.lineWidth = 2;
                ctx.setLineDash([3, 3]);
                ctx.beginPath();
                dataPoints.forEach((d, i) => {
                    const x = getX(d.time);
                    const y = getY2(d.val2);
                    if (i === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                });
                ctx.stroke();
                ctx.setLineDash([]);
            }

            // Plot tertiary series if present (val3, e.g. pulse)
            const points3 = dataPoints.filter(d => d.val3 > 0);  // readings without a pulse are skipped
            if (color3 && points3.length) {
                const vals3 = points3.map(d => d.val3);
                let min3 = Math.min(...vals3);
                let max3 = Math.max(...vals3);
                if (min3 === max3) { min3 -= 5; max3 += 5; }
                const range3 = max3 - min3;
                const getY3 = (v) => padTop + plotH - ((v - min3) / range3) * plotH;

                ctx.strokeStyle = color3;
                ctx.lineWidth = 1.8;
                ctx.beginPath();
                points3.forEach((d, i) => {
                    const x = getX(d.time);
                    const y = getY3(d.val3);
                    if (i === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                });
                ctx.stroke();
            }

            // X-axis date labels
            ctx.fillStyle = '#64748b';
            ctx.font = '11px sans-serif';
            ctx.textAlign = 'left';
            ctx.fillText(dataPoints[0].time.toISOString().substring(5, 10), padLeft, h - 8);
            ctx.textAlign = 'right';
            ctx.fillText(dataPoints[dataPoints.length - 1].time.toISOString().substring(5, 10), w - padRight, h - 8);
        }

        function drawEmptyChart(msg) {
            const canvas = document.getElementById('modalChartCanvas');
            const ctx = canvas.getContext('2d');
            const dpr = window.devicePixelRatio || 1;
            canvas.width = canvas.parentElement.clientWidth * dpr;
            canvas.height = canvas.parentElement.clientHeight * dpr;
            ctx.scale(dpr, dpr);
            const w = canvas.parentElement.clientWidth;
            const h = canvas.parentElement.clientHeight;

            ctx.clearRect(0, 0, w, h);
            ctx.fillStyle = '#64748b';
            ctx.font = '13px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(msg, w / 2, h / 2);
        }

        /* Existing Live ECG rendering */
        function drawGridOnly() {
            const canvas = document.getElementById('ecgCanvas');
            const ctx = canvas.getContext('2d');
            canvas.width = canvas.parentElement.clientWidth * window.devicePixelRatio;
            canvas.height = canvas.parentElement.clientHeight * window.devicePixelRatio;
            ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
            const w = canvas.parentElement.clientWidth;
            const h = canvas.parentElement.clientHeight;

            ctx.fillStyle = '#070a13';
            ctx.fillRect(0, 0, w, h);
            ctx.strokeStyle = '#1e293b';
            ctx.lineWidth = 1;

            const grid = 20;
            for (let x = 0; x < w; x += grid) {
                ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
            }
            for (let y = 0; y < h; y += grid) {
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }

            ctx.strokeStyle = '#38bdf8';
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(0, h / 2);
            ctx.lineTo(w, h / 2);
            ctx.stroke();
        }

        function drawEcg(samples) {
            const canvas = document.getElementById('ecgCanvas');
            const ctx = canvas.getContext('2d');
            canvas.width = canvas.parentElement.clientWidth * window.devicePixelRatio;
            canvas.height = canvas.parentElement.clientHeight * window.devicePixelRatio;
            ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
            const w = canvas.parentElement.clientWidth;
            const h = canvas.parentElement.clientHeight;

            ctx.fillStyle = '#070a13';
            ctx.fillRect(0, 0, w, h);
            ctx.strokeStyle = '#1e293b';
            ctx.lineWidth = 1;

            const grid = 20;
            for (let x = 0; x < w; x += grid) {
                ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
            }
            for (let y = 0; y < h; y += grid) {
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }

            if (!samples || samples.length === 0) return;

            const displaySamples = samples.slice(0, Math.min(samples.length, 125 * 5)); // 5 seconds of ECG
            ctx.strokeStyle = '#38bdf8';
            ctx.lineWidth = 2;
            ctx.beginPath();

            const midY = h / 2;
            const scaleY = h / 3.0; // mV scale
            const stepX = w / (displaySamples.length - 1);

            for (let i = 0; i < displaySamples.length; i++) {
                const x = i * stepX;
                const y = midY - (displaySamples[i] * scaleY);
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }
            ctx.stroke();
        }

        function cycleTheme() {
            const current = localStorage.getItem('uniscale-theme') || 'system';
            const next = current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system';
            localStorage.setItem('uniscale-theme', next);
            applyTheme(next);
        }

        function applyTheme(theme) {
            const isDark = theme === 'dark' || (theme === 'system' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
            if (isDark) {
                document.documentElement.removeAttribute('data-theme');
            } else {
                document.documentElement.setAttribute('data-theme', 'light');
            }
            const textEl = document.getElementById('theme-text');
            if (textEl) {
                textEl.textContent = theme === 'system' ? 'System' : theme === 'light' ? 'Light' : 'Dark';
            }
        }

        applyTheme(localStorage.getItem('uniscale-theme') || 'system');

        const IMPORT_SOURCES = {
            'lab': { accept: '.pdf,.json', label: 'Lab report (.pdf or .json)',
                     hint: 'The PDF Quest emails you or downloads from MyQuest, or a lab JSON export.' },
            'dna': { accept: '.zip,.txt,.csv', label: 'Your DNA file (.zip, .txt or .csv)',
                     hint: 'A Promethease report zip, or the raw data download from AncestryDNA, 23andMe or MyHeritage.' },
            'garmin': { accept: '.zip,.fit', label: 'Garmin export (.zip) or workout (.fit)',
                        hint: 'The full "Export Your Data" zip from Garmin Connect brings in weights, daily activity, workouts and sleep/HRV. Large exports can take several minutes.' },
            'csv': { accept: '.csv', label: 'CSV export (.csv)',
                     hint: 'Body composition exported from the Fitdays app, or blood pressure exported from Omron Connect. The format is detected automatically.' },
            'vesync': { accept: '.json', label: 'VeSync history (.json)',
                        hint: 'Weigh-in history scraped from the VeSync scale app.' },
            'google-fit': { accept: '.zip', label: 'Google Takeout (.zip)',
                            hint: 'A Google Takeout zip that includes Fit: daily activity, weights and blood pressure.' },
        };

        function updateImportSource() {
            const src = IMPORT_SOURCES[document.getElementById('importSource').value];
            const input = document.getElementById('importFile');
            input.value = '';
            input.accept = src.accept;
            document.getElementById('importFileLabel').textContent = src.label;
            document.getElementById('importHint').textContent = src.hint;
            document.getElementById('importDnaExtras').style.display =
                document.getElementById('importSource').value === 'dna' ? '' : 'none';
        }

        const pct = v => (v === null || v === undefined) ? '—' : `${v}%`;

        let profileBirthYear = null;

        // Your name (or, in multi-user mode, your user id) in the header and Dr. Gemma's greeting.
        let signedInUser = null;
        function showProfileName(name) {
            const badge = document.getElementById('user-badge');
            const label = name || signedInUser;
            badge.textContent = label ? '👤 ' + label : '';
            badge.hidden = !label;
            document.getElementById('chat-greeting-name').textContent = name ? ', ' + name : '';
        }

        async function openProfileDialog() {
            const dlg = document.getElementById('profileDialog');
            const st = dlg.querySelector('.import-status');
            st.className = 'import-status';
            st.textContent = '';
            try {
                const p = await (await fetch('/api/profile')).json();
                document.getElementById('profName').value = p.name ?? '';
                document.getElementById('profDob').value = p.date_of_birth ?? '';
                document.getElementById('profDob').max = new Date().toISOString().slice(0, 10);
                const hint = document.getElementById('profDobHint');
                profileBirthYear = p.birth_year ?? null;
                hint.hidden = !(p.birth_year && !p.date_of_birth);
                hint.textContent = hint.hidden ? '' : `Only the birth year (${p.birth_year}) is on file; add the full date for an exact age.`;
                document.getElementById('profSex').value = p.sex ?? '';
                document.getElementById('profSmoker').checked = !!p.smoker;
                document.getElementById('profDiabetic').checked = !!p.diabetic;
                document.getElementById('profBpTreated').checked = !!p.bp_treated;
                document.getElementById('profCounterTitle').value = p.counter?.title ?? '';
                document.getElementById('profCounterElements').value = (p.counter?.elements ?? []).join(', ');
            } catch (err) {
                st.classList.add('err');
                st.textContent = 'Could not load profile: ' + err.message;
            }
            dlg.showModal();
        }

        // The counter set from the profile form: null (no tile) when the title or buttons are blank.
        function profileCounter() {
            const title = document.getElementById('profCounterTitle').value.trim();
            const elements = document.getElementById('profCounterElements').value.split(',').map(e => e.trim()).filter(Boolean);
            return title && elements.length ? { title, elements } : null;
        }

        async function saveProfile(event) {
            event.preventDefault();
            const st = event.target.querySelector('.import-status');
            const dob = document.getElementById('profDob').value;
            const body = {
                name: document.getElementById('profName').value.trim() || null,
                date_of_birth: dob || null,
                birth_year: dob ? null : profileBirthYear,  // keep a year-only entry until a full date is given
                sex: document.getElementById('profSex').value || null,
                smoker: document.getElementById('profSmoker').checked,
                diabetic: document.getElementById('profDiabetic').checked,
                bp_treated: document.getElementById('profBpTreated').checked,
                counter: profileCounter(),
            };
            st.className = 'import-status';
            try {
                const res = await fetch('/api/profile', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || res.status));
                st.classList.add('ok');
                st.textContent = 'Saved. Biological age and ASCVD risk are recalculated.';
                fetchCounter().then(renderCounterCard).catch(() => {});
                showProfileName(data.name);
                loadSummary();
            } catch (err) {
                st.classList.add('err');
                st.textContent = 'Save failed: ' + err.message;
            }
        }

        function openImportDialog() {
            const dlg = document.getElementById('importDialog');
            const source = document.getElementById('importSource').value;
            dlg.querySelector('form').reset();
            document.getElementById('importSource').value = source;  // remember the last choice
            updateImportSource();
            const st = dlg.querySelector('.import-status');
            st.className = 'import-status';
            st.textContent = '';
            dlg.showModal();
        }

        function describeImport(source, data) {
            if (source === 'lab') return `Imported ${data.total_markers} biomarkers from ${data.lab_name} (${(data.timestamp || '').slice(0, 10)}).`;
            if (source === 'dna') return `Imported ${Number(data.snps_found || 0).toLocaleString()} SNPs into your genomic profile.`;
            const parts = Object.entries(data)
                .filter(([, v]) => typeof v === 'number' && v > 0)
                .map(([k, v]) => `${v.toLocaleString()} ${k.replace(/_found$/, '').replace(/_/g, ' ')}`);
            return `Imported ${parts.join(', ')}.`;
        }

        async function submitImport(event) {
            event.preventDefault();
            const form = event.target;
            const source = document.getElementById('importSource').value;
            const st = form.querySelector('.import-status');
            const btn = form.querySelector('button[type="submit"]');
            btn.disabled = true;
            st.className = 'import-status';
            st.textContent = 'Importing… large files can take a few minutes.';
            try {
                const res = await fetch(`/api/import/${source}`, { method: 'POST', body: new FormData(form) });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || `HTTP ${res.status}`);
                    throw new Error(detail);
                }
                st.classList.add('ok');
                st.textContent = describeImport(source, data);
                loadSummary();
            } catch (err) {
                st.classList.add('err');
                st.textContent = 'Import failed: ' + err.message;
            } finally {
                btn.disabled = false;
            }
        }

        // Show whose vault this is: the profile name, else (multi-user mode) the user id.
        Promise.all([
            fetch('/api/config').then(r => r.json()).catch(() => ({})),
            fetch('/api/profile').then(r => r.json()).catch(() => ({})),
        ]).then(([cfg, prof]) => {
            signedInUser = cfg.user || null;
            showProfileName(prof.name);
        });

        // Installable app: the service worker only keeps the page and icons (never /api data) for offline start.
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(() => {});
        }

        loadSummary();
        loadHistory();
        loadPatterns();
        setInterval(loadPatterns, 10 * 60 * 1000);  // reads the whole vault, so not on the 15 s refresh

        // Hide the chat entry points when the hub has no assistant; show its configured name.
        fetch('/api/assistant').then(r => r.json()).then(a => {
            document.body.classList.toggle('no-assistant', !a.available);
            document.querySelectorAll('.assistant-name').forEach(el => { el.textContent = a.name; });
        }).catch(() => document.body.classList.add('no-assistant'));

        setInterval(() => {
            loadSummary();
            loadHistory();
        }, 15000);
    </script>
</body>
</html>
"""
