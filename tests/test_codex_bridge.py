"""Codex integration tests with a local fake peer: no inference/token use."""
from __future__ import annotations

import asyncio
from copy import deepcopy

from cc_remote.protocol import (
    AnswerQuestion, GetHistory, GetModels, GetTurnDetail, Interrupt, ListSessions,
    NewSession, Query, SetEffort, SetModel, SwitchSession, UpdateQueuedQuery,
)
from cc_remote.wrapper.codex_bridge import CodexBridge
from cc_remote.wrapper.codex_client import CodexUnavailable, CodexRpcError
from cc_remote.wrapper.codex_stream import item_events
from tests.test_multisession import _mk_machine


class Peer:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.responses = []
        self.turns = [{"id":"old-turn","status":"completed","items":[
            {"type":"userMessage","id":"user-old","content":[{"type":"text","text":"old prompt"}]},
            {"type":"agentMessage","id":"answer-old","phase":"final","text":"old answer"},
        ]}]
        self.external = False
        self.fail_start = None
        self.counter = 0

    async def start(self, on_event, on_disconnect=None):
        self.on_event, self.on_disconnect = on_event, on_disconnect
        self.ready = True

    async def stop(self):
        self.ready = False

    async def call(self, method, params=None, **kwargs):
        params = params or {}
        self.calls.append((method,deepcopy(params)))
        if method=="account/read": return {"requiresOpenaiAuth":False}
        thread={"id":params.get("threadId","existing"),"cwd":"/tmp","name":"existing", "updatedAt":1000,
                "gitInfo":{"branch":"feature/sample"},
                "status":{"type":"active" if self.external else "idle"},"turns":deepcopy(self.turns)}
        if method=="thread/list": return {"data":[] if params.get("archived") else [thread],"nextCursor":None}
        if method in {"thread/read", "thread/resume", "thread/start"}:
            if method=="thread/start":thread["id"]="created"
            return {"thread":thread,"cwd":"/tmp","model":"model-a","reasoningEffort":"high", "approvalPolicy":"on-request", "sandbox":{"type":"readOnly"}}
        if method=="model/list":return {"data":[{"id":"a","model":"model-a","displayName":"Model A","description":"","isDefault":True,"defaultReasoningEffort":"high","supportedReasoningEfforts":[{"reasoningEffort":"high"},{"reasoningEffort":"low"}]},{"id":"b","model":"model-b","displayName":"Model B","description":"","isDefault":False,"defaultReasoningEffort":"low","supportedReasoningEfforts":[{"reasoningEffort":"low"}]}]}
        if method=="turn/start":
            if self.fail_start:raise self.fail_start
            self.counter+=1
            return {"turn":{"id":f"turn-{self.counter}","status":"inProgress","items":[]}}
        if method=="turn/interrupt":
            await self.on_event({"method":"turn/completed","params":{"threadId":params['threadId'],"turn":{"id":params['turnId'],"status":"interrupted","items":[]}}})
            return {}
        if method=="thread/items/list":return {"data":[{"turnId":"old-turn","item":item} for item in deepcopy(self.turns[0]["items"])],"nextCursor":None}
        if method=="thread/turns/list":return {"data":list(reversed(deepcopy(self.turns))),"nextCursor":None}
        return {}

    async def respond(self, identity,result):self.responses.append((identity,result))
    async def reject(self, identity):self.responses.append((identity,{"rejected":True}))


async def setup():
    machine,transport=_mk_machine(); peer=Peer()
    machine._codex=CodexBridge(machine,peer)
    await machine._codex.ensure()
    return machine,transport,peer


async def settle(predicate):
    for _ in range(100):
        if predicate():return
        await asyncio.sleep(.001)
    assert predicate()


async def cleanup(machine):
    for ctx in machine.sessions.values():
        for task in [ctx.turn_task,ctx.queued_query_drain_task]:
            if task:task.cancel()
    await machine._codex.stop()


