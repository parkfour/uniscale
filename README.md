# UniScale

**UniScale** is a unified, offline-first personal health hub, BLE bridge, and telemetry vault. It liberates your health data from vendor clouds (Garmin Connect, Google Fit, Yunmai, Fitdays, VeSync) by interfacing directly with your devices over Bluetooth Low Energy, storing metrics locally in an open format, and providing a self-hosted web dashboard.

---

## Key Capabilities

* **Multi-Scale Bluetooth Protocol Engine:**
  * **Yunmai Mini (M1US):** Automatic `0x10` handshake packet (byte 15 = `0x02`) on connect to **force physical scale LCD to display in imperial pounds** instead of reverting to metric. Decodes weight and bioimpedance.
  * **Fitdays (Icomon / Chipsea PCB):** Decodes broadcast and notify GATT packets on Service `0xFFB0` for `PCB` and AAA-series boards.
  * **VeSync / Etekcity (ESF series):** Decodes weight and impedance for ESF-24, ESF-551, and FIT-8S models on Service `0xFFF0`.
* **Blood Pressure & Real-Time ECG:**
  * **Viatom / Wellue BP2:** Blood pressure (Systolic, Diastolic, Pulse) and continuous **125 Hz Lead-I analog ECG waveform streaming** with real-time cardiac rhythm rendering.
  * **Omron Cuff:** Standard Bluetooth SIG IEEE-11073 SFLOAT parsing on Service `0x1810`.
