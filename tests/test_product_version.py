from __future__ import annotations

import json
import re
from pathlib import Path

from cc_remote import __version__
from cc_remote.protocol import PROTOCOL_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_product_version_is_consistent_across_runtime_and_web_metadata():
    assert __version__ == "0.1.0"
    assert re.fullmatch(r"(?:0|[1-9]\d*)\.\d+\.\d+", __version__)

    package = json.loads((ROOT / "web/package.json").read_text())
    package_lock = json.loads((ROOT / "web/package-lock.json").read_text())
    build_manifest = json.loads(
        (ROOT / "web/public/cc-remote-build.json").read_text()
    )

    assert package["version"] == __version__
    assert package_lock["version"] == __version__
    assert package_lock["packages"][""]["version"] == __version__
    assert build_manifest == {
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
    }
    from amo import __version__ as public_version
    assert public_version == __version__
    assert PROTOCOL_VERSION == 39