def test_catalog_models_history_are_correlated_and_native_ids_namespaced():
    async def run():
        m,tr,p=await setup()
        result=await m._handle(ListSessions(engine='codex',client_id='phone',cmd_id='list'))
        assert result.request_id=='list' and result.sessions[0].session_id=='codex@existing'
        assert result.sessions[0].engine=='codex'
        assert result.sessions[0].git_branch=='feature/sample'
        assert all(params.get('useStateDbOnly') is True for method,params in p.calls if method=='thread/list')
        assert (await m._handle(GetModels(engine='codex',client_id='phone'))).models[0]["id"]=='model-a'
        result=await m._handle(GetHistory(session_id='codex@existing',detail='summary',client_id='phone'))
        assert result.authoritative and result.to=='phone' and result.turns[0].prompt=='old prompt'
        assert result.turns[0].blocks[0]['text']=='old answer'
        detail=await m._handle(GetTurnDetail(session_id='codex@existing',turn_id='old-turn',client_id='phone'))
        assert detail.authoritative and any(e['type']=='delta' for e in detail.events)
        assert not any(method=='thread/resume' for method,_ in p.calls)
        await cleanup(m)
    asyncio.run(run())


def test_switch_and_new_chat_focus_do_not_stop_other_sessions():
    async def run():
        m,tr,p=await setup()
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing',client_id='phone'))
        old=m.sessions['codex@existing'];assert m.focused_sid==old.key
        await m._handle(NewSession(engine='codex',cwd='/tmp',prompt='first',msg_id='first-message',request_id='create',client_id='phone'))
        await settle(lambda:any(method=='turn/start' for method,_ in p.calls))
        assert m.focused_sid=='codex@created'
        assert any(e.type=='session_focus' and e.session_id=='codex@created' and e.request_id=='create' for e in tr.sent)
        assert not any(method=='turn/interrupt' for method,_ in p.calls)
        assert old.sdk.session_id=='existing'
        await cleanup(m)
    asyncio.run(run())


def test_busy_send_and_edit_queue_until_explicit_start_now():
    async def run():
        m,tr,p=await setup()
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing']
        await m._handle(Query(sid=ctx.key,prompt='first',msg_id='a',delivery='queue',cmd_id='command-'+str(len(p.calls)),client_id='phone'))
        await settle(lambda:ctx.sdk.turn_id is not None)
        await m._handle(Query(sid=ctx.key,prompt='second',msg_id='b',delivery='queue',cmd_id='command-'+str(len(p.calls)),client_id='phone'))
        await settle(lambda:len(ctx.queued_queries)==1)
        await m._handle(UpdateQueuedQuery(sid=ctx.key,msg_id='b',prompt='edited',start_now=False,cmd_id='edit',client_id='phone'))
        assert len([x for x in p.calls if x[0]=='turn/start'])==1
        assert not any(method=='turn/interrupt' for method,_ in p.calls)
        assert ctx.queued_queries[0].msg_id=='b' and ctx.queued_queries[0].prompt=='edited'
        await m._handle(UpdateQueuedQuery(sid=ctx.key,msg_id='b',prompt='edited',start_now=True,cmd_id='guide',client_id='phone'))
        await settle(lambda:len([x for x in p.calls if x[0]=='turn/start'])==2)
        assert len([x for x in p.calls if x[0]=='turn/interrupt'])==1
        assert [args['input'][0]['text'] for method,args in p.calls if method=='turn/start']==['first','edited']
        await cleanup(m)
    asyncio.run(run())


def test_model_and_effort_can_be_changed_during_a_turn_for_the_next_turn():
    async def run():
        m,tr,p=await setup()
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing']
        await m._codex.start_turn(ctx,Query(sid=ctx.key,prompt='first',msg_id='first'))
        first=next(params for method,params in p.calls if method=='turn/start')
        assert first['model']=='model-a' and first['effort']=='high'
        await m._handle(SetModel(sid=ctx.key,model='model-b'))
        await m._handle(SetEffort(sid=ctx.key,effort='low',engine='codex'))
        assert ctx.state=='running'
        assert ctx.sdk.model=='model-b' and ctx.sdk.effort=='low'
        assert not any(method=='turn/interrupt' for method,_ in p.calls)
        await p.on_event({'method':'turn/completed','params':{'threadId':'existing',
            'turn':{'id':ctx.sdk.turn_id,'status':'completed','items':[]}}})
        await m._codex.start_turn(ctx,Query(sid=ctx.key,prompt='second',msg_id='second'))
        turns=[params for method,params in p.calls if method=='turn/start']
        assert turns[-1]['model']=='model-b'
        assert turns[-1]['effort']=='low'
        await cleanup(m)
    asyncio.run(run())


