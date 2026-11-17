import asyncio
import logging
import struct
from typing import Callable, Optional
from bleak import BleakClient
from uniscale.models import BloodPressureMeasurement

logger = logging.getLogger(__name__)

BP_SERVICE_UUID = "00001810-0000-1000-8000-00805f9b34fb"
BP_MEASUREMENT_CHAR = "00002a35-0000-1000-8000-00805f9b34fb"


def parse_ieee11073_sfloat(raw: int) -> float:
    """Decodes a 16-bit IEEE-11073 SFLOAT (4-bit exponent, 12-bit mantissa)."""
    mantissa = raw & 0x0FFF
    if mantissa >= 0x0800:
        mantissa -= 0x1000
    exponent = raw >> 12
    if exponent >= 0x08:
        exponent -= 0x10
    return mantissa * (10 ** exponent)


def parse_standard_bp_packet(data: bytes, device_name: str, device_address: str) -> Optional[BloodPressureMeasurement]:
    """
    Parses standard Bluetooth SIG Blood Pressure Measurement characteristic (0x2A35).
    """
    if len(data) < 7:
        return None

    flags = data[0]
    unit_kpa = bool(flags & 0x01)
    timestamp_present = bool(flags & 0x02)
    pulse_present = bool(flags & 0x04)

    offset = 1
    # Systolic, Diastolic, MAP (each 2 bytes SFLOAT)
    raw_sys = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    raw_dia = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    raw_map = struct.unpack_from("<H", data, offset)[0]
    offset += 2

    sys_val = int(round(parse_ieee11073_sfloat(raw_sys)))
    dia_val = int(round(parse_ieee11073_sfloat(raw_dia)))

    if unit_kpa:
        # Convert kPa to mmHg (1 kPa = 7.50062 mmHg)
        sys_val = int(round(sys_val * 7.50062))
        dia_val = int(round(dia_val * 7.50062))

    if timestamp_present:
        offset += 7  # skip 7-byte timestamp

    pulse_val = 70
    if pulse_present and len(data) >= offset + 2:
        raw_pulse = struct.unpack_from("<H", data, offset)[0]
        pulse_val = int(round(parse_ieee11073_sfloat(raw_pulse)))

    if 40 < sys_val < 260 and 30 < dia_val < 160:
        return BloodPressureMeasurement(
            systolic=sys_val,
            diastolic=dia_val,
            pulse=pulse_val,
            device_name=device_name,
            device_address=device_address
        )

    return None


class OmronClient:
    def __init__(self, address: str, name: str = "Omron BP"):
        self.address = address
        self.name = name
        self.client: Optional[BleakClient] = None
        self.on_measurement: Optional[Callable[[BloodPressureMeasurement], None]] = None

    async def connect_and_listen(self, timeout: float = 20.0):
        async with BleakClient(self.address, timeout=timeout) as client:
            self.client = client
            logger.info(f"Connected to Omron BP monitor: {self.address}")

            def callback(sender, data: bytearray):
                meas = parse_standard_bp_packet(bytes(data), self.name, self.address)
                if meas and self.on_measurement:
                    self.on_measurement(meas)

            await client.start_notify(BP_MEASUREMENT_CHAR, callback)
            logger.info("Listening for blood pressure readings on Omron monitor...")
            await asyncio.sleep(timeout)
            await client.stop_notify(BP_MEASUREMENT_CHAR)
