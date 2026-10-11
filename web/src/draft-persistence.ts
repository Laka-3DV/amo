import {cloneDraft,sameDraft,type ComposerDraft,type DraftCacheSnapshot,type DraftPersistence,type DraftHistoryEntry} from "./composer-drafts.ts";

type LiveRecord=DraftCacheSnapshot["drafts"][number];
type Row={kind:"live";value:LiveRecord}|{kind:"history";value:DraftHistoryEntry}|{kind:"reset";at:number};
/** Save changed records only. Attachment payloads live separately so dictation
 * never serializes every cached image again. A transaction commits both parts. */
export function browserDraftPersistence(): DraftPersistence {
  let database: Promise<IDBDatabase>|null=null;
  let last: DraftCacheSnapshot={version:1,drafts:[],history:[]};
  let lastGc=0;
  const hashes=new Map<string,Promise<string>>();
  const open=()=>database ??= new Promise<IDBDatabase>((resolve,reject)=>{
    if(typeof indexedDB==="undefined"){reject(new Error("Storage unavailable"));return;}
    const request=indexedDB.open("dsh-remote-drafts",2);
    request.onupgradeneeded=()=>{
      if(!request.result.objectStoreNames.contains("cache"))request.result.createObjectStore("cache");
      if(!request.result.objectStoreNames.contains("attachments"))request.result.createObjectStore("attachments");
    };
    request.onsuccess=()=>{const db=request.result;db.onversionchange=()=>{db.close();database=null;};resolve(db);};
    request.onerror=()=>reject(request.error);
    request.onblocked=()=>reject(new Error("Storage blocked"));
  });
  const digest=(data:string)=>{
    let hash=hashes.get(data);
    if(!hash){
      hash=crypto.subtle.digest("SHA-256",new TextEncoder().encode(data))
        .then(buffer=>"asset:"+Array.from(new Uint8Array(buffer),v=>v.toString(16).padStart(2,"0")).join(""));
      hashes.set(data,hash);
      if(hashes.size>128)hashes.delete(hashes.keys().next().value!);
    }
    return hash;
  };
  const maps=(snapshot:DraftCacheSnapshot)=>new Map<string,Row>([
    ...snapshot.drafts.map(value=>["live:"+value.key,{kind:"live",value}] as [string,Row]),
    ...snapshot.history.map(value=>["history:"+value.id,{kind:"history",value}] as [string,Row]),
  ]);
  return {
    async load(){
      const db=await open();
      const rows=await new Promise<Row[]>((resolve,reject)=>{
        const tx=db.transaction("cache","readonly"),request=tx.objectStore("cache").getAll();
        request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
      });
      const snapshot:DraftCacheSnapshot={version:1,drafts:[],history:[]};
      for(const row of rows){
        if(row?.kind==="live" && row.value?.draft)snapshot.drafts.push(row.value);
        else if(row?.kind==="history" && row.value?.draft)snapshot.history.push(row.value);
        else if(row?.kind==="reset")snapshot.resetAt=row.at;
      }
      const data=new Map<string,Promise<string>>();
      const decode=async(draft:ComposerDraft)=>{
        if(!draft || !Array.isArray(draft.images) || !Array.isArray(draft.files))return;
        await Promise.all([...draft.images,...draft.files].map(async item=>{
          if(!item || typeof item.data!=="string" || !item.data.startsWith("asset:"))return;
          const key=item.data;
          let content=data.get(key);
          if(!content){content=new Promise<string>((resolve,reject)=>{
            const request=db.transaction("attachments","readonly").objectStore("attachments").get(key);
            request.onsuccess=()=>typeof request.result==="string" ? resolve(request.result) : reject(new Error("Missing cached attachment"));
            request.onerror=()=>reject(request.error);
          });data.set(key,content);}
          item.data=await content;
          hashes.set(item.data,Promise.resolve(key));
        }));
      };
      await Promise.all([...snapshot.drafts,...snapshot.history].map(item=>decode(item?.draft)));
      snapshot.drafts.sort((a,b)=>a.at-b.at);snapshot.history.sort((a,b)=>a.at-b.at);
      last=snapshot;
      return snapshot;
    },
    async save(snapshot){
      const db=await open(),previous=maps(last),next=maps(snapshot);
      const changed=Array.from(next).filter(([key,row])=>{
        const old=previous.get(key);
        if(!old || old.kind==="reset" || row.kind==="reset" || old.kind!==row.kind)return true;
        return row.value.at!==old.value.at || !sameDraft(row.value.draft,old.value.draft)
          || (row.kind==="history" && old.kind==="history" && row.value.reason!==old.value.reason);
      });
      const assets=new Map<string,string>();
      const encoded=await Promise.all(changed.map(async([key,row])=>{
        if(row.kind==="reset")return [key,row] as const;
        const copy={...row,value:{...row.value,draft:cloneDraft(row.value.draft)}} as typeof row;
        await Promise.all([...copy.value.draft.images,...copy.value.draft.files].map(async item=>{
          const content=item.data;item.data=await digest(content);assets.set(item.data,content);
        }));
        return [key,copy] as const;
      }));
      await new Promise<void>((resolve,reject)=>{
        const tx=db.transaction(["cache","attachments"],"readwrite");
        const cache=tx.objectStore("cache"),attachments=tx.objectStore("attachments");
        const reset=cache.get("reset");
        reset.onsuccess=()=>{
          const existing=reset.result?.at ?? 0;
          const epoch=Math.max(existing,snapshot.resetAt ?? 0);
          if((snapshot.resetAt ?? 0)>existing){cache.clear();attachments.clear();}
          if(epoch)cache.put({kind:"reset",at:epoch},"reset");
          const accepted=encoded.filter(([,row])=>row.kind!=="reset" && row.value.at>=epoch);
          const usedNow=new Set<string>();
          for(const [,row] of accepted)if(row.kind!=="reset")
            for(const item of [...row.value.draft.images,...row.value.draft.files])usedNow.add(item.data);
          const deletions=Array.from(previous).filter(([key])=>!next.has(key));
          let pending=deletions.length+accepted.length+usedNow.size;
          const collect=()=>{
            if(Date.now()-lastGc<=30_000)return;
            const values=cache.getAll();
            values.onsuccess=()=>{
              const used=new Set<string>();
              for(const row of values.result as Row[])if(row?.kind!=="reset" && row?.value?.draft
                  && Array.isArray(row.value.draft.images) && Array.isArray(row.value.draft.files))
                for(const item of [...row.value.draft.images,...row.value.draft.files])used.add(item.data);
              const cursor=attachments.openKeyCursor();
              cursor.onsuccess=()=>{const item=cursor.result;if(!item)return;
                if(!used.has(String(item.key)))attachments.delete(item.key);item.continue();};
              lastGc=Date.now();
            };
          };
          const done=()=>{if(--pending===0)collect();};
          for(const [key,old] of deletions){
            const current=cache.get(key);
            current.onsuccess=()=>{
              if(old.kind!=="reset" && (!current.result || current.result.value?.at<=old.value.at))cache.delete(key);
              done();
            };
          }
          for(const [key,row] of accepted){
            const current=cache.get(key);
            current.onsuccess=()=>{
              if(row.kind!=="reset" && (!current.result || current.result.value?.at<=row.value.at))cache.put(row,key);
              done();
            };
          }
          for(const key of usedNow){
            const found=attachments.getKey(key);
            found.onsuccess=()=>{if(found.result===undefined)attachments.put(assets.get(key)!,key);done();};
          }
          if(pending===0)collect();
        };
        tx.oncomplete=()=>{last=snapshot;resolve();};
        tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
      });
    },
  };
}
