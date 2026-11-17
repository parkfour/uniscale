# UniScale Documentation

Welcome to the documentation for **UniScale**, a unified, offline-first personal health hub, BLE bridge, and telemetry vault.

UniScale is designed to give you complete ownership and sovereignty over your physical health data. It connects directly to smart scales, blood pressure monitors, ECG devices, and wearable watches over Bluetooth Low Energy (watches through Gadgetbridge's local export, never Google Health Connect), storing all metrics locally in open, transparent formats.

```
                    ┌──────────────────────────────┐
                    │      PHYSICAL HARDWARE       │
                    └──────────────┬───────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
 ┌──────────────┐           ┌──────────────┐           ┌──────────────┐
 │  Yunmai /    │           │ Viatom BP2 / │           │    Garmin    │
 │  Fitdays /   │           │  Omron Cuff  │           │    Watch     │
 │    VeSync    │           │  (BP + ECG)  │           │   Wearable   │
 └──────┬───────┘           └──────┬───────┘           └──────┬───────┘
        │ BLE Direct               │ BLE Direct               │ BLE
        │ (Auto-lbs fix)           │ (125 Hz ECG)             ▼
        │                          │               ┌──────────────────────┐
        │                          │               │     Gadgetbridge     │
        │                          │               │  (F-Droid / Offline) │
        │                          │               └──────────┬───────────┘
        │                          │                          │
        │                          │                          │ auto-exported
        │                          │                          │ database file
        ▼                          ▼                          ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                          UniScale Ecosystem                             │
 │                                                                         │
 │  ┌────────────────────────┐              ┌───────────────────────────┐  │
 │  │   Android Mobile App   │              │     Self-Hosted Server    │  │
 │  │ (Compose / Health Hub) │───(HTTP Sync)▶   (FastAPI / Web Vault)   │  │
 │  └────────────────────────┘              └───────────────────────────┘  │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## Documentation Sections

1. **[Android Mobile App Guide](mobile_app.md)**
   * Mobile architecture & Jetpack Compose UI
   * Direct Bluetooth Low Energy (BLE) engine & device decoders
   * Real-time 125 Hz ECG canvas rendering
   * Gadgetbridge offline watch integration (no Health Connect)
   * Hub synchronization & portable JSON configuration
   * Building and installing debug APKs

2. **[Health Service & Backend Guide](health_service.md)**
   * Architecture overview & storage engine (JSONL / SQLite)
   * Declarative configuration system (`config.toml`)
   * Background BLE daemon (`uniscale daemon`)
   * Self-hosted web dashboard & API server (`uniscale serve`)
   * Longevity biomarker analysis (PhenoAge & 10-Year ASCVD risk)
   * Sleep stage and overnight HRV tracking
   * Supplement stack tracking and daily adherence
   * Sovereign genomics & SNP risk profiling
   * Data ingestion & migration (Garmin, Google Fit, ADB, Lab reports)
   * Local AI clinical integration (Gemma / Ollama)
   * Standard systemd service deployment

3. **[REST API & Data Models Reference](api_reference.md)**
   * Unified data schemas (Weights, Blood Pressure, ECG, Activities, Labs, Sleep, Supplements, Genomics)
   * API endpoints for telemetry sync, history, analysis, and dashboard querying
   * Local storage record formats

4. **[Multi-User Setup](multi_user.md)**
   * One isolated vault per person behind Basic auth, OAuth/OIDC or mTLS reverse proxies
   * Per-device tokens or client certificates, several devices per person
   * CLI/daemon `--user`, data layout and migration, security checklist

---

## Quickstart

### 1. Python Health Service & Daemon
```bash
# Clone and setup environment
git clone https://github.com/your-org/uniscale.git
cd uniscale
uv sync

# Initialize default configuration (~/.config/uniscale/config.toml)
uv run uniscale config init

# Start the web dashboard and REST API
uv run uniscale serve --port 8088

# Run the background BLE daemon to listen for scales and cuffs
uv run uniscale daemon
```

### 2. Android Mobile App
```bash
# Build debug APK
cd android
./gradlew assembleDebug

# Install via ADB onto a connected device
adb install -r app/build/outputs/apk/debug/app-debug.apk
```
