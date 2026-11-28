import asyncio
import json
import logging
import os
from typing import Optional
from bleak import BleakScanner
from uniscale.config import UniscaleConfig, load_config, vault_db_path, vault_locked
from uniscale.models import BloodPressureMeasurement, EcgRecording, WeightMeasurement
from uniscale.protocols.etekcity import ETEKCITY_SERVICE_UUID, EtekcityClient, parse_etekcity_packet
from uniscale.protocols.fitdays import FITDAYS_SERVICE_UUID, FitdaysClient, parse_fitdays_data
from uniscale.protocols.omron import BP_SERVICE_UUID, OmronClient
from uniscale.protocols.viatom_bp2 import VIATOM_SERVICE_UUID, ViatomBP2Client
from uniscale.protocols.yunmai import YUNMAI_SERVICE_UUID, YunmaiClient, parse_yunmai_packet
from uniscale.sync.garmin import GarminSync

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("uniscale.daemon")


class UniscaleDaemon:
    def __init__(self, config: Optional[UniscaleConfig] = None, enable_garmin: Optional[bool] = None):
        self.config = config or load_config()

        # Legacy Garmin sync check
        use_garmin = enable_garmin if enable_garmin is not None else self.config.legacy_cloud.garmin.enabled
        self.garmin = GarminSync(
            email=self.config.legacy_cloud.garmin.email,
            password=self.config.legacy_cloud.garmin.password
        ) if use_garmin else None

        self.active_connections = set()
        # In multi-user mode this is the --user / UNISCALE_USER vault (NoUserError if unset);
        # ECG recordings live next to it so they stay with that user's data.
        self.log_file = vault_db_path(self.config)
        self.data_dir = os.path.dirname(os.path.abspath(self.log_file))
        self.ecg_dir = os.path.join(self.data_dir, "ecg")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.ecg_dir, exist_ok=True)

    def upload_to_hub(self, endpoint: str, payload: dict):
        hub_url = self.config.sync.remote_hub_url.rstrip("/")
        if not hub_url:
            return
        try:
            import httpx
            url = f"{hub_url}/api/sync/{endpoint}"
            headers = {}
            if self.config.sync.api_token:
                headers["Authorization"] = f"Bearer {self.config.sync.api_token}"

            cert = None
            if self.config.sync.client_cert and self.config.sync.client_key:
                cert = (
                    os.path.expanduser(self.config.sync.client_cert),
                    os.path.expanduser(self.config.sync.client_key)
                )

            verify = os.path.expanduser(self.config.sync.ca_cert) if self.config.sync.ca_cert else True

            with httpx.Client(cert=cert, verify=verify, timeout=10) as client:
                res = client.post(url, json=payload, headers=headers)
                if res.status_code in (200, 201):
                    logger.info(f"Successfully uploaded {endpoint} to hub {hub_url}")
                else:
                    logger.warning(f"Hub upload failed [{res.status_code}]: {res.text}")
        except Exception as e:
            logger.error(f"Failed uploading to remote hub {hub_url}: {e}")

    @vault_locked

    def log_measurement(self, record_type: str, data: dict):
        with open(self.log_file, "a") as f:
            f.write(json.dumps({"type": record_type, **data}, default=str) + "\n")

    def handle_weight(self, meas: WeightMeasurement):
        logger.info(f"=== WEIGHT MEASUREMENT RECEIVED ===")
        logger.info(f"  Device: {meas.device_name} ({meas.device_address})")
        logger.info(f"  Weight: {meas.weight_lb} lb ({meas.weight_kg} kg)")
        if meas.impedance:
            logger.info(f"  Impedance: {meas.impedance} ohms")

        self.log_measurement("weight", meas.model_dump())
        self.upload_to_hub("weight", meas.model_dump(mode="json"))

        if self.garmin:
            self.garmin.sync_weight(meas)

    def handle_bp(self, meas: BloodPressureMeasurement):
        logger.info(f"=== BLOOD PRESSURE MEASUREMENT RECEIVED ===")
        logger.info(f"  Device: {meas.device_name} ({meas.device_address})")
        logger.info(f"  Pressure: {meas.systolic}/{meas.diastolic} mmHg")
        logger.info(f"  Pulse: {meas.pulse} bpm")

        self.log_measurement("blood_pressure", meas.model_dump())
        self.upload_to_hub("bp", meas.model_dump(mode="json"))

        if self.garmin:
            self.garmin.sync_blood_pressure(meas)

    def handle_ecg(self, ecg: EcgRecording):
        logger.info(f"=== ECG RECORDING RECEIVED ===")
        logger.info(f"  Device: {ecg.device_name}")
        logger.info(f"  Heart Rate: {ecg.heart_rate} bpm")
        logger.info(f"  Samples: {len(ecg.samples_mv)} ({ecg.duration_seconds}s at {ecg.sample_rate_hz}Hz)")

        self.log_measurement("ecg", ecg.model_dump())
        self.upload_to_hub("ecg", ecg.model_dump(mode="json"))
        filename = os.path.join(self.ecg_dir, f"ecg_{ecg.timestamp:%Y%m%d_%H%M%S}.json")
        with open(filename, "w") as f:
            json.dump(ecg.model_dump(), f, default=str, indent=2)
        logger.info(f"  Saved ECG rhythm strip to {filename}")

    async def on_device_detected(self, device, advertisement_data):
        address = device.address
        if address in self.active_connections:
            return

        name = device.name or advertisement_data.local_name or ""
        uuids = [str(u).lower() for u in (advertisement_data.service_uuids or [])]
        name_lower = name.lower()

        # Check Yunmai
        if self.config.devices.enable_yunmai and ("yunmai" in name_lower or any("ffe0" in u for u in uuids)):
            self.active_connections.add(address)
            asyncio.create_task(self._process_yunmai(address, name or "Yunmai Mini"))

        # Check VeSync / Etekcity
        elif self.config.devices.enable_vesync and (any(k in name_lower for k in ["esf", "scaly", "etekcity"]) or any("fff0" in u for u in uuids)):
            self.active_connections.add(address)
            asyncio.create_task(self._process_etekcity(address, name or "Etekcity ESF"))

        # Check Fitdays (PCB / Icomon / AAA)
        elif self.config.devices.enable_fitdays and (any(k in name_lower for k in ["pcb", "aaa", "chipsea", "icomon", "leaone", "lfscale"]) or any("ffb0" in u for u in uuids)):
            self.active_connections.add(address)
            asyncio.create_task(self._process_fitdays(address, name or "Fitdays Scale"))

        # Check Viatom BP2
        elif self.config.devices.enable_viatom_bp2 and ("bp2" in name_lower or VIATOM_SERVICE_UUID in uuids):
            self.active_connections.add(address)
            asyncio.create_task(self._process_viatom_bp2(address, name or "Viatom BP2"))

        # Check Omron BP
        elif self.config.devices.enable_omron and ("omron" in name_lower or any("1810" in u for u in uuids)):
            self.active_connections.add(address)
            asyncio.create_task(self._process_omron(address, name or "Omron BP"))

    async def _process_yunmai(self, address: str, name: str):
        try:
            client = YunmaiClient(address, name=name)
            client.on_measurement = self.handle_weight
            await client.connect_and_listen(timeout=15.0)
        except Exception as e:
            logger.error(f"Error handling Yunmai scale ({address}): {e}")
        finally:
            self.active_connections.discard(address)

    async def _process_etekcity(self, address: str, name: str):
        try:
            client = EtekcityClient(address, name=name)
            client.on_measurement = self.handle_weight
            await client.connect_and_listen(timeout=15.0)
        except Exception as e:
            logger.error(f"Error handling Etekcity scale ({address}): {e}")
        finally:
            self.active_connections.discard(address)

    async def _process_fitdays(self, address: str, name: str):
        try:
            client = FitdaysClient(address, name=name)
            client.on_measurement = self.handle_weight
            await client.connect_and_listen(timeout=15.0)
        except Exception as e:
            logger.error(f"Error handling Fitdays scale ({address}): {e}")
        finally:
            self.active_connections.discard(address)

    async def _process_viatom_bp2(self, address: str, name: str):
        try:
            client = ViatomBP2Client(address, name=name)
            client.on_bp_measurement = self.handle_bp
            client.on_ecg_recording = self.handle_ecg
            await client.connect_and_listen(timeout=30.0)
        except Exception as e:
            logger.error(f"Error handling Viatom BP2 ({address}): {e}")
        finally:
            self.active_connections.discard(address)

    async def _process_omron(self, address: str, name: str):
        try:
            client = OmronClient(address, name=name)
            client.on_measurement = self.handle_bp
            await client.connect_and_listen(timeout=15.0)
        except Exception as e:
            logger.error(f"Error handling Omron monitor ({address}): {e}")
        finally:
            self.active_connections.discard(address)

    async def run(self):
        logger.info("Starting uniscale background daemon...")
        logger.info("Monitoring for Yunmai, Fitdays, VeSync scales, Viatom BP2, and Omron...")
        scanner = BleakScanner(detection_callback=self.on_device_detected)
        await scanner.start()
        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            await scanner.stop()
            logger.info("Daemon stopped.")


if __name__ == "__main__":
    daemon = UniscaleDaemon()
    asyncio.run(daemon.run())
