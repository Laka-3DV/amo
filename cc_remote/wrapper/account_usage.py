"""Project wallet/limit reads locally; authentication remains with each engine."""
from __future__ import annotations

import re
from decimal import Decimal, localcontext
from uuid import uuid4

from cc_remote.protocol import AccountBalance, AccountUsageReport, StatusRateLimit, StatusRateLimitWindow
from .codex_client import CodexRpcError, CodexUnavailable
from .dsh_client import DshError

_DECIMAL = re.compile(r"^-?\d{1,20}(\.\d{1,24})?$")


def wallet_balances(payload):
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        return []
    amounts = {}
    for field, kind in (("value", "recharge"), ("bonusWallets", "bonus")):
        rows = payload.get(field, [])
        if not isinstance(rows, list) or len(rows) > 64:
            raise ValueError("invalid wallet rows")
        for row in rows:
            if not isinstance(row, dict) or row.get("currency") not in {"CNY", "USD"}:
                raise ValueError("invalid wallet currency")
            value = row.get("balance")
            if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
                raise ValueError("invalid wallet amount")
            currency = row["currency"]
            amounts.setdefault(currency, {"recharge": Decimal(0), "bonus": Decimal(0)})
            with localcontext() as context:
                context.prec = 64
                amounts[currency][kind] += Decimal(value)
    result = []
    with localcontext() as context:
        context.prec = 64
        for currency, parts in amounts.items():
            result.append(AccountBalance(currency=currency,
                total=format(parts["recharge"] + parts["bonus"], "f"),
                recharge=format(parts["recharge"], "f"), bonus=format(parts["bonus"], "f")))
    return result


def quota_limits(payload):
    if not isinstance(payload, dict):
        return []
    mapping = payload.get("rateLimitsByLimitId")
    rows = list(mapping.values())[:16] if isinstance(mapping, dict) else [payload.get("rateLimits")]
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        windows = {}
        for kind in ("primary", "secondary"):
            raw = row.get(kind)
            if not isinstance(raw, dict):
                continue
            values = {}
            for source, target in (("usedPercent", "used_percent"), ("windowDurationMins", "window_duration_mins"), ("resetsAt", "resets_at")):
                value = raw.get(source)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53 - 1:
                    values[target] = value
            if values:
                windows[kind] = StatusRateLimitWindow(**values)
        if not windows:
            continue
        names = {target: row[source][:cap] for source, target, cap in (
            ("limitId", "limit_id", 128), ("limitName", "limit_name", 256), ("planType", "plan_type", 64),
        ) if isinstance(row.get(source), str)}
        result.append(StatusRateLimit(**names, **windows))
    return result


async def account_usage(machine, cmd):
    response = AccountUsageReport(engine=cmd.engine, request_id=cmd.cmd_id or uuid4().hex,
        to=getattr(cmd, "client_id", None))
    try:
        if cmd.engine == "dsh":
            client, _ = machine._dsh_client_ready()
            raw = await client.call("account/getBalance", {"client": {
                "version": "0.2.0-rc.2", "locale": "zh-CN", "timezoneOffsetSeconds": 0}})
            response.balances = wallet_balances(raw)
            response.available = bool(response.balances)
            response.reason = None if response.available else "signed_out" if raw is None else "not_provided"
        else:
            await machine._codex.ensure()
            raw = await machine._codex.client.call("account/read", {"refreshToken": False})
            account = raw.get("account") or {}
            plan = account.get("planType")
            if isinstance(plan, str):
                response.plan = plan[:64]
            if account.get("type") != "chatgpt":
                response.reason = "api_key" if account.get("type") == "apiKey" else "not_provided"
            else:
                response.limits = quota_limits(await machine._codex.client.call("account/rateLimits/read", {}))
                response.available = bool(response.limits)
                response.reason = None if response.available else "not_provided"
    except (DshError, CodexRpcError, CodexUnavailable, OSError, ValueError, TimeoutError):
        # Provider exceptions can include account/token material. Never relay them.
        response.available = False
        response.reason = "unavailable"
    await machine.transport.send(response)
    return response
