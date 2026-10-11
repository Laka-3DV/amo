"""Entry point: `python -m cc_remote.relay`."""
from __future__ import annotations

import uvicorn

from cc_remote.config import relay_config, validate_relay_config
from cc_remote.log import logger
from cc_remote.relay.log_safety import uvicorn_log_config
from cc_remote.relay.server import create_app

log = logger("cc_remote.relay")


def main() -> None:
    cfg = relay_config()
    validate_relay_config(cfg)
    app = create_app(cfg)
    log.info("starting relay", host=cfg.host, port=cfg.port)
    # Uvicorn's access log includes the full WebSocket target (including query
    # strings). Keep it disabled even though browser credentials are cookie-only.
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        log_config=uvicorn_log_config(),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1,::1",
        ws_max_size=cfg.ws_max_size_bytes,
        ws_max_queue=64,
        # Mobile carriers and iOS backgrounding drop a single keepalive pong
        # routinely (carrier NAT idle, signal handoff, Safari suspending the
        # page). The stock 20s/20s default makes the relay itself kill the
        # phone's connection, which the logs show as ~20-40s disconnect
        # cycles. The browser already runs its own 20s application heartbeat,
        # so a generous protocol keepalive only reaps genuinely dead links.
        ws_ping_interval=60.0,
        ws_ping_timeout=120.0,
    )


if __name__ == "__main__":
    main()
