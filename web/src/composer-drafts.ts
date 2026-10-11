import type { Engine, QueryFile, QueryImg, Space } from "./protocol";
import type { ComposerPaste } from "./composer-pastes";
export type { ComposerPaste } from "./composer-pastes";

export interface ComposerDraft {
  input: string;
  images: QueryImg[];
  files: QueryFile[];
  pastes: ComposerPaste[];
}
export type DraftReason = "auto" | "saved" | "sent" | "cleared" | "replaced" | "clipboard";
export interface DraftHistoryEntry {
  id: string;
  key: string;
  label: string;
  at: number;
  reason: DraftReason;
  draft: ComposerDraft;
}
export interface DraftCacheSnapshot {
  version: 1;
  resetAt?: number;
  drafts: Array<{key: string; at: number; draft: ComposerDraft}>;
  history: DraftHistoryEntry[];
}
export interface DraftPersistence {
  load(): Promise<unknown>;
  save(snapshot: DraftCacheSnapshot): Promise<void>;
}
const MAX_DRAFTS = 32;
const MAX_RETAINED_BYTES = 64 * 1024 * 1024;
const MAX_HISTORY_BYTES = 24 * 1024 * 1024;
const MAX_HISTORY = 50;
const RETENTION_MS = 30 * 86400_000;
const REASONS = new Set<DraftReason>(["auto", "saved", "sent", "cleared", "replaced", "clipboard"]);
export function emptyDraft(): ComposerDraft { return { input:"", images:[], files:[], pastes:[] }; }
export function cloneDraft(draft: ComposerDraft): ComposerDraft {
  return {input:draft.input,images:draft.images.map(item=>({media_type:item.media_type,data:item.data})),
    files:draft.files.map(item=>({filename:item.filename,data:item.data})),pastes:draft.pastes.map(item=>({id:item.id,text:item.text,chars:item.chars,lines:item.lines}))};
}
export function draftHasContent(draft: ComposerDraft): boolean {
  return !!draft.input || !!draft.images.length || !!draft.files.length || !!draft.pastes.length;
}
function draftBytes(draft: ComposerDraft): number {
  return draft.input.length*2 + draft.images.reduce((sum,item)=>sum+item.data.length,0)
    + draft.files.reduce((sum,item)=>sum+item.filename.length*2+item.data.length,0)
    + draft.pastes.reduce((sum,item)=>sum+item.text.length*2,0);
}
export function sameDraft(a: ComposerDraft,b: ComposerDraft): boolean {
  return a.input===b.input && a.images.length===b.images.length && a.files.length===b.files.length
    && a.pastes.length===b.pastes.length
    && a.images.every((v,i)=>v.data===b.images[i].data && v.media_type===b.images[i].media_type)
    && a.files.every((v,i)=>v.data===b.files[i].data && v.filename===b.files[i].filename)
    && a.pastes.every((v,i)=>v.text===b.pastes[i].text);
}
function validDraft(raw: unknown): raw is ComposerDraft {
  if (!raw || typeof raw!=="object") return false;
  const d=raw as ComposerDraft;
  return typeof d.input==="string" && Array.isArray(d.images) && d.images.length<=8
    && Array.isArray(d.files) && d.files.length<=8 && Array.isArray(d.pastes) && d.pastes.length<=100
    && d.images.every(v=>v && ["image/png","image/jpeg","image/jpg","image/webp"].includes(v.media_type) && typeof v.data==="string")
    && d.files.every(v=>v && typeof v.filename==="string" && typeof v.data==="string")
    && d.pastes.every(v=>v && typeof v.id==="string" && typeof v.text==="string"
      && Number.isFinite(v.chars) && Number.isFinite(v.lines))
    && draftBytes(d)<=MAX_RETAINED_BYTES;
}
function fresh(at: unknown): at is number {
  return typeof at==="number" && Number.isFinite(at) && at>Date.now()-RETENTION_MS && at<=Date.now()+60_000;
}
function scopeOf(key: string): string {
  try { const parts=JSON.parse(key);return Array.isArray(parts) && parts.length===4 ? JSON.stringify(parts.slice(0,3)) : key; }
  catch {return key;}
}
export function sameDraftScope(a: string,b: string): boolean {return scopeOf(a)===scopeOf(b);}
export function composerDraftKey(machineId: string,space: Space,engine: Engine,sessionId: string): string {
  return JSON.stringify([machineId,space,engine,sessionId]);
}

