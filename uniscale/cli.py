import argparse
import asyncio
import os
import sys
import zipfile
from datetime import date
from uniscale.config import NoUserError, generate_default_config_file, load_config, normalize_user, vault_db_path
from uniscale.daemon import UniscaleDaemon
from uniscale.models import BloodPressureMeasurement, WeightMeasurement
from uniscale.sync.garmin import GarminSync


def cmd_scan(args):
    print("Scanning for Bluetooth health devices (Scales, BP2, Omron)...")
    from uniscale.daemon import UniscaleDaemon
    from bleak import BleakScanner

    async def _scan():
        def callback(d, adv):
            name = d.name or adv.local_name
            if name or adv.service_uuids:
                print(f"[{d.address}] {name or 'Unknown'} | RSSI: {adv.rssi} | Services: {adv.service_uuids}")

        scanner = BleakScanner(detection_callback=callback)
        await scanner.start()
        await asyncio.sleep(args.timeout)
        await scanner.stop()

    asyncio.run(_scan())


def cmd_daemon(args):
    cfg = load_config(args.config)
    enable_garmin = False if args.no_garmin else None
    daemon = UniscaleDaemon(config=cfg, enable_garmin=enable_garmin)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        print("\nDaemon terminated by user.")


def cmd_history(args):
    log_file = vault_db_path()
    if not os.path.exists(log_file):
        print("No measurements recorded yet.")
        return

    from uniscale.readings import load_vault
    records = load_vault(log_file)

    req_type = getattr(args, "type", None)
    limit = getattr(args, "limit", 25)

    filtered = []
    for entry in reversed(records):
        mtype = entry.get("type")
        if not mtype:
            if "weight_kg" in entry:
                mtype = "weight"
            elif "systolic" in entry:
                mtype = "blood_pressure"
            elif "samples_mv" in entry:
                mtype = "ecg"
            elif "sport" in entry:  # before "steps": workouts carry steps too
                mtype = "workout"
            elif "steps" in entry:
                mtype = "activity"
            else:
                mtype = "other"
            entry["type"] = mtype

        if req_type and req_type != mtype:
            continue
        filtered.append(entry)
        if len(filtered) >= limit:
            break

    print(f"{'TIMESTAMP':<20} | {'TYPE':<15} | {'MEASUREMENT':<32} | {'DEVICE'}")
    print("-" * 92)
    for entry in filtered:
        mtype = entry.get("type", "unknown")
        ts = str(entry.get("timestamp") or entry.get("date") or "")[:19].replace("T", " ")
        dev = entry.get("device_name") or entry.get("source") or "Unknown"

        if mtype == "weight" or "weight_kg" in entry:
            lb = entry.get("weight_lb", round(entry.get("weight_kg", 0) * 2.20462, 1))
            val = f"{lb} lb ({entry.get('weight_kg')} kg)"
        elif mtype in ("blood_pressure", "bp") or "systolic" in entry:
            val = f"{entry.get('systolic')}/{entry.get('diastolic')} mmHg (P: {entry.get('pulse', 0)})"
        elif mtype == "ecg" or "samples_mv" in entry:
            val = f"{len(entry.get('samples_mv', []))} pts ({entry.get('duration_seconds', 30)}s HR: {entry.get('heart_rate', '--')})"
        elif mtype == "activity":
            val = f"{entry.get('steps', 0):,} steps, {entry.get('distance_km', 0)} km ({entry.get('calories', '--')} kcal)"
        elif mtype == "workout" or "sport" in entry:
            dur = round(entry.get("duration_seconds", 0) / 60, 1)
            dist = entry.get("distance_km") or (entry.get("distance_meters", 0) / 1000.0)
            val = f"{entry.get('sport', 'workout').capitalize()}: {dist:.2f} km in {dur}m"
        else:
            val = str(entry)[:32]

        print(f"{ts:<20} | {mtype:<15} | {val:<32} | {dev}")


