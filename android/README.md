# UniScale Android Application

The UniScale Android client is an offline-first companion application built with Kotlin and Jetpack Compose. It interfaces directly with smart scales, blood pressure monitors, and ECG devices over Bluetooth Low Energy, aggregates watch telemetry from Android Health Connect (via Gadgetbridge), and synchronizes with your self-hosted UniScale Hub.

---

## Features

* **Multi-Scale BLE Engine:**
  * Auto-discovers and connects to Yunmai (M1US), Fitdays (PCB/Icomon), and VeSync/Etekcity scales.
  * Overrides the default metric scale display on Yunmai with imperial pounds using the `0x02` handshake packet.
* **Viatom BP2 Support:**
  * Reads systolic, diastolic, and pulse measurements.
  * Streams real-time 125 Hz Lead-I ECG waveform and renders it on a custom hardware-accelerated Compose canvas.
* **Garmin Watch Offline Sync:**
  * Reads sleep and workouts from Gadgetbridge's auto-export database (Settings → Data → **Watch**, then pick the file); steps and distance still come through Android Health Connect.
  * **Background Sync:** a WorkManager job uploads what opening the app would (Gadgetbridge sleep and workouts, Health Connect steps, weigh-ins and BP, queued supplement ticks) to the hub every 15 min, 30 min, 1 h, 2 h or 4 h (Settings slider; default 1 h, Off to disable). No persistent notification; it only runs with a network connection and when Auto Upload to Hub is on.
* **Tile dashboard:** Activity | Sleep, Weight | Blood Pressure and ECG | Protocol as half-width pairs; tap to expand a tile to the full row and tap again to shrink it. A measurement in progress expands its tile automatically. Expanded, Weight and BP show the change since the previous reading (e.g. "−0.2 lb since Nov 9").
* **Today's Protocol:** the hub's supplement checklist grouped by slot (wake, breakfast, lunch, dinner, bed); tick off doses, also offline. Optional reminders at slot times you set in Settings.
* **Ask the assistant (e.g. Dr. Gemma):** chat icon in the top bar, shown only when the hub has one set up; the reply arrives as a notification, so you can close the app while it thinks.
* **Screen stays on** while the app is open, because Android stops delivering BLE scan results from scales and BP cuffs once the screen is off.
* **Self-Hosted Hub Telemetry Sync:**
  * Automatically transmits measurements to your UniScale server via HTTPS.
* **Portable Configuration:**
  * Configurable via in-app UI or external `uniscale_config.json` in the device's Downloads directory.

---

## Building & Installation

### Prerequisites
* JDK 17+
* Android SDK Platform 35
* Gradle (or use system gradle `gradle assembleDebug`)

### Build Debug APK
```bash
cd android
gradle assembleDebug
```
The compiled APK will be generated at:
`app/build/outputs/apk/debug/app-debug.apk`

### Install onto Device via ADB
```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

---

## Configuration (`uniscale_config.json`)

You can configure the application in three ways:

### 1. In-App Settings Dialog
Tap the **Gear icon** in the top app bar to view and edit settings:
* **Hub URL:** e.g. `https://health.example.com`
* **API Bearer Token:** Optional shared secret for sync endpoints
* **Auto Upload to Hub:** Toggle automatic background telemetry uploads
* **Supplement Reminders:** On/off, and the time of each slot (24 h, e.g. `07:30`)
* **Measurement Done Sound:** A ringtone played when a weigh-in, BP reading or ECG finishes (Choose opens Android's picker; Silent turns it off; default is the notification sound)
* **Background Sync:** How often the app syncs to the hub while closed (Off, 15m, 30m, 1h, 2h, 4h)
* **Hardware Drivers:** Enable or disable specific Bluetooth drivers
* **Force LED Display to lb:** Forces Yunmai scales to display pounds
* **Test Connection:** Verify connectivity and ping response to your server

### 2. External JSON File
Place a configuration file named `uniscale_config.json` in your phone's `Download/` folder (`/sdcard/Download/uniscale_config.json`):
```json
{
  "hub_url": "https://health.example.com",
  "api_token": "your-token-here",
  "auto_upload_hub": true,
  "auto_sync_health_connect": true,
  "force_yunmai_pounds": true,
  "preferred_unit": "lb",
  "enable_yunmai": true,
  "enable_fitdays": true,
  "enable_vesync": true,
  "enable_viatom_bp2": true,
  "enable_omron": true
}
```
Tap **Import** in the settings dialog or relaunch the app to load it automatically.

### 3. Bundled Asset Defaults
Default baseline configuration ships in `app/src/main/assets/uniscale_config.json`.