/** Bounded live drafts and recovery versions. Persistence is optional so pure
 * protocol tests remain synchronous and never access a browser or the network. */
export class ComposerDraftStore {
  private drafts=new Map<string,{draft:ComposerDraft;bytes:number;at:number}>();
  private history: DraftHistoryEntry[]=[];
  private labels=new Map<string,string>();
  private retainedBytes=0;
  private dirtyKeys=new Set<string>();
  private cleared=false;
  private resetAt=0;
  private saveTimer: ReturnType<typeof setTimeout> | null=null;
  private autoTimers=new Map<string,ReturnType<typeof setTimeout>>();
  private saving=Promise.resolve();
  private listeners=new Set<()=>void>();
  readonly ready: Promise<void>;
  storageWarning="";
  generation=0;
  invalidatePending(): void {this.generation++;}
  private persistence?: DraftPersistence;
  constructor(persistence?: DraftPersistence) {
    this.persistence=persistence;
    this.ready=persistence ? persistence.load().then(raw=>this.hydrate(raw)).catch(()=>{
      this.storageWarning="浏览器存储暂不可用，当前草稿仍保留在本页内存中";
    }) : Promise.resolve();
  }
  private hydrate(raw: unknown): void {
    if (!raw || typeof raw!=="object" || this.cleared) return;
    const state=raw as DraftCacheSnapshot;
    if (state.version!==1) return;
    this.resetAt=Number.isFinite(state.resetAt) ? state.resetAt! : 0;
    if (Array.isArray(state.drafts)) for (const item of state.drafts.slice(-MAX_DRAFTS)) {
      if (item && typeof item.key==="string" && item.key.length<4096 && !this.dirtyKeys.has(item.key)
          && fresh(item.at) && validDraft(item.draft)) this.retain(item.key,item.draft,item.at);
    }
    if (Array.isArray(state.history)) {
      this.history=state.history.filter(item=>item && typeof item.id==="string" && typeof item.key==="string"
        && typeof item.label==="string" && fresh(item.at) && REASONS.has(item.reason) && validDraft(item.draft))
        .slice(-MAX_HISTORY).map(item=>({...item,draft:cloneDraft(item.draft)})).concat(this.history);
      this.pruneHistory();
    }
    this.notify();
  }
  setLabel(key: string,label: string): void {this.labels.set(key,label.slice(0,160));}
  get(key: string): ComposerDraft {
    const entry=this.drafts.get(key);
    if (!entry) return emptyDraft();
    this.drafts.delete(key);this.drafts.set(key,entry);
    return cloneDraft(entry.draft);
  }
  private retain(key: string,draft: ComposerDraft,at=Date.now()): void {
    const previous=this.drafts.get(key);
    if (previous) {this.retainedBytes-=previous.bytes;this.drafts.delete(key);}
    if (!draftHasContent(draft)) return;
    const stored=cloneDraft(draft),bytes=draftBytes(stored);
    this.drafts.set(key,{draft:stored,bytes,at});this.retainedBytes+=bytes;
    while (this.drafts.size>MAX_DRAFTS || this.retainedBytes>MAX_RETAINED_BYTES) {
      const oldest=this.drafts.entries().next().value;
      if (!oldest) break;
      this.drafts.delete(oldest[0]);this.retainedBytes-=oldest[1].bytes;
    }
  }
  set(key: string,draft: ComposerDraft): void {
    const previous=this.drafts.get(key);
    if (previous && !draftHasContent(draft)) this.archive(key,previous.draft,"auto");
    this.dirtyKeys.add(key);this.retain(key,draft);this.scheduleSave();
    const oldTimer=this.autoTimers.get(key);if(oldTimer)clearTimeout(oldTimer);
    this.autoTimers.delete(key);
    if (this.persistence && draftHasContent(draft)) {
      const timer=setTimeout(()=>{
        this.autoTimers.delete(key);
        const latest=this.drafts.get(key);if(latest)this.archive(key,latest.draft,"auto");
      },5000);
      this.autoTimers.set(key,timer);
    }
  }
  archive(key: string,draft: ComposerDraft,reason: DraftReason,label=this.labels.get(key) ?? "草稿"): boolean {
    if (!draftHasContent(draft) || draftBytes(draft)>MAX_HISTORY_BYTES) return false;
    const last=this.history.slice().reverse().find(item=>item.key===key);
    if (last && sameDraft(last.draft,draft)) {
      if (reason==="auto" || last.reason===reason) return true;
      last.reason=reason;last.at=Date.now();last.label=label;
    } else {
      this.history.push({id:crypto.randomUUID(),key,label,at:Date.now(),reason,draft:cloneDraft(draft)});
    }
    this.pruneHistory();this.scheduleSave();this.notify();return true;
  }
  private pruneHistory(): void {
    this.history=this.history.filter(item=>fresh(item.at)).sort((a,b)=>a.at-b.at);
    let bytes=this.history.reduce((sum,item)=>sum+draftBytes(item.draft),0);
    while(this.history.length>MAX_HISTORY || bytes>MAX_HISTORY_BYTES) bytes-=draftBytes(this.history.shift()!.draft);
  }
  listHistory(key: string,all=false): DraftHistoryEntry[] {
    this.pruneHistory();
    return this.history.filter(item=>all ? scopeOf(item.key)===scopeOf(key) : item.key===key)
      .slice().reverse().map(item=>({...item,draft:cloneDraft(item.draft)}));
  }
  removeHistory(id: string): void {
    this.history=this.history.filter(item=>item.id!==id);this.scheduleSave();this.notify();
  }
  delete(key: string): void {
    this.dirtyKeys.add(key);
    const entry=this.drafts.get(key);if(entry){this.retainedBytes-=entry.bytes;this.drafts.delete(key);}
    const timer=this.autoTimers.get(key);if(timer)clearTimeout(timer);this.autoTimers.delete(key);
    this.scheduleSave();
  }
  rekey(oldKey: string,newKey: string): void {
    if(oldKey===newKey)return;
    const entry=this.drafts.get(oldKey);this.delete(oldKey);
    if(entry)this.set(newKey,entry.draft);
    this.rekeyHistory(oldKey,newKey);
  }
  rekeyHistory(oldKey: string,newKey: string): void {
    for(const item of this.history)if(item.key===oldKey)item.key=newKey;
    this.scheduleSave();this.notify();
  }
  clear(): void {
    this.invalidatePending();this.cleared=true;this.resetAt=Date.now();this.drafts.clear();this.history=[];this.retainedBytes=0;this.labels.clear();this.dirtyKeys.clear();
    for(const timer of this.autoTimers.values())clearTimeout(timer);this.autoTimers.clear();
    this.notify();void this.flush();
  }
  subscribe(listener: ()=>void): ()=>void {this.listeners.add(listener);return ()=>{this.listeners.delete(listener);};}
  private notify(): void {queueMicrotask(()=>{for(const listener of this.listeners)listener();});}
  private scheduleSave(): void {
    if(!this.persistence || this.saveTimer!==null)return;
    this.saveTimer=setTimeout(()=>{this.saveTimer=null;void this.flush();},500);
  }
  private snapshot(): DraftCacheSnapshot {
    return {version:1,resetAt:this.resetAt,drafts:Array.from(this.drafts,([key,value])=>({key,at:value.at,draft:cloneDraft(value.draft)})),
      history:this.history.map(item=>({...item,draft:cloneDraft(item.draft)}))};
  }
  async flush(): Promise<void> {
    if(this.saveTimer!==null){clearTimeout(this.saveTimer);this.saveTimer=null;}
    if(!this.persistence)return;
    await this.ready;
    this.saving=this.saving.then(()=>this.persistence!.save(this.snapshot())).then(()=>{
      const recovered=!!this.storageWarning;this.storageWarning="";if(recovered)this.notify();
    }).catch(()=>{
      this.storageWarning="自动保存暂未写入浏览器，草稿仍保留在当前页面，请勿关闭页面";this.notify();
    });
    await this.saving;
  }
}
