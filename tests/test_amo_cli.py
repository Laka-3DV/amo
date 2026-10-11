"""Public installation interface, with no running native agents or model calls."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat

from dotenv import dotenv_values
import pytest

from amo.__main__ import initialize, main, prepare_environment


@pytest.fixture
def public_env(monkeypatch, tmp_path):
    original = dict(os.environ)
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(("AMO_", "CC_REMOTE_")) or key in {
            "PUSH_DB_PATH", "DEVICE_DB_PATH", "LOGIN_PASSWORD", "SESSION_SECRET",
            "WRAPPER_TOKEN", "RELAY_URL", "PUBLIC_ORIGIN",
        }:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AMO_HOME", str(tmp_path / "private-state"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    yield tmp_path
    for key in tuple(os.environ):
        if key not in original:
            os.environ.pop(key, None)
    for key, value in original.items():
        os.environ[key] = value


def test_init_generates_private_independent_credentials_without_printing_them(public_env, capsys):
    target = public_env / "private/relay.env"
    assert initialize(["--file", str(target), "--origin", "https://amo.example.com"]) == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    config = dotenv_values(target)
    secrets = [config[key] for key in ("LOGIN_PASSWORD", "SESSION_SECRET", "WRAPPER_TOKEN")]
    assert len(set(secrets)) == 3
    assert all(len(value) >= 32 for value in secrets)
    assert config["RELAY_HOST"] == "127.0.0.1"
    assert config["RELAY_URL"] == "wss://amo.example.com/ws"
    assert config["AMO_DSH_URL"] == "http://127.0.0.1:3080"
    output = capsys.readouterr().out
    assert not any(value in output for value in secrets)
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        initialize(["--file", str(target)])
    assert target.read_bytes() == original


@pytest.mark.parametrize("origin", [
    "http://amo.example.com", "https://user:password@amo.example.com",
    "https://amo.example.com/path", "https://amo.example.com?secret=example",
    "https://amo.example.com#section", "https://amo.example.com:invalid",
    "https://amo.example.com:65536", "https://amo.example.com:0",
    "https://amo.example.com\nLOGIN_PASSWORD=injected", "file:///tmp/test",
])
def test_init_rejects_unsafe_origins_before_creating_config(public_env, origin):
    with pytest.raises(ValueError):
        initialize(["--origin", origin])
    assert not (public_env / ".env").exists()


def test_loopback_trial_custom_port_and_machine_validation(public_env):
    initialize(["--origin", "http://127.0.0.1:9876", "--machine-id", "sample-mac"])
    values = dotenv_values(public_env / ".env")
    assert values["RELAY_PORT"] == "9876"
    assert values["RELAY_URL"] == "ws://127.0.0.1:9876/ws"
    assert values["AMO_MACHINE_ID"] == "sample-mac"
    with pytest.raises(ValueError, match="machine id"):
        initialize(["--machine-id", "../other"])


def test_public_environment_aliases_and_disabled_checkout_dotenv(public_env, monkeypatch):
    (public_env / ".env").write_text("LOGIN_PASSWORD=should-not-load\n")
    monkeypatch.setenv("AMO_MACHINE_ID", "sample-mac")
    monkeypatch.setenv("AMO_DSH_URL", "http://127.0.0.1:3080")
    monkeypatch.setenv("AMO_CODEX_ENABLED", "0")
    monkeypatch.setenv("AMO_CODEX_BIN", "/sample/bin/codex")
    home = prepare_environment()
    assert home == public_env / "private-state"
    assert os.environ["CC_REMOTE_MACHINE_ID"] == "sample-mac"
    assert os.environ["CC_REMOTE_CODEX_ENABLED"] == "0"
    assert os.environ["CC_REMOTE_CODEX_BIN"] == "/sample/bin/codex"
    assert os.environ["CC_REMOTE_STATE_DIR"] == str(home)
    assert os.environ["CC_REMOTE_DEVICE_CONFIG"] == str(home / "device.json")
    assert os.environ["PUSH_DB_PATH"] == str(home / "relay-push.sqlite3")
    assert os.environ["DEVICE_DB_PATH"] == str(home / "relay-devices.sqlite3")
    assert "LOGIN_PASSWORD" not in os.environ


def test_pairing_file_is_consumed_by_public_connector_configuration(public_env):
    from cc_remote.config import WrapperConfig
    home = prepare_environment()
    home.mkdir(mode=0o700)
    device = home / "device.json"
    device.write_text(json.dumps({
        "machine_id": "paired-mac", "label": "Sample Mac",
        "relay_url": "wss://amo.example.com/ws", "wrapper_token": "test-only-token",
    }))
    device.chmod(0o600)
    config = WrapperConfig()
    assert config.machine_id == "paired-mac"
    assert config.relay_url == "wss://amo.example.com/ws"
    assert config.wrapper_token == "test-only-token"
    assert config.state_dir == home


def test_launch_agent_has_portable_paths_and_no_embedded_credentials(public_env):
    source = Path(__file__).resolve().parents[1] / "deploy/render_macos_service.py"
    spec = importlib.util.spec_from_file_location("amo_service_renderer", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, home = public_env / "checkout", public_env / "user"
    data = module.render(root, home)
    assert data["ProgramArguments"] == [str(root / ".venv/bin/python"), "-m", "amo", "connector"]
    assert data["WorkingDirectory"] == str(root)
    assert data["EnvironmentVariables"]["AMO_HOME"] == str(home / ".amo")
    assert data["EnvironmentVariables"]["PYTHON_DOTENV_DISABLED"] == "1"
    assert "LOGIN_PASSWORD" not in data["EnvironmentVariables"]
    assert "WRAPPER_TOKEN" not in data["EnvironmentVariables"]


def test_public_commands_report_version_and_fail_without_starting_services(public_env, capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
    assert main(["relay", "unexpected"]) == 1
    assert "environment" in capsys.readouterr().err
