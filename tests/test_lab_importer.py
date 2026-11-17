import os
import pytest
from uniscale.models import BloodTestMeasurement, LabMarker
from uniscale.importer import parse_quest_pdf, import_lab_file, save_history


def test_models():
    m = LabMarker(
        name="Total Cholesterol",
        value=210.0,
        unit="mg/dL",
        reference_range="<200",
        flag="H",
        category="lipid"
    )
    assert m.value == 210.0
    assert m.flag == "H"

    rec = BloodTestMeasurement(
        markers={"total_cholesterol": m}
    )
    assert rec.type == "blood_test"
    assert rec.markers["total_cholesterol"].value == 210.0


def test_parse_quest_pdf_if_available():
    p = os.environ.get("UNISCALE_TEST_LAB_PDF", "")
    if not p or not os.path.exists(p):
        pytest.skip("Set UNISCALE_TEST_LAB_PDF to a Quest PDF report to run this test")

    meas = parse_quest_pdf(p)
    assert meas is not None
    assert meas.lab_name == "Quest Diagnostics"
    assert len(meas.markers) >= 30
    assert "total_cholesterol" in meas.markers
    assert "hdl_cholesterol" in meas.markers
    assert "glucose" in meas.markers
    assert "hba1c" in meas.markers
