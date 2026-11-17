"""Builds small synthetic Garmin FIT files for tests, so no real device files live in the repo."""
import struct
from datetime import datetime, timezone

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z
ENUM, UINT8, UINT16, UINT32, SINT32 = (0x00, 1, "B"), (0x02, 1, "B"), (0x84, 2, "H"), (0x86, 4, "I"), (0x85, 4, "i")

_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def _crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = _CRC_TABLE[crc & 0xF]
            crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[nibble]
    return crc


def fit_time(dt: datetime) -> int:
    return int(dt.timestamp()) - FIT_EPOCH


def build_fit(messages) -> bytes:
    """messages: [(global_msg_num, [(field_num, type, value), ...]), ...]; one local type per message."""
    body = b""
    for local, (msg_num, fields) in enumerate(messages):
        local %= 16
        body += struct.pack("<BBBHB", 0x40 | local, 0, 0, msg_num, len(fields))
        for num, (base, size, _), _ in fields:
            body += struct.pack("<BBB", num, size, base)
        body += struct.pack("<B", local)
        for _, (_, _, fmt), value in fields:
            body += struct.pack("<" + fmt, value)
    header = struct.pack("<BBHI4s", 14, 0x10, 2100, len(body), b".FIT")
    header += struct.pack("<H", _crc(header))
    data = header + body
    return data + struct.pack("<H", _crc(data))


def sleep_fit(start: datetime, stages, score=70, deep_score=80, rem_score=75) -> bytes:
    """stages: [(offset_seconds, level)] with level 1 awake, 2 light, 3 deep, 4 rem; the last entry marks the stop."""
    stop = datetime.fromtimestamp(start.timestamp() + stages[-1][0], timezone.utc)
    msgs = [(0, [(0, ENUM, 49), (1, UINT16, 1), (4, UINT32, fit_time(stop))]),
            (21, [(253, UINT32, fit_time(start)), (0, ENUM, 74), (1, ENUM, 0)])]
    for offset, level in stages[:-1]:
        t = datetime.fromtimestamp(start.timestamp() + offset, timezone.utc)
        msgs.append((275, [(253, UINT32, fit_time(t)), (0, ENUM, level)]))
    msgs.append((21, [(253, UINT32, fit_time(stop)), (0, ENUM, 74), (1, ENUM, 1)]))
    msgs.append((346, [(6, UINT8, score), (3, UINT8, deep_score), (9, UINT8, rem_score)]))
    return build_fit(msgs)


def hrv_fit(when: datetime, last_night_ms=40, weekly_ms=42, status=4) -> bytes:
    return build_fit([(0, [(0, ENUM, 68), (1, UINT16, 1), (4, UINT32, fit_time(when))]),
                      (370, [(253, UINT32, fit_time(when)), (0, UINT16, weekly_ms * 128),
                             (1, UINT16, last_night_ms * 128), (6, ENUM, status)])])


def deg_to_sc(deg: float) -> int:
    return int(deg * 2147483648.0 / 180.0)


def workout_fit(
    start: datetime,
    duration_s: int = 1800,
    distance_m: float = 5000.0,
    calories: int = 350,
    avg_hr: int = 152,
    max_hr: int = 175,
    avg_cadence: int = 166,
    max_cadence: int = 182,
    elevation_gain: float = 45.0,
    elevation_loss: float = 40.0,
    avg_power: int = 245,
    max_power: int = 320,
    stride_length_mm: int = 11200,
    stance_time_ms_scaled: int = 2350,
    stance_balance_scaled: int = 4960,
    vert_osc_scaled: int = 820,
    vert_ratio_scaled: int = 730,
    te_scaled: int = 38,
    anaerobic_te_scaled: int = 22,
    start_lat: float = 47.603456,
    start_lon: float = -122.333456,
    records=None
) -> bytes:
    """Builds a synthetic Garmin workout FIT file with running dynamics, laps, and records."""
    lat_sc = deg_to_sc(start_lat)
    lon_sc = deg_to_sc(start_lon)
    msgs = [
        # File ID
        (0, [(0, ENUM, 4), (1, UINT16, 1), (4, UINT32, fit_time(start))]),
        # Session (18)
        (18, [
            (253, UINT32, fit_time(start)),
            (2, UINT32, fit_time(start)),
            (5, ENUM, 1),  # running
            (7, UINT32, duration_s * 1000),
            (8, UINT32, duration_s * 1000),
            (9, UINT32, int(distance_m * 100)),
            (11, UINT16, calories),
            (16, UINT8, avg_hr),
            (17, UINT8, max_hr),
            (18, UINT8, avg_cadence),
            (19, UINT8, max_cadence),
            (20, UINT16, avg_power),
            (21, UINT16, max_power),
            (22, UINT16, int(elevation_gain)),
            (23, UINT16, int(elevation_loss)),
            (24, UINT8, te_scaled),
            (34, UINT16, avg_power + 5),  # normalized power
            (89, UINT16, vert_osc_scaled),
            (91, UINT16, stance_time_ms_scaled),
            (132, UINT16, vert_ratio_scaled),
            (133, UINT16, stance_balance_scaled),
            (134, UINT16, stride_length_mm),
            (137, UINT8, anaerobic_te_scaled),
            (3, SINT32, lat_sc),
            (4, SINT32, lon_sc),
            (38, SINT32, lat_sc + 1000),
            (39, SINT32, lon_sc + 1000),
        ]),
        # Lap (19)
        (19, [
            (253, UINT32, fit_time(start)),
            (8, UINT32, 360000),  # 360 s
            (9, UINT32, 100000),  # 1000 m
            (15, UINT8, avg_hr - 5),
        ]),
    ]
    if records:
        for offset_s, lat, lon, hr in records:
            t = fit_time(start) + offset_s
            msgs.append((20, [
                (253, UINT32, t),
                (0, SINT32, deg_to_sc(lat)),
                (1, SINT32, deg_to_sc(lon)),
                (3, UINT8, hr),
            ]))
    return build_fit(msgs)
