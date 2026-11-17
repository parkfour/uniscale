# UniScale Android Mobile App

The **UniScale Android Application** (`android/`) is an offline-first companion application built with modern Android standards (Kotlin, Jetpack Compose, Material3, and Coroutines/Flow). It interfaces directly with smart scales, blood pressure monitors, and real-time ECG hardware over Bluetooth Low Energy (BLE), reads watch data from Gadgetbridge's auto-exported database (it doesn't use Android Health Connect), and automatically synchronizes measurements to your self-hosted UniScale Hub.

---

## 1. Core Architecture

The mobile application is structured around reactive state streams and offline-first persistence:

```
┌────────────────────────────────────────────────────────────────────────┐
│                         Jetpack Compose UI                             │
│       DashboardScreen │ EcgWaveformCanvas │ SettingsDialog             │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Observes StateFlows
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Managers & Coordinators                         │
│   BleManager      │ GadgetbridgeExport   │ HubSyncManager              │
│   (BLE GATT / RX) │ LocalReadings        │ (HTTP REST Ingestion)       │
└───────────┬───────────────────────┬──────────────────────┬─────────────┘
            │                       │                      │
            ▼                       ▼                      ▼
┌──────────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
│   Bluetooth Devices  │ │ Gadgetbridge export  │ │ Self-Hosted UniScale │
│ Yunmai, Fitdays, BP2 │ │ + app-private store  │ │   HTTP/HTTPS Service │
└──────────────────────┘ └──────────────────────┘ └──────────────────────┘
```

### Key Modules:
* **`com.uniscale.app.MainActivity`**: Main entry point, runtime permissions management (BLE scan/connect, notification), and dashboard lifecycle.
* **`com.uniscale.app.ble.BleManager`**: Handles BLE scanning, connection lifecycle, MTU negotiation, GATT characteristic subscription, packet routing, and packet reassembly.
* **`com.uniscale.app.ble.DeviceDecoders`**: Byte-level parsers for body composition, impedance, blood pressure, and real-time ECG packets.
* **`com.uniscale.app.sync.GadgetbridgeExport`**: Reads sleep, workouts and daily steps/distance from Gadgetbridge's auto-exported database.
* **`com.uniscale.app.sync.LocalReadings`**: Keeps the phone's weigh-ins and BP readings (newest 200, last 90 days) in app-private storage until the hub has them.
* **`com.uniscale.app.sync.HubSyncManager`**: Async HTTP client that delivers collected telemetry payloads to the UniScale server.
* **`com.uniscale.app.config.AppConfigManager`**: Manages portable application preferences with in-memory caching and JSON file import/export.
* **`com.uniscale.app.ui.EcgWaveformCanvas`**: Hardware-accelerated Compose canvas that renders 125 Hz Lead-I cardiac rhythms in real time.

---

## 2. Bluetooth Low Energy (BLE) Engine

The BLE engine operates without proprietary vendor apps or cloud bridges. It scans for advertising peripherals matching known service UUIDs and manufacturer signatures.

### Supported Devices & Protocols

#### 1. Yunmai Mini & Smart Scales (M1US)
* **Identification:** Broadcasts standard Yunmai manufacturer data.
* **Imperial Unit Display Fix:** By default, Yunmai scales often revert to showing metric kilograms on their physical LCD screen. UniScale immediately transmits a proprietary `0x10` handshake packet with byte 15 set to `0x02` upon GATT connection:
  ```kotlin
  // Forces physical LED/LCD display to render imperial pounds
  val handshake = byteArrayOf(0x10, ..., 0x02, ...)
  writeCharacteristic(handshake)
  ```
* **Payload Decoding:** Decodes weight (kg/lb) and bioelectrical impedance (resistance in ohms), deriving body fat %, bone mass, muscle mass, water %, and visceral fat rating.

#### 2. Fitdays / Icomon / Chipsea Scales
* **Service UUID:** `0000FFB0-0000-1000-8000-00805F9B34FB` (Characteristics `0xFFB1` / `0xFFB2`)
* **Hardware Profiles:** Supports `PCB` and AAA-series protocol revisions.
* **Operation:** Listens for broadcast manufacturer advertisement frames or subscribed notification streams to extract stabilized weigh-ins and dual-frequency impedance.

#### 3. VeSync / Etekcity Scales
* **Service UUID:** `0000FFF0-0000-1000-8000-00805F9B34FB`
* **Hardware Models:** ESF-24, ESF-551, and FIT-8S series.
* **Operation:** Parses broadcast packets for live tare weights, stabilized weigh-ins, and body composition parameters.

