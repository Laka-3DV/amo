"""Public CLI. Native adapters remain behind one stable Amo interface."""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit


def prepare_environment() -> Path:
    from dotenv import load_dotenv
    load_dotenv(Path.cwd() / ".env")
    home = Path(os.environ.get("AMO_HOME", "~/.amo")).expanduser().resolve()
    aliases = {
        "AMO_MACHINE_ID": "CC_REMOTE_MACHINE_ID",
        "AMO_DSH_URL": "CC_REMOTE_DSH_URL",
        "AMO_CODEX_ENABLED": "CC_REMOTE_CODEX_ENABLED",
        "AMO_CODEX_BIN": "CC_REMOTE_CODEX_BIN",
    }
    for public, internal in aliases.items():
        if public in os.environ:
            os.environ[internal] = os.environ[public]
    os.environ.setdefault("CC_REMOTE_STATE_DIR", str(home))
    os.environ.setdefault("CC_REMOTE_DEVICE_CONFIG", str(home / "device.json"))
    os.environ.setdefault("PUSH_DB_PATH", str(home / "relay-push.sqlite3"))
    os.environ.setdefault("DEVICE_DB_PATH", str(home / "relay-devices.sqlite3"))
    return home


def initialize(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m amo init")
    parser.add_argument("--origin", default="http://127.0.0.1:8765")
    parser.add_argument("--machine-id", default="my-mac")
    parser.add_argument("--file", type=Path, default=Path(".env"))
    args = parser.parse_args(arguments)
    if any(ord(char) <= 32 or ord(char) == 127 for char in args.origin):
        raise ValueError("origin must not contain whitespace or control characters")
    origin = args.origin.rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment):
        raise ValueError("origin must be a credential-free http(s) origin")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise ValueError("a public relay requires HTTPS")
    # Reject malformed ports before writing credentials.
    if parsed.port is not None and parsed.port == 0:
        raise ValueError("origin port must be between 1 and 65535")
    from cc_remote.config import _MACHINE_ID_RE
    if not _MACHINE_ID_RE.fullmatch(args.machine_id):
        raise ValueError("invalid machine id")
    from cc_remote.device import _write_private_text
    ws = "wss" if parsed.scheme == "https" else "ws"
    body = "\n".join([
        "# Amo private configuration — do not commit or share this file.",
        "RELAY_HOST=127.0.0.1",
        f"RELAY_PORT={parsed.port if loopback and parsed.port else 8765}",
        f"PUBLIC_ORIGIN={origin}",
        f"LOGIN_PASSWORD={secrets.token_urlsafe(24)}", f"SESSION_SECRET={secrets.token_hex(32)}",
        f"WRAPPER_TOKEN={secrets.token_urlsafe(32)}", "WEB_STATIC_DIR=web/dist",
        f"RELAY_URL={ws}://{parsed.netloc}/ws", f"AMO_MACHINE_ID={args.machine_id}",
        "AMO_DSH_URL=http://127.0.0.1:3080", "AMO_CODEX_ENABLED=1", "",
    ])
    _write_private_text(args.file.expanduser(), body, replace=False)
    print(f"Created private configuration: {args.file}. Login password is stored there; it was not printed.")
    return 0


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if not args or args[0] in {"--help", "-h"}:
        print("Amo — remote control for your local coding agents\n\n"
              "Usage: python -m amo {relay|connector|pair|init|version}\n"
              "  relay       Serve the browser client and relay\n"
              "  connector   Connect this machine's DSH and Codex\n"
              "  pair        Pair with a relay: pair HTTPS_ORIGIN CODE --name NAME\n"
              "  init        Generate private configuration (see init --help)\n"
              "  version     Print the Amo version")
        return 0
    command, rest = args[0], args[1:]
    try:
        prepare_environment()
        if command == "version":
            from amo import __version__
            print(__version__)
        elif command == "init":
            return initialize(rest)
        elif command == "pair":
            from cc_remote.device import parser
            paired = parser().parse_args(["pair", *rest])
            return int(paired.handler(paired))
        elif command == "relay":
            if rest:
                raise ValueError("relay takes configuration from its environment")
            from cc_remote.relay.__main__ import main as run
            run()
        elif command == "connector":
            if rest:
                raise ValueError("connector takes configuration from its environment")
            from cc_remote.wrapper.__main__ import main as run
            asyncio.run(run())
        else:
            raise ValueError(f"unknown command: {command}")
    except KeyboardInterrupt:
        return 130
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"Amo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
