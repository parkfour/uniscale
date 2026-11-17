import asyncio
import logging
import struct
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from bleak import BleakClient
from uniscale.models import BloodPressureMeasurement, EcgRecording

logger = logging.getLogger(__name__)

VIATOM_SERVICE_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"
VIATOM_WRITE_CHAR = "8b00ace7-eb0b-49b0-bbe9-9aee0a26e1a3"
VIATOM_NOTIFY_CHAR = "0734594a-a8e7-4b1a-a6b1-cd5243059a57"

# CRC-8/CCITT table (poly 0x07, init 0x00)
CRC8_TABLE = [
    0x00, 0x07, 0x0E, 0x09, 0x1C, 0x1B, 0x12, 0x15,
    0x38, 0x3F, 0x36, 0x31, 0x24, 0x23, 0x2A, 0x2D,
    0x70, 0x77, 0x7E, 0x79, 0x6C, 0x6B, 0x62, 0x65,
    0x48, 0x4F, 0x46, 0x41, 0x54, 0x53, 0x5A, 0x5D,
    0xE0, 0xE7, 0xEE, 0xE9, 0xFC, 0xFB, 0xF2, 0xF5,
    0xD8, 0xDF, 0xD6, 0xD1, 0xC4, 0xC3, 0xCA, 0xCD,
    0x90, 0x97, 0x9E, 0x99, 0x8C, 0x8B, 0x82, 0x85,
    0xA8, 0xAF, 0xA6, 0xA1, 0xB4, 0xB3, 0xBA, 0xBD,
    0xC7, 0xC0, 0xC9, 0xCE, 0xDB, 0xDC, 0xD5, 0xD2,
    0xFF, 0xF8, 0xF1, 0xF6, 0xE3, 0xE4, 0xED, 0xEA,
    0xB7, 0xB0, 0xB9, 0xBE, 0xAB, 0xAC, 0xA5, 0xA2,
    0x8F, 0x88, 0x81, 0x86, 0x93, 0x94, 0x9D, 0x9A,
    0x27, 0x20, 0x29, 0x2E, 0x3B, 0x3C, 0x35, 0x32,
    0x1F, 0x18, 0x11, 0x16, 0x03, 0x04, 0x0D, 0x0A,
    0x57, 0x50, 0x59, 0x5E, 0x4B, 0x4C, 0x45, 0x42,
    0x6F, 0x68, 0x61, 0x66, 0x73, 0x74, 0x7D, 0x7A,
    0x89, 0x8E, 0x87, 0x80, 0x95, 0x92, 0x9B, 0x9C,
    0xB1, 0xB6, 0xBF, 0xB8, 0xAD, 0xAA, 0xA3, 0xA4,
    0xF9, 0xFE, 0xF7, 0xF0, 0xE5, 0xE2, 0xEB, 0xEC,
    0xC1, 0xC6, 0xCF, 0xC8, 0xDD, 0xDA, 0xD3, 0xD4,
    0x69, 0x6E, 0x67, 0x60, 0x75, 0x72, 0x7B, 0x7C,
    0x51, 0x56, 0x5F, 0x58, 0x4D, 0x4A, 0x43, 0x44,
    0x19, 0x1E, 0x17, 0x10, 0x05, 0x02, 0x0B, 0x0C,
    0x21, 0x26, 0x2F, 0x28, 0x3D, 0x3A, 0x33, 0x34,
    0x4E, 0x49, 0x40, 0x47, 0x52, 0x55, 0x5C, 0x5B,
    0x76, 0x71, 0x78, 0x7F, 0x6A, 0x6D, 0x64, 0x63,
    0x3E, 0x39, 0x30, 0x37, 0x22, 0x25, 0x2C, 0x2B,
    0x06, 0x01, 0x08, 0x0F, 0x1A, 0x1D, 0x14, 0x13,
    0xAE, 0xA9, 0xA0, 0xA7, 0xB2, 0xB5, 0xBC, 0xBB,
    0x96, 0x91, 0x98, 0x9F, 0x8A, 0x8D, 0x84, 0x83,
    0xDE, 0xD9, 0xD0, 0xD7, 0xC2, 0xC5, 0xCC, 0xCB,
    0xE6, 0xE1, 0xE8, 0xEF, 0xFA, 0xFD, 0xF4, 0xF3,
]


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = CRC8_TABLE[0xFF & (crc ^ b)]
    return crc


def build_lepu_packet(cmd: int, payload: bytes = b"", seq: int = 1) -> bytes:
    length = len(payload)
    pkt = bytearray(7 + length + 1)
    pkt[0] = 0xA5
    pkt[1] = cmd & 0xFF
    pkt[2] = (~cmd) & 0xFF
    pkt[3] = 0x00  # TX
    pkt[4] = seq & 0xFF
    pkt[5] = length & 0xFF
    pkt[6] = (length >> 8) & 0xFF
    if length > 0:
        pkt[7:7 + length] = payload
    pkt[7 + length] = crc8(pkt[:7 + length])
    return bytes(pkt)


