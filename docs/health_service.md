# UniScale Health Service & Daemon

The **UniScale Health Service** is a self-hosted, offline-first personal health hub, background telemetry listener, and analytics platform. Built with Python 3.11+, FastAPI, and Bleak, it centralizes health records, analyzes biological longevity markers, tracks supplements and sleep, and powers an interactive web dashboard.

---

## 1. System Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Ingestion Interfaces                            │
│  BLE Daemon       Mobile App REST Sync    Bulk Importers (FIT, Takeout)│
│  (Bleak async)    (FastAPI /api/sync/*)   (Garmin, Google Fit, ADB)    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Local Storage Engine                            │
│           Append-Only JSONL Vault (~/.config/uniscale/)                 │
│              - Fast streaming reads & immutable log                    │
│              - Deduplication & entity merging                          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
       ┌────────────────────────────┼────────────────────────────┐
       ▼                            ▼                            ▼
┌──────────────┐             ┌──────────────┐             ┌──────────────┐
│  Dashboard   │             │  Analytics   │             │   Local AI   │
│  Web UI &    │             │  Longevity,  │             │   Clinical   │
│  REST API    │             │  Sleep & DNA │             │   Assistant  │
└──────────────┘             └──────────────┘             └──────────────┘
```

---

## 2. Configuration System (`config.toml`)

UniScale uses a declarative TOML configuration file located by default at `~/.config/uniscale/config.toml`.

### Initialize Default Configuration
```bash
uv run uniscale config init
```
Or display active configuration:
```bash
uv run uniscale config show
```

### Configuration Schema (`uniscale.example.toml`)

```toml
[general]
# Logging verbosity: DEBUG, INFO, WARNING, ERROR
log_level = "INFO"

# Local data storage directory
data_dir = "~/.config/uniscale"

# Local storage backend: "jsonl" or "sqlite"
storage_backend = "jsonl"


[server]
# Web dashboard & API bind settings (loopback by default; use a reverse proxy,
# or bind 0.0.0.0 together with api_token)
host = "127.0.0.1"
port = 8088

# Public or local network external URL
external_url = "http://localhost:8088"

# Optional shared secret. When set, every route except /health requires it,
# as a Bearer token (apps) or as the HTTP Basic password (browser).
# api_token = "your-secure-token-here"


[sync]
# Upstream UniScale hub URL (used when CLI daemon uploads to a remote server)
remote_hub_url = ""


[devices]
# Automatic BLE scanning and discovery
auto_scan = true

# Toggle individual device hardware drivers
enable_yunmai = true
enable_fitdays = true
enable_vesync = true
enable_viatom_bp2 = true
enable_omron = true

[devices.yunmai]
# Force physical LED display to imperial pounds instead of kg
force_pounds = true

[devices.fitdays]
auto_tare = true

# Optional: Specific known devices to alias or whitelist
# [[devices.known]]
# name = "Bathroom Scale"
# address = "AA:BB:CC:DD:EE:01"
# type = "yunmai"


[legacy_cloud.garmin]
# Optional legacy sync to Garmin Connect cloud (disabled by default)
enabled = false
email = ""
password = ""
```

---

## 3. Background BLE Daemon (`uniscale daemon`)

The background daemon scans continuously for nearby Bluetooth Low Energy health devices without requiring mobile devices or vendor cloud applications:

```bash
uv run uniscale daemon
```

### What the Daemon Does:
1. **Continuous Passive & Active BLE Scanning:** Listens for advertisements using Bleak.
2. **Device Detection:** Automatically identifies Yunmai, Fitdays, VeSync, Viatom BP2, and Omron hardware.
3. **Automatic Imperial Units Fix:** When a Yunmai scale connects, the daemon immediately transmits the `0x10` handshake packet (byte 15 = `0x02`), forcing the scale display to render in pounds (`lb`) rather than kilograms (`kg`).
4. **Local Append & Remote Forwarding:** Appends measurements to `~/.config/uniscale/measurements.jsonl` and, if configured with a `remote_hub_url`, forwards records via REST API.

---

## 4. Web Dashboard & REST Service (`uniscale serve`)

Launch the web dashboard and API server:
```bash
uv run uniscale serve --port 8088
```

Open `http://localhost:8088` in any browser to access the dashboard:
* **Dark Cardiac Aesthetic:** High-contrast responsive interface designed for desktop and mobile browsers.
* **Live ECG Waveform Viewer:** Visualizes 125 Hz Lead-I ECG rhythm recordings.
* **Telemetry Trends:** Interactive charts for body weight, body fat %, muscle mass, blood pressure (Systolic/Diastolic), and resting heart rate.
* **Activity & Workouts:** Daily steps, distance, active minutes, and imported `.FIT` workout tracks.
* **Biological Longevity & Sleep Panels:** Real-time calculation of PhenoAge biological age, ASCVD risk scores, sleep architecture, and overnight HRV.

### Install as an app (PWA)

The dashboard is an installable web app: it gets its own Home Screen icon and opens full-screen, without the
browser's address bar.

* **iPhone / iPad:** open the dashboard in Safari → Share → **Add to Home Screen**.
* **Android (Chrome):** menu → **Install app** (or **Add to Home screen**).
* **Desktop Chrome / Edge:** the install icon at the right of the address bar.

Installing needs HTTPS (or `localhost`). The app's manifest, service worker (`/sw.js`) and icons are served
without authentication because they contain no data; everything else keeps its normal protection. The service
worker caches only the page itself and its icons, so the app still opens when the server is unreachable; health
data always comes live from `/api/` and is never stored on the device. If you put a client-certificate (mTLS)
proxy in front, test opening the installed app on each phone: some browsers handle client certificates
differently in an installed app than in a normal tab.

---

## 5. Analytics & Longevity Engines

### 1. Phenotypic Age & Longevity (`uniscale.longevity`)
UniScale implements the validated **Morgan Levine PhenoAge algorithm** using clinical blood biomarkers:
* **Inputs:** Chronological age, Albumin, Creatinine, Serum Glucose, C-Reactive Protein (CRP), Lymphocyte %, Mean Corpuscular Volume (MCV), Red Cell Distribution Width (RDW), Alkaline Phosphatase (ALP), and White Blood Cell (WBC) count.
* **Output:** Biological PhenoAge, phenotypic age acceleration, and mortality risk metrics.
* **Missing markers:** a marker the lab panel lacks falls back to a typical adult value. The dashboard tile says how many of the 9 were measured and which were assumed; for lymphocyte % and hs-CRP it also shows the range the result could span (lymphocytes 20–40 %, hs-CRP 0.3–3 mg/L). hs-CRP is read from lab reports in mg/L. Ask for a CBC with differential and hs-CRP to get all 9.
* **Body context:** the tile also shows the latest BMI and body fat. These don't enter PhenoAge (no validated model adds them), and genomics doesn't either: genes shift risk, not measured biological age.
* **Atherosclerotic Cardiovascular Disease (ASCVD) 10-Year Risk:** Calculates risk score based on age, systolic BP, total cholesterol, HDL, and smoking status.

### 2. Sleep Architecture & Recovery (`uniscale.sleep`)
* **Stages:** Tracks Deep Sleep, REM Sleep, Light Sleep, and Awake intervals.
* **Oximetry & Respiration:** Monitors overnight average and minimum SpO2 levels.
* **Autonomic Balance:** Tracks nocturnal Heart Rate Variability (rMSSD in milliseconds) and overnight stress index.

### 3. Supplement Tracking & Stack Management (`uniscale.supplements`)
* **Customizable Active Stack:** Edit and personalize daily longevity stacks (e.g. Creatine, Omega-3, Magnesium Glycinate, Vitamin D3/K2, CoQ10) directly via web UI or CLI.
* **Non-Dogmatic Clinical Review:** Supplement suggestions are treated as adaptive starting hypotheses, not rigid absolutes; consult Dr. Gemma to review dosages, interactions, and suggest additions based on your telemetry.
* **Adherence Progress:** Calculates daily completion percentage and streak.
* **Quick Log & Undo:** Fast intake logging from the dashboard checklist or command-line, with undo support.

### 4. Sovereign Genomics & Pharmacogenomics (`uniscale.genomics`)
* **Raw DNA Ingestion:** Parses raw microarray text dumps (23andMe, AncestryDNA, or Promethease).
* **Targeted SNP Interpretation:** Evaluates longevity, cardiovascular, and metabolic variants:
  * **APOE Genotyping:** rs429358 & rs7412 (evaluates ε2, ε3, ε4 alleles for lipid and neurodegenerative risk).
  * **MTHFR:** rs1801133 (C677T) & rs1801131 (A1298C) for methylation efficiency.
  * **COMT:** rs4680 (Val158Met) for catecholamine metabolism.
  * **FTO:** rs9939609 for metabolic rate and satiety.
* **Pedigree Trio Analysis:** Evaluates genetic inheritance when maternal and paternal data are available.

---

## 6. Data Ingestion & Importers

Before closing third-party cloud accounts, import your complete history into UniScale:

### 1. Garmin Connect Bulk Takeout & Workouts
* Ingest an entire Garmin Takeout archive (daily steps, summaries, weights) or individual `.FIT` activity files:
  ```bash
  # Ingest full takeout archive (.zip or extracted directory)
  uv run uniscale import-garmin /path/to/garmin_export.zip

  # Ingest a single workout activity FIT file
  uv run uniscale import-garmin /path/to/activity.fit

  # Sleep straight from the watch: Gadgetbridge's SLEEP_*.fit (its HRV_STATUS_*.fit next to it is
  # picked up too), or a whole folder of them, e.g. copied off the phone
  uv run uniscale import-garmin /path/to/SLEEP_2026-01-15_11-00-00.fit
  uv run uniscale import-garmin /path/to/gadgetbridge_files/
  ```

### 2. Google Fit Takeout
* Ingest the whole Google Fit history from the Takeout `.zip` (read in place, never extracted) or an extracted folder:
  daily summaries (steps, calories, move minutes, average/min/max/resting heart rate, SpO2, heart points), every
  weight and blood-pressure reading, sleep nights and workouts. The per-second sensor streams are not read; the
  daily summary condenses them.
  ```bash
  uv run uniscale import-google-fit ~/Downloads/takeout-*.zip
  ```
* Records the vault already has are not imported again; they are listed in `google_fit_already_there.jsonl` next
  to the vault (rewritten on each import). Days the vault already has get only the heart-rate fields they lack.

### 3. Android Device Direct Extraction (ADB)
* Extract SQLite databases and DevLogs directly from connected Android devices:
  ```bash
  uv run uniscale import-adb
  ```
  Extracts Yunmai weigh-in logs and ViHealth high-resolution ECG records.

### 4. Clinical Lab PDF Import (Quest Diagnostics / LabCorp)
* Ingest comprehensive metabolic and lipid panels directly from PDF or text reports:
  ```bash
  uv run uniscale import-lab /path/to/quest_results.pdf
  ```

### 5. CSV Exports (Fitdays & Omron)
* Ingest exported CSV spreadsheets:
  ```bash
  uv run uniscale import-csv /path/to/export.csv
  ```

### 6. Places from OwnTracks
The Places card lists spots where you stayed an hour or more, worked out from
[OwnTracks](https://owntracks.org/) locations. Two ways in:

* **From an OwnTracks Recorder you already run** (the hub reads it):
  ```toml
  [owntracks]
  recorder_url = "https://location.example.com/owntracks"
  username = "..."   # the Recorder's HTTP Basic login, if any
  password = "..."
  # user / device: only needed if the Recorder has more than one
  ```
  Once a day is over, the hub fetches that day's points (when the dashboard or app next loads places)
  and keeps only the places, not the track. To fill in earlier days: `uv run uniscale place sync --days 90`.
* **Straight from the phone:** set OwnTracks to HTTP mode with the URL `https://<your hub>/api/owntracks`
  (with `server.api_token` set: any username, the token as password). Points go to `locations.jsonl`
  next to the vault.

Stays of 60 minutes or more within ~100 m count. Name a place once and later stays within 150 m take the same name; places named **Home** (as many as you like, e.g. after a move) are kept but left out of the lists. Places can also be added by hand (Places card →
Add Place, or `uniscale place add`); give coordinates if you want them on the map.

---

## 7. Local AI Clinical Assistant (`uniscale.ai`)

UniScale can analyze your aggregated health metrics using a local Large Language Model (e.g. Gemma, Llama) served by any OpenAI-compatible server such as **Ollama** or **llama.cpp**:

### Command-Line AI Evaluation
```bash
# Analyze full profile using a local LLM server
uv run uniscale analyze --endpoint http://localhost:11434 --model gemma:latest

# Or generate the sanitized clinical evaluation prompt without invoking the LLM
uv run uniscale analyze --prompt-only
```

### In-Dashboard Chat
The web dashboard provides an interactive `/api/chat` endpoint where you can ask questions regarding your blood pressure trends, recovery status, or biomarker history against your local LLM.

---

## 8. Command-Line Reference

| Command | Description |
|---|---|
| `uniscale scan [--timeout 15]` | Scan for nearby BLE scales, cuffs, and monitors |
| `uniscale daemon [--no-garmin]` | Run background BLE listener & auto-unit fixer |
| `uniscale serve [--port 8088]` | Run self-hosted web dashboard & sync API |
| `uniscale history [--type weight]` | Inspect recorded measurements in the terminal |
| `uniscale config {init,show}` | Initialize or inspect configuration file |
| `uniscale analyze` | Generate clinical AI health summary via local LLM |
| `uniscale supplement {log,list,stack}` | Log intakes and track daily supplement adherence |
| `uniscale genomics {report,import}` | Generate genetic report or ingest raw DNA file |
| `uniscale import-garmin <path>` | Ingest Garmin `.fit` workouts or Takeout `.zip` |
| `uniscale import-google-fit <zip or dir>` | Ingest a Google Fit Takeout (`.zip` or extracted) |
| `uniscale import-lab <path>` | Parse and ingest clinical laboratory PDF report |
| `uniscale import-adb` | Extract vendor records from connected phone via ADB |
| `uniscale import-csv <path>` | Import Fitdays or Omron CSV export |

---

## 9. Standard Systemd Service Deployment

To run UniScale automatically on system boot (e.g. on a home server or Raspberry Pi):

Create `/etc/systemd/system/uniscale.service`:

```ini
[Unit]
Description=UniScale Personal Health Hub & Vault
After=network.target

[Service]
Type=simple
User=uniscale
Group=uniscale
WorkingDirectory=/opt/uniscale
Environment=PYTHONPATH=/opt/uniscale
Environment=UNISCALE_CONFIG=/opt/uniscale/config.toml
ExecStart=/opt/uniscale/venv/bin/uvicorn uniscale.server:app --host 127.0.0.1 --port 8088
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now uniscale.service
sudo systemctl status uniscale.service
```

### Deploy on push

If the server runs UniScale from a git checkout and also hosts the git repository (e.g. a self-hosted
Gitea or Forgejo), it can deploy itself whenever `main` is pushed. `scripts/deploy-on-push.sh` installs new
Python dependencies, runs the test suite against the new commit, fast-forwards the checkout, restarts the
service and checks `/health`. If the tests fail, nothing changes; if the new version doesn't come up, it rolls
back to the previous commit. Untracked files in the checkout (`config.toml`, `data/`) are never touched.

Install a root-owned copy, so a push can't change what runs as root:
```bash
sudo install -o root -g root -m 755 scripts/deploy-on-push.sh /usr/local/sbin/uniscale-deploy
```

`/etc/systemd/system/uniscale-deploy.service`:
```ini
[Unit]
Description=Deploy the newest UniScale main (tests, restart, rollback on failure)
After=network.target uniscale.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/uniscale-deploy
# The user that owns the checkout and runs uniscale.service; also APP, SERVICE, BRANCH, HEALTH_URL
Environment=OWNER=uniscale
TimeoutStartSec=15min
```

`/etc/systemd/system/uniscale-deploy.path`, pointing at the bare repository on the same machine:
```ini
[Unit]
Description=Deploy UniScale when its git repository receives a push

[Path]
PathChanged=/path/to/repositories/you/uniscale.git/refs/heads
PathChanged=/path/to/repositories/you/uniscale.git/packed-refs
Unit=uniscale-deploy.service

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now uniscale-deploy.path
journalctl -u uniscale-deploy -f      # watch a deploy
```

The checkout's `origin` must be fetchable by `OWNER` (a local path to the bare repository works), and the
app's venv needs `pytest` and `httpx` for the test run.