def cmd_counter(args):
    import json
    from uniscale.counter import get_counter_state, get_daily_history, set_counter, adjust_counter, import_historic_records
    from uniscale.config import vault_db_path

    v_path = vault_db_path()
    target_date = getattr(args, "date", None)

    if getattr(args, "import_file", None):
        with open(args.import_file, "r", encoding="utf-8") as f:
            if args.import_file.endswith(".json"):
                raw_json = json.load(f)
                entries = raw_json if isinstance(raw_json, list) else raw_json.get("entries", [])
            else:
                import csv
                entries = [{k.strip(): (v or "0").strip() for k, v in row.items()} for row in csv.DictReader(f)]
        count = import_historic_records(entries, db_path=v_path)
        print(f"Imported {count} days from {args.import_file}.")
        return

    if getattr(args, "set", None):
        counts = {}
        for pair in args.set:
            name, _, n = pair.rpartition("=")
            counts[name] = int(n)
        state = set_counter(counts, db_path=v_path, target_date=target_date)
    elif getattr(args, "inc", None):
        state = adjust_counter(item=args.inc, delta=1, db_path=v_path, target_date=target_date)
    elif getattr(args, "dec", None):
        state = adjust_counter(item=args.dec, delta=-1, db_path=v_path, target_date=target_date)
    else:
        state = get_counter_state(v_path, target_date=target_date)

    els = state["elements"]
    if not els:
        print('No counter set. Add one to your profile, e.g. "counter": {"title": "D Tracker", "elements": ["D(out)", "D(in)"]}')
        return
    days = getattr(args, "history", 10) or 10
    history = get_daily_history(v_path, days=days)
    print(f"\n--- {state['title']} ---")
    print(f"{state['date']}: " + " | ".join(f"{e['label']} = {state['counts'][e['key']]}" for e in els))
    print("All-time: " + " | ".join(f"{e['label']} = {state['totals'][e['key']]}" for e in els) + "\n")
    print(f"Past {len(history)} days:")
    print(f"{'Date':<12}" + "".join(f"{e['label']:<10}" for e in els) + "Total")
    for h in history:
        print(f"{h['date']:<12}" + "".join(f"{h['counts'][e['key']]:<10}" for e in els) + str(h["total"]))

def cmd_place(args):
    from datetime import datetime, timezone, timedelta
    from uniscale.places import get_dwell_places, add_custom_place, delete_dwell_place
    from uniscale.config import vault_db_path

    v_path = vault_db_path()
    action = getattr(args, "place_action", "list") or "list"

    if action == "add":
        name = args.name
        target_date = getattr(args, "date", None) or (datetime.now(timezone.utc).astimezone().date() - timedelta(days=1)).strftime("%Y-%m-%d")
        mins = getattr(args, "mins", 60) or 60
        lat = getattr(args, "lat", 0.0) or 0.0
        lon = getattr(args, "lon", 0.0) or 0.0
        p = add_custom_place(
            date_str=target_date,
            name=name,
            avg_lat=lat,
            avg_lon=lon,
            duration_seconds=int(mins * 60),
            db_path=v_path
        )
        print(f"Added place '{p['name']}' ({p['dwell_formatted']}) for {p['date']} [ID: {p['id']}].")
        return

    if action == "sync":
        from uniscale.places import sync_recorder
        read = sync_recorder(v_path, days=getattr(args, "days", None))
        if not read:
            print("Nothing to read: set [owntracks] recorder_url, or every finished day is already read.")
        else:
            print(f"Read {len(read)} day(s) from the OwnTracks Recorder ({read[0]} to {read[-1]}).")
        return

    if action == "delete":
        pid = args.place_id
        ok = delete_dwell_place(pid, db_path=v_path)
        if ok:
            print(f"Deleted place {pid}.")
        else:
            print(f"Place {pid} not found.")
        return

    # Default action: list
    target_date = getattr(args, "date", None) or "previous"
    data = get_dwell_places(v_path, target_date=target_date)
    places = data.get("places", [])
    print(f"\n--- Dwell Places (>1 hr) for {data.get('date', target_date)} ---")
    if not places:
        print("No qualifying dwell places recorded.")
        return
    print(f"{'Name':<24} {'Duration':<12} {'Coords':<22} {'ID'}")
    print("-" * 75)
    for p in places:
        c_str = f"{p['avg_lat']:.4f}, {p['avg_lon']:.4f}" if p['avg_lat'] or p['avg_lon'] else "Manual"
        print(f"{p.get('name') or 'Unassigned':<24} {p['dwell_formatted']:<12} {c_str:<22} {p['id']}")


def cmd_analyze(args):
    from uniscale.ai import build_health_profile, generate_health_summary_prompt, query_llm
    cfg = load_config(getattr(args, "config", None))
    db_path = getattr(args, "db", None) or vault_db_path(cfg)
    profile = build_health_profile(db_path)
    prompt = generate_health_summary_prompt(profile)

    if args.prompt_only:
        print(prompt)
        return

    print("Analyzing health telemetry with local AI (Gemma)...")
    endpoint = getattr(args, "endpoint", None) or cfg.ai.endpoint
    model = getattr(args, "model", None) or cfg.ai.model
    try:
        response = query_llm(prompt, endpoint=endpoint, model=model)
        print("\n=== Health Insights & Clinical Evaluation ===")
        print(response)
    except Exception as e:
        print(f"[-] Could not reach LLM endpoint ({e}).")
        print("\nHere is your generated clinical prompt to run manually or pass to Gemma 4:\n")
        print(prompt)


