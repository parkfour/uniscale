import asyncio
import logging
from typing import Callable, Optional
from bleak import BleakClient
from uniscale.models import WeightMeasurement

logger = logging.getLogger(__name__)

YUNMAI_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
YUNMAI_NOTIFY_CHAR = "0000ffe4-0000-1000-8000-00805f9b34fb"
YUNMAI_WRITE_CHAR = "0000ffe9-0000-1000-8000-00805f9b34fb"

# Display unit codes
UNIT_KG = 0x01
UNIT_LB = 0x02
UNIT_JIN = 0x03


def build_yunmai_unit_config(unit: int = UNIT_LB) -> bytearray:
    """
    Builds the initialization packet written to characteristic 0xFFE9 upon connect.
    Byte 15 sets the hardware scale display unit:
    0x01 = kg, 0x02 = lb, 0x03 = jin / st.
    """
    packet = bytearray(18)
    packet[0] = 0x0D  # Header
    packet[1] = 0x12  # Length (18 bytes)
    packet[2] = 0x10  # Command: User Config / Handshake
    packet[3] = 0x00
    # User profile placeholders (standard generic defaults: 30yo, 175cm, male)
    packet[4] = 0x00
    packet[5] = 0x01  # User ID
    packet[6] = 0x01  # Male
    packet[7] = 30    # Age
    packet[8] = 175   # Height (cm)
    packet[15] = unit # Display Unit: 0x02 = POUNDS!
    
    # Checksum: XOR of bytes 1 through 16
    checksum = 0
    for b in packet[1:17]:
        checksum ^= b
    packet[17] = checksum
    return packet


def parse_yunmai_packet(data: bytes, device_name: str, device_address: str) -> Optional[WeightMeasurement]:
    """
    Parses incoming GATT notification from characteristic 0xFFE4.
    """
    if len(data) < 14:
        return None

    # Yunmai packet header is 0x0D
    if data[0] != 0x0D:
        return None

    cmd = data[2]
    # 0x02 = Weight measurement packet
    if cmd == 0x02:
        status = data[8] if len(data) > 8 else 0
        # 1 = measuring (unstable), 2 = stabilized final weight
        is_stable = (status == 2) or (len(data) >= 17 and data[14] > 0)
        
        # Raw weight is encoded as 16-bit integer (scaled by 100 for kg)
        raw_weight = (data[13] << 8) | data[14]
        if raw_weight == 0 and len(data) >= 12:
            raw_weight = (data[10] << 8) | data[11]

        weight_kg = round(raw_weight / 100.0, 2)
        
        # Impedance for body fat
        impedance = 0
        if len(data) >= 17:
            impedance = (data[15] << 8) | data[16]

        if weight_kg > 10.0:
            return WeightMeasurement(
                weight_kg=weight_kg,
                impedance=impedance if impedance > 0 else None,
                device_name=device_name,
                device_address=device_address
            )

    return None


class YunmaiClient:
    def __init__(self, address: str, name: str = "Yunmai Mini", unit: int = UNIT_LB):
        self.address = address
        self.name = name
        self.unit = unit
        self.client: Optional[BleakClient] = None
        self.on_measurement: Optional[Callable[[WeightMeasurement], None]] = None

    async def connect_and_listen(self, timeout: float = 20.0):
        async with BleakClient(self.address, timeout=timeout) as client:
            self.client = client
            logger.info(f"Connected to Yunmai scale: {self.address}")

            # Send the unit configuration command to switch display to Pounds!
            cmd = build_yunmai_unit_config(self.unit)
            await client.write_gatt_char(YUNMAI_WRITE_CHAR, cmd, response=False)
            logger.info("Sent Yunmai unit configuration packet (set display to Pounds)")

            def callback(sender, data: bytearray):
                meas = parse_yunmai_packet(bytes(data), self.name, self.address)
                if meas and self.on_measurement:
                    self.on_measurement(meas)

            await client.start_notify(YUNMAI_NOTIFY_CHAR, callback)
            logger.info("Listening for weight data on Yunmai scale...")
            await asyncio.sleep(timeout)
            await client.stop_notify(YUNMAI_NOTIFY_CHAR)