#### 4. Viatom / Wellue BP2 (Blood Pressure & Real-Time ECG)
* **Dual Operation:**
  * **Blood Pressure Mode:** Intercepts completed blood pressure readings (Systolic, Diastolic, Pulse rate).
  * **Real-Time ECG Streaming:** Streams 125 Hz Lead-I ECG waveform packets using an internal packet reassembler (`LepuPacketReassembler`). Packets are converted into millivolt (`mV`) amplitude floats and pushed into a circular sample buffer.

#### 5. Omron Blood Pressure Cuffs
* **Service UUID:** `00001810-0000-1000-8000-00805F9B34FB` (Standard Bluetooth SIG Blood Pressure Service)
* **Operation:** Reads standard IEEE-11073 16-bit SFLOAT formatted systolic, diastolic, and mean arterial pressure (MAP).

---

## 3. Real-Time ECG Waveform Canvas

The real-time ECG screen (`EcgWaveformCanvas.kt`) provides medical-monitor-style cardiac rendering:

* **Sampling Rate:** 125 samples per second (125 Hz).
* **Buffer Architecture:** Circular rolling buffer holding up to 30 seconds of high-fidelity waveform points.
* **Rendering Engine:** Native Compose `Canvas` utilizing custom `Path` drawing and hardware acceleration.
* **Visual Theme:** High-contrast dark cardiac aesthetic with dynamic sweep bar, grid lines, and live heart rate overlay.

---

## 4. Watch Data (Gadgetbridge)

To integrate wearable smartwatches (such as Garmin watches) completely offline, UniScale uses **Gadgetbridge**, which
connects to the watch directly over BLE (no Garmin Connect cloud).

### Gadgetbridge auto-export (no Health Connect)

Gadgetbridge keeps everything it downloads from the watch in its own database, including what Health Connect doesn't
carry: Garmin's own sleep score, overnight HRV and its status, and full workout summaries. Android doesn't let other
apps read Gadgetbridge's private folder, so UniScale reads the copy Gadgetbridge exports:

1. In Gadgetbridge, open **Settings → Auto export**, choose a location in shared storage (e.g. a `Data` folder in
   internal storage, *not* `Android/data`), set an interval, and run it once. It writes one file, `Gadgetbridge.db`,
   and overwrites it on each export.
2. In UniScale, open Settings → Data, tap **Watch** and pick that file. The app keeps read access to it across reboots.
3. On every refresh (app start, **Refresh**, **Watch**) `GadgetbridgeExport` reads the last 30 days:
   * **Sleep** (`GadgetbridgeNights`): one record per night with the watch's sleep score, deep/light/REM/awake
     time, time in bed, overnight HRV and status, average and lowest SpO2, and breathing rate, dated by the local
     wake-up day. Sent to `POST /api/sync/sleep`.
   * **Workouts** (`GadgetbridgeWorkouts`): sport, start, duration, distance, steps, calories, average/max heart
     rate, cadence, climb, power, aerobic training effect and VO2max, plus the watch's extras: anaerobic training
     effect, training load, recovery time, Body Battery at start/end, sweat loss, temperature, running form (ground
     contact, vertical oscillation and ratio, stride length), time in each heart-rate zone, the splits (usually per
     km, with pace and heart rate), and the heart rate during the workout from `GARMIN_ACTIVITY_SAMPLE` (about one
     sample a minute). Sent to `POST /api/sync/workout`; the dashboard shows them when you open a workout.
   * **Steps and distance** for today and yesterday from `GARMIN_ACTIVITY_SAMPLE`, whose `STEPS` and `DISTANCE_CM`
     are running totals for the day: the day's largest value after midnight. Today's total is sent on every sync,
     a finished day's once, to `POST /api/sync/activity`.

   Each record is uploaded once per hub and user. Second-by-second data and GPS tracks stay in the watch's FIT files; import those on the
   server (see the Garmin importer) if you want them.

### Scale and blood-pressure readings

The app doesn't use Android Health Connect: it neither reads nor writes it. Each weigh-in and BP reading is saved
in the app's private storage (`LocalReadings`, `readings.json`) and uploaded to the hub; every sync uploads them again
until the hub has them, so readings taken offline are sent later. The Weight and BP tiles show these plus the hub's
history (`GET /api/history`), so a fresh install shows past readings as soon as the hub is reachable.
**Clear Today** (Settings → Data) deletes today's readings on the phone; the hub keeps its copy.

---

## 5. Hub Synchronization

The mobile app includes an automated background sync engine (`HubSyncManager`):

* **HTTP REST Ingestion:** Transmits newly acquired measurements to your self-hosted UniScale server:
  * `POST /api/sync/weight`
  * `POST /api/sync/bp`
  * `POST /api/sync/ecg`
  * `POST /api/sync/activity`