def cmd_import_csv(args):
    from uniscale.importer import import_csv_file
    path = os.path.expanduser(args.path)
    if not os.path.exists(path):
        print(f"[-] File not found: {path}")
        return
    try:
        res = import_csv_file(path)
    except ValueError as e:
        print(f"[-] {e}")
        return
    count = res.get("weights_found") or res.get("blood_pressure_found")
    print(f"[+] Imported {count} records from {res['source']} CSV into {vault_db_path()}.")


def cmd_import_vesync(args):
    from uniscale.importer import import_vesync_file
    path = os.path.expanduser(args.path)
    if not os.path.exists(path):
        print(f"[-] File not found: {path}")
        return
    res = import_vesync_file(path)
    print(f"[+] Successfully imported {res['total_parsed']} body composition records from VeSync ({res['target_db']}).")


def cmd_import_lab(args):
    from uniscale.importer import import_lab_file
    path = os.path.expanduser(args.path)
    if not os.path.exists(path):
        print(f"[-] File not found: {path}")
        return
    try:
        res = import_lab_file(path, target_db=vault_db_path())
        print(f"[+] Successfully imported {res['total_markers']} biomarkers from {res['lab_name']} report ({res['timestamp']}) into {res['target_db']}.")
    except Exception as e:
        print(f"[-] Failed to import lab report: {e}")


def cmd_import_adb(args):
    print("Connecting to phone via ADB to import vendor databases...")
    from uniscale.importer import import_from_adb_device
    res = import_from_adb_device()
    print(f"[+] Yunmai weigh-ins imported: {res['yunmai_records']}")
    print(f"[+] ViHealth ECG recordings imported: {res['vihealth_ecgs']}")
    if res["errors"]:
        for e in res["errors"]:
            print(f"[-] {e}")
    print(f"[+] All records saved to {vault_db_path()}")


def cmd_import_garmin(args):
    from uniscale.importer import import_garmin_file
    path = os.path.expanduser(args.path)
    if not os.path.exists(path):
        print(f"[-] Error: Path does not exist: {path}")
        return
    if not (path.lower().endswith((".fit", ".zip")) or os.path.isdir(path)):
        print("[-] Unrecognized Garmin input (must be .zip archive, directory of export, or .fit file)")
        return
    try:
        res = import_garmin_file(path)
    except (ValueError, zipfile.BadZipFile) as e:
        print(f"[-] {e}")
        return
    if "workout" in res:
        print(f"[+] Successfully imported FIT workout: {res['workout']}")
    else:
        print("[+] Successfully ingested Garmin export:")
        print(f"    - Historical Weights: {res['weights_found']}")
        print(f"    - Daily Activity Summaries: {res['daily_summaries_found']}")
        print(f"    - Workouts & Activities: {res['workouts_found']}")
        if "sleep_records_found" in res:
            print(f"    - Sleep & Recovery Nights: {res['sleep_records_found']}")
    print(f"[+] Stored locally in {vault_db_path()}")


def cmd_import_google_fit(args):
    from uniscale.importer import parse_google_fit_takeout
    path = os.path.expanduser(args.path)
    if not os.path.exists(path):
        print(f"[-] Error: Path does not exist: {path}")
        return
    res = parse_google_fit_takeout(path)
    print(f"[+] Google Fit Takeout: {res['daily_activities_found']} days, {res['weights_found']} weights, "
          f"{res['blood_pressure_found']} BP readings, {res['sleep_found']} nights, {res['workouts_found']} workouts.")
    print(f"[+] {res['already_there']} were already in the vault, listed in {res['already_there_file']}")
    print(f"[+] Heart rate added to {res['days_heart_rate_added']} days the vault already had")


def cmd_serve(args):
    import uvicorn
    cfg = load_config(args.config)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    print(f"Starting UniScale Health Service on {host}:{port}...")
    uvicorn.run("uniscale.server:app", host=host, port=port, reload=args.reload)


