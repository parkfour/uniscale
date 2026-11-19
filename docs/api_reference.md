# UniScale REST API & Data Models Reference

This document provides a technical reference for the UniScale REST API, data models, and local storage formats.

---

## 1. REST API Endpoints

The UniScale service exposes a lightweight, async REST API built with FastAPI. All endpoints default to returning JSON.

**Authentication:** when `server.api_token` is set, every route except `/health` requires it,
either as `Authorization: Bearer <token>` (Android app, daemon, scripts) or as the password of
HTTP Basic auth (any username), which browsers prompt for when opening the dashboard. Without a
token the API is open to anything that can reach it, so leave `server.host` at `127.0.0.1` unless
a token is set.

### System & Health

#### `GET /health`
Returns the operational health and reachability of the service.
* **Response `200 OK`:**
  ```json
  {
    "status": "healthy",
    "timestamp": "2026-11-07T12:00:00Z",
    "version": "0.4.0",
    "records_count": 1420
  }
  ```

#### `GET /api/config`
Returns safe, sanitized server configuration settings.
* **Response `200 OK`:**
  ```json
  {
    "storage_backend": "jsonl",
    "log_level": "INFO",
    "auto_scan": true
  }
  ```

---

### Dashboard & History

#### `GET /api/summary`
Aggregates the latest recorded value for each metric category (latest weight, latest blood pressure, latest ECG, today's activity, latest workout, recent sleep, supplement adherence, and biological PhenoAge).
* **Response `200 OK`:**
  ```json
  {
    "latest_weight": {
      "weight_lb": 164.2,
      "weight_kg": 74.5,
      "body_fat_percent": 14.8,
      "timestamp": "2026-11-07T08:15:00Z"
    },
    "latest_bp": {
      "systolic": 118,
      "diastolic": 76,
      "pulse": 62,
      "timestamp": "2026-11-07T08:20:00Z"
    },
    "today_activity": {
      "steps": 8420,
      "distance_km": 6.35,
      "calories": 480
    },
    "longevity": {
      "pheno_age": 32.4,
      "chronological_age": 36.0,
      "age_acceleration": -3.6
    }
  }
  ```

#### `GET /api/history`
Returns historical records filtered by category and query limits.
* **Query Parameters:**
  * `record_type` (optional): Filter by record type (`weight`, `blood_pressure`, `ecg`, `activity`, `workout`, `blood_test`, `sleep`, `supplement`)
  * `limit` (optional, default `500`, max `5000`): Maximum entries to return
* **Response `200 OK`:** Array of matching record objects.

#### `GET /api/workouts`
Device-tracked workouts for the dashboard's **Last Workout** tile.
* **Query Parameters:** `per_sport` (optional, default 5, max 50): how many recent workouts to return per sport.
* **Response `200 OK`:** the latest workout, and one entry per sport (the sport with the most recent workout
  first) with its total `count` and its `recent` workouts, newest first.
  ```json
  {
    "latest": {"type": "workout", "sport": "running", "timestamp": "2026-11-07T15:00:00Z", "distance_km": 5.0, "...": "..."},
    "sports": [
      {"sport": "running", "count": 12, "recent": [{"sport": "running", "timestamp": "2026-11-07T15:00:00Z", "...": "..."}]},
      {"sport": "walking", "count": 3, "recent": ["..."]}
    ]
  }
  ```
* **One sport's history:** `?sport=running&offset=0&limit=50` (limit max 200) returns that sport's workouts newest
  first, one page at a time, with totals over all of them. The dashboard's Workouts modal uses it for the
  scrollable per-type history.
  ```json
  {"sport": "running", "total": 120, "distance_km": 610.5, "duration_seconds": 216000,
   "first": "2024-03-02T07:10:00Z", "workouts": [{"sport": "running", "...": "..."}]}
  ```

#### `GET /api/activity/today`
Returns aggregated daily steps, distance, and energy expenditure for the current day.

---

### Ingestion & Sync Endpoints

#### `POST /api/sync/weight`
Ingest a new weight and bioimpedance measurement.
* **Status:** `201 Created`
* **Payload:**
  ```json
  {
    "weight_kg": 74.5,
    "weight_lb": 164.2,
    "body_fat_percent": 14.8,
    "bmi": 22.1,
    "impedance": 480,
    "muscle_mass_lb": 134.2,
    "water_percent": 61.2,
    "bone_mass_lb": 7.4,
    "visceral_fat": 5,
    "bmr_kcal": 1720,
    "timestamp": "2026-11-07T08:15:00Z",
    "device_name": "Yunmai Mini",
    "device_address": "00:11:22:33:44:55"
  }
  ```

#### `POST /api/sync/bp`
Ingest a blood pressure reading.
* **Status:** `201 Created`
* **Payload:**
  ```json
  {
    "systolic": 118,
    "diastolic": 76,
    "pulse": 62,
    "timestamp": "2026-11-07T08:20:00Z",
    "device_name": "Viatom BP2",
    "device_address": "AA:BB:CC:DD:EE:FF"
  }
  ```

#### `POST /api/sync/ecg`
Ingest an ECG rhythm recording.
* **Status:** `201 Created`
* **Payload:**
  ```json
  {
    "heart_rate": 64,
    "sample_rate_hz": 125,
    "samples_mv": [-0.05, -0.02, 0.12, 0.85, -0.22, 0.04],
    "duration_seconds": 30.0,
    "rhythm_status": "Normal Sinus Rhythm",
    "timestamp": "2026-11-07T08:21:00Z",
    "device_name": "Viatom BP2",
    "device_address": "AA:BB:CC:DD:EE:FF"
  }
  ```

#### `POST /api/sync/activity`
Ingest daily summary statistics (steps, distance).
* **Status:** `201 Created`
* **Payload:**
  ```json
  {
    "date": "2026-11-07",
    "steps": 9450,
    "distance_km": 7.12,
    "calories": 520,
    "active_minutes": 65,
    "resting_heart_rate": 58,
    "source": "Garmin"
  }
  ```

#### `POST /api/sync/workout`
Ingest a specific workout activity.
* **Status:** `201 Created`
* **Payload:**
  ```json
  {
    "sport": "running",
    "timestamp": "2026-11-07T07:00:00Z",
    "duration_seconds": 1820.0,
    "distance_meters": 5000.0,
    "calories": 380,
    "avg_heart_rate": 152,
    "max_heart_rate": 171,
    "avg_cadence": 81,
    "elevation_gain_m": 25.0,
    "steps": 5200,
    "avg_power": 230,
    "max_power": 300,
    "aerobic_training_effect": 3.2,
    "vo2max": 45.3,
    "device_name": "Garmin"
  }
  ```
  `avg_cadence` uses the FIT convention (strides per minute for running, i.e. steps per minute ÷ 2). Everything
  after `max_heart_rate` is optional, as are these watch extras (sent by the phone app from Gadgetbridge):
  `anaerobic_training_effect`, `training_load`, `recovery_time_hours`, `body_battery_start`, `body_battery_end`,
  `sweat_loss_ml`, `avg_temperature_c`, `ground_contact_ms`, `vertical_oscillation_mm`, `vertical_ratio_percent`,
  `stride_length_m`, `max_cadence`, `elevation_loss_m`, `normalized_power`, `active_calories`, and
  ```json
  {
    "hr_zones": [{"zone": 4, "name": "Threshold", "min_bpm": 150, "seconds": 1500.0}],
    "splits": [{"distance_m": 1000.0, "seconds": 360.0, "avg_hr": 145}],
    "hr_samples": [[0, 100], [60, 135], [120, 150]]
  }
  ```
  `hr_samples` are `[seconds from start, bpm]`. A workout sent again with the same `timestamp` (e.g. with more
  detail) merges with the stored one; the copy with more data wins.

#### `POST /api/sync/lab`
Ingest clinical blood panel markers.
* **Status:** `201 Created`
* **Payload:**
  ```json
  {
    "timestamp": "2026-10-15T09:00:00Z",
    "lab_name": "Example Lab",
    "panel_name": "Comprehensive Metabolic & Lipid Panel",
    "fasting": true,
    "markers": {
      "albumin": {"name": "Albumin", "value": 4.6, "unit": "g/dL", "reference_range": "3.6-5.1"},
      "glucose": {"name": "Glucose", "value": 88.0, "unit": "mg/dL", "reference_range": "65-99"},
      "crp": {"name": "hs-CRP", "value": 0.4, "unit": "mg/L", "reference_range": "<1.0"}
    }
  }
  ```

#### `POST /api/sync/sleep`
Ingest a sleep session record.
* **Status:** `201 Created`
* **Payload:**
  ```json
  {
    "date": "2026-11-07",
    "duration_seconds": 28200,
    "deep_sleep_seconds": 6120,
    "rem_sleep_seconds": 6480,
    "light_sleep_seconds": 14400,
    "awake_seconds": 1200,
    "sleep_score": 88,
    "avg_spo2": 97.2,
    "hrv_rmssd_ms": 58.4,
    "hrv_status": "balanced",
    "device_name": "Garmin"
  }
  ```
  `sleep_score` is the score reported by the watch; a score computed on the phone goes in `estimated_sleep_score`
  instead. The server computes `duration_hours` and the `*_minutes` fields itself. A record with the same date,
  duration and `device_name` as one already stored returns `"status": "already_exists"`. Several records for the
  same night (e.g. phone and watch import) are merged when read; a session under half the night's length is kept
  as a separate nap.

#### `POST /api/sync/sleep/fit` (multipart/form-data)
Ingest one night from the watch's own FIT files (as downloaded by Gadgetbridge). Fields: `sleep_file` (a
`SLEEP_*.fit`) and optional `hrv_file` (the matching `HRV_STATUS_*.fit`). The night is parsed into the same record
as above, with the watch's score, stages and overnight HRV, and stored the same way. `422` if the file can't be parsed.

