import os
import tempfile
from uniscale.config import UniscaleConfig, generate_default_config_file, load_config


def test_default_config():
    cfg = UniscaleConfig()
    assert cfg.general.storage_backend == "jsonl"
    assert cfg.server.port == 8088
    assert cfg.devices.enable_yunmai is True
    assert cfg.devices.yunmai.force_pounds is True
    assert cfg.legacy_cloud.garmin.enabled is False


def test_generate_and_load_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        conf_file = os.path.join(tmpdir, "test_config.toml")
        generate_default_config_file(conf_file)
        assert os.path.exists(conf_file)

        loaded = load_config(conf_file)
        assert loaded.server.port == 8088
        assert loaded.devices.auto_scan is True
