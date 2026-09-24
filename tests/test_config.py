import pytest

from vtrade.config import load_config


def test_project_config_loads():
    cfg = load_config()
    assert cfg.symbol and cfg.timeframe
    assert cfg.llm.enabled is False  # the reviewer is opt-in


def test_unknown_key_is_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("risk:\n  risk_per_trad: 0.01\n")
    with pytest.raises(ValueError, match="Unknown config keys"):
        load_config(path)


def test_invalid_thresholds_are_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("strategy:\n  entry_threshold: 0.4\n  exit_threshold: 0.5\n")
    with pytest.raises(ValueError, match="exit_threshold"):
        load_config(path)
