"""Desktop lifecycle fences survive compaction, append races and cold opens."""
import json

from cc_remote.wrapper.codex_lifecycle import NativeLifecycle, apply_lifecycle


def append(path, kind, turn, **fields):
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'event_msg','payload':{'type':kind,'turn_id':turn,**fields}})+'\n')


def test_compaction_is_not_an_interrupt_and_completion_is_authoritative(tmp_path):
    source=tmp_path/'sessions'/'rollout.jsonl';source.parent.mkdir();source.touch()
    lifecycle=NativeLifecycle(tmp_path);thread={'path':str(source)}
    append(source,'task_started','a')
    append(source,'turn_aborted','a',reason='interrupted')
    append(source,'task_started','b')
    with source.open('a') as stream:stream.write(json.dumps({'type':'compacted','payload':{'message':'private summary'}})+'\n')
    proof=lifecycle.read(thread)
    assert proof.active=='b' and proof.terminals['a']=='interrupted'
    interrupted={'id':'b','status':'interrupted','items':[]}
    assert apply_lifecycle(interrupted,proof)['status']=='inProgress'
    assert apply_lifecycle({'id':'a','status':'interrupted'},proof)['status']=='interrupted'
    append(source,'task_complete','b')
    completed=lifecycle.read(thread)
    assert completed.active is None and apply_lifecycle(interrupted,completed)['status']=='completed'
    assert proof.active=='b', 'cache updates cannot mutate an in-flight projection'
    assert NativeLifecycle(tmp_path).read(thread).terminals['b']=='completed'


def test_partial_appends_are_retried_and_unknown_items_do_not_supply_fences(tmp_path):
    source=tmp_path/'sessions'/'rollout.jsonl';source.parent.mkdir();source.touch()
    lifecycle=NativeLifecycle(tmp_path);thread={'path':str(source)}
    append(source,'user_message','not-authority',message='task_started')
    start=json.dumps({'type':'event_msg','payload':{'type':'task_started','turn_id':'real'}})
    source.write_text(source.read_text()+start[:20]);assert lifecycle.read(thread).active is None
    with source.open('a') as stream:stream.write(start[20:]+'\n')
    assert lifecycle.read(thread).active=='real'
    append(source,'task_complete','real',error={'message':'private provider response'})
    proof=lifecycle.read(thread)
    assert proof.terminals['real']=='failed' and 'private' not in repr(proof)
    source.write_text('')
    assert lifecycle.read(thread).active is None


def test_untrusted_path_and_oversized_record_cannot_grant_write_ownership(tmp_path,monkeypatch):
    lifecycle=NativeLifecycle(tmp_path)
    private=tmp_path/'auth.json';private.write_text('{}')
    assert lifecycle.read({'path':str(private)}) is None
    other=tmp_path/'elsewhere.jsonl';other.write_text('{}\n')
    link=tmp_path/'sessions'/'link.jsonl';link.parent.mkdir();link.symlink_to(other)
    assert lifecycle.read({'path':str(link)}) is None
    monkeypatch.setattr('cc_remote.wrapper.codex_lifecycle.MAX_RECORD_BYTES',50)
    source=link.parent/'large.jsonl';source.write_text('x'*100+'\n')
    proof=lifecycle.read({'path':str(source)})
    assert proof.complete is False
    assert apply_lifecycle({'id':'b','status':'interrupted'},proof)['status']=='interrupted'
