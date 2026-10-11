# Amo contributor instructions

Amo supports DeepSeek Harness (DSH) and Codex. Keep their local adapters, shared relay, authentication, TLS, protocol and browser client compatible. Do not add unsupported engines or unrelated execution modules.

- DSH must remain restricted to loopback; Codex uses local stdio only.
- Never forward native credentials, arbitrary plugin requests or unknown native payloads through the relay.
- Public installation commands use `python -m amo`; preserve internal protocol compatibility.
- Backend changes: run `pytest tests/` with no model calls.
- Web changes: run `npm --prefix web run build`, `test:reliability` and lint; use browser tests for relevant interaction changes.
- Native acceptance tests must use isolated temporary sessions; never interrupt a real session.
- Do not commit credentials, personal paths, live conversation IDs, logs, screenshots or deployment state. Use synthetic test data and example domains.
- Make intentional commits containing only the files for the current change.