def test_streamed_answer_is_not_duplicated_and_unknown_payload_is_not_forwarded():
    async def run():
        m,tr,p=await setup();await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing'];await m._codex.start_turn(ctx,Query(prompt='hello',msg_id='a'))
        for method,params in [
            ('item/started',{'item':{'id':'answer','type':'agentMessage','phase':'final','text':''}}),
            ('item/agentMessage/delta',{'itemId':'answer','delta':'hello'}),
            ('item/completed',{'item':{'id':'answer','type':'agentMessage','phase':'final','text':'hello world'}}),
            ('item/completed',{'item':{'id':'secret','type':'new-unknown','credential':'SECRET'}}),
        ]:await m._codex.on_event({'method':method,'params':{'threadId':'existing',**params}})
        assert ''.join(e.text for e in tr.sent if e.type=='delta')=='hello world'
        assert 'SECRET' not in ''.join(e.model_dump_json() for e in tr.sent)
        await cleanup(m)
    asyncio.run(run())


def test_approval_and_user_questions_can_be_answered_and_cancelled_without_implicit_accept():
    async def run():
        m,tr,p=await setup();await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing'];await m._codex.start_turn(ctx,Query(prompt='hello',msg_id='a'))
        await m._codex.on_event({'id':'approval','method':'item/commandExecution/requestApproval','params':{'threadId':'existing','command':'pwd'}})
        await settle(lambda:bool(ctx.pending_asks))
        ask=next(iter(ctx.pending_asks))
        await m._handle(AnswerQuestion(sid=ctx.key,ask_id=ask,answer='允许本次'))
        await settle(lambda:bool(p.responses));assert p.responses[-1]==('approval',{'decision':'accept'})
        await m._codex.on_event({'id':'question','method':'item/tool/requestUserInput','params':{'threadId':'existing','questions':[{'id':'q','header':'确认','question':'哪一个？','options':[{'label':'A'},{'label':'B'}]}]}})
        await settle(lambda:bool(ctx.pending_asks))
        await m._handle(Interrupt(sid=ctx.key))
        await settle(lambda:len(p.responses)==2)
        assert p.responses[-1]==('question',{'answers':{}})
        assert any(e.type=='ask_user_closed' and e.reason=='cancelled' for e in tr.sent)
        await cleanup(m)
    asyncio.run(run())


def test_lost_start_response_does_not_replay_mutation_or_accept_later_writes():
    async def run():
        m,tr,p=await setup();await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing'];p.fail_start=CodexUnavailable('timeout')
        await m._handle(Query(sid=ctx.key,prompt='uncertain',msg_id='a',delivery='queue',cmd_id='command-'+str(len(p.calls)),client_id='phone'))
        await settle(lambda:ctx.write_state=='read_only')
        await settle(lambda:not ctx.queued_queries)
        await m._handle(Query(sid=ctx.key,prompt='must not send',msg_id='b',delivery='queue',cmd_id='command-'+str(len(p.calls)),client_id='phone'))
        assert len([x for x in p.calls if x[0]=='turn/start'])==1
        assert ctx.state=='running'
        assert ctx.control_mode=='remote', 'an uncertain Remote send is not desktop ownership'
        await cleanup(m)
    asyncio.run(run())


def test_external_active_thread_is_read_only_and_cannot_be_queued_or_interrupted():
    async def run():
        m,tr,p=await setup();p.external=True
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing'];assert ctx.write_state=='read_only'
        await m._handle(Query(sid=ctx.key,prompt='wrong',msg_id='a',delivery='queue',cmd_id='command-'+str(len(p.calls)),client_id='phone'))
        await m._handle(Interrupt(sid=ctx.key))
        assert not any(method in {'thread/resume','turn/start','turn/interrupt'} for method,_ in p.calls)
        await cleanup(m)
    asyncio.run(run())


def test_effort_control_targets_codex_only_and_checks_its_native_model_capabilities():
    async def run():
        m,tr,p=await setup();await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        await m._handle(SetEffort(sid='codex@existing',effort='low'))
        assert m.sessions['codex@existing'].sdk.effort=='low'
        assert not m._codex.owns(GetModels(engine='dsh'))
        await cleanup(m)
    asyncio.run(run())


