import json
import logging
import os
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from uniscale.config import load_profile, vault_db_path

logger = logging.getLogger(__name__)


def _demographics(p) -> Dict[str, Any]:
    d = p.model_dump(mode="json")
    d["age"] = p.age_on(date.today())
    return d


def build_health_profile(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Extracts structured health metrics from measurements.jsonl for AI analysis,
    including body composition, vitals, ECG, activities, 5-year labs, longevity (PhenoAge),
    sleep architecture, supplements, and genomics.
    """
    path = db_path or vault_db_path()
    if not os.path.exists(path):
        return {"error": f"Database not found at {path}"}

    from uniscale.readings import load_vault
    records = load_vault(path)

    weights = [r for r in records if "weight_kg" in r or r.get("type") == "weight"]
    bps = [r for r in records if "systolic" in r or r.get("type") == "blood_pressure"]
    ecgs = [r for r in records if "samples_mv" in r or r.get("type") == "ecg"]
    activities = [r for r in records if r.get("type") == "activity"]  # load_vault types every record
    workouts = [r for r in records if "sport" in r or r.get("type") == "workout"]
    labs = [r for r in records if r.get("type") == "blood_test" or "markers" in r]
    sleeps = [r for r in records if r.get("type") == "sleep"]

    # Longevity
    from uniscale.longevity import get_longevity_profile
    longevity_data = get_longevity_profile(path)

    # Supplements
    from uniscale.supplements import get_supplements_summary
    supplements_data = get_supplements_summary(path)

    # Sleep summary
    from uniscale.sleep import get_sleep_summary
    sleep_data = get_sleep_summary(path)

    # Genomics
    from uniscale.genomics import get_genomics_profile
    genomics_data = get_genomics_profile(path)

    profile = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_records": len(records),
        "weights": {
            "count": len(weights),
            "latest": weights[-1] if weights else None,
            "recent_samples": weights[-10:] if weights else [],
        },
        "blood_pressure": {
            "count": len(bps),
            "latest": bps[-1] if bps else None,
            "recent_samples": bps[-10:] if bps else [],
        },
        "ecg": {
            "count": len(ecgs),
            "latest": {
                "timestamp": ecgs[-1].get("timestamp"),
                "heart_rate": ecgs[-1].get("heart_rate"),
                "rhythm_status": ecgs[-1].get("rhythm_status", "Normal Sinus Rhythm"),
                "duration_seconds": ecgs[-1].get("duration_seconds", 30.0),
            } if ecgs else None,
        },
        "activity": {
            "count": len(activities),
            "latest": activities[-1] if activities else None,
            "recent_7_days": activities[-7:] if activities else [],
        },
        "workouts": {
            "count": len(workouts),
            "recent": workouts[-10:] if workouts else [],
        },
        "labs": {
            "count": len(labs),
            "latest": labs[-1] if labs else None,
            "history": labs,
        },
        "longevity": longevity_data,
        "demographics": _demographics(load_profile(path)),
        "supplements": supplements_data,
        "sleep": sleep_data,
        "genomics": genomics_data
    }

    # Counters, places and health day by day, and the correlations between them
    try:
        from uniscale.patterns import build_patterns
        profile["patterns"] = build_patterns(path, records)
    except Exception as e:  # never let this cost the chat its whole dossier
        logger.warning(f"Couldn't build the daily patterns: {e}")

    if activities:
        recent_7 = activities[-7:]
        steps_7 = [a.get("steps", 0) for a in recent_7 if a.get("steps")]
        profile["activity"]["avg_steps_7d"] = round(sum(steps_7) / len(steps_7)) if steps_7 else 0

        recent_30 = activities[-30:]
        steps_30 = [a.get("steps", 0) for a in recent_30 if a.get("steps")]
        profile["activity"]["avg_steps_30d"] = round(sum(steps_30) / len(steps_30)) if steps_30 else 0

    return profile


def generate_health_summary_prompt(profile: Dict[str, Any], log_days: int = 30) -> str:
    """
    Constructs an extensive clinical dossier formatted for Gemma 4 clinical analysis, with the last
    `log_days` days of the daily log (the patterns always cover the full log).
    """
    from uniscale.patterns import patterns_prompt
    patterns_text = patterns_prompt(profile.get("patterns") or {}, log_days)
    w_latest = profile.get("weights", {}).get("latest") or {}
    bp_latest = profile.get("blood_pressure", {}).get("latest") or {}
    ecg_latest = profile.get("ecg", {}).get("latest") or {}
    act_latest = profile.get("activity", {}).get("latest") or {}
    avg_7 = profile.get("activity", {}).get("avg_steps_7d", "N/A")
    avg_30 = profile.get("activity", {}).get("avg_steps_30d", "N/A")
    workouts = profile.get("workouts", {}).get("recent") or []

    w_val = w_latest.get("weight_lb") or (round(w_latest.get("weight_kg", 0) * 2.20462, 1) if w_latest.get("weight_kg") else "--")
    w_kg = w_latest.get("weight_kg", "--")
    bmi = w_latest.get("bmi", "--")
    fat = w_latest.get("body_fat_percent", "--")

    bp_val = f"{bp_latest.get('systolic', '--')}/{bp_latest.get('diastolic', '--')} mmHg" if bp_latest else "No recent BP reading"
    pulse = bp_latest.get("pulse") or "--"

    ecg_rhythm = ecg_latest.get("rhythm_status", "--")
    ecg_hr = ecg_latest.get("heart_rate", "--")

    act_steps = act_latest.get("steps", "--")
    act_km = act_latest.get("distance_km", "--")
    act_cal = act_latest.get("calories", "--")
    act_rhr = act_latest.get("resting_heart_rate", "--")

    recent_workouts_text = ""
    for wo in workouts[-5:]:
        sport = wo.get("sport", "workout")
        dist = wo.get("distance_km") or (round(wo.get("distance_meters", 0) / 1000.0, 2))
        dur = round(wo.get("duration_seconds", 0) / 60, 1)
        hr = wo.get("avg_heart_rate", "--")
        ts = str(wo.get("timestamp", ""))[:10]
        recent_workouts_text += f"   - {ts}: {sport.upper()} – {dist} km in {dur} min (Avg HR: {hr} bpm)\n"

    # Labs
    labs_latest = profile.get("labs", {}).get("latest") or {}
    labs_markers = labs_latest.get("markers", {})
    labs_text = ""
    if labs_markers:
        labs_ts = str(labs_latest.get("timestamp", ""))[:10]
        labs_text = f"   - Latest Lab Date: {labs_ts} ({labs_latest.get('lab_name', 'Clinical Lab')})\n"
        for mk, mv in labs_markers.items():
            nm = mv.get("name") or mk
            val = mv.get("value")
            unit = mv.get("unit", "")
            flag = f" [{mv.get('flag')}]" if mv.get("flag") and mv.get("flag") != "Normal" else ""
            labs_text += f"     • {nm}: {val} {unit}{flag}\n"
    else:
        labs_text = "   - No clinical blood tests recorded.\n"

    # Longevity
    longev = profile.get("longevity", {}).get("current", {})
    pheno_age = longev.get("phenotypic_age", "N/A")
    c_age = longev.get("chronological_age", "N/A")
    delta_yrs = longev.get("biological_age_delta", "N/A")
    ascvd_raw = longev.get("ten_year_ascvd_risk")
    ascvd_10 = f"{ascvd_raw}%" if ascvd_raw is not None else f"N/A ({longev.get('ascvd_category') or 'not computed'})"
    demo = profile.get("demographics") or {}
    risk_flags = [label for key, label in (("smoker", "smoker"), ("diabetic", "diabetic"), ("bp_treated", "on BP medication")) if demo.get(key)]
    demo_str = (f"Name: {demo.get('name') or 'not set'} | Age: {demo.get('age') or 'not set'} | "
                f"Sex: {demo.get('sex') or 'not set'} | Risk factors: {', '.join(risk_flags) or 'none reported'}")

    # Sleep
    sleep_s = profile.get("sleep", {})
    latest_sl = sleep_s.get("latest") or {}
    avg_sl = sleep_s.get("averages_7d") or {}
    sl_dur = latest_sl.get("duration_hours", "N/A")
    sl_score = latest_sl.get("sleep_score", "N/A")
    sl_hrv = latest_sl.get("hrv_rmssd_ms", avg_sl.get("avg_hrv_rmssd_ms", "N/A"))
    sl_spo2 = latest_sl.get("avg_spo2", "N/A")

    # Supplements & Protocol
    supp = profile.get("supplements", {})
    checklist = supp.get("checklist", [])
    active_stack_items = supp.get("active_stack", checklist)
    if active_stack_items:
        stack_lines = []
        for c in active_stack_items:
            when = ", ".join(c["slots"]) if c.get("slots") else c.get("frequency")
            timing = f" ({when})" if when else ""
            purpose = f" – {c['purpose']}" if c.get('purpose') else ""
            cat = f" [{c.get('category')}]" if c.get('category') and c.get('category') != 'supplement' else ""
            stack_lines.append(f"     • {c['name']}: {c['dosage']:g} {c.get('unit', 'mg')}{timing}{cat}{purpose}")
        regimen_text = "\n".join(stack_lines)
    else:
        regimen_text = "     • No supplements configured in active regimen."

    taken_supps = [f"{c['name']} ({c['dosage']:g}{c['unit']})" for c in checklist if c.get("taken_today")]
    supp_today_str = ", ".join(taken_supps) if taken_supps else "None logged today yet"

    recent_intakes = supp.get("recent_intakes", [])
    recent_intake_names = []
    seen_names = set()
    for r in recent_intakes:
        nm = r.get("name")
        if nm and nm.lower() not in seen_names:
            seen_names.add(nm.lower())
            recent_intake_names.append(f"{nm} ({r.get('dosage')}{r.get('unit', 'mg')})")
    recent_intake_str = ", ".join(recent_intake_names[:8]) if recent_intake_names else "No recent intake logs"

    # Genomics
    gen_raw = profile.get("genomics", {})
    gen = gen_raw.get("key_highlights", {})
    gen_lines = "\n".join(
        f"   - {k.replace('_', ' ').title()}: {v}" for k, v in gen.items()
    ) or "   - No genomic data imported."
    trio_lines = "\n".join(
        f"   - {t['trait']}: {t['finding']}" for t in gen_raw.get("trio_insights", [])
    )

    prompt = f"""You are "Dr. Gemma", an expert preventive medicine, clinical longevity, and sports science AI advisor. You are reviewing the patient's sovereign, unified health vault telemetry.

### Patient Comprehensive Biometric Dossier:
1. **Biological Age & Cardiovascular Longevity:**
   - {demo_str}
   - Chronological Age: {c_age} | Phenotypic Age (PhenoAge): {pheno_age} years (Delta: {delta_yrs} years)
   - 10-Year ASCVD Risk (ACC/AHA Pooled Cohort Equations): {ascvd_10}

2. **Body Composition & Physical Vitals:**
   - Latest Weight: {w_val} lb ({w_kg} kg) | BMI: {bmi} | Body Fat: {fat}%
   - Latest Blood Pressure: {bp_val} (Pulse: {pulse} bpm)
   - Lead-I ECG Rhythm: {ecg_rhythm} (HR: {ecg_hr} bpm, 30s Lead-I trace)
   - Resting Heart Rate: {act_rhr} bpm

3. **Sleep Architecture & Autonomic Recovery:**
   - Latest Sleep: {sl_dur} hrs (Score: {sl_score}/100)
   - Nocturnal SpO2: {sl_spo2}% | Nightly HRV (rMSSD): {sl_hrv} ms
   - 7-Day Sleep Duration Avg: {avg_sl.get('avg_duration_hours', '--')} hrs | Deep: {avg_sl.get('deep_sleep_percent', '--')}% | REM: {avg_sl.get('rem_sleep_percent', '--')}%

4. **Daily Physical Activity & Training:**
   - Today's Steps: {act_steps} ({act_km} km, {act_cal} kcal)
   - 7-Day Avg Steps: {avg_7} | 30-Day Avg Steps: {avg_30}
{recent_workouts_text or '   - No recent workouts.'}

5. **Clinical Blood Chemistry:**
{labs_text}
6. **Active Daily Supplements & Protocol (User-Configured):**
   - Active Regimen / Current Stack:
{regimen_text}
   - Logged Taken Today: {supp_today_str}
   - Recently Taken Compounds (History): {recent_intake_str}

7. **Genomics & Pharmacogenomics:**
{gen_lines}
{trio_lines}

{patterns_text}

How to answer:
- Start with the answer itself. No greeting, no thanks, no compliments, no "great question", and don't restate the question or re-introduce yourself in later turns.
- Don't recite the dossier back. Quote a number only where it drives a conclusion ("LDL 142 mg/dL, so ...").
- Be concise and concrete: short headed sections and bullet points; specific doses, timings and thresholds rather than generalities.
- Say plainly when the data can't support a judgement, what is missing, and which lab or measurement would settle it.
- Flag anything that warrants a doctor's attention directly, without hedging the rest of the answer.
- This is a continuing conversation: build on your earlier answers instead of repeating them.

Supplement & Longevity Protocol Principles:
- Treat supplement recommendations as personalized, collaborative hypotheses rather than dogmatic absolutes. Regimens must adapt to what the patient is actually taking, their reported tolerance, and their evolving biometrics.
- When the patient asks about their supplements or asks for your opinion on what they are taking:
  1. Review their current stack, dosages, and recent intakes against their full biometric dossier (cardiovascular vitals, blood labs, sleep architecture/HRV, and genomics).
  2. Critique synergies and timing: Evaluate morning vs. evening scheduling (e.g. fat-soluble vitamins with food, magnesium before sleep, adaptogens), potential contraindications, and excessive or insufficient dosages.
  3. Say briefly what is working, and proactively propose evidence-based additions ("things to add") or adjustments that address specific biomarkers (e.g., elevated blood pressure, sub-optimal deep sleep, high ASCVD risk, lipid fractions, or MTHFR/APOE variants).
  4. For every suggested addition or adjustment, state:
     - Proposed compound name & recommended starting dosage/unit
     - Optimal timing (morning/evening, with/without food)
     - Physiological mechanism & specific biomarker it targets
     - Monitoring guidelines (what labs or metrics to watch)
"""
    return prompt.strip()


def query_chat(
    messages: List[Dict[str, str]],
    endpoint: str = "http://localhost:11434",
    model: str = "gemma:latest",
    timeout: int = 240,
    max_tokens: Optional[int] = None,
    thinking: Optional[bool] = None
) -> Tuple[str, bool]:
    """
    Sends a chat (system/user/assistant messages) to an OpenAI-compatible endpoint (Ollama, llama.cpp
    server, vLLM, ...). `endpoint` is the server's base URL (a trailing /v1 is stripped).
    Returns (reply, truncated), truncated meaning the model stopped at max_tokens mid-answer.
    thinking=False asks thinking models to answer without reasoning first (ignored by servers and
    models that don't support it); None leaves the server's default.
    """
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    api_url = f"{base}/v1/chat/completions"
    payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": thinking}

    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            choice = json.loads(response.read().decode("utf-8"))["choices"][0]
            return choice["message"]["content"], choice.get("finish_reason") == "length"
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"Unable to reach LLM endpoint at {api_url}: {e}")
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"Unexpected response from {api_url}: {e}")


def query_llm(
    prompt: str,
    endpoint: str = "http://localhost:11434",
    model: str = "gemma:latest",
    timeout: int = 240,
    max_tokens: Optional[int] = None
) -> str:
    """A single-prompt request (`uniscale analyze`); see query_chat."""
    return query_chat([{"role": "user", "content": prompt}], endpoint, model, timeout, max_tokens)[0]


# ponytail: fixed cap so one answer can't hog a shared GPU box; a cut-off reply offers "Continue".
CHAT_MAX_TOKENS = 2048
# Context window the chat assumes when [ai] context_tokens isn't set (llama.cpp -c per slot). A long
# conversation drops its oldest turns to stay inside it, leaving room for the reply.
CHAT_CONTEXT_TOKENS = 8192
LONG_LOG_CONTEXT = 16384  # from this context size up, the prompt carries 90 days of the daily log, else 30
CHARS_PER_TOKEN = 3  # measured ~3.0 on the health dossier with Gemma's tokenizer; errs on the safe side


def _fit_context(chat: List[Dict[str, str]], context_tokens: int = CHAT_CONTEXT_TOKENS) -> List[Dict[str, str]]:
    """Keeps the system message and as many of the latest turns as fit, starting on a user turn."""
    budget = (context_tokens - CHAT_MAX_TOKENS - 256) * CHARS_PER_TOKEN - len(chat[0]["content"])
    turns = chat[1:]
    while len(turns) > 1 and (sum(len(m["content"]) for m in turns) > budget or turns[0]["role"] != "user"):
        turns = turns[1:]
    return [chat[0]] + turns


CONTINUE_INSTRUCTION = ("Your last answer was cut off. Continue exactly where it stopped, mid-sentence if "
                        "need be. Don't repeat, summarise or re-introduce anything.")


def chat_with_agent(
    messages: List[Dict[str, str]],
    endpoint: str = "http://localhost:11434",
    model: str = "gemma:latest",
    db_path: Optional[str] = None,
    continue_reply: bool = False,
    thinking: bool = False,
    context_tokens: int = CHAT_CONTEXT_TOKENS
) -> Dict[str, Any]:
    """
    Handles interactive dialogue with Dr. Gemma, injecting full patient telemetry.
    If Ollama is offline or warming up, delivers an intelligent clinical response
    derived directly from the patient's biomarkers.
    """
    profile = build_health_profile(db_path)

    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = m.get("content", "").strip()
            break

    # A real conversation: the dossier and instructions as the system message, then the turns as they
    # happened, so the model continues it instead of starting a fresh consultation every time.
    log_days = 90 if context_tokens >= LONG_LOG_CONTEXT else 30
    chat = [{"role": "system", "content": generate_health_summary_prompt(profile, log_days)}]
    chat += [{"role": "assistant" if m.get("role") == "assistant" else "user", "content": m.get("content", "")}
             for m in messages if m.get("content")]
    if continue_reply:
        chat.append({"role": "user", "content": CONTINUE_INSTRUCTION})
    chat = _fit_context(chat, context_tokens)

    try:
        answer, truncated = query_chat(chat, endpoint=endpoint, model=model, timeout=240,
                                       max_tokens=CHAT_MAX_TOKENS, thinking=thinking)
        return {
            "reply": answer,
            "truncated": truncated,
            "engine": f"Local LLM ({model})",
            "model": model,
            "status": "success"
        }
    except Exception as e:
        logger.warning(f"Local LLM query failed ({e}), generating sovereign clinical synthesis...")
        fallback = generate_clinical_fallback(last_user_msg, profile)
        return {
            "reply": fallback,
            "engine": "UniScale Sovereign Clinical Engine (Local Fallback)",
            "model": model,
            "status": "fallback"
        }


def generate_clinical_fallback(query: str, profile: Dict[str, Any]) -> str:
    """
    Reply used when the configured LLM endpoint is unreachable: the dossier the
    model would have seen, with no interpretation added.
    """
    dossier = generate_health_summary_prompt(profile)
    body = dossier.split("### Patient Comprehensive Biometric Dossier:", 1)[-1].split("Instructions:", 1)[0]
    return (
        "### Local AI unavailable\n\n"
        "The LLM endpoint in the [ai] config section could not be reached, so no analysis was generated. "
        "This is the data the assistant would have used:\n" + body.rstrip()
    )
