"""Real subprocess/stdio framing with a deterministic local peer, zero tokens."""
import asyncio
import os

from cc_remote.wrapper.codex_client import CodexClient, CodexUnavailable


def test_stdio_handshake_requests_and_secret_scrubbing(tmp_path, monkeypatch):
    script=tmp_path/'codex-fake'
    script.write_text('''#!/usr/bin/env python3
import sys,json,os
experimental=False
for line in sys.stdin:
    value=json.loads(line)
    if 'method' not in value: continue
    method=value['method']
    if method=='initialized': continue
    if method=='initialize': experimental=value['params'].get('capabilities',{}).get('experimentalApi') is True
    if method=='probe':
        print(json.dumps({'id':'approval','method':'item/commandExecution/requestApproval','params':{'threadId':'t'}}),flush=True)
        result={'clean':not any(os.environ.get(k) for k in ['WRAPPER_TOKEN','LOGIN_PASSWORD','SESSION_SECRET']), 'experimental':experimental}
    else: result={}
    print(json.dumps({'id':value['id'],'result':result}),flush=True)
''')
    script.chmod(0o700)
    for key in ['WRAPPER_TOKEN','LOGIN_PASSWORD','SESSION_SECRET']:monkeypatch.setenv(key,'must-not-leak')
    async def run():
        client=CodexClient(str(script),timeout=2)
        events=[]
        async def event(frame):
            events.append(frame['method'])
            await client.respond(frame['id'],{'decision':'decline'})
        await client.start(event)
        assert await client.call('probe')=={'clean':True,'experimental':True}
        for _ in range(30):
            if events:break
            await asyncio.sleep(.01)
        assert events==['item/commandExecution/requestApproval']
        await client.stop();assert not client.ready and not client.pending
    asyncio.run(run())


def test_absent_executable_reports_unavailable_without_starting_a_process(tmp_path):
    async def run():
        client=CodexClient(str(tmp_path/'missing'))
        try:await client.start(lambda frame:None)
        except CodexUnavailable:pass
        else:raise AssertionError('must reject missing executable')
        assert client.process is None
    asyncio.run(run())


def test_launch_agent_inherits_only_enabled_global_loopback_proxy_and_never_overrides_env():
    from cc_remote.wrapper.codex_client import system_proxy_env
    output='''<dictionary> {
  HTTPEnable : 1
  HTTPProxy : 127.0.0.1
  HTTPPort : 7890
  HTTPSEnable : 1
  HTTPSProxy : ::1
  HTTPSPort : 7890
  __SCOPED__ : <dictionary> {
    HTTPEnable : 1
    HTTPProxy : private.example
    HTTPPort : 3128
  }
}'''
    assert system_proxy_env(output,{})=={'HTTP_PROXY':'http://127.0.0.1:7890','HTTPS_PROXY':'http://[::1]:7890'}
    assert system_proxy_env(output,{'HTTPS_PROXY':'http://explicit:1234'})=={'HTTP_PROXY':'http://127.0.0.1:7890'}
    assert system_proxy_env(output,{'https_proxy':'http://explicit:1234'})=={'HTTP_PROXY':'http://127.0.0.1:7890'}
    assert system_proxy_env(output,{'ALL_PROXY':'socks5://explicit:1234'})=={}
    assert system_proxy_env(output.replace('Enable : 1','Enable : 0'),{})=={}
    assert system_proxy_env(output.replace('127.0.0.1','private.example').replace('::1','private.example'),{})=={}