def test_stop_during_turn_start_waits_for_native_id_and_does_not_lose_the_stop():
    async def run():
        m,tr,p=await setup();await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing'];gate=asyncio.Event();original=p.call
        async def delayed(method,params=None,**kwargs):
            if method=='turn/start':await gate.wait()
            return await original(method,params,**kwargs)
        p.call=delayed
        task=asyncio.create_task(m._codex.start_turn(ctx,Query(prompt='first',msg_id='pending')))
        await settle(lambda:ctx.sdk.pending_query is not None)
        await m._handle(Interrupt(sid=ctx.key));assert ctx.state=='interrupting'
        gate.set();await task
        assert len([x for x in p.calls if x[0]=='turn/interrupt'])==1
        assert ctx.state=='idle'
        await cleanup(m)
    asyncio.run(run())


def test_external_read_only_thread_can_resume_after_its_native_turn_finishes():
    async def run():
        m,tr,p=await setup();p.external=True
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing'];old_seq=ctx.seq
        assert ctx.write_state=='read_only'
        p.external=False
        await m._handle(SwitchSession(engine='codex',session_id=ctx.key))
        assert m.sessions[ctx.key] is ctx and ctx.seq>old_seq
        assert ctx.state=='idle' and ctx.write_state=='writable'
        await cleanup(m)
    asyncio.run(run())


def test_message_fork_passes_the_exact_native_turn_boundary():
    async def run():
        from cc_remote.protocol import ForkSession
        m,tr,p=await setup();original=p.call
        async def fork_peer(method,params=None,**kwargs):
            if method=='thread/fork':
                p.calls.append((method,params));return {'thread':{'id':'forked','cwd':'/tmp'}}
            return await original(method,params,**kwargs)
        p.call=fork_peer
        result=await m._handle(ForkSession(session_id='codex@existing',last_turn_id='old-turn',request_id='fork',client_id='phone'))
        assert result.session_id=='codex@forked' and result.last_turn_id=='old-turn'
        assert next(params for method,params in p.calls if method=='thread/fork')['lastTurnId']=='old-turn'
        await cleanup(m)
    asyncio.run(run())


def test_reopening_a_blocked_codex_session_republishes_the_same_answerable_question():
    async def run():
        m,tr,p=await setup();await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        await m._codex.on_event({'id':'approval','method':'item/commandExecution/requestApproval','params':{'threadId':'existing','command':'pwd'}})
        ctx=m.sessions['codex@existing'];await settle(lambda:bool(ctx.pending_asks))
        ask=next(iter(ctx.pending_asks));tr.sent.clear()
        await m._handle(SwitchSession(engine='codex',session_id=ctx.key,client_id='new-phone'))
        assert any(e.type=='ask_user' and e.ask_id==ask and e.to=='new-phone' for e in tr.sent)
        await m._handle(AnswerQuestion(sid=ctx.key,ask_id=ask,answer='拒绝'))
        await settle(lambda:bool(p.responses));assert p.responses[-1]==('approval',{'decision':'decline'})
        await cleanup(m)
    asyncio.run(run())


def test_missing_codex_file_target_cannot_fall_back_to_the_focused_dsh_workspace(tmp_path):
    async def run():
        from cc_remote.protocol import GetFilePreview
        from tests.test_multisession import _mk_ctx
        m,tr,p=await setup();ctx=_mk_ctx('dsh@focused');ctx.engine='dsh';ctx.cwd=str(tmp_path)
        m.sessions[ctx.key]=ctx;m.focused_sid=ctx.key
        (tmp_path/'private.txt').write_text('must not read another session')
        result=await m._handle(GetFilePreview(sid='codex@missing',path='private.txt',request_id='read'))
        assert result.error and not result.content
        await cleanup(m)
    asyncio.run(run())


def test_resume_uses_metadata_and_paginated_head_instead_of_full_transcript():
    async def run():
        m,tr,p=await setup()
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        assert all(not params.get('includeTurns') for method,params in p.calls if method=='thread/read')
        assert next(params for method,params in p.calls if method=='thread/resume')['excludeTurns'] is True
        await cleanup(m)
    asyncio.run(run())