* **Garmin Watch Offline Integration:**
  * Connects your **Garmin watch** directly to your phone via **Gadgetbridge** over BLE.
  * Reads sleep (the watch's own score, stages, overnight HRV, SpO2), workout summaries and daily steps and
    distance from Gadgetbridge's auto-exported database, with no cloud and no Google Health Connect in between.
  * The app doesn't use Health Connect at all: readings stay between your devices, the phone and your hub.
* **Configurable Self-Hosted Service:**
  * Self-contained web dashboard (dark cardiac theme, responsive charts, live ECG waveform canvas).
  * Binds to `127.0.0.1` by default; an optional shared `api_token` protects every route (Bearer token for the apps, HTTP Basic password in the browser).
* **Complete Account Liberation & Migration:**
  * Importers for Garmin bulk data archives (`.FIT` workouts and daily health summaries) and the watch's own sleep/HRV `.FIT` files.
  * Importer for the full Google Fit Takeout (`.zip` read in place): daily summaries with heart rate, weights, blood pressure, sleep and workouts.

---

## Architecture

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│ Yunmai /     │      │ Viatom BP2 / │      │ Garmin watch │
│ Fitdays /    │      │ Omron cuff   │      └──────┬───────┘
│ VeSync scale │      │ (BP + ECG)   │             │ BLE
└──────┬───────┘      └──────┬───────┘             ▼
       │ BLE (auto-lbs fix)  │ BLE (125 Hz ECG)  ┌──────────────────────┐
       └──────────┬──────────┘                   │ Gadgetbridge         │
                  │                              │ (auto-exported DB)   │
                  ▼                              └──────────┬───────────┘
┌─────────────────────────────────────────┐                 │ file read
│ Android app (Compose)                   │◀────────────────┘ (sleep, workouts,
│ tiles, live ECG, supplement checklist,  │                    steps, distance;
│ Ask Dr. Gemma                           │                    background job)
│   readings kept on the phone until the  │
│   hub has them (no Health Connect)      │
└────────────────────┬────────────────────┘
                     │ HTTPS: /api/sync/*, supplements, chat
                     ▼
┌──────────────────────────────────────────────────────────┐
│ Self-hosted hub (FastAPI)                                │
│   vault (measurements.jsonl), web dashboard,             │
│   biological age, supplement protocol                    │
│                                    ◀── importers: Garmin │
│                                        archive, Google   │
│                                        Fit Takeout, lab  │
│                                        PDFs, DNA         │
│        │                                                 │
│        ▼                                                 │
│   local LLM "Dr. Gemma" (optional; GET /api/assistant)   │
└──────────────────────────────────────────────────────────┘
```

**Without a phone:** on a Linux machine within Bluetooth range of the scale or cuff,
`uniscale daemon` (Bleak) listens for the same devices and writes readings into that
machine's local vault, and also posts them to a hub when `[sync] remote_hub_url` is set.

---

## Configuration System

UniScale uses a declarative configuration file (`config.toml`).

### 1. Initialize Configuration
Generate a starter configuration file with default settings:
```bash
# Creates ~/.config/uniscale/config.toml (or specify custom path)
uv run uniscale config init
```

### 2. Configuration Schema (`uniscale.example.toml`)

```toml
# ==============================================================================
# UniScale Configuration File
# Unified offline-first health bridge & telemetry hub
# ==============================================================================

[general]
# Logging verbosity: DEBUG, INFO, WARNING, ERROR
log_level = "INFO"

# Base directory for local metrics, JSONL files, and ECG recordings
data_dir = "~/.config/uniscale"

# Storage format. Only "jsonl" is implemented; "sqlite" is reserved.
storage_backend = "jsonl"


[server]
# Interface and port for the self-hosted web dashboard & sync API.
# Loopback by default (put a reverse proxy in front). If you bind 0.0.0.0,
# set api_token too.
host = "127.0.0.1"
port = 8088

# Optional external URL (if hosted behind a reverse proxy / domain)
external_url = "https://health.example.com"

# Optional shared secret. When set, every route except /health requires it,
# as a Bearer token (Android app, daemon) or as the HTTP Basic password (browser).
# api_token = "your-secure-token-here"


[sync]
# Upstream UniScale hub URL (used when the CLI daemon uploads to a remote server)
remote_hub_url = ""

# Optional client certificate for an upstream server that requires one
client_cert = ""
client_key = ""
ca_cert = ""
api_token = ""


[devices]
# Driver switches are honored by `uniscale daemon`. auto_scan, force_pounds, auto_tare and
# [[devices.known]] are parsed but not acted on yet (the Yunmai pounds handshake is always sent).
auto_scan = true

# Enable or disable specific hardware drivers
enable_yunmai = true
enable_fitdays = true
enable_vesync = true
enable_viatom_bp2 = true
enable_omron = true

[devices.yunmai]
# Send 0x02 handshake to force physical LED display to pounds instead of kg
force_pounds = true

[devices.fitdays]
auto_tare = true

# Optional: Specific known/paired devices to whitelist or alias
# [[devices.known]]
# name = "Primary Bathroom Scale"
# address = "AA:BB:CC:DD:EE:01"
# type = "yunmai"

# [[devices.known]]
# name = "Desk BP & ECG Cuff"
# address = "AA:BB:CC:DD:EE:02"
# type = "viatom_bp2"


[ai]
# OpenAI-compatible LLM server (Ollama, llama.cpp, ...) for the chat assistant. The prompt carries your whole
# health profile, so keep this on a machine you control.
endpoint = "http://localhost:11434"
model = "gemma:latest"
# context_tokens = 8192  # the server's context per request (llama.cpp -c per slot); 16384+ sends 90 days of daily log


[profile]
# Needed for chronological age in PhenoAge / ASCVD calculations
# birth_year = 1980


# Daily supplement regimen tracked by the dashboard checklist (optional)
# [[supplements.stack]]
# name = "Vitamin D3"
# dosage = 2000
# unit = "IU"
# category = "vitamin"
# frequency = "morning"
# purpose = "Example entry"


[owntracks]
# Optional OwnTracks Recorder: finished days become "places > 1 hr" on the dashboard
# recorder_url = "https://location.example.com/owntracks"
# username = ""
# password = ""


[legacy_cloud.garmin]
# Optional legacy sync to Garmin Connect cloud (disabled by default for full privacy)
enabled = false
email = ""
password = ""
```

### 3. Inspect Active Configuration
```bash
uv run uniscale config show
```

---

## CLI & Daemon Usage

### 1. Requirements & Setup
Using modern `uv`:
```bash
git clone https://github.com/your-org/uniscale.git
cd uniscale
uv sync
```

### 2. Available Commands

* **Scan for Nearby Health Hardware:**
  ```bash
  uv run uniscale scan --timeout 15
  ```

* **Run the Background BLE Daemon:**
  Listens continuously for scales and monitors, applies the imperial pound fix to Yunmai, logs measurements to the local vault, and forwards them to a hub if `[sync] remote_hub_url` is set:
  ```bash
  uv run uniscale daemon
  ```

* **Run the Self-Hosted Web Dashboard & Sync API:**
  ```bash
  uv run uniscale serve --port 8088
  ```

* **Inspect Measurement History:**
  ```bash
  uv run uniscale history
  ```

* **More:** `profile`, `supplement`, `chat`, `analyze`, `genomics`, `import-lab`, `import-csv`,
  `import-vesync`, `import-sleep` and `garmin-test`; see `uv run uniscale --help`.

---

## Data Migration & Importing

Before deleting third-party cloud accounts, import your complete history into UniScale:

### 1. Garmin Data Export (`.FIT` files & summaries)
1. Request your bulk archive from [Garmin Account Data Request](https://www.garmin.com/account/datarequest).
2. Ingest all workouts and daily summaries:
   ```bash
   uv run uniscale import-garmin /path/to/extracted/garmin_export/
   # Or import a single activity file directly (.fit or .gpx):
   uv run uniscale import-garmin /path/to/activity.fit
   ```
   Workouts keep running dynamics (stride, ground contact time and balance, vertical oscillation and ratio,
   power), laps as splits, the heart-rate curve and the GPS track. A single `.fit` or `.gpx` can also be
   uploaded from the dashboard's Workouts card.

### 2. Google Fit Takeout
1. Request an archive from [Google Takeout](https://takeout.google.com/) (select only Google Fit).
2. Import it (the `.zip` as downloaded; records you already have are skipped and listed in
   `google_fit_already_there.jsonl` next to your vault):
   ```bash
   uv run uniscale import-google-fit ~/Downloads/takeout-*.zip
   ```

### 3. On-Device Vendor Databases (Yunmai & ViHealth)
Extract DevLogs and AI ECG recordings directly from a connected Android phone over ADB:
```bash
uv run uniscale import-adb
```

---

## Deployment

The service binds to `127.0.0.1:8088` by default. For access from other devices,
put it behind a reverse proxy that terminates HTTPS and forwards to that address,
and set `server.api_token` so every route except `/health` requires the shared secret.

### Systemd Service (`/etc/systemd/system/uniscale.service`)

```ini
[Unit]
Description=UniScale Personal Health Hub & Vault
After=network.target

[Service]
Type=simple
User=uniscale
Group=www-data
WorkingDirectory=/opt/uniscale
Environment=PYTHONPATH=/opt/uniscale
Environment=UNISCALE_CONFIG=/opt/uniscale/config.toml
ExecStart=/opt/uniscale/venv/bin/uvicorn uniscale.server:app --host 127.0.0.1 --port 8088
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Android Mobile App (`android/`)

Located in `android/`, built with:
* **Jetpack Compose + Material3**
* **Direct BLE Scanner & Handshake Router**
* **Real-Time 125 Hz ECG Canvas**
* **Gadgetbridge Auto-Export Reader:** In Settings → Data, tap **Watch** and pick Gadgetbridge's auto-export file; nightly sleep, workout summaries and daily steps and distance sync to the hub from it, also in the background (Settings → Background Sync: Off, 15m, 30m, 1h, 2h or 4h, which also sends the phone's weigh-ins and BP readings and queued supplement ticks).
* **No Health Connect:** the app neither reads nor writes Android Health Connect. Weigh-ins and BP readings are kept in the app's private storage until the hub has them; the Weight and BP tiles show those plus the hub's history.
* **Declarative Mobile Configuration (`uniscale_config.json`):**
  * **In-App Settings Dialog:** Tap the gear icon in the top app bar to configure upstream Hub URL, API bearer token, auto-sync switches, and hardware driver toggles. Includes an instant "Test Hub Connection" latency ping.
  * **Portable JSON Configuration:** Export active app configuration to `/sdcard/Download/uniscale_config.json` or import pre-existing configuration files with a single tap.
  * **Bundled Defaults:** Default configuration ships in `app/src/main/assets/uniscale_config.json`.

---

## License

MIT License. Free and open source.
