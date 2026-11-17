import asyncio
import logging
from typing import Callable, Optional
from bleak import BleakClient
from uniscale.models import WeightMeasurement

logger = logging.getLogger(__name__)

FITDAYS_SERVICE_UUID = "0000ffb0-0000-1000-8000-00805f9b34fb"
FITDAYS_NOTIFY_CHAR = "0000ffb2-0000-1000-8000-00805f9b34fb"


def parse_fitdays_data(data: bytes, device_name: str, device_address: str) -> Optional[WeightMeasurement]:
    """
    Parses broadcast manufacturer data or GATT notify payload from Fitdays / Icomon (PCB) scales.
    """
    if len(data) < 6:
        return None

    # Common prefixes: 0xCF, 0xCB, 0xCA, or Icomon 0x02
    header = data[0]

    # Icomon / Lefu standard broadcast packet
    if header in (0xCF, 0xCB, 0xCA, 0x02):
        # Weight bytes typically at index 2, 3 or 1, 2
        raw_weight = (data[2] << 8) | data[3] if len(data) >= 4 else (data[1] << 8) | data[2]
        
        # Determine divisor: Fitdays often uses / 100 for kg (e.g. 6420 -> 64.20 kg)
        if raw_weight > 20000:
            weight_kg = round(raw_weight / 1000.0, 2)
        elif raw_weight > 2000:
            weight_kg = round(raw_weight / 100.0, 2)
        else:
            weight_kg = round(raw_weight / 10.0, 2)

        impedance = None
        if len(data) >= 8:
            raw_imp = (data[4] << 8) | data[5]
            if 100 < raw_imp < 3000:
                impedance = raw_imp

        if weight_kg > 10.0:
            return WeightMeasurement(
                weight_kg=weight_kg,
                impedance=impedance,
                device_name=device_name,
                device_address=device_address
            )

    return None


class FitdaysClient:
    def __init__(self, address: str, name: str = "Fitdays PCB"):
        self.address = address
        self.name = name
        self.client: Optional[BleakClient] = None
        self.on_measurement: Optional[Callable[[WeightMeasurement], None]] = None

    async def connect_and_listen(self, timeout: float = 20.0):
        async with BleakClient(self.address, timeout=timeout) as client:
            self.client = client
            logger.info(f"Connected to Fitdays/Icomon scale: {self.address}")

            def callback(sender, data: bytearray):
                meas = parse_fitdays_data(bytes(data), self.name, self.address)
                if meas and self.on_measurement:
                    self.on_measurement(meas)

            # Try subscribing to FFB2 or custom notify
            for char_uuid in [FITDAYS_NOTIFY_CHAR, "0000fff4-0000-1000-8000-00805f9b34fb"]:
                try:
                    await client.start_notify(char_uuid, callback)
                    logger.info(f"Subscribed to Fitdays notify characteristic {char_uuid}")
                    break
                except Exception:
                    continue

            logger.info("Listening for weight data on Fitdays scale...")
            await asyncio.sleep(timeout)
