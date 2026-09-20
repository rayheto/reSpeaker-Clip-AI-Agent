"""Tests for the Clip service console entry point (``backend.service_cli``).

The npm ``respeaker-clip`` package launches this module, so its argument and
environment contract is part of the distribution surface.
"""

from __future__ import annotations

import json

import pytest

from backend import service_cli


def test_defaults_resolve_to_clip_mode(monkeypatch):
    monkeypatch.delenv("VOICE_INPUT_MODE", raising=False)
    args = service_cli.parse_args([])

    assert args.host == "0.0.0.0"
    assert args.port == 5000
    assert not args.no_clip
    config = service_cli.resolved_config(args)
    assert config["input_mode"] == "clip"
    assert config["clip_enabled"] is True


def test_environment_default_is_used_when_mode_is_not_passed(monkeypatch):
    monkeypatch.setenv("VOICE_INPUT_MODE", "both")
    args = service_cli.parse_args([])
    assert service_cli.resolved_config(args)["input_mode"] == "both"


def test_explicit_flags_override_options(monkeypatch):
    monkeypatch.delenv("VOICE_INPUT_MODE", raising=False)
    args = service_cli.parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "8080",
            "--input-mode",
            "browser",
            "--ble-address",
            "AA:BB:CC:DD:EE:FF",
        ]
    )
    config = service_cli.resolved_config(args)
    assert config["host"] == "127.0.0.1"
    assert config["port"] == 8080
    assert config["input_mode"] == "browser"
    assert config["ble_address"] == "AA:BB:CC:DD:EE:FF"
    # browser mode does not need the BLE runtime, but is not disabled either.
    assert config["clip_enabled"] is False


def test_no_clip_disables_the_runtime():
    args = service_cli.parse_args(["--no-clip"])
    assert service_cli.resolved_config(args)["clip_enabled"] is False


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_invalid_ports_are_rejected(port):
    with pytest.raises(SystemExit):
        service_cli.parse_args(["--port", port])


def test_unknown_input_mode_is_rejected():
    with pytest.raises(SystemExit):
        service_cli.parse_args(["--input-mode", "microphone"])


def test_apply_environment_exports_resolved_values(monkeypatch):
    monkeypatch.delenv("VOICE_INPUT_MODE", raising=False)
    monkeypatch.delenv("CLIP_BLE_ADDRESS", raising=False)
    monkeypatch.delenv("CLIP_BLE_NAME", raising=False)

    args = service_cli.parse_args(["--ble-name", "Clip", "--input-mode", "both"])
    service_cli.apply_environment(args)

    assert service_cli.os.environ["VOICE_INPUT_MODE"] == "both"
    assert service_cli.os.environ["CLIP_BLE_NAME"] == "Clip"


def test_print_config_does_not_start_the_service(capsys, monkeypatch):
    monkeypatch.delenv("VOICE_INPUT_MODE", raising=False)
    assert service_cli.main(["--print-config", "--port", "5999"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["port"] == 5999
    assert payload["clip_enabled"] is True


def test_no_agent_disables_the_agent_and_narrows_the_input_mode(monkeypatch):
    monkeypatch.delenv("VOICE_INPUT_MODE", raising=False)
    monkeypatch.delenv("AGENT_ENABLED", raising=False)
    args = service_cli.parse_args(["--no-agent", "--input-mode", "both"])
    config = service_cli.resolved_config(args)

    assert config["agent_enabled"] is False
    assert config["clip_enabled"] is True
    # 'both' would advertise browser voice, which the agent serves.
    assert config["input_mode"] == "clip"


def test_no_agent_exports_the_narrowed_input_mode(monkeypatch):
    """The exported mode must match what the service actually serves."""
    monkeypatch.setenv("VOICE_INPUT_MODE", "both")
    service_cli.apply_environment(service_cli.parse_args(["--no-agent"]))
    assert service_cli.os.environ["VOICE_INPUT_MODE"] == "clip"


def test_agent_mode_keeps_both(monkeypatch):
    """Agent mode keeps 'both': browser voice is part of the agent service."""
    monkeypatch.setenv("VOICE_INPUT_MODE", "both")
    monkeypatch.delenv("AGENT_ENABLED", raising=False)
    service_cli.apply_environment(service_cli.parse_args([]))
    assert service_cli.os.environ["VOICE_INPUT_MODE"] == "both"


def test_no_agent_exports_the_setting(monkeypatch):
    monkeypatch.delenv("AGENT_ENABLED", raising=False)
    service_cli.apply_environment(service_cli.parse_args(["--no-agent"]))
    assert service_cli.os.environ["AGENT_ENABLED"] == "false"


@pytest.mark.parametrize(
    "argv",
    [
        ["--no-agent", "--no-clip"],  # would serve nothing at all
        ["--no-agent", "--input-mode", "browser"],  # browser voice needs the agent
    ],
)
def test_no_agent_rejects_empty_deployments(argv, monkeypatch):
    monkeypatch.delenv("VOICE_INPUT_MODE", raising=False)
    with pytest.raises(SystemExit):
        service_cli.parse_args(argv)


def test_print_config_reports_agent_enabled(capsys, monkeypatch):
    monkeypatch.delenv("VOICE_INPUT_MODE", raising=False)
    monkeypatch.delenv("AGENT_ENABLED", raising=False)
    assert service_cli.main(["--print-config"]) == 0
    assert json.loads(capsys.readouterr().out)["agent_enabled"] is True


def test_dot_env_in_the_working_directory_is_loaded(tmp_path, monkeypatch):
    """A pip install must honour ./.env, not just a checkout's .env."""
    monkeypatch.chdir(tmp_path)
    # delitem records the current value, so teardown removes what dotenv sets.
    monkeypatch.delitem(service_cli.os.environ, "GROQ_RTC_FINAL_MODEL", raising=False)
    (tmp_path / ".env").write_text("GROQ_RTC_FINAL_MODEL=whisper-large-v3\n")

    assert service_cli.main(["--print-config"]) == 0
    assert service_cli.os.environ["GROQ_RTC_FINAL_MODEL"] == "whisper-large-v3"


def test_real_environment_wins_over_the_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TTS_VOICE", "from-systemd")
    (tmp_path / ".env").write_text("TTS_VOICE=from-file\n")

    service_cli.main(["--print-config"])
    assert service_cli.os.environ["TTS_VOICE"] == "from-systemd"


def test_explicit_env_file_is_honoured(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(service_cli.os.environ, "CLIP_BLE_NAME", raising=False)
    env_file = tmp_path / "clip.env"
    env_file.write_text("CLIP_BLE_NAME=DeskClip\n")

    assert service_cli.main(["--print-config", "--env-file", str(env_file)]) == 0
    assert service_cli.os.environ["CLIP_BLE_NAME"] == "DeskClip"
    assert json.loads(capsys.readouterr().out)["env_file"] == str(env_file)


def test_a_missing_explicit_env_file_fails_fast():
    with pytest.raises(SystemExit):
        service_cli.main(["--print-config", "--env-file", "/nope/.env"])