def cmd_profile(args):
    from pydantic import ValidationError
    from uniscale.config import ProfileConfig, load_profile, save_profile
    db_path = vault_db_path()
    profile = load_profile(db_path)
    updates = {k: v for k, v in {
        "name": args.name, "date_of_birth": args.date_of_birth,
        "birth_year": args.birth_year, "sex": args.sex, "smoker": args.smoker,
        "diabetic": args.diabetic, "bp_treated": args.bp_treated,
    }.items() if v is not None}
    if args.birth_year is not None and args.date_of_birth is None:
        updates["date_of_birth"] = None  # a bare year replaces a full date of birth
    if args.counter is not None:
        title, _, elements = args.counter.partition(":")
        updates["counter"] = {"title": title, "elements": [e.strip() for e in elements.split(",") if e.strip()]} if args.counter.strip() else None
    if updates:
        try:
            profile = save_profile(ProfileConfig.model_validate({**profile.model_dump(), **updates}), db_path)
        except ValidationError as e:
            print(f"[-] Invalid profile: {e.errors()[0]['msg']}")
            return
        print("[+] Profile saved.")
    print(f"Profile ({os.path.dirname(db_path)}):")
    for key, value in profile.model_dump().items():
        print(f"  {key.replace('_', ' ')}: {'not set' if value is None else value}")


def cmd_config(args):
    if args.config_action == "init":
        target = args.path or "~/.config/uniscale/config.toml"
        p = generate_default_config_file(target)
        print(f"[+] Initialized UniScale configuration file at {p}")
    elif args.config_action == "show":
        cfg = load_config(args.config)
        print(f"# Active configuration loaded from environment / disk:")
        print(cfg.model_dump_json(indent=2))


def cmd_garmin_test(args):
    print("Testing Garmin Connect authentication and upload...")
    sync = GarminSync()
    if sync.login():
        print("[+] Successfully logged in to Garmin Connect!")
        if args.send_test_weight:
            test_meas = WeightMeasurement(
                weight_kg=64.0,
                weight_lb=141.1,
                device_name="uniscale Test",
                device_address="00:00:00:00:00:00"
            )
            print(f"Sending test weight ({test_meas.weight_lb} lb) to Garmin Connect...")
            ok = sync.sync_weight(test_meas)
            print("[+] Result:", "Success" if ok else "Failed")
    else:
        print("[-] Login failed. Ensure GARMIN_EMAIL and GARMIN_PASSWORD environment variables are set.")


def cmd_supplement(args):
    from uniscale.supplements import log_supplement_intake, get_supplements_summary, active_stack
    if args.supp_action == "log":
        res = log_supplement_intake(
            name=args.name,
            dosage=args.dosage,
            unit=args.unit,
            category=args.category,
            frequency=args.frequency,
            notes=args.notes
        )
        print(f"[+] Logged: {res['name']} {res['dosage']}{res['unit']} ({res['category']}, {res['frequency']})")
    elif args.supp_action == "list":
        summary = get_supplements_summary(days=args.days)
        print(f"=== UniScale Supplement Adherence ({summary['today_date']}) ===")
        print(f"Today's Progress: {summary['completed_count']}/{summary['total_count']} ({summary['completion_rate_percent']}%)")
        for item in summary["checklist"]:
            st = "DONE" if item["taken_today"] else "PENDING"
            print(f"  [{st}] {item['name']} ({item['dosage']}{item['unit']} - {item['frequency']}): {item['purpose']}")
        print(f"\nRecent Logged Intakes:")
        for r in summary["recent_intakes"][:10]:
            print(f"  - {str(r.get('timestamp'))[:16]}: {r.get('name')} {r.get('dosage')}{r.get('unit')}")
    elif args.supp_action == "stack":
        from uniscale.supplements import add_to_stack, remove_from_stack, reset_stack_to_config
        if args.reset:
            stack = reset_stack_to_config()
            print(f"[+] Reset stack to [[supplements.stack]] in config ({len(stack)} compounds).")
        elif args.remove:
            updated = remove_from_stack(args.remove)
            if updated is None:
                print(f"[-] '{args.remove}' is not in the active regimen stack.")
            else:
                print(f"[+] Removed '{args.remove}' from active regimen stack ({len(updated)} compounds remaining).")
        elif args.add or args.name:
            if not args.name or args.dosage is None:
                print("[-] Please specify --name and --dosage of the supplement to add to stack.")
                return
            item = {
                "name": args.name,
                "dosage": args.dosage,
                "unit": args.unit,
                "frequency": args.frequency,
                "category": args.category,
                "purpose": getattr(args, "purpose", "") or ""
            }
            updated = add_to_stack(item)
            print(f"[+] Added/updated in stack: {item['name']} {item['dosage']}{item['unit']} ({item['frequency']})")
        else:
            stack = active_stack()
            print("=== Configured Active Longevity Stack ===")
            if not stack:
                print("  (No compounds configured in active stack. Use 'uniscale supplement stack --add --name <name> ...' to add)")
            for s in stack:
                purp = f": {s['purpose']}" if s.get('purpose') else ""
                print(f"  • {s['name']} ({s['dosage']}{s['unit']}, {s['frequency']}){purp}")
    else:
        print("Usage: uniscale supplement {log,list,stack} [options]")


