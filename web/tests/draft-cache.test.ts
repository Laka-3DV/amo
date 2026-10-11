import assert from "node:assert/strict";
import {ComposerDraftStore,composerDraftKey,emptyDraft,type DraftCacheSnapshot,type DraftPersistence} from "../src/composer-drafts.ts";

const key=composerDraftKey("machine-a","code","dsh","session-a");
const other=composerDraftKey("machine-b","code","dsh","session-a");
let saved:DraftCacheSnapshot={version:1,drafts:[],history:[]};
let fail=false;
const persistence:DraftPersistence={
  async load(){return structuredClone(saved);},
  async save(value){if(fail)throw Error("quota");saved=structuredClone(value);},
};
const store=new ComposerDraftStore(persistence);await store.ready;
const original={...emptyDraft(),input:"语音草稿\n第二段",images:[{media_type:"image/png" as const,data:"cached-image"}]};
store.setLabel(key,"测试会话");store.set(key,original);store.archive(key,original,"saved");
await store.flush();
const restored=new ComposerDraftStore(persistence);await restored.ready;
assert.equal(restored.get(key).input,original.input,"a fresh browser store must restore the unsent text");
assert.equal(restored.get(key).images[0].data,"cached-image","attachments survive persistence too");
const clone=restored.get(key);clone.images[0].data="edited";
assert.equal(restored.get(key).images[0].data,"cached-image","recovery copies cannot mutate cached records");
restored.archive(key,original,"cleared");restored.set(key,emptyDraft());await restored.flush();
assert.equal(restored.get(key).input,"");
assert.equal(restored.listHistory(key)[0].reason,"cleared","automatic empty-draft protection must not downgrade the explicit clear reason");
assert.equal(restored.listHistory(key)[0].draft.input,original.input);
restored.set(other,{...emptyDraft(),input:"other machine"});restored.archive(other,restored.get(other),"saved");
assert.equal(restored.listHistory(key,true).some(item=>item.key===other),false,"history cannot cross a remote-machine scope");
for(let i=0;i<60;i++)restored.archive(key,{...emptyDraft(),input:`version ${i}`} ,"saved");
assert.equal(restored.listHistory(key).length,50);
assert.equal(restored.listHistory(key)[0].draft.input,"version 59");
const nextKey=composerDraftKey("machine-a","code","dsh","native-session");
restored.set(key,{...emptyDraft(),input:"new native id"});restored.rekey(key,nextKey);
assert.equal(restored.get(key).input,"");assert.equal(restored.get(nextKey).input,"new native id");
assert.equal(restored.listHistory(nextKey).length,50,"rekey carries recovery history with its draft");
assert.equal(restored.archive(nextKey,{...emptyDraft(),input:"x".repeat(13*1024*1024)},"cleared"),false,
  "an oversized recovery snapshot must reject instead of promising a successful archive");
assert.equal(restored.get(nextKey).input,"new native id");
fail=true;await restored.flush();
assert.ok(restored.storageWarning);assert.equal(restored.get(nextKey).input,"new native id","a storage failure cannot destroy the in-memory draft");
fail=false;await restored.flush();assert.equal(restored.storageWarning,"");
store.clear();restored.clear();await Promise.all([store.flush(),restored.flush()]);
assert.deepEqual(saved.drafts,[]);assert.deepEqual(saved.history,[]);

// A late storage read must not overwrite typing performed while hydration waits.
let finishLoad:(value:unknown)=>void=()=>{};
const delayed=new ComposerDraftStore({load:()=>new Promise(resolve=>{finishLoad=resolve;}),async save(){}});
delayed.set(key,{...emptyDraft(),input:"typed now"});
finishLoad({version:1,drafts:[{key,at:Date.now(),draft:{...emptyDraft(),input:"older"}}],history:[]});
await delayed.ready;assert.equal(delayed.get(key).input,"typed now");delayed.clear();await delayed.flush();

const corrupt=new ComposerDraftStore({async load(){return {version:1,drafts:[
  {key,at:Date.now()-31*86400_000,draft:{...emptyDraft(),input:"expired"}},
  {key:other,at:Date.now(),draft:{input:"bad",images:null,files:[],pastes:[]}},
],history:[]};},async save(){}});
await corrupt.ready;assert.equal(corrupt.get(key).input,"");assert.equal(corrupt.get(other).input,"");corrupt.clear();await corrupt.flush();
console.log("persistent draft cache tests passed");