def build_bp2_sync_time(t: Optional[time.struct_time] = None) -> bytes:
    if t is None:
        t = time.localtime()
    payload = struct.pack("<HBBBBB", t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    return build_lepu_packet(0xEC, payload)


def build_bp2_get_config() -> bytes:
    return build_lepu_packet(0x06)


def build_bp2_read_file_start(filename: str = "bp2nibp.list") -> bytes:
    name_bytes = filename.encode("ascii")[:20]
    payload = name_bytes + b"\x00" * (20 - len(name_bytes))
    return build_lepu_packet(0xF2, payload)


def build_bp2_read_file_data(offset: int) -> bytes:
    return build_lepu_packet(0xF3, struct.pack("<I", offset))


def build_bp2_read_file_end() -> bytes:
    return build_lepu_packet(0xF4)


class LepuPacketReassembler:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes, on_packet: Callable[[int, bytes], None]):
        self.buffer.extend(data)
        while len(self.buffer) >= 8:
            idx = self.buffer.find(b"\xA5")
            if idx < 0:
                self.buffer.clear()
                return
            if idx > 0:
                del self.buffer[:idx]

            if len(self.buffer) < 7:
                return

            cmd = self.buffer[1]
            cmd_inv = self.buffer[2]
            if cmd_inv != ((~cmd) & 0xFF):
                del self.buffer[0]
                continue

            length = self.buffer[5] | (self.buffer[6] << 8)
            total_len = 7 + length + 1
            if length > 2048:
                del self.buffer[0]
                continue

            if len(self.buffer) < total_len:
                return  # Wait for more chunks

            pkt_bytes = bytes(self.buffer[:total_len])
            del self.buffer[:total_len]

            calc_crc = crc8(pkt_bytes[:-1])
            recv_crc = pkt_bytes[-1]
            if calc_crc == recv_crc:
                payload = pkt_bytes[7:-1]
                on_packet(cmd, payload)

    def reset(self):
        self.buffer.clear()


