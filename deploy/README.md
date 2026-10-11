# Deploy Amo

[English README](../README.md) · [中文 README](../README.zh-CN.md)

The relay and connector are separate roles. Use a source checkout of the same Amo revision for both. This release documents source installation; it does not depend on prebuilt installation packages.

## HTTPS relay

A dedicated Linux service account, Python 3.12+, Node 22.12+ and a domain pointing to your server are required. Only the TLS proxy needs to accept public traffic. Keep the Python relay on loopback.

1. Clone `https://github.com/Laka-3DV/amo` into `/opt/amo`. Create a dedicated `amo` service user and private directories `/etc/amo` and `/var/lib/amo`; the service user needs read access to code/configuration and write access only to its state directory.
2. Build the virtual environment and web client from that checkout:

   ```bash
   cd /opt/amo
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-relay.txt
   npm --prefix web ci
   npm --prefix web run build
   ```

3. Generate a new private configuration, replacing the example domain:

   ```bash
   .venv/bin/python -m amo init --origin https://amo.example.com --file /etc/amo/relay.env
   ```

   Run this as the appropriate administrator, then set `/etc/amo/relay.env` to `root:amo` and `0640`, and `/etc/amo` to `root:amo` and `0750` so the service can traverse it. Set `/var/lib/amo` to `amo:amo` and `0700`. The session secret, web password and connector token must be independently generated; do not copy another deployment's values. The service does not load the source checkout's `.env`.

4. Review [amo-relay.service](amo-relay.service), adapting installation paths if needed. Install it as `/etc/systemd/system/amo-relay.service`, run `systemctl daemon-reload`, then `systemctl enable --now amo-relay`.
5. Merge [Caddyfile](Caddyfile) into your own TLS proxy configuration, replacing **both** occurrences of `amo.example.com`. Validate it before reloading. Preserve unrelated sites. Other reverse proxies must pass WebSocket upgrades and forward to the same loopback port.
6. Verify `https://YOUR_DOMAIN/healthz`, authenticate in the browser, and pair a connector from Device Center.

A local relay can also run on the agent computer. Remote use still needs a trusted HTTPS/WSS path; an ordinary web link does not eliminate the relay.

### Independent users on one relay

A shared single-owner password is not an isolation policy. Configure these fields in the private relay environment when separate users need access:

```dotenv
LOGIN_USERS_JSON={"alice":{"password":"REPLACE_WITH_ALICE_RANDOM_PASSWORD","machines":["alice-mac"]},"bob":{"password":"REPLACE_WITH_BOB_RANDOM_PASSWORD","machines":["bob-mac"]}}
WRAPPER_TOKENS_JSON={"alice-mac":"REPLACE_WITH_ALICE_RANDOM_DEVICE_TOKEN","bob-mac":"REPLACE_WITH_BOB_RANDOM_DEVICE_TOKEN"}
```

These policies replace the single-owner login/token settings. Use distinct random values and explicit machine lists; avoid `machines:["*"]`. Each person uses their own native agent login and pairs their own device under their own account. For migration from an existing shared login, back up device state, rotate the session-signing secret to invalidate old cookies, and review/re-pair old device ownership during an idle maintenance window.

## macOS connector

Install and sign in to your own local DSH and/or Codex first. The connector does not need Node for an existing relay unless your chosen native agent requires it.

```bash
git clone https://github.com/Laka-3DV/amo.git
cd amo
bash deploy/install-macos.sh https://amo.example.com YOUR_PAIRING_CODE "My Mac"
```

Replace the origin and short-lived code from your own web account. This source installer creates `.venv`, installs connector dependencies, pairs once if no private device file exists, and writes `~/Library/LaunchAgents/io.amo.connector.plist`. Generated paths come from the current checkout and user's home directory.

The device credential is stored at `~/.amo/device.json` (`0600`), and local logs at `~/.amo/logs`. The service disables automatic `.env` loading. An already-running service is not restarted; only restart during an idle window after checking running tasks, queued messages and pending questions.

To inspect the service:

```bash
launchctl print gui/$(id -u)/io.amo.connector
```

To stop it deliberately after tasks finish:

```bash
launchctl bootout gui/$(id -u)/io.amo.connector
```

For foreground use, install `requirements-wrapper.txt` in a virtual environment, pair with `python -m amo pair`, then run `PYTHON_DOTENV_DISABLED=1 python -m amo connector` from the checkout. Linux connectors may use the same foreground command or a service configured for their own user and private environment. Windows service installation is not yet validated.

## Updates and rollback

Record the deployed source commit. Build a fresh checkout/virtual environment before changing a live installation. Verify its unit tests, web build and configuration. Switch relay and connectors only when all relevant tasks, queues and questions are idle; keep the previous checkout and private configuration backups for rollback. Do not delete native project files or conversations as part of an upgrade.

Secrets and runtime data belong outside Git. Public source should never contain host-specific configuration, native authentication, conversation history, service logs or private screenshots.
