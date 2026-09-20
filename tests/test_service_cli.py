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