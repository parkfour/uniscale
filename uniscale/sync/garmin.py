import logging
import os
from typing import Optional
from garminconnect import Garmin
from uniscale.models import BloodPressureMeasurement, WeightMeasurement

logger = logging.getLogger(__name__)


class GarminSync:
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None):
        self.email = email or os.getenv("GARMIN_EMAIL")
        self.password = password or os.getenv("GARMIN_PASSWORD")
        self.tokenstore = os.path.expanduser("~/.config/uniscale/garmin_tokens")
        self.client: Optional[Garmin] = None

    def login(self) -> bool:
        if not self.email or not self.password:
            logger.warning("Garmin credentials not provided (GARMIN_EMAIL / GARMIN_PASSWORD).")
            return False

        try:
            os.makedirs(os.path.dirname(self.tokenstore), exist_ok=True)
            self.client = Garmin(self.email, self.password)
            self.client.login(self.tokenstore)
            logger.info("Successfully authenticated with Garmin Connect.")
            return True
        except Exception as e:
            logger.error(f"Failed to authenticate with Garmin Connect: {e}")
            return False

    def sync_weight(self, measurement: WeightMeasurement) -> bool:
        if not self.client and not self.login():
            return False

        try:
            ts_str = measurement.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            res = self.client.add_body_composition(
                timestamp=ts_str,
                weight=measurement.weight_kg,
                percent_fat=measurement.body_fat_percent,
                bmi=measurement.bmi
            )
            logger.info(f"Synced weight ({measurement.weight_lb} lb / {measurement.weight_kg} kg) to Garmin Connect: {res}")
            return True
        except Exception as e:
            logger.error(f"Error syncing weight to Garmin Connect: {e}")
            return False

    def sync_blood_pressure(self, measurement: BloodPressureMeasurement) -> bool:
        if not self.client and not self.login():
            return False

        try:
            ts_str = measurement.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            res = self.client.set_blood_pressure(
                systolic=measurement.systolic,
                diastolic=measurement.diastolic,
                pulse=measurement.pulse,
                timestamp=ts_str,
                notes=f"Auto-synced via uniscale ({measurement.device_name})"
            )
            logger.info(f"Synced Blood Pressure ({measurement.systolic}/{measurement.diastolic} mmHg) to Garmin Connect: {res}")
            return True
        except Exception as e:
            logger.error(f"Error syncing Blood Pressure to Garmin Connect: {e}")
            return False