def test_desktop_compaction_head_is_read_only_running_and_mirrored(tmp_path):
    from cc_remote.wrapper.codex_lifecycle import NativeLifecycle
    from tests.test_codex_lifecycle import append
    async def run():
        m,tr,p=await setup()
        source=tmp_path/'sessions'/'rollout.jsonl';source.parent.mkdir();source.touch()
        append(source,'task_started','old-turn')
        p.turns[0]['status']='interrupted'
        original=p.call
        async def call(method,params=None,**kwargs):
            result=await original(method,params,**kwargs)
            if 'thread' in result:result['thread']['path']=str(source)
            return result
        p.call=call;m._codex.lifecycle=NativeLifecycle(tmp_path)
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing']
        assert ctx.state=='running' and not ctx.sdk.writable
        assert not any(method=='thread/resume' for method,_ in p.calls)
        h=await m._codex.history(GetHistory(session_id=ctx.key,detail='summary'))
        assert h.in_progress and not h.turns[0].done and not h.turns[0].interrupted
        append(source,'task_complete','old-turn')
        await asyncio.sleep(1.1)
        assert ctx.state=='idle'
        await m._handle(SwitchSession(engine='codex',session_id=ctx.key))
        assert ctx.sdk.writable and ctx.state=='idle'
        h=await m._codex.history(GetHistory(session_id=ctx.key,detail='summary'))
        assert h.turns[0].done and not h.turns[0].interrupted
        await cleanup(m)
    asyncio.run(run())


def test_image_history_is_lazy_source_bound_and_survives_cold_read():
    import base64,io
    from PIL import Image
    from cc_remote.protocol import GetHistoryImage
    async def run():
        m,tr,p=await setup()
        data=io.BytesIO();Image.new('RGB',(16,16),'red').save(data,format='PNG')
        encoded=base64.b64encode(data.getvalue()).decode()
        p.turns[0]['items'][0]['content'].append({'type':'image','url':'data:image/png;base64,'+encoded})
        h=await m._codex.history(GetHistory(session_id='codex@existing',detail='summary'))
        ref=h.turns[0].imageRefs[0]
        assert ref['width']==16 and not h.turns[0].images
        response=await m._handle(GetHistoryImage(session_id='codex@existing',turn_id='old-turn',image_id=ref['image_id'],variant='full',request_id='img',client_id='phone'))
        assert response.data==encoded and response.to=='phone' and not response.error
        unknown=await m._handle(GetHistoryImage(session_id='codex@existing',turn_id='old-turn',image_id='unknown',variant='full',request_id='other'))
        assert unknown.error and not unknown.data
        other=io.BytesIO();Image.new('RGB',(16,16),'blue').save(other,format='PNG')
        p.turns[0]['items'][0]['content'][-1]['url']='data:image/png;base64,'+base64.b64encode(other.getvalue()).decode()
        replaced=await m._handle(GetHistoryImage(session_id='codex@existing',turn_id='old-turn',image_id=ref['image_id'],variant='full',request_id='replaced'))
        assert replaced.error and not replaced.data, 'a locator must never return replaced image bytes'
        await cleanup(m)
    asyncio.run(run())


def test_model_defaults_must_be_present_in_native_catalog_and_failures_are_reviewed_copy():
    from cc_remote.wrapper.codex_stream import turn_failure
    async def run():
        m,tr,p=await setup();original=p.call
        async def call(method,params=None,**kwargs):
            if method=='config/read':return {'config':{'model':'desktop-only','model_reasoning_effort':'bogus'}}
            return await original(method,params,**kwargs)
        p.call=call
        models=await m._codex.models(GetModels(engine='codex'))
        assert models.default_model=='model-a' and models.default_effort=='high'
        await cleanup(m)
    asyncio.run(run())
    safe=turn_failure({'error':{'message':"The 'private-model' model is not supported. Bearer SECRET https://private"}})
    assert 'SECRET' not in safe and 'private' not in safe and '模型不可用' in safe


def test_idle_writer_conflict_opens_read_only_and_reentry_recovers_after_release():
    async def run():
        m,tr,p=await setup();original=p.call;locked=True
        async def call(method,params=None,**kwargs):
            if method=='thread/resume' and locked:
                raise CodexRpcError(-32600,'thread existing already has an active writer')
            return await original(method,params,**kwargs)
        p.call=call
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing',client_id='phone'))
        ctx=m.sessions['codex@existing']
        assert ctx.state=='idle' and not ctx.sdk.writable
        assert not any(e.type=='error' for e in tr.sent)
        assert any(e.type=='snapshot' and '派生' in e.control.reason for e in tr.sent)
        locked=False
        await m._handle(SwitchSession(engine='codex',session_id=ctx.key))
        assert ctx.sdk.writable
        await cleanup(m)
    asyncio.run(run())


