import logging
import math
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uniscale.config import load_profile, vault_db_path

logger = logging.getLogger(__name__)

# Typical healthy-adult spread for markers a panel often lacks, in lab units:
# (value that makes PhenoAge younger, value that makes it older). Used to show
# how far an assumed marker could move the result, not to guess its value.
ASSUMED_SPAN = {"lymphocytes": (40.0, 20.0), "crp": (0.3, 3.0)}
PHENO_MARKERS = 9


def calculate_pheno_age(
    markers: Dict[str, Any],
    chronological_age: float,
    crp_mg_dl: float = 0.1
) -> Dict[str, Any]:
    """
    Computes Morgan Levine's Phenotypic Age (PhenoAge) from 9 clinical blood biomarkers
    and chronological age (Levine et al., 2018 - Aging Cell).

    Biomarkers required:
    - Albumin (g/dL -> converted to g/L)
    - Creatinine (mg/dL -> converted to umol/L)
    - Fasting Glucose (mg/dL -> converted to mmol/L)
    - ln(CRP in mg/dL) (defaults to 0.1 mg/dL / 1.0 mg/L if unmeasured)
    - Lymphocyte percentage (%)
    - Mean Cell Volume (MCV, fL)
    - Red Cell Distribution Width (RDW, %)
    - Alkaline Phosphatase (ALP, U/L)
    - White Blood Cell count (WBC, 10^3 cells/uL)
    """
    assumed: List[str] = []

    def get_val(key: str, default: float) -> float:
        m = markers.get(key)
        if isinstance(m, dict):
            m = m.get("value")
        try:
            if m is not None:
                return float(m)
        except (ValueError, TypeError):
            pass
        assumed.append(key)
        return default

    # 1. Units conversion (missing markers fall back to typical adult reference values)
    albumin_g_dl = get_val("albumin", 4.2)
    creatinine_mg_dl = get_val("creatinine", 0.86)
    glucose_mg_dl = get_val("glucose", 100.0)
    alb_g_l = albumin_g_dl * 10.0
    creat_umol = creatinine_mg_dl * 88.4
    gluc_mmol = glucose_mg_dl * 0.0555
    lymph_pct = get_val("lymphocytes", 30.0)
    mcv_fl = get_val("mcv", 90.0)
    rdw_pct = get_val("rdw", 13.0)
    alp_ul = get_val("alkaline_phosphatase", 70.0)
    wbc_val = get_val("wbc", 6.0)
    # Labs report hs-CRP in mg/L; the model wants mg/dL.
    crp_mg_dl = get_val("crp", crp_mg_dl * 10.0) / 10.0

    # 2. Gompertz linear risk score (xb)
    xb = (
        -19.9067
        - 0.0336 * alb_g_l
        + 0.0095 * creat_umol
        + 0.1953 * gluc_mmol
        + 0.0954 * math.log(max(crp_mg_dl, 0.01))
        - 0.0120 * lymph_pct
        + 0.0268 * mcv_fl
        + 0.3306 * rdw_pct
        + 0.0019 * alp_ul
        + 0.0554 * wbc_val
        + 0.0804 * chronological_age
    )

    # 3. 10-year cumulative mortality hazard M
    gamma = 0.0076927
    b0 = (math.exp(120.0 * gamma) - 1.0) / gamma
    exp_xb = math.exp(xb)
    M = 1.0 - math.exp(-exp_xb * b0)
    M = min(max(M, 0.0001), 0.9999)

    # 4. Phenotypic Age
    val = -0.00553 * math.log(1.0 - M)
    pheno_age = 141.50225 + (math.log(val) / 0.090165)

    delta = pheno_age - chronological_age
    acceleration_pct = (delta / chronological_age) * 100.0 if chronological_age > 0 else 0.0

    return {
        "chronological_age": round(chronological_age, 1),
        "phenotypic_age": round(pheno_age, 1),
        "delta_years": round(delta, 1),
        "acceleration_percent": round(acceleration_pct, 1),
        "biological_status": "Rejuvenated" if delta < -3.0 else ("Optimal" if delta <= 0 else "Accelerated"),
        "mortality_hazard_score": round(xb, 4),
        "ten_year_mortality_risk_percent": round(M * 100.0, 2),
        "biomarkers_used": {
            "albumin_g_dl": albumin_g_dl,
            "creatinine_mg_dl": creatinine_mg_dl,
            "fasting_glucose_mg_dl": glucose_mg_dl,
            "lymphocyte_percent": lymph_pct,
            "mcv_fl": mcv_fl,
            "rdw_percent": rdw_pct,
            "alp_ul": alp_ul,
            "wbc_10_3_ul": wbc_val,
            "crp_mg_dl": crp_mg_dl
        },
        "assumed_markers": assumed,
    }


