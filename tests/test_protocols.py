import pytest
from uniscale.protocols.yunmai import build_yunmai_unit_config, parse_yunmai_packet, UNIT_LB, UNIT_KG
from uniscale.protocols.etekcity import parse_etekcity_packet
from uniscale.protocols.fitdays import parse_fitdays_data
from uniscale.protocols.omron import parse_standard_bp_packet, parse_ieee11073_sfloat
from uniscale.protocols.viatom_bp2 import ViatomBP2Client, build_lepu_packet, crc8
import struct

def test_viatom_bp2_parsing():
    client = ViatomBP2Client("AA:BB:CC:DD:EE:05", "BP2 Test")
    results = []
    client.on_bp_measurement = lambda m: results.append(m)

    # Lepu Protocol V2 RT_DATA (CMD 0x08):
    # payload: status=1 (BP end), bat_status=0, bat_pct=95, sys=118, dia=78, map=91, hr=68
    payload = struct.pack("<BBBHHHH", 1, 0, 95, 118, 78, 91, 68)
    pkt = build_lepu_packet(0x08, payload)

    # Feed packet in 2 chunks to verify reassembly
    client.reassembler.feed(pkt[:10], client.handle_packet)
    client.reassembler.feed(pkt[10:], client.handle_packet)

    assert len(results) == 1
    assert results[0].systolic == 118
    assert results[0].diastolic == 78
    assert results[0].pulse == 68

def test_viatom_bp2_file_parsing():
    client = ViatomBP2Client("AA:BB:CC:DD:EE:05", "BP2 Test")
    results = []
    client.on_bp_measurement = lambda m: results.append(m)

    # bp2nibp.list: 10-byte header + 37-byte record
    header = b"\x01\x01" + b"\x00" * 8
    rec = struct.pack("<IIB4sHHHH16s", 1700000000, 1, 0, b"\x00"*4, 122, 82, 95, 74, b"\x00"*16)
    file_bytes = header + rec

    client.parse_bp_file(file_bytes)

    assert len(results) == 1
    assert results[0].systolic == 122
    assert results[0].diastolic == 82
    assert results[0].pulse == 74
    # Unit config packet must be 18 bytes, start with 0x0D, cmd 0x10, and byte 15 == 0x02 for lb
    pkt = build_yunmai_unit_config(UNIT_LB)
    assert len(pkt) == 18
    assert pkt[0] == 0x0D
    assert pkt[1] == 0x12
    assert pkt[2] == 0x10
    assert pkt[15] == 0x02


def test_yunmai_packet_parsing():
    # Yunmai measurement packet: 0x0D, length, cmd 0x02, weight at index 13, 14
    # Example: 64.00 kg -> 6400 (0x1900)
    data = bytearray(18)
    data[0] = 0x0D
    data[2] = 0x02  # cmd: weight
    data[8] = 0x02  # stable
    data[13] = 0x19
    data[14] = 0x00
    data[15] = 0x02
    data[16] = 0x58  # 600 ohms

    meas = parse_yunmai_packet(bytes(data), "Yunmai Test", "AA:BB:CC:DD:EE:01")
    assert meas is not None
    assert meas.weight_kg == 64.0
    assert meas.weight_lb == 141.1
    assert meas.impedance == 600


def test_etekcity_packet_parsing():
    # Etekcity packet: 0xA5, status 0x13, unit 1 (lb), weight 14100 (141.0 lb) -> 0x3714
    data = bytearray([0xA5, 0x01, 0x37, 0x14, 0x00, 0x00, 0x02, 0x58])
    meas = parse_etekcity_packet(bytes(data), "Etekcity Test", "AA:BB:CC:DD:EE:02")
    assert meas is not None
    assert meas.weight_lb == 141.0
    assert round(meas.weight_kg, 1) == 64.0


def test_fitdays_packet_parsing():
    # Fitdays packet: 0xCF, status, weight 6420 -> 0x1914 (64.20 kg)
    data = bytearray([0xCF, 0x01, 0x19, 0x14, 0x02, 0x58])
    meas = parse_fitdays_data(bytes(data), "Fitdays Test", "AA:BB:CC:DD:EE:03")
    assert meas is not None
    assert meas.weight_kg == 64.20


def test_omron_bp_parsing():
    # Standard 0x2A35 packet:
    # flags: 0x00 (mmHg, no timestamp, no pulse)
    # Systolic: 120 (0x0078), Diastolic: 80 (0x0050), MAP: 93 (0x005D)
    data = bytearray([0x00, 0x78, 0x00, 0x50, 0x00, 0x5D, 0x00])
    meas = parse_standard_bp_packet(bytes(data), "Omron Test", "AA:BB:CC:DD:EE:04")
    assert meas is not None
    assert meas.systolic == 120
    assert meas.diastolic == 80