class ViatomBP2Client:
    def __init__(self, address: str, name: str = "Viatom BP2"):
        self.address = address
        self.name = name
        self.client: Optional[BleakClient] = None
        self.on_bp_measurement: Optional[Callable[[BloodPressureMeasurement], None]] = None
        self.on_ecg_recording: Optional[Callable[[EcgRecording], None]] = None
        self.reassembler = LepuPacketReassembler()
        self.file_size = 0
        self.file_buffer = bytearray()
        self.live_ecg_samples: list[float] = []

    def handle_packet(self, cmd: int, payload: bytes):
        # 0x08: Real-Time Data
        if cmd == 0x08 and len(payload) >= 1:
            status = payload[0]
            # Type 1 or 5: BP Measurement Complete
            if (status == 1 or status == 5) and len(payload) >= 11:
                sys_val, dia_val, map_val, hr_val = struct.unpack_from("<HHHH", payload, 3)
                if 40 <= sys_val <= 280 and 20 <= dia_val <= 200:
                    bp = BloodPressureMeasurement(
                        systolic=sys_val,
                        diastolic=dia_val,
                        pulse=hr_val if 25 <= hr_val <= 250 else 70,
                        device_name=self.name,
                        device_address=self.address
                    )
                    logger.info(f"BP2 RT Measured BP: {bp.systolic}/{bp.diastolic} mmHg, Pulse: {bp.pulse}")
                    if self.on_bp_measurement:
                        self.on_bp_measurement(bp)

            # Type 2 or 6: ECG Measuring
            elif (status == 2 or status == 6) and len(payload) >= 5:
                hr_val = struct.unpack_from("<H", payload, 3)[0]
                for i in range(5, len(payload) - 1, 2):
                    val = struct.unpack_from("<h", payload, i)[0]
                    self.live_ecg_samples.append(round(val * 0.003098, 4))

                if len(self.live_ecg_samples) >= 375:
                    ecg = EcgRecording(
                        heart_rate=hr_val if 20 <= hr_val <= 250 else 70,
                        sample_rate_hz=250,
                        samples_mv=list(self.live_ecg_samples[-375:]),
                        duration_seconds=round(len(self.live_ecg_samples[-375:]) / 250.0, 1),
                        rhythm_status="Normal Sinus Rhythm",
                        device_name=self.name,
                        device_address=self.address
                    )
                    if self.on_ecg_recording:
                        self.on_ecg_recording(ecg)

        # 0xF2: File Start Response
        elif cmd == 0xF2 and len(payload) >= 4:
            self.file_size = struct.unpack_from("<I", payload, 0)[0]
            self.file_buffer.clear()
            logger.info(f"BP2 File size: {self.file_size} bytes")

        # 0xF3: File Data Chunk
        elif cmd == 0xF3:
            self.file_buffer.extend(payload)
            if self.file_size > 0 and len(self.file_buffer) >= self.file_size:
                data = bytes(self.file_buffer)
                file_type = data[1] if len(data) >= 2 else 0
                if file_type == 2:
                    self.parse_ecg_file(data)
                else:
                    self.parse_bp_file(data)

    def parse_ecg_file(self, data: bytes):
        if len(data) < 50:
            return
        hr_val = struct.unpack_from("<H", data, 20)[0]
        rec_time = struct.unpack_from("<I", data, 10)[0]
        diag_val = struct.unpack_from("<I", data, 16)[0]

        samples = []
        for i in range(48, len(data) - 1, 2):
            raw = struct.unpack_from("<h", data, i)[0]
            samples.append(round(raw * 0.003098, 4))

        status = "Normal Sinus Rhythm" if diag_val == 0 else f"ECG Analyzed (Flag {diag_val})"
        duration = float(rec_time) if 5 <= rec_time <= 300 else len(samples) / 125.0

        ecg = EcgRecording(
            heart_rate=hr_val if 20 <= hr_val <= 250 else 70,
            sample_rate_hz=125,
            samples_mv=samples,
            duration_seconds=round(duration, 1),
            rhythm_status=status,
            device_name=self.name,
            device_address=self.address
        )
        if self.on_ecg_recording:
            logger.info(f"BP2 Ingested ECG File Record: {ecg.heart_rate} bpm, {len(ecg.samples_mv)} samples")
            self.on_ecg_recording(ecg)

    def parse_bp_file(self, data: bytes):
        header_size = 10
        record_size = 37
        if len(data) < header_size + record_size:
            return

        num_records = (len(data) - header_size) // record_size
        logger.info(f"BP2 parsing {num_records} stored blood pressure records from cuff memory")

        for i in range(num_records):
            offset = header_size + (i * record_size)
            ts, user_id, flag = struct.unpack_from("<IIB", data, offset)
            sys_val, dia_val, map_val, hr_val = struct.unpack_from("<HHHH", data, offset + 13)

            if 40 <= sys_val <= 280 and 20 <= dia_val <= 200:
                rec_dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 1500000000 else datetime.now(timezone.utc)
                bp = BloodPressureMeasurement(
                    systolic=sys_val,
                    diastolic=dia_val,
                    pulse=hr_val if 25 <= hr_val <= 250 else 70,
                    timestamp=rec_dt,
                    device_name=self.name,
                    device_address=self.address
                )
                if self.on_bp_measurement:
                    logger.info(f"BP2 Ingested File Record: {bp.systolic}/{bp.diastolic} Pulse: {bp.pulse} @ {bp.timestamp}")
                    self.on_bp_measurement(bp)

    async def connect_and_listen(self, timeout: float = 30.0):
        async with BleakClient(self.address, timeout=timeout) as client:
            self.client = client
            logger.info(f"Connected to Viatom BP2: {self.address}")

            def notification_handler(sender, data: bytearray):
                self.reassembler.feed(bytes(data), self.handle_packet)

            await client.start_notify(VIATOM_NOTIFY_CHAR, notification_handler)
            logger.info("Subscribed to Viatom BP2 notify stream")

            # Sync time & get config
            await client.write_gatt_char(VIATOM_WRITE_CHAR, build_bp2_sync_time(), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(VIATOM_WRITE_CHAR, build_bp2_get_config(), response=False)
            await asyncio.sleep(0.2)

            # Request bp2nibp.list
            await client.write_gatt_char(VIATOM_WRITE_CHAR, build_bp2_read_file_start("bp2nibp.list"), response=False)

            # File chunk fetch loop
            start_time = time.time()
            last_req_offset = -1
            while time.time() - start_time < timeout:
                await asyncio.sleep(0.1)
                if self.file_size > 0:
                    curr_len = len(self.file_buffer)
                    if curr_len < self.file_size and curr_len != last_req_offset:
                        last_req_offset = curr_len
                        await client.write_gatt_char(VIATOM_WRITE_CHAR, build_bp2_read_file_data(curr_len), response=False)
                    elif curr_len >= self.file_size:
                        await client.write_gatt_char(VIATOM_WRITE_CHAR, build_bp2_read_file_end(), response=False)
                        break

            logger.info("BP2 listen cycle completed")
            await client.stop_notify(VIATOM_NOTIFY_CHAR)