def test_native_read_failures_return_correlated_history_and_detail_without_leaving_loading_open():
    async def run():
        m,tr,p=await setup();original=p.call
        async def call(method,params=None,**kwargs):
            if method in {'thread/turns/list','thread/items/list'}:
                raise CodexRpcError(-32601,'Private native error must not be forwarded')
            return await original(method,params,**kwargs)
        p.call=call
        history=await m._handle(GetHistory(session_id='codex@existing',detail='summary',client_id='phone'))
        assert history.type=='history' and history.revision and history.to=='phone'
        assert history.authoritative is False and history.error and not history.turns
        detail=await m._handle(GetTurnDetail(session_id='codex@existing',turn_id='old-turn',client_id='phone'))
        assert detail.type=='turn_detail' and detail.revision and detail.to=='phone'
        assert detail.authoritative is False and detail.error and not detail.events
        assert 'Private' not in history.error and 'Private' not in detail.error
        await cleanup(m)
    asyncio.run(run())


def test_native_detail_rejects_a_stale_revision_with_the_requested_correlation_key():
    async def run():
        m,tr,p=await setup()
        stale=m._history_revision('codex@existing')
        m._bump_history_revision('codex@existing')
        detail=await m._handle(GetTurnDetail(
            session_id='codex@existing',turn_id='old-turn',
            revision=stale,client_id='phone'))
        assert detail.type=='turn_detail' and detail.revision==stale
        assert detail.authoritative is False and detail.error
        assert not detail.events and detail.to=='phone'
        assert not any(method=='thread/items/list' for method,_ in p.calls)
        await cleanup(m)
    asyncio.run(run())


def test_native_reasoning_summaries_survive_a_fresh_history_read():
    async def run():
        m,tr,p=await setup()
        p.turns[0]['items'].insert(1,{'id':'thought-with-body','type':'reasoning','summary':['Visible summary'],'content':[]})
        p.turns[0]['items'].insert(2,{'id':'thought-no-body','type':'reasoning','summary':[],'content':[],'encryptedContent':'SECRET'})
        result=await m._handle(GetHistory(session_id='codex@existing',detail='summary',client_id='fresh-ipad'))
        thoughts=[b for b in result.turns[0].blocks if b.get('processKind')=='reasoning']
        assert thoughts[0]['summary']=='Visible summary'
        assert thoughts[1]['summary']=='本机 Codex 未提供这段思考正文'
        assert 'SECRET' not in result.model_dump_json()
        detail=await m._handle(GetTurnDetail(session_id='codex@existing',turn_id='old-turn',client_id='fresh-phone'))
        assert any(e['type']=='process' and e.get('summary')=='Visible summary' for e in detail.events)
        await cleanup(m)
    asyncio.run(run())


def test_thin_history_keeps_a_detail_affordance_without_inventing_event_counts():
    async def run():
        m,tr,p=await setup()
        result=await m._handle(GetHistory(session_id='codex@existing',detail='summary',client_id='fresh-phone'))
        assert result.turns[0].detailEventCount==0
        assert result.turns[0].detailAvailable and not result.turns[0].detailLoaded
        await m._handle(GetHistory(session_id='codex@existing',detail='full',client_id='full-reader'))
        assert [params for method,params in p.calls if method=='thread/turns/list'][-1]['itemsView']=='full'
        await cleanup(m)
    asyncio.run(run())


def test_remote_new_threads_offer_user_input_in_default_mode_and_restore_queue_on_focus():
    from cc_remote.wrapper.codex_questions import TOOL_NAME
    async def run():
        m,tr,p=await setup()
        await m._handle(NewSession(engine='codex',cwd='/tmp',request_id='create',client_id='phone'))
        tools=next(args['dynamicTools'] for method,args in p.calls if method=='thread/start')
        assert len(tools)==1 and tools[0]['name']==TOOL_NAME
        ctx=m.sessions['codex@created']
        await m._handle(Query(sid=ctx.key,prompt='first',msg_id='first',delivery='queue',cmd_id='first',client_id='phone'))
        await settle(lambda:ctx.sdk.turn_id is not None)
        await m._handle(Query(sid=ctx.key,prompt='waiting',msg_id='waiting',delivery='queue',cmd_id='waiting',client_id='phone'))
        await settle(lambda:len(ctx.queued_queries)==1)
        tr.sent.clear()
        await m._handle(SwitchSession(engine='codex',session_id=ctx.key,client_id='new-phone'))
        queue=next(e for e in tr.sent if e.type=='query_queue')
        assert queue.can_start_now and queue.items[0].msg_id=='waiting'
        assert len([x for x in p.calls if x[0]=='turn/start'])==1
        await cleanup(m)
    asyncio.run(run())