* **Background Sync:** Settings → Background Sync (Off, 15m, 30m, 1h, 2h, 4h). A WorkManager job sends what opening
  the app would: Gadgetbridge sleep, workouts, steps and distance, the phone's weigh-ins and BP readings, and queued
  supplement ticks. Already-sent readings are skipped.
* **Bearer Token Authentication:** Optional API token header (`Authorization: Bearer <token>`) for protected instances.
* **Supplement checklist:** the dashboard's *Today's Protocol* card shows the hub's checklist grouped by slot
  (`GET /api/supplements`). Ticking a dose sends `POST /api/supplements` with the slot and the tap time; unticking
  sends `POST /api/supplements/intake/delete`. Ticks made offline are queued on the phone and replayed in order;
  the tap time makes a replay idempotent. The protocol itself is edited only on the dashboard.
* **Supplement reminders (optional):** Settings → Supplement Reminders, with a clock time per slot. At each slot
  time a WorkManager job posts a notification if that slot still has unticked doses.
* **Dashboard layout:** half-width tiles in pairs: Activity | Sleep, Weight | Blood Pressure, ECG | Protocol. Tap a
  tile to expand it to the full row (hiding its neighbour); tap again to shrink it. A measurement in progress
  (scale, cuff or ECG) expands its tile, which stays open until tapped. Weight and BP show the live reading, else the
  latest one on the phone; expanded, they also show the change since the previous reading.
  When a weigh-in, BP reading or ECG finishes, the phone plays the sound chosen in Settings → Measurement Done
  Sound (any of the phone's ringtones, or Silent). The Protocol tile opens the checklist as its own page, closed with **Done**.
* **Ask the assistant:** the chat icon in the top bar, shown only when the hub has a chat assistant
  (`GET /api/assistant`, which also gives its name). Each question goes to `POST /api/chat` with the thread; a
  WorkManager job waits for the reply (up to 5 minutes) and posts a notification, so the app can be closed. The
  conversation is kept only on the phone (`gemma_chat.json` in app storage); *New chat* clears it.
* **Latency & Connectivity Ping:** The settings dialog features a **Test Connection** button that checks server reachability and token authentication against `/api/config`.

---

## 6. Configuration Management

UniScale provides flexible configuration options without requiring source code modifications:

### Option A: In-App Settings Dialog
Tap the **Gear icon** in the top navigation bar to edit:
* **Hub Server URL:** (e.g. `http://192.168.1.50:8088` or `https://health.example.com`)
* **API Bearer Token:** Optional shared secret
* **Automatic Upload Toggle:** Enable or disable automatic background uploads upon measurement
* **Device Drivers:** Enable or disable specific hardware protocols (Yunmai, Fitdays, VeSync, BP2, Omron)
* **Unit Preference:** Toggle between imperial pounds (`lb`) and metric kilograms (`kg`)

### Option B: Portable Configuration File (`uniscale_config.json`)
The application can import or export configuration via an external JSON file saved in the device's public `Download/` directory (`/sdcard/Download/uniscale_config.json`):

```json
{
  "hub_url": "https://health.example.com",
  "api_token": "your-secure-token-here",
  "auto_upload_hub": true,
  "force_yunmai_pounds": true,
  "preferred_unit": "lb",
  "enable_yunmai": true,
  "enable_fitdays": true,
  "enable_vesync": true,
  "enable_viatom_bp2": true,
  "enable_omron": true
}
```
* **Export:** Tap **Export** in settings to snapshot current preferences to the file (the `api_token` is stripped during export to protect credentials on shared storage).
* **Import:** Tap **Import** to reload updated settings from storage, or launch the app with this file present (an imported `api_token` updates the app's secret; if blank, the existing stored token is preserved).

### Option C: Bundled Asset Defaults
Baseline defaults ship packaged in `app/src/main/assets/uniscale_config.json`.

---

## 7. Building & Installation

### Prerequisites
* **Java Development Kit (JDK):** Version 17 or higher
* **Android SDK:** Platform API 35 (Android 15) and Build Tools 35.0.0
* **Gradle:** Included wrapper (`gradlew`)

### Build Debug APK
```bash
cd android
./gradlew assembleDebug
```
The resulting APK is written to:
`app/build/outputs/apk/debug/app-debug.apk`

### Install onto Device via ADB
Connect your Android device via USB (or wireless ADB) with Developer Options and USB Debugging enabled:
```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

### Required Android Permissions
The application requests the following permissions at runtime:
* `BLUETOOTH_SCAN`, `BLUETOOTH_CONNECT` (Android 12+)
* `ACCESS_FINE_LOCATION` (Android 11 and lower for BLE beacon discovery)
* `POST_NOTIFICATIONS` (Android 13+) for supplement reminders and Dr. Gemma's replies.
