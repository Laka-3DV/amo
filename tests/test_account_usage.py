"""Zero-token account display: distinct units, no private fields on the wire."""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cc_remote.protocol import GetAccountUsage, deserialize
from cc_remote.wrapper.account_usage import account_usage, quota_limits, wallet_balances
from cc_remote.wrapper.codex_client import CodexUnavailable
from tests.test_codex_bridge import setup, cleanup


def test_wallets_keep_decimal_precision_and_separate_currencies():
    rows = wallet_balances({"status":"ready", "value":[
        {"currency":"CNY", "balance":"136.8269542200000000", "token":"SECRET"},
        {"currency":"USD", "balance":"1.25"}],
        "bonusWallets":[{"currency":"CNY", "balance":"0.0000000000000001"}]})
    assert rows[0].total == "136.8269542200000001"
    assert rows[1].total == "1.25"
    assert "SECRET" not in str(rows)
    assert wallet_balances({"status":"failed"}) == []
    with pytest.raises(ValueError):
        wallet_balances({"status":"ready", "value":[{"currency":"CNY","balance":"NaN"}]})


def test_limits_project_known_windows_without_credits_or_identifiers():
    rows = quota_limits({"rateLimitsByLimitId":{"codex":{
        "limitId":"codex", "planType":"prolite", "credits":{"balance":"SECRET"},
        "email":"SECRET", "primary":{"usedPercent":22,"windowDurationMins":10080,"resetsAt":1792295987,"secret":"SECRET"},
        "secondary":{"usedPercent":False}}}})
    assert rows[0].primary.used_percent == 22
    assert rows[0].primary.window_duration_mins == 10080
    assert rows[0].secondary is None
    assert "SECRET" not in str(rows)


def test_native_reads_do_not_load_or_claim_a_session_and_failure_is_not_zero():
    async def run():
        machine, transport, peer = await setup()
        original = peer.call
        async def call(method, params=None, **kwargs):
            if method == "account/read":
                return {"account":{"type":"chatgpt", "planType":"prolite", "email":"SECRET"},"requiresOpenaiAuth":True}
            if method == "account/rateLimits/read":
                return {"rateLimits":{"primary":{"usedPercent":22,"windowDurationMins":10080}}}
            return await original(method, params, **kwargs)
        peer.call = call
        cmd = GetAccountUsage(engine="codex", cmd_id="usage-read", client_id="phone")
        result = await machine._handle(cmd)
        assert result.available and result.request_id == "usage-read" and result.to == "phone"
        assert deserialize(result.model_dump_json()).engine == "codex"
        assert not machine.sessions and not any(method.startswith("thread/") for method, _ in peer.calls)
        assert "SECRET" not in result.model_dump_json()
        async def failure(*args, **kwargs): raise CodexUnavailable("SECRET")
        peer.call = failure
        result = await account_usage(machine, cmd)
        assert not result.available and result.reason == "unavailable" and not result.balances
        assert "SECRET" not in result.model_dump_json()
        await cleanup(machine)
    asyncio.run(run())


def test_dsh_wallet_read_is_only_the_native_loopback_projection():
    async def run():
        calls = []
        async def call(method, payload):
            calls.append((method,payload))
            return {"status":"ready","value":[{"currency":"CNY","balance":"7.5"}],"bonusWallets":[]}
        sent=[]
        async def send(value): sent.append(value)
        client=SimpleNamespace(call=call)
        machine=SimpleNamespace(_dsh_client_ready=lambda:(client,None),transport=SimpleNamespace(send=send))
        result=await account_usage(machine, GetAccountUsage(engine="dsh",cmd_id="balance-read",client_id="ipad"))
        assert result.available and result.balances[0].total=="7.5" and result.to=="ipad"
        assert calls[0][0]=="account/getBalance" and set(calls[0][1])=={"client"}
    asyncio.run(run())


def test_unknown_engine_is_rejected():
    with pytest.raises(ValidationError): GetAccountUsage(engine="arbitrary",cmd_id="read")
