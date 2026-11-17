import asyncio
import sys
from bleak import BleakScanner

VIATOM_SERVICE_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"

async def scan():
    print("Scanning for Bluetooth health devices (BP2, Scales, Omron)...")
    print("Press the power/measure button on your device to ensure Bluetooth is active.\n")

    def detection_callback(device, advertisement_data):
        name = device.name or advertisement_data.local_name or "Unknown"
        uuids = [str(u).lower() for u in (advertisement_data.service_uuids or [])]
        mfg_data = advertisement_data.manufacturer_data
        
        is_target = False
        target_type = []

        if "bp2" in name.lower() or VIATOM_SERVICE_UUID in uuids:
            is_target = True
            target_type.append("Viatom/Wellue BP2 (Blood Pressure + ECG)")
        if "yunmai" in name.lower() or any("ffe0" in u or "ffe5" in u for u in uuids):
            is_target = True
            target_type.append("Yunmai Smart Scale")
        if any(k in name.lower() for k in ["pcb", "aaa", "chipsea", "icomon", "leaone", "lfscale"]) or any("ffb0" in u for u in uuids):
            is_target = True
            target_type.append("Fitdays / Icomon Scale")
        if any(k in name.lower() for k in ["esf", "scaly", "etekcity"]) or any("fff0" in u for u in uuids):
            is_target = True
            target_type.append("VeSync / Etekcity Scale")
        if "omron" in name.lower() or any("1810" in u for u in uuids):
            is_target = True
            target_type.append("Omron Blood Pressure")

        if is_target or "scale" in name.lower() or "bp" in name.lower():
            print(f"[*] FOUND TARGET: {device.address} | Name: {name} | Match: {', '.join(target_type)}")
            print(f"    RSSI: {advertisement_data.rssi} dBm")
            print(f"    Services: {uuids}")
            if mfg_data:
                print(f"    Manufacturer Data: {mfg_data}")
            print("-" * 50)

    scanner = BleakScanner(detection_callback=detection_callback)
    await scanner.start()
    await asyncio.sleep(8.0)
    await scanner.stop()
    print("Scan complete.")

if __name__ == "__main__":
    asyncio.run(scan())