---

### Supplements & Genomics

#### `GET /api/supplements`
Returns current adherence checklist, active stack, suggestion catalog, and recent intakes.
The checklist has one entry per stack item per slot (`wake`, `breakfast`, `lunch`, `dinner`, `bed`), in day order,
each with `slot`, `taken_today` and `taken_at`; an item with no slots ("as needed") appears once with `slot: null`.
An intake that names a slot ticks that slot; one without a slot ticks the item's earliest open slot.

#### `POST /api/supplements`
Log one intake (used by the phone's checklist). With `timestamp` set the call is idempotent: an intake with the
same name and timestamp is not written twice, so a client can safely resend.
```json
{ "name": "Magnesium Glycinate", "dosage": 200, "unit": "mg", "slot": "bed", "timestamp": "2026-01-01T21:00:00+00:00" }
```

#### `POST /api/supplements/quick-log`
Quickly log intake of a supplement:
```json
{
  "name": "Creatine Monohydrate",
  "dosage": 5.0,
  "unit": "g",
  "category": "supplement",
  "frequency": "daily",
  "slot": "breakfast"
}
```
`slot` is optional.

#### `GET /api/supplements/stack`
Returns the active customized regimen stack and evidence-based suggestion catalog.

#### `PUT /api/supplements/stack`
Save or replace the user's active regimen stack:
```json
{
  "stack": [
    {
      "name": "Magnesium Glycinate",
      "dosage": 400.0,
      "unit": "mg",
      "category": "mineral",
      "frequency": "evening",
      "slots": ["bed"],
      "purpose": "Slow-wave sleep and recovery"
    }
  ]
}
```

#### `POST /api/supplements/stack/item`
Add or update a single compound in the active regimen.

#### `DELETE /api/supplements/stack/item/{name}`
Remove a compound from the active regimen by name (case-insensitive; names may contain `/`). Returns 404 if the name is not in the stack.

#### `POST /api/supplements/intake/delete`
Delete a logged supplement intake event by timestamp and optional name. Only `type: "supplement"` records are matched; `measurements.jsonl` is replaced atomically.

`slots` lists when in the day the item is taken (it can be several); `[]` means as needed. A stack saved
before slots existed gets them from `frequency` (morning → breakfast, evening → dinner, with meals → all three meals).

The edited stack is stored in `supplements_stack.json` next to the measurements database and, once it exists, takes precedence over `[[supplements.stack]]` in the config. Run `uniscale supplement stack --reset` to delete it and fall back to the config again.

#### `GET /api/genomics`
Returns the sovereign genomic interpretation profile (APOE, MTHFR, COMT, metabolic traits).

#### `POST /api/genomics/import`
Upload raw DNA text dump (`.txt`) for immediate processing.

#### `GET /api/profile` / `PUT /api/profile`
The signed-in user's profile, stored as `profile.json` next to their vault:
```json
{"name": "Alex", "date_of_birth": "1980-06-15", "birth_year": 1980, "sex": "female",
 "smoker": false, "diabetic": false, "bp_treated": false,
 "counter": {"title": "D Tracker", "elements": ["D(out)", "D(in)"]}}
```
`counter` defines the daily counter tile (up to 8 distinct buttons of at most 20 characters); `null` means no tile.
`name` is shown in the dashboard header and Dr. Gemma's greeting. With `date_of_birth` set, ages are exact on each
lab date and `birth_year` follows it; `birth_year` alone still works for anyone who prefers not to give the full
date. `sex` is `"male"`, `"female"` or `null`; 10-year ASCVD risk is `null` until it is set. `PUT` replaces the
whole profile (`422` for a malformed `counter`, a date of birth in the future or before 1900, an out-of-range `birth_year`, or an
unknown `sex`). In single-user mode, `[profile]`
in `config.toml` supplies defaults for fields the saved profile doesn't set.

#### `GET /api/counter` / `POST /api/counter`
The profile's counter set and today's counts (`?date=YYYY-MM-DD` for another day). Each element has a `key` made
from its label (`"D(out)"` → `"d_out"`); counts, totals and history are keyed by it:
```json
{"title": "D Tracker", "elements": [{"key": "d_out", "label": "D(out)"}, {"key": "d_in", "label": "D(in)"}],
 "date": "2026-11-17", "counts": {"d_out": 2, "d_in": 1}, "totals": {"d_out": 40, "d_in": 31},
 "daily_history": [{"date": "2026-11-17", "counts": {"d_out": 2, "d_in": 1}, "total": 3}], "history": [...]}
```
`POST {"item": "D(out)", "delta": 1}` taps one counter (by key or label; `delta` defaults to 1, counts never go
below 0); `POST {"counts": {"d_out": 3}, "date": "2026-11-10"}` sets a day. Unknown counters are a `422`.
`GET /api/counter/history?days=30` returns the daily history; `POST /api/counter/history {"entries": [{"date": ..., "counts": {...}}]}` imports past days.

#### `POST /api/import/dna` (multipart/form-data)
Used by the dashboard's **📥 Import** dialog (DNA). Fields:
* `file` (required): a Promethease report `.zip`, or AncestryDNA / 23andMe / MyHeritage raw data (`.zip`, `.txt`, `.csv`).
* `father`, `mother` (optional): parents' Promethease report zips, for trio phasing.
* `ancestry` (optional): AncestryDNA raw data zip, merged with a Promethease `file`.

Returns `201` with `snps_found` and `trio_status`; `422` if no genotypes can be read (nothing is saved). Max 200 MB per file.

#### `POST /api/import/lab` (multipart/form-data)
Used by the **📥 Import** dialog (Lab results). Field `file`: a Quest Diagnostics PDF report (requires `pdftotext` from poppler-utils on the server) or a `BloodTestMeasurement` JSON file. Returns `201` with `total_markers`, `lab_name` and `timestamp`; `422` if the report can't be parsed.

#### `POST /api/import/{source}` (multipart/form-data)
Device and app exports, also used by the **📥 Import** dialog. Field `file`; `source` is one of:

| `source` | File | Imports |
| :--- | :--- | :--- |
| `garmin` | Garmin Connect "Export Your Data" `.zip`, a single `.fit` workout, or a Gadgetbridge `SLEEP_*.fit` | weights, daily activity, workouts, sleep/HRV |
| `csv` | Fitdays or Omron Connect `.csv` (detected from content) | body composition or blood pressure |
| `vesync` | VeSync scraped history `.json` | body composition |
| `google-fit` | Google Takeout `.zip` | daily activity (steps, calories, avg/min/max/resting heart rate, SpO2), weights, blood pressure, sleep, workouts |

Returns `201` with per-type counts (e.g. `weights_found`, `sleep_records_found`); `422` if nothing usable was found; `404` for an unknown source. Limits: 4 GB for `garmin` / `google-fit`, 200 MB otherwise; if a reverse proxy sits in front, its body-size limit (nginx `client_max_body_size`, Apache `LimitRequestBody`) must allow this too. Uploads are staged in a hidden `.import-*` folder inside the data directory and removed afterwards.

All imports, from the dashboard or the CLI, write to the same vault: `UNISCALE_DB_PATH` if set, otherwise `measurements.jsonl` in `[general] data_dir`.

---

### Local AI Chat

#### `GET /api/assistant`
Whether this hub has a chat assistant, and its name: `{"available": true, "name": "Dr. Gemma"}`. The app and the
dashboard show their chat buttons only when `available` is true. It is true when the config's `[ai]` section sets an
`endpoint` (or `enabled = true`), and false on a fresh install or with `enabled = false`; `name` comes from
`[ai] name` (default "Dr. Gemma").

#### `POST /api/chat`
Returns 404 when no assistant is set up, so nothing is sent to a model.
Query local LLM (via Ollama or custom local endpoint) with automatic clinical context generation from your health vault.
* **Payload:**
  ```json
  {
    "messages": [
      {"role": "user", "content": "How are my blood pressure and resting heart rate trending this week?"}
    ]
  }
  ```
* The LLM endpoint and model come from the `[ai]` section of the server config; requests cannot choose them.
* The context sent with each chat also carries a day-by-day log (counters, hours away from home and the places
  stayed at, weight, BP, resting HR, sleep, steps) and the correlations the hub computed between them, same day and
  next day, so the model explains real figures rather than working them out. `[ai] context_tokens` (default 8192)
  is the server's context per request; at 16384 or more the log covers 90 days, otherwise 30.

---

## 2. Storage Format (JSON Lines)

UniScale stores records by default in an append-only JSONL file at `~/.config/uniscale/measurements.jsonl`.

### Characteristics:
* **Human-Readable & Line-Delimited:** Every line is a standalone, valid JSON object.
* **Crash-Resilient:** Writes are single-line appends; corrupted lines can be easily audited or trimmed without affecting previous records.
* **Automatic Type Tagging:** Records carry a `"type"` discriminant (`weight`, `blood_pressure`, `ecg`, `activity`, `workout`, `blood_test`, `sleep`, `supplement`, `genomics`).
* **Deduplication on read:** every reader (dashboard, API, Dr. Gemma, longevity, CLI) loads the vault through
  `uniscale.readings.load_vault`, which returns one record per activity day (fields merged), per sleep night, per
  workout (richest copy), and per weight / blood-pressure reading: copies of one reading within 10 minutes with
  nearly the same value (e.g. the live upload and the Health Connect copy) become one record, the most detailed copy
  winning. A blood-pressure pulse of 0 counts as not measured. The file itself is never rewritten.
