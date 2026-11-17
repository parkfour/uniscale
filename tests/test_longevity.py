import pytest
from uniscale.longevity import calculate_pheno_age, calculate_ascvd_risk, get_longevity_profile

def test_calculate_pheno_age():
    sample_markers = {
        "albumin": {"value": 4.5},
        "creatinine": {"value": 0.9},
        "glucose": {"value": 90.0},
        "lymphocytes": {"value": 33.0},
        "mcv": {"value": 90.0},
        "rdw": {"value": 12.5},
        "alkaline_phosphatase": {"value": 60.0},
        "wbc": {"value": 5.0}
    }
    res = calculate_pheno_age(sample_markers, chronological_age=60.0)
    assert "phenotypic_age" in res
    assert "delta_years" in res
    assert res["chronological_age"] == 60.0
    # Should be significantly lower than chronological age for optimal markers
    assert res["delta_years"] < 0
    assert res["biological_status"] in ["Rejuvenated", "Optimal"]

def test_calculate_ascvd_risk():
    res = calculate_ascvd_risk(
        age=60,
        total_chol=220,
        hdl=80,
        systolic_bp=130,
        bp_treated=False,
        smoker=False,
        diabetic=False
    )
    assert "ten_year_ascvd_risk_percent" in res
    assert "risk_category" in res
    assert 0.0 < res["ten_year_ascvd_risk_percent"] < 100.0


def test_calculate_pheno_age_missing_markers_defaults():
    # Calling with empty markers should fall back to neutral defaults without error
    res = calculate_pheno_age({}, chronological_age=50.0)
    bu = res["biomarkers_used"]
    assert bu["creatinine_mg_dl"] == 0.86
    assert bu["fasting_glucose_mg_dl"] == 100.0
    assert bu["albumin_g_dl"] == 4.2
    assert "phenotypic_age" in res


def test_pheno_age_reports_assumed_markers_and_uses_measured_crp():
    full = {k: {"value": v} for k, v in {
        "albumin": 4.5, "creatinine": 0.9, "glucose": 90, "mcv": 90, "rdw": 12.5,
        "alkaline_phosphatase": 60, "wbc": 5.5,
    }.items()}
    res = calculate_pheno_age(full, 50)
    assert res["assumed_markers"] == ["lymphocytes", "crp"]
    # hs-CRP comes in mg/L; 1.0 mg/L is the assumed default, so the result must not change.
    same = calculate_pheno_age({**full, "crp": {"value": 1.0}}, 50)
    assert same["phenotypic_age"] == res["phenotypic_age"]
    assert same["assumed_markers"] == ["lymphocytes"]
    # Higher inflammation means an older phenotype.
    assert calculate_pheno_age({**full, "crp": {"value": 5.0}}, 50)["phenotypic_age"] > res["phenotypic_age"]
