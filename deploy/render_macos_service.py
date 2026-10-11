"""Write a portable LaunchAgent; never enroll or restart a device implicitly."""
import os
from pathlib import Path
import plistlib
import sys


def render(root: Path, home: Path) -> dict:
    state = home / ".amo"
    return {
        "Label": "io.amo.connector",
        "ProgramArguments": [str(root / ".venv/bin/python"), "-m", "amo", "connector"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PYTHON_DOTENV_DISABLED": "1", "AMO_HOME": str(state),
            "AMO_DSH_URL": "http://127.0.0.1:3080",
            "PATH": f"{home}/.local/bin:{home}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(state / "logs/connector.stdout.log"),
        "StandardErrorPath": str(state / "logs/connector.stderr.log"),
    }


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("This LaunchAgent is for macOS.")
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    logs = home / ".amo/logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(logs.parent, 0o700)
    os.chmod(logs, 0o700)
    target = home / "Library/LaunchAgents/io.amo.connector.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        plistlib.dump(render(root, home), handle)
    target.chmod(0o600)
    print(target)
