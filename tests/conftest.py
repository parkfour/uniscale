import pytest


@pytest.fixture(autouse=True)
def no_owntracks_recorder(monkeypatch):
    """Tests never reach a real OwnTracks Recorder, even when the machine's config names one
    (the deploy runs this suite on the server), nor OpenStreetMap. Tests that need it patch in a fake."""
    from uniscale import places

    def blocked(*_args, **_kwargs):
        raise RuntimeError("OwnTracks Recorder is not reachable from tests")

    monkeypatch.setattr(places, "_recorder_get", blocked)
    monkeypatch.setattr(places, "nearby_pois", blocked)