def test_dynamic_user_input_roundtrips_multiple_questions_and_is_cancelled_by_guide():
    import json
    from cc_remote.wrapper.codex_questions import TOOL_NAME
    async def run():
        m,tr,p=await setup()
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        ctx=m.sessions['codex@existing']
        await m._codex.start_turn(ctx,Query(prompt='hello',msg_id='a'))
        questions=[{'id':'one','header':'选择','question':'哪一个？','options':[{'label':'A'},{'label':'B'}]},
                   {'id':'two','header':'补充','question':'补充说明'}]
        await m._codex.on_event({'id':'dynamic','method':'item/tool/call','params':{
            'threadId':'existing','tool':TOOL_NAME,'callId':'local-call','arguments':{'questions':questions}}})
        await settle(lambda:bool(ctx.pending_asks))
        first=next(iter(ctx.pending_asks))
        assert ctx.pending_ask_specs[first]['allow_text']
        await m._handle(AnswerQuestion(sid=ctx.key,ask_id=first,answer='自定义答案'))
        await settle(lambda:bool(ctx.pending_asks) and first not in ctx.pending_asks)
        second=next(iter(ctx.pending_asks))
        await m._handle(SwitchSession(engine='codex',session_id=ctx.key,client_id='phone-reloaded'))
        assert any(e.type=='ask_user' and e.ask_id==second and e.to=='phone-reloaded' for e in tr.sent)
        await m._handle(AnswerQuestion(sid=ctx.key,ask_id=second,answer='详细文字'))
        await settle(lambda:bool(p.responses))
        assert p.responses[-1][1]['success']
        assert json.loads(p.responses[-1][1]['contentItems'][0]['text'])=={
            'answers':{'one':{'answers':['自定义答案']},'two':{'answers':['详细文字']}}}
        await m._codex.on_event({'id':'cancel-dynamic','method':'item/tool/call','params':{
            'threadId':'existing','tool':TOOL_NAME,'arguments':{'questions':questions}}})
        await settle(lambda:bool(ctx.pending_asks))
        await m._handle(Query(sid=ctx.key,prompt='guide now',msg_id='guide',delivery='queue',cmd_id='queue-guide',client_id='phone'))
        await settle(lambda:len(ctx.queued_queries)==1)
        await m._handle(UpdateQueuedQuery(sid=ctx.key,msg_id='guide',prompt='guide now',start_now=True,cmd_id='guide-now',client_id='phone'))
        await settle(lambda:len([x for x in p.calls if x[0]=='turn/start'])==2)
        await settle(lambda:len(p.responses)==2)
        assert p.responses[-1][0]=='cancel-dynamic' and p.responses[-1][1]['success'] is False
        assert not ctx.pending_asks
        assert len([x for x in p.calls if x[0]=='turn/interrupt'])==1
        await cleanup(m)
    asyncio.run(run())


def test_invalid_or_unknown_dynamic_requests_cannot_open_prompts_or_forward_payloads():
    from cc_remote.wrapper.codex_questions import TOOL_NAME
    async def run():
        m,tr,p=await setup()
        await m._handle(SwitchSession(engine='codex',session_id='codex@existing'))
        for identity,tool,args in [('bad',TOOL_NAME,{'questions':[{'id':'q','question':'ok'},{'id':'q','question':'duplicate'}]}),
                                   ('plugin','private_plugin',{'secret':'never relay'})]:
            await m._codex.on_event({'id':identity,'method':'item/tool/call','params':{
                'threadId':'existing','tool':tool,'arguments':args}})
        await settle(lambda:len(p.responses)==2)
        assert not any(e.type=='ask_user' for e in tr.sent)
        assert not m.sessions['codex@existing'].pending_asks
        assert p.responses[0][1]['success'] is False and p.responses[1][1]=={'rejected':True}
        await cleanup(m)
    asyncio.run(run())
