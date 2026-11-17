import os
import sys

import uniscale.ai
from uniscale import cli


def test_chat_uses_ai_config_when_flags_omitted(tmp_path, monkeypatch):
    monkeypatch.delenv("UNISCALE_CONFIG", raising=False)  # restored after the test; main() sets it
    cfg = tmp_path / "config.toml"
    cfg.write_text('[ai]\nendpoint = "http://llm.example:8080"\nmodel = "test-model"\n')
    seen = {}

    def fake_chat(messages, endpoint, model, **kwargs):
        seen.update(endpoint=endpoint, model=model, **kwargs)
        return {"engine": "fake", "reply": "ok"}

    monkeypatch.setattr(uniscale.ai, "chat_with_agent", fake_chat)
    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "chat", "hi"])
    cli.main()
    assert seen["endpoint"] == "http://llm.example:8080"
    assert seen["model"] == "test-model"
    assert seen["db_path"].endswith("measurements.jsonl")
    assert os.environ.get("UNISCALE_CONFIG") == str(cfg.resolve())

    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "chat", "--model", "flag-model", "hi"])
    cli.main()
    assert seen["model"] == "flag-model"


def test_analyze_uses_ai_config_when_flags_omitted(tmp_path, monkeypatch):
    monkeypatch.delenv("UNISCALE_CONFIG", raising=False)  # restored after the test; main() sets it
    cfg = tmp_path / "config.toml"
    cfg.write_text('[ai]\nendpoint = "http://llm.example:8080"\nmodel = "test-model"\n')
    seen = {}

    def fake_query(prompt, endpoint, model):
        seen.update(endpoint=endpoint, model=model)
        return "analysis ok"

    monkeypatch.setattr(uniscale.ai, "query_llm", fake_query)
    monkeypatch.setattr(uniscale.ai, "build_health_profile", lambda db_path: {})
    monkeypatch.setattr(uniscale.ai, "generate_health_summary_prompt", lambda profile: "prompt")
    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "analyze"])
    cli.main()
    assert seen == {"endpoint": "http://llm.example:8080", "model": "test-model"}


def test_cli_supplement_stack(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UNISCALE_CONFIG", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[general]\ndata_dir = "{data_dir}"\n')

    # Add supplement via CLI
    monkeypatch.setattr(sys, "argv", [
        "uniscale", "-c", str(cfg), "supplement", "stack",
        "--add", "--name", "CoQ10", "--dosage", "100", "--unit", "mg", "--frequency", "morning", "--purpose", "Mitochondrial energy"
    ])
    cli.main()
    out = capsys.readouterr().out
    assert "[+] Added/updated in stack: CoQ10 100.0mg (morning)" in out

    # List stack via CLI
    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "supplement", "stack"])
    cli.main()
    out_list = capsys.readouterr().out
    assert "CoQ10" in out_list
    assert "100.0mg" in out_list

    # Remove supplement via CLI
    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "supplement", "stack", "--remove", "CoQ10"])
    cli.main()
    out_rm = capsys.readouterr().out
    assert "[+] Removed 'CoQ10' from active regimen stack" in out_rm

    # Removing a missing name reports it instead of claiming success
    cli.main()
    assert "is not in the active regimen stack" in capsys.readouterr().out

    # --add without --dosage is rejected rather than defaulting to 100 mg
    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "supplement", "stack", "--add", "--name", "Zinc"])
    cli.main()
    assert "--dosage" in capsys.readouterr().out
    assert "Zinc" not in (data_dir / "supplements_stack.json").read_text()

    # --reset drops the edited stack file and falls back to config
    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "supplement", "stack", "--reset"])
    cli.main()
    assert "[+] Reset stack" in capsys.readouterr().out
    assert not (data_dir / "supplements_stack.json").exists()