def calculate_ascvd_risk(
    age: int,
    total_chol: float,
    hdl: float,
    systolic_bp: int,
    bp_treated: bool = False,
    smoker: bool = False,
    diabetic: bool = False,
    race: str = "white",
    sex: str = "male"
) -> Dict[str, Any]:
    """
    Computes 10-Year ASCVD Risk using the 2013 ACC/AHA Pooled Cohort Equations.
    """
    age = max(min(age, 79), 40)
    ln_age = math.log(age)
    ln_tc = math.log(max(total_chol, 100.0))
    ln_hdl = math.log(max(hdl, 20.0))
    ln_sbp = math.log(max(systolic_bp, 90.0))

    if sex.lower() == "male":
        # White / Non-Black Men
        h = (
            12.344 * ln_age
            + 11.853 * ln_tc
            - 2.664 * ln_age * ln_tc
            - 7.990 * ln_hdl
            + 1.769 * ln_age * ln_hdl
            + (1.797 if bp_treated else 1.764) * ln_sbp
            + (7.837 - 1.795 * ln_age if smoker else 0.0)
            + (0.658 if diabetic else 0.0)
        )
        mean_terms = 61.18
        s10 = 0.9144
    else:
        # White Women
        h = (
            -29.799 * ln_age
            + 4.884 * (ln_age ** 2)
            + 13.540 * ln_tc
            - 3.114 * ln_age * ln_tc
            - 13.578 * ln_hdl
            + 3.149 * ln_age * ln_hdl
            + (2.019 if bp_treated else 1.957) * ln_sbp
            + (7.574 - 1.665 * ln_age if smoker else 0.0)
            + (0.661 if diabetic else 0.0)
        )
        mean_terms = -29.18
        s10 = 0.9665

    risk = 100.0 * (1.0 - math.pow(s10, math.exp(h - mean_terms)))
    risk = round(min(max(risk, 0.5), 99.0), 1)

    category = "Low (<5%)"
    if risk >= 20.0:
        category = "High (≥20%)"
    elif risk >= 7.5:
        category = "Borderline / Intermediate (7.5–19.9%)"
    elif risk >= 5.0:
        category = "Borderline (5–7.4%)"

    return {
        "ten_year_ascvd_risk_percent": risk,
        "risk_category": category,
        "parameters": {
            "age": age,
            "total_cholesterol": total_chol,
            "hdl": hdl,
            "systolic_bp": systolic_bp,
            "bp_treated": bp_treated,
            "smoker": smoker,
            "diabetic": diabetic
        }
    }


