import asyncio
import logging
from typing import Callable, Optional
from bleak import BleakClient
from uniscale.models import WeightMeasurement

logger = logging.getLogger(__name__)

ETEKCITY_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
ETEKCITY_NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
ETEKCITY_WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"


def parse_etekcity_packet(data: bytes, device_name: str, device_address: str) -> Optional[WeightMeasurement]:
    """
    Parses notification payload from Etekcity/VeSync ESF scales.
    Supports ESF-24, ESF-551, and FIT-8S format.
    """
    if len(data) < 7:
        return None

    # Common Etekcity preamble: 0xA5 or 0x10
    header = data[0]
    
    # Format A (ESF-24 / ESF-551 standard GATT packet):
    # Length >= 8
    # Status at byte 1 (0x12 unstable, 0x13 or 0x14 stable)
    if header == 0xA5 or header == 0x10:
        # Check stabilization flag
        # Weight bytes typically at index 2, 3 (big endian) or 4, 5
        raw_weight = (data[2] << 8) | data[3]
        if raw_weight == 0 and len(data) >= 6:
            raw_weight = (data[4] << 8) | data[5]

        # Unit byte: 0 = kg, 1 = lb, 2 = st
        unit = data[1] & 0x03 if len(data) > 1 else 0

        # Divisor is typically 100 (e.g. 6400 -> 64.00 kg or 14100 -> 141.00 lb)
        if unit == 1:  # scale reports in lb
            weight_lb = round(raw_weight / 100.0 if raw_weight > 1000 else raw_weight / 10.0, 1)
            weight_kg = round(weight_lb / 2.20462262, 2)
        else:  # scale reports in kg
            weight_kg = round(raw_weight / 100.0 if raw_weight > 1000 else raw_weight / 10.0, 2)
            weight_lb = round(weight_kg * 2.20462262, 1)

        impedance = None
        if len(data) >= 10:
            raw_imp = (data[6] << 8) | data[7]
            if raw_imp > 0:
                impedance = raw_imp

        if weight_kg > 10.0:
            return WeightMeasurement(
                weight_kg=weight_kg,
                weight_lb=weight_lb,
                impedance=impedance,
                device_name=device_name,
                device_address=device_address
            )

    return None


class EtekcityClient:
    def __init__(self, address: str, name: str = "Etekcity ESF"):
        self.address = address
        self.name = name
        self.client: Optional[BleakClient] = None
        self.on_measurement: Optional[Callable[[WeightMeasurement], None]] = None

    async def connect_and_listen(self, timeout: float = 20.0):
        async with BleakClient(self.address, timeout=timeout) as client:
            self.client = client
            logger.info(f"Connected to Etekcity/VeSync scale: {self.address}")

            def callback(sender, data: bytearray):
                meas = parse_etekcity_packet(bytes(data), self.name, self.address)
                if meas and self.on_measurement:
                    self.on_measurement(meas)

            await client.start_notify(ETEKCITY_NOTIFY_CHAR, callback)
            logger.info("Listening for weight data on Etekcity scale...")
            await asyncio.sleep(timeout)
            await client.stop_notify(ETEKCITY_NOTIFY_CHAR)
