"""Console entry point for the reSpeaker Clip service.

This is the Python side of the ``respeaker-clip`` npm package: the npm CLI
creates a virtualenv, installs this project into it (``pip install -r
requirements.txt`` from a checkout, or the ``respeaker-clip-service``
distribution), and launches ``python -m backend.service_cli`` with the flags
below.  It can also be used directly:

    python -m backend.service_cli --input-mode clip --port 5000

The Flask reloader is always off: the Clip runtime owns exactly one BLE
connection per process, and a reload would fork a second owner.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000
DEFAULT_INPUT_MODE = "clip"
INPUT_MODES = ("browser", "clip", "both")

logger = logging.getLogger("clip.service")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="respeaker-clip-service",
        description="Run the reSpeaker Clip voice service (BLE runtime + HTTP API).",
    )
    parser.add_argument("--host", default=os.getenv("CLIP_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=_port,
        default=_port(os.getenv("CLIP_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument(
        "--input-mode",
        choices=INPUT_MODES,
        default=None,
        help=(
            "Voice input to serve: 'clip' (device only), 'browser' (system mic) "
            f"or 'both'. Defaults to $VOICE_INPUT_MODE or '{DEFAULT_INPUT_MODE}'."
        ),
    )
    parser.add_argument(
        "--ble-address",
        default=None,
        help="Pin the Clip by BLE address instead of scanning.",
    )
    parser.add_argument(
        "--ble-name",
        default=None,
        help="BLE name to scan for when no address is pinned.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help=(
            "Load configuration from this .env file. Defaults to ./.env when it "
            "exists; real environment variables always win."
        ),
    )
    parser.add_argument(
        "--no-clip",
        action="store_true",
        help="Serve the HTTP API without starting the BLE runtime.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the resolved configuration as JSON and exit (used by tooling).",
    )
    return parser


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"port must be an integer, got {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be in 1..65535, got {port}")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def apply_environment(args: argparse.Namespace) -> dict[str, str]:
    """Export the resolved options as environment variables.

    ``config.py`` reads the environment at import time, so this must run before
    anything imports :mod:`config`. Returns the values that were set.
    """
    resolved: dict[str, str] = {}
    mode = args.input_mode or os.getenv("VOICE_INPUT_MODE") or DEFAULT_INPUT_MODE
    resolved["VOICE_INPUT_MODE"] = mode
    os.environ["VOICE_INPUT_MODE"] = mode

    if args.ble_address:
        resolved["CLIP_BLE_ADDRESS"] = args.ble_address
        os.environ["CLIP_BLE_ADDRESS"] = args.ble_address
    if args.ble_name:
        resolved["CLIP_BLE_NAME"] = args.ble_name
        os.environ["CLIP_BLE_NAME"] = args.ble_name
    if args.no_clip:
        resolved["CLIP_ENABLED"] = "false"
        os.environ["CLIP_ENABLED"] = "false"
    return resolved


def load_environment_file(args: argparse.Namespace) -> str | None:
    """Load a ``.env`` file before anything imports :mod:`config`.

    ``config.py`` calls ``load_dotenv()``, which searches upwards from *its own*
    directory — in a pip install that is site-packages, so a project-local
    ``.env`` would be missed. Loading the working directory's ``.env`` here (or
    ``--env-file``) makes both install modes behave the same. Existing
    environment variables are never overwritten, so systemd ``EnvironmentFile``
    and container env still take precedence.
    """
    from dotenv import load_dotenv

    explicit = args.env_file or os.getenv("RESPEAKER_CLIP_ENV_FILE")
    if explicit:
        path = os.path.abspath(explicit)
        if not os.path.isfile(path):
            raise SystemExit(f"env file not found: {path}")
    else:
        path = os.path.join(os.getcwd(), ".env")
        if not os.path.isfile(path):
            return None
    load_dotenv(path, override=False)
    return path


def resolved_config(args: argparse.Namespace) -> dict[str, object]:
    mode = args.input_mode or os.getenv("VOICE_INPUT_MODE") or DEFAULT_INPUT_MODE
    return {
        "host": args.host,
        "port": args.port,
        "input_mode": mode,
        "clip_enabled": not args.no_clip and mode in ("clip", "both"),
        "ble_address": args.ble_address or os.getenv("CLIP_BLE_ADDRESS") or None,
        "ble_name": args.ble_name or os.getenv("CLIP_BLE_NAME") or None,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Both must happen before anything imports config.py, which snapshots the
    # environment at import time.
    env_file = load_environment_file(args)

    if args.print_config:
        config = resolved_config(args)
        config["env_file"] = env_file
        print(json.dumps(config, indent=2))
        return 0

    apply_environment(args)
    logging.basicConfig(level=logging.INFO)

    # Imported late: config.py snapshots the environment at import time.
    from app import create_app

    resolved = resolved_config(args)
    app = create_app(clip_enabled=bool(resolved["clip_enabled"]))
    logger.info(
        "Clip service listening on %s:%s (input_mode=%s, clip_enabled=%s, env_file=%s)",
        resolved["host"],
        resolved["port"],
        resolved["input_mode"],
        resolved["clip_enabled"],
        env_file or "-",
    )
    app.run(
        host=str(resolved["host"]),
        port=int(str(resolved["port"])),
        debug=False,
        use_reloader=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())