def get_longevity_profile(db_path: Optional[str] = None, birth_year: Optional[int] = None) -> Dict[str, Any]:
    """
    Extracts all laboratory reports and physical vitals from measurements.jsonl
    to assemble a multi-year biological age and cardiovascular longevity report.
    Birth year, sex and ASCVD risk factors come from the vault owner's profile (load_profile);
    an explicit birth_year overrides the profile's.
    """
    path = db_path or vault_db_path()
    profile = load_profile(path)
    explicit_year = birth_year
    if birth_year is None:
        birth_year = profile.birth_year
    if birth_year is None:
        return {"error": "Set your date of birth in your profile (dashboard 👤 Profile, or `uniscale profile --date-of-birth YYYY-MM-DD`) to compute biological age"}
    if not os.path.exists(path):
        return {"error": "Database not found"}

    labs = []
    latest_bp = None
    latest_body = None

    from uniscale.readings import load_vault
    for d in load_vault(path):  # sorted by time, so the last BP seen is the latest
        if d.get("type") == "blood_test":
            labs.append(d)
        elif d.get("type") == "blood_pressure":
            latest_bp = d
        elif d.get("type") == "weight" and d.get("bmi"):
            latest_body = d

    labs.sort(key=lambda x: str(x.get("timestamp") or ""))

    if not labs:
        return {"error": "No blood test records found"}

    yearly_reports = []
    default_sbp = latest_bp.get("systolic", 130) if latest_bp else 130

    for l in labs:
        ts = str(l.get("timestamp") or "")[:10]
        try:
            yr = int(ts[:4])
        except Exception:
            continue
        # Exact age on the lab date when the full date of birth is known (and not overridden).
        c_age = yr - birth_year
        if explicit_year is None and profile.date_of_birth:
            try:
                c_age = profile.age_on(date.fromisoformat(ts))
            except ValueError:
                pass
        m = l.get("markers", {})

        p_res = calculate_pheno_age(m, c_age)

        # ASCVD
        tc = m.get("total_cholesterol", {}).get("value", 200.0)
        hdl = m.get("hdl_cholesterol", {}).get("value", 50.0)
        if profile.sex:
            ascvd_res = calculate_ascvd_risk(
                c_age, tc, hdl, default_sbp,
                bp_treated=profile.bp_treated, smoker=profile.smoker,
                diabetic=profile.diabetic, sex=profile.sex,
            )
        else:
            # The pooled-cohort equations differ by sex; don't guess.
            ascvd_res = {"ten_year_ascvd_risk_percent": None, "risk_category": "Set sex in your profile"}

        yearly_reports.append({
            "date": ts,
            "year": yr,
            "chronological_age": c_age,
            "phenotypic_age": p_res["phenotypic_age"],
            "delta_years": p_res["delta_years"],
            "acceleration_percent": p_res["acceleration_percent"],
            "ascvd_risk_percent": ascvd_res["ten_year_ascvd_risk_percent"],
            "ascvd_category": ascvd_res["risk_category"],
            "key_markers": {
                "albumin": m.get("albumin", {}).get("value"),
                "creatinine": m.get("creatinine", {}).get("value"),
                "glucose": m.get("glucose", {}).get("value"),
                "hba1c": m.get("hba1c", {}).get("value"),
                "rdw": m.get("rdw", {}).get("value"),
                "wbc": m.get("wbc", {}).get("value"),
                "total_cholesterol": tc,
                "hdl": hdl,
                "chol_hdl_ratio": m.get("chol_hdl_ratio", {}).get("value")
            }
        })

    latest = yearly_reports[-1]
    latest_markers = labs[-1].get("markers", {})
    assumed = calculate_pheno_age(latest_markers, latest["chronological_age"])["assumed_markers"]
    pheno_range = None
    if any(k in ASSUMED_SPAN for k in assumed):
        lo, hi = (
            calculate_pheno_age(
                {**latest_markers, **{k: ASSUMED_SPAN[k][i] for k in assumed if k in ASSUMED_SPAN}},
                latest["chronological_age"],
            )["phenotypic_age"]
            for i in (0, 1)
        )
        pheno_range = [lo, hi]
    body = None
    if latest_body:
        body = {
            "date": str(latest_body.get("timestamp") or "")[:10],
            "bmi": latest_body.get("bmi"),
            "body_fat_percent": latest_body.get("body_fat_percent"),
        }

    return {
        "current": {
            "chronological_age": latest["chronological_age"],
            "phenotypic_age": latest["phenotypic_age"],
            "biological_age_delta": latest["delta_years"],
            "status": "Youthful Rejuvenation" if latest["delta_years"] < -5.0 else ("Optimal Longevity" if latest["delta_years"] <= 0 else "Accelerated Aging"),
            "ten_year_ascvd_risk": latest["ascvd_risk_percent"],
            "ascvd_category": latest["ascvd_category"],
            "latest_lab_date": latest["date"],
            "markers_measured": PHENO_MARKERS - len(assumed),
            "assumed_markers": assumed,
            "phenotypic_age_range": pheno_range,
            "body": body,
        },
        "multi_year_trajectory": yearly_reports,
        "methodology": {
            "pheno_age_model": "Morgan Levine (Yale/Columbia), Gompertz Proportional Hazards, NHANES-validated",
            "ascvd_model": "2013 ACC/AHA Pooled Cohort Equations"
        }
    }