def cmd_import_sleep(args):
    from uniscale.sleep import import_garmin_sleep_from_zip, get_sleep_summary
    path = os.path.expanduser(args.path)
    if not os.path.exists(path):
        print(f"[-] File not found: {path}")
        return
    count = import_garmin_sleep_from_zip(path)
    print(f"[+] Successfully imported {count} sleep and recovery records into vault.")
    summary = get_sleep_summary()
    if summary["latest"]:
        l = summary["latest"]
        print(f"[+] Latest Sleep ({l.get('date')}): {l.get('duration_hours')} hrs | Score: {l.get('sleep_score')}/100 | HRV: {l.get('hrv_rmssd_ms')} ms")


def cmd_genomics(args):
    from uniscale.genomics import get_genomics_profile, import_dna_file
    db_path = vault_db_path()
    if args.gen_action == "report" or not args.gen_action:
        prof = get_genomics_profile(db_path)
        print("=== Sovereign Genomic & Pharmacogenomics Profile ===")
        ts = prof.get("trio_status", {})
        if ts.get("is_phased_trio"):
            print(f"Pedigree Trio: Self ({ts.get('user_snps_total'):,} SNPs) | Father ({ts.get('father_snps_total'):,} SNPs) | Mother ({ts.get('mother_snps_total'):,} SNPs)")
        for k, v in prof.get("key_highlights", {}).items():
            print(f"  • {k.replace('_', ' ').title()}: {v}")
        if prof.get("trio_insights"):
            print("\nFamily Lineage & Inheritance Findings:")
            for ti in prof["trio_insights"]:
                print(f"  • {ti['trait']}: {ti['finding']}")
                print(f"    {ti['detail']}")
        print("\nClinical Variants Analyzed:")
        for v in prof.get("variants", []):
            inh = f" [{v.get('inheritance')}]" if v.get("inheritance") else ""
            print(f"  [{v.get('badge')}] {v.get('gene')} ({v.get('rsid')} - {v.get('genotype')}): {v.get('status')}{inh}")
            print(f"      Significance: {v.get('significance')}")
            print(f"      Action: {v.get('recommendation')}")
        if prof.get("top_promethease_findings"):
            print("\nTop Promethease High-Magnitude Findings (>= 2.5):")
            for tf in prof["top_promethease_findings"][:10]:
                print(f"  • {tf['gene']} ({tf['rsid']} {tf['genotype']}) [Mag {tf['magnitude']} - {tf['repute']}]: {tf['summary']}")
    elif args.gen_action == "import-trio":
        self_zip = os.path.expanduser(args.self)
        father_zip = os.path.expanduser(args.father) if args.father else None
        mother_zip = os.path.expanduser(args.mother) if args.mother else None
        ancestry_zip = os.path.expanduser(args.ancestry) if getattr(args, "ancestry", None) else None
        try:
            prof = import_dna_file(self_zip, father_zip, mother_zip, ancestry_zip=ancestry_zip, db_path=db_path)
        except (ValueError, OSError, zipfile.BadZipFile) as e:
            print(f"[-] Failed to import DNA: {e}")
            return
        ts = prof.get("trio_status", {})
        print(f"[+] Successfully imported and phased Family Trio:")
        print(f"    - Self SNPs: {ts.get('user_snps_total'):,}")
        print(f"    - Father SNPs: {ts.get('father_snps_total'):,}")
        print(f"    - Mother SNPs: {ts.get('mother_snps_total'):,}")
        print(f"    - Total Family SNPs: {ts.get('total_family_snps', 0):,}")
        print(f"[+] Saved genomic profile to {db_path}")
    elif args.gen_action == "import":
        path = os.path.expanduser(args.path)
        if not os.path.exists(path):
            print(f"[-] File not found: {path}")
            return
        # Family members' archives go through `genomics import-trio --father/--mother`.
        try:
            prof = import_dna_file(path, db_path=db_path)
        except (ValueError, OSError, zipfile.BadZipFile) as e:
            print(f"[-] Failed to import DNA: {e}")
            return
        count = prof.get("snps_found", prof.get("trio_status", {}).get("user_snps_total", 0))
        print(f"[+] Imported {count:,} SNPs and saved genomic profile to {db_path}")


def cmd_chat(args):
    from uniscale.ai import chat_with_agent
    cfg = load_config(getattr(args, "config", None))
    endpoint = getattr(args, "endpoint", None) or cfg.ai.endpoint
    model = getattr(args, "model", None) or cfg.ai.model
    db_path = vault_db_path(cfg)
    query = " ".join(args.query) if args.query else "Provide a clinical summary of my longevity biomarkers and recovery."
    print(f"Querying Dr. Gemma: '{query}'...")
    res = chat_with_agent([{"role": "user", "content": query}], endpoint=endpoint, model=model, db_path=db_path,
                          thinking=cfg.ai.thinking, context_tokens=cfg.ai.context_tokens)
    print(f"\n[{res['engine']}]\n")
    print(res["reply"])


def main():
    parser = argparse.ArgumentParser(description="uniscale - Unified Health Bridge")
    parser.add_argument("-c", "--config", help="Path to config.toml (defaults to ~/.config/uniscale/config.toml or ./uniscale.toml)")
    parser.add_argument("-u", "--user", help="User whose vault to use in multi-user mode (or set UNISCALE_USER)")
    subparsers = parser.add_subparsers(dest="command")

    p_scan = subparsers.add_parser("scan", help="Scan for nearby Bluetooth health devices")
    p_scan.add_argument("--timeout", type=float, default=10.0, help="Scan timeout in seconds")

    p_daemon = subparsers.add_parser("daemon", help="Run background auto-detection & sync daemon")
    p_daemon.add_argument("--no-garmin", action="store_true", help="Disable Garmin cloud sync")

    p_history = subparsers.add_parser("history", help="Show local measurement history")
    p_history.add_argument("-n", "--limit", type=int, default=25, help="Number of records to display (default: 25)")
    p_history.add_argument("-t", "--type", choices=["weight", "blood_pressure", "ecg", "activity", "workout", "sleep", "supplement", "genomics"], help="Filter by record type")

    p_counter = subparsers.add_parser("counter", help="Show or change today's (or a past day's) counts for the counter set in your profile")
    p_counter.add_argument("-d", "--date", help="Target date (YYYY-MM-DD), defaults to today")
    p_counter.add_argument("--set", nargs="+", metavar="NAME=N", help='Set counts explicitly, e.g. --set "D(out)=3" "D(in)=1"')
    p_counter.add_argument("--inc", metavar="NAME", help="Add 1 to a counter (its label or key)")
    p_counter.add_argument("--dec", metavar="NAME", help="Take 1 from a counter (its label or key)")
    p_counter.add_argument("--import", dest="import_file", help="JSON or CSV of past days (a date column plus one column per counter)")
    p_counter.add_argument("-n", "--history", type=int, default=10, help="Number of historical days to list (default: 10)")

    p_place = subparsers.add_parser("place", help="View, add, or delete dwell places (>1 hr)")
    p_place_sub = p_place.add_subparsers(dest="place_action")
    p_place_list = p_place_sub.add_parser("list", help="List dwell places for a date")
    p_place_list.add_argument("-d", "--date", help="Date (YYYY-MM-DD or 'previous'), default: previous")
    p_place_add = p_place_sub.add_parser("add", help="Manually add a dwell place")
    p_place_add.add_argument("name", help="Name of the place (e.g. Gym, Office, Library)")
    p_place_add.add_argument("-d", "--date", help="Date (YYYY-MM-DD), default: yesterday")
    p_place_add.add_argument("-m", "--mins", type=int, default=60, help="Duration in minutes (default: 60)")
    p_place_add.add_argument("--lat", type=float, default=0.0, help="Latitude (optional)")
    p_place_add.add_argument("--lon", type=float, default=0.0, help="Longitude (optional)")
    p_place_sync = p_place_sub.add_parser("sync", help="Read places from the OwnTracks Recorder ([owntracks] in config)")
    p_place_sync.add_argument("--days", type=int, help="Re-read this many days back (default: days not read yet)")
    p_place_del = p_place_sub.add_parser("delete", help="Delete a dwell place by ID")
    p_place_del.add_argument("place_id", help="Place ID to delete")

    p_supp = subparsers.add_parser("supplement", help="Track medications, supplements, and longevity stacks")
    p_supp_sub = p_supp.add_subparsers(dest="supp_action")
    p_supp_log = p_supp_sub.add_parser("log", help="Log a supplement or medication intake")
    p_supp_log.add_argument("name", help="Name of supplement (e.g. Creatine, Omega-3)")
    p_supp_log.add_argument("dosage", type=float, help="Dosage amount (e.g. 5000, 2000)")
    p_supp_log.add_argument("--unit", default="mg", help="Dosage unit (default: mg, or IU, g)")
    p_supp_log.add_argument("--category", default="supplement", choices=["supplement", "medication", "vitamin", "mineral", "longevity", "nootropic"])
    p_supp_log.add_argument("--frequency", default="daily", choices=["daily", "morning", "evening", "as_needed"])
    p_supp_log.add_argument("--notes", help="Optional notes")

    p_supp_list = p_supp_sub.add_parser("list", help="List active daily stack adherence and recent intakes")
    p_supp_list.add_argument("--days", type=int, default=7, help="Days of history to analyze")

    p_supp_stack = p_supp_sub.add_parser("stack", help="View and edit active longevity regimen stack")
    p_supp_stack.add_argument("--add", action="store_true", help="Add or update a compound in your stack")
    p_supp_stack.add_argument("--remove", help="Remove a compound from your stack by name")
    p_supp_stack.add_argument("--name", help="Name of supplement to add/update")
    p_supp_stack.add_argument("--reset", action="store_true", help="Discard edits and revert to [[supplements.stack]] in config")
    p_supp_stack.add_argument("--dosage", type=float, help="Dosage amount (required with --add)")
    p_supp_stack.add_argument("--unit", default="mg", help="Dosage unit (default: mg)")
    p_supp_stack.add_argument("--frequency", default="daily", help="Frequency (morning, evening, daily, as needed)")
    p_supp_stack.add_argument("--category", default="supplement", help="Category (supplement, vitamin, mineral, longevity, nootropic)")
    p_supp_stack.add_argument("--purpose", default="", help="Purpose or target biomarker")

    p_sleep_imp = subparsers.add_parser("import-sleep", help="Import Garmin or wearable sleep and recovery records")
    p_sleep_imp.add_argument("path", help="Path to Garmin data export zip file")

    p_gen = subparsers.add_parser("genomics", help="Analyze offline raw DNA files (ApoE, MTHFR, pharmacogenomics)")
    p_gen_sub = p_gen.add_subparsers(dest="gen_action")
    p_gen_sub.add_parser("report", help="View sovereign genomic & pharmacogenomics profile")
    p_gen_trio = p_gen_sub.add_parser("import-trio", help="Import Promethease and Ancestry archives for family trio")
    p_gen_trio.add_argument("--self", required=True, help="Path to offspring Promethease zip")
    p_gen_trio.add_argument("--father", help="Path to father Promethease zip")
    p_gen_trio.add_argument("--mother", help="Path to mother Promethease zip")
    p_gen_trio.add_argument("--ancestry", help="Path to raw AncestryDNA zip file")
    p_gen_imp = p_gen_sub.add_parser("import", help="Import AncestryDNA or 23andMe raw genotype file/zip")
    p_gen_imp.add_argument("path", help="Path to raw DNA text file")

    p_chat = subparsers.add_parser("chat", help="Interactive clinical consultation with Dr. Gemma")
    p_chat.add_argument("query", nargs="*", help="Question or prompt for Dr. Gemma")
    p_chat.add_argument("--endpoint", help="OpenAI-compatible LLM base URL (default: ai.endpoint from config, http://localhost:11434)")
    p_chat.add_argument("--model", help="LLM model name (default: ai.model from config, gemma:latest)")

    p_analyze = subparsers.add_parser("analyze", help="Generate AI health insights from device telemetry (Gemma 4)")
    p_analyze.add_argument("--prompt-only", action="store_true", help="Print the structured prompt without sending to an LLM")
    p_analyze.add_argument("--endpoint", help="OpenAI-compatible LLM base URL (default: ai.endpoint from config, http://localhost:11434)")
    p_analyze.add_argument("--model", help="LLM model name (default: ai.model from config, gemma:latest)")
    p_analyze.add_argument("--db", help="Path to custom measurements.jsonl")

    p_import = subparsers.add_parser("import-adb", help="Extract and import databases from connected phone via ADB")

    p_garmin_imp = subparsers.add_parser("import-garmin", help="Import a Garmin .FIT file or full data export (.zip or folder), including sleep/HRV")
    p_garmin_imp.add_argument("path", help="Path to .fit file or extracted Garmin export directory")

    p_gfit_imp = subparsers.add_parser("import-google-fit", help="Import Google Fit Takeout export directory")
    p_gfit_imp.add_argument("path", help="Path to Google Takeout .zip or extracted Takeout/Fit directory")

    p_csv = subparsers.add_parser("import-csv", help="Import Fitdays or Omron exported CSV file")
    p_csv.add_argument("path", help="Path to exported CSV file")

    p_vesync = subparsers.add_parser("import-vesync", help="Import VeSync scraped JSON history")
    p_vesync.add_argument("path", help="Path to VeSync scraped JSON file")

    p_lab = subparsers.add_parser("import-lab", help="Import Quest Diagnostics or LabCorp PDF lab report")
    p_lab.add_argument("path", help="Path to lab report PDF or JSON")

    p_serve = subparsers.add_parser("serve", help="Run self-hosted UniScale web dashboard & sync API")
    p_serve.add_argument("--host", help="Bind host (default: server.host from config, 127.0.0.1)")
    p_serve.add_argument("--port", type=int, help="Bind port (default: server.port from config, 8088)")
    p_serve.add_argument("--reload", action="store_true", help="Enable auto-reload for development")

    p_prof = subparsers.add_parser("profile", help="Show or edit your profile (name, date of birth, sex, ASCVD risk factors)")
    p_prof.add_argument("--name", help="Your name, shown in the dashboard")
    p_prof.add_argument("--date-of-birth", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="Date of birth (for chronological / biological age)")
    p_prof.add_argument("--birth-year", type=int, help="Year of birth only, if you'd rather not give the full date")
    p_prof.add_argument("--sex", choices=["male", "female"], help="Needed for the ASCVD pooled-cohort equations")
    p_prof.add_argument("--smoker", action=argparse.BooleanOptionalAction, default=None, help="Current smoker")
    p_prof.add_argument("--diabetic", action=argparse.BooleanOptionalAction, default=None, help="Diabetic")
    p_prof.add_argument("--bp-treated", action=argparse.BooleanOptionalAction, default=None, help="Taking blood-pressure medication")
    p_prof.add_argument("--counter", metavar='"TITLE: A, B"', help='Daily counter tile, e.g. "D Tracker: D(out), D(in)"; "" removes it')

    p_cfg = subparsers.add_parser("config", help="Manage configuration file")
    p_cfg.add_argument("config_action", choices=["init", "show"], help="Action: init (create default file), show (print active config)")
    p_cfg.add_argument("path", nargs="?", help="Destination path for config init (optional)")

    p_garmin = subparsers.add_parser("garmin-test", help="Test Garmin Connect credentials")
    p_garmin.add_argument("--send-test-weight", action="store_true", help="Send a test 141.1 lb reading")

    args = parser.parse_args()
    # Modules that call load_config() themselves (server, supplements, longevity) see -c too.
    if args.config:
        os.environ["UNISCALE_CONFIG"] = os.path.abspath(os.path.expanduser(args.config))
    if args.user:
        try:
            os.environ["UNISCALE_USER"] = normalize_user(args.user)
        except ValueError as e:
            parser.error(str(e))
    try:
        if args.command == "scan":
            cmd_scan(args)
        elif args.command == "daemon":
            cmd_daemon(args)
        elif args.command == "history":
            cmd_history(args)
        elif args.command == "counter":
            cmd_counter(args)
        elif args.command == "place":
            cmd_place(args)
        elif args.command == "analyze":
            cmd_analyze(args)
        elif args.command == "import-adb":
            cmd_import_adb(args)
        elif args.command == "import-garmin":
            cmd_import_garmin(args)
        elif args.command == "import-google-fit":
            cmd_import_google_fit(args)
        elif args.command == "import-csv":
            cmd_import_csv(args)
        elif args.command == "import-vesync":
            cmd_import_vesync(args)
        elif args.command == "import-lab":
            cmd_import_lab(args)
        elif args.command == "serve":
            cmd_serve(args)
        elif args.command == "import-sleep":
            cmd_import_sleep(args)
        elif args.command == "supplement":
            cmd_supplement(args)
        elif args.command == "genomics":
            cmd_genomics(args)
        elif args.command == "chat":
            cmd_chat(args)
        elif args.command == "profile":
            cmd_profile(args)
        elif args.command == "config":
            cmd_config(args)
        elif args.command == "garmin-test":
            cmd_garmin_test(args)
        else:
            parser.print_help()
    except NoUserError as e:
        print(f"[-] {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
