import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { PROTOCOL_VERSION } from "../src/protocol";

const SID = "dsh@mobile-fixture";

/** Production App, reducer, composer and RelayWs. Only the network peer is
 * replaced: every user action must emit the real v38 wire command, zero tokens. */
async function openApp(page: Page, options: {codex?: boolean; deferQueueUpdates?: boolean; nativeThinHistory?: boolean; codexId?: string; fragment?: string; blockReplayCache?: boolean; forkable?: boolean} = {}) {
  if (options.blockReplayCache) await page.addInitScript(() => {
    const open = indexedDB.open.bind(indexedDB);
    indexedDB.open = ((name: string, version?: number) => name === "cc_remote_cache"
      ? {} as IDBOpenDBRequest : open(name, version)) as typeof indexedDB.open;
  });
  await page.route("**/healthz", route => route.fulfill({json:{ok:true,capabilities:["account_usage","queued_attachments","queued_edit_hold"]}}));
  const commands: Record<string, unknown>[] = [];
  let socket: WebSocketRoute;
  let seq = 10;
  let running = false;
  let currentSid = SID;
  let acceptAnswers = false;
  let createdPrompt = "";
  let includeCreated = false;
  let createdSession = false;
  let delayCreate = false;
  let holdRosters = false;
  let pendingCreate: Record<string, unknown> | null = null;
  const CREATED = "dsh@created-fixture";
  const queued: Record<string, unknown>[] = [];
  const editors = new Set<string>();
  const queueState = () => emit({type:"query_queue",can_start_now:true,items:queued.map(q=>({msg_id:q.msg_id,kind:"queue",prompt_preview:q.prompt,image_count:(q.images as unknown[] | undefined)?.length ?? 0,file_count:(q.files as unknown[] | undefined)?.length ?? 0,retained_bytes:100})),total_count:queued.length,total_bytes:queued.length*100});
  const turns = Array.from({ length: 24 }, (_, index) => ({
    id: `history-${index}`, ...(options.forkable ? {forkPointId:`native-turn-${index}`} : {}), prompt: `历史问题 ${index}`, done: true,
    detailLoaded: true, detailEventCount: 0,
    blocks: [{ kind: "text", message_id: `text-${index}`, channel: "final",
      text: Array.from({ length: 5 }, () => `历史回复 ${index}，用于检查实际应用中的阅读位置。`).join("\n\n"), done: true }],
  }));
  const send = (event: Record<string, unknown>) => socket.send(JSON.stringify({
    v: PROTOCOL_VERSION, ts: Date.now() / 1000, ...event,
  }));
  const emit = (event: Record<string, unknown>) => send({ sid: currentSid, seq: ++seq, ...event });
  const completeCreate = () => {
    const cmd=pendingCreate;if(!cmd)return;
    pendingCreate=null;createdSession=true;
    emit({type:"snapshot",sid:"tmp-created",cc_session_id:"tmp-created",state:"running",tail_text:"",cwd:cmd.cwd});
    emit({type:"session_focus",sid:"tmp-created",session_id:"tmp-created",cwd:cmd.cwd,request_id:cmd.request_id});
    emit({type:"user_msg",sid:"tmp-created",msg_id:cmd.msg_id,client_msg_id:cmd.msg_id,prompt:cmd.prompt});
    emit({type:"state",sid:"tmp-created",state:"running"});
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    await route.fulfill({ json: path === "/api/devices" ? { devices: [{ machine_id: "default", label: "测试设备", online: true }] }
      : path === "/api/health" ? {capabilities:["queued_attachments","queued_edit_hold"]}
      : { authenticated: true } });
  });
  await page.routeWebSocket(/\/ws(?:\?|$)/, (peer) => {
    socket = peer;
    peer.onMessage((raw) => {
      const cmd = JSON.parse(raw.toString());
      commands.push(cmd);
      if (cmd.type === "hello") {
        send({ type: "hello", role: "wrapper" });
        if(options.codex)send({type:"engine_catalog",engines:[{id:"dsh",display_name:"DSH",available:true,spaces:["code"]},{id:"codex",display_name:"Codex",available:true,spaces:["code"]}]});
        emit({ type: "snapshot", cc_session_id: SID, state: running ? "running" : "idle", tail_text: "" });
        queueState();
      } else if (cmd.type === "list_sessions") {
        if (holdRosters) return;
        if(options.codex && cmd.engine==="codex"){
          send({type:"session_list",engine:"codex",space:"code",request_id:cmd.cmd_id,sessions:[{session_id:options.codexId ?? "codex@native",engine:"codex",summary:"Codex 项目会话",cwd:"/repo/sample-project",state:running?"running":"idle",last_modified:"950"}]});
        }else send({ type: "session_list", engine: "dsh", space: "code", request_id: cmd.cmd_id,
          sessions: [...(includeCreated ? [{session_id:CREATED,summary:"刚创建的对话",cwd:"/repo/sample-project",engine:"dsh",state:"running",last_modified:"2000"}] : []), { session_id: SID, summary: "移动端测试", cwd: "/repo/sample-project", engine: "dsh", state: running ? "running" : "idle", last_modified: "1000" },
            { session_id: "dsh@other", summary: "另一目录的正在运行任务", cwd: "/repo/Robot", engine: "dsh", state: "running", last_modified: "900" }] });
      } else if (cmd.type === "fork_session") {
        send({type:"session_forked",session_id:"codex@forked-fixture",parent_session_id:cmd.session_id,request_id:cmd.request_id,last_turn_id:cmd.last_turn_id,cwd:"/repo/sample-project",target:"same_cwd"});
      } else if (cmd.type === "new_session") {
        createdPrompt = cmd.prompt;
        pendingCreate = cmd;
        if (!delayCreate) completeCreate();
      } else if (cmd.type === "switch_session") {
        currentSid=cmd.session_id;
        emit({ type: "session_focus", session_id: cmd.session_id, cwd: "/repo/sample-project" });
        emit({ type: "snapshot", cc_session_id: cmd.session_id, state: running ? "running" : "idle", tail_text: "", sid: cmd.session_id });
      } else if (cmd.type === "get_history") {
        send({ type: "history", session_id: cmd.session_id, revision: "fixture-v1", build_seq: ++seq,
          live_seq: seq, authoritative: true, detail: "summary", events: [], turns: [CREATED,"tmp-created"].includes(cmd.session_id) ? [{id:"created-message",prompt:createdPrompt,done:false,blocks:[]}]
            : options.nativeThinHistory && cmd.session_id.startsWith("codex@") ? [{...turns.at(-1),detailLoaded:false,detailEventCount:0,detailAvailable:true}] : turns, has_more: false,
          newest_id: turns.at(-1)?.id, oldest_id: turns[0].id, in_progress: running });
      } else if (cmd.type === "get_turn_detail") {
        send({type:"turn_detail",session_id:cmd.session_id,turn_id:cmd.turn_id,revision:"fixture-v1",before:cmd.before ?? null,has_more:false,events:[
          {v:PROTOCOL_VERSION,ts:1,type:"user_msg",msg_id:cmd.turn_id,prompt:"历史问题 23"},
          {v:PROTOCOL_VERSION,ts:2,type:"process",item_id:"native-history-thought",kind:"reasoning",phase:"end",status:"succeeded",title:"思考",summary:"本机历史提供的思考摘要"},
          {v:PROTOCOL_VERSION,ts:3,type:"turn_end",turn_id:cmd.turn_id,result:{subtype:"completed",duration_ms:2000,is_error:false}},
        ]});
      } else if (cmd.type === "browse_files") {
        send({type:"files_list",sid:cmd.sid,request_id:cmd.request_id,path:cmd.path || ".",parent:cmd.path === "src" ? "." : null,
          entries:cmd.query ? [{name:"app.py",path:"src/app.py",kind:"file"}]
            : cmd.path === "src" ? [{name:"app.py",path:"src/app.py",kind:"file"}]
            : [{name:"src",path:"src",kind:"directory"},{name:"README.md",path:"README.md",kind:"file"}]});
      } else if (cmd.type === "get_file_preview") {
        send({type:"file_preview",sid:cmd.sid,request_id:cmd.request_id,path:cmd.path,format:"text",content:"print('preview works')",size:22,truncated:false,mtime_ns:"1"});
      } else if (cmd.type === "get_account_usage") {
        send({type:"account_usage_report",engine:cmd.engine,request_id:cmd.cmd_id,available:true,
          balances:cmd.engine==="dsh"?[{currency:"CNY",total:"136.8269542200000000",recharge:"130",bonus:"6.82695422"}]:[],
          plan:cmd.engine==="codex"?"prolite":null,
          limits:cmd.engine==="codex"?[{limit_id:"codex",primary:{used_percent:22,window_duration_mins:10080,resets_at:1792295987}}]:[]});
      } else if (cmd.type === "get_models") {
        if(options.codex && cmd.engine==="codex")send({type:"models",engine:"codex",models:[{id:"codex:model",display_name:"Codex Model",description:"",efforts:["low","high"],is_default:true}],default_model:"codex:model",default_effort:"high"});
        else send({ type: "models", engine: "dsh", models: [{ id: "dsh:model", display_name: "DeepSeek", description: "", efforts: ["high"], is_default: true }], default_model: "dsh:model", default_effort: "high" });
      } else if (cmd.type === "query" && cmd.delivery === "queue") {
        queued.push(cmd);queueState();
        if(!running){const q=queued.shift()!;queueState();running=true;emit({type:"user_msg",msg_id:q.msg_id,client_msg_id:q.msg_id,prompt:q.prompt});emit({type:"state",state:"running"});}
      } else if (cmd.type === "get_queued_query") {
        const q=queued.find(q=>q.msg_id===cmd.msg_id);
        if(q && cmd.edit_action === "begin") editors.add(cmd.msg_id);
        if(cmd.edit_action === "end") editors.delete(cmd.msg_id);
        send({type:"queued_query_detail",sid:cmd.sid,msg_id:cmd.msg_id,request_id:cmd.cmd_id,
          prompt:q?.prompt ?? null,kind:"queue",image_count:(q?.images as unknown[] | undefined)?.length ?? 0,file_count:(q?.files as unknown[] | undefined)?.length ?? 0});
      } else if (cmd.type === "update_queued_query") {
        if (!options.deferQueueUpdates) {
        const q=queued.find(q=>q.msg_id===cmd.msg_id);if(q){q.prompt=cmd.prompt;q.images=[...(q.images as unknown[] ?? []),...(cmd.add_images ?? [])];q.files=[...(q.files as unknown[] ?? []),...(cmd.add_files ?? [])];}
        if(q) editors.delete(cmd.msg_id);
        if(q&&cmd.start_now){queued.splice(queued.indexOf(q),1);queued.unshift(q);emit({type:"state",state:"interrupting"});}
        queueState();send({type:"queued_query_updated",sid:cmd.sid,msg_id:cmd.msg_id,request_id:cmd.cmd_id,updated:!!q});
        if(!running && queued.length && !editors.has(String(queued[0].msg_id))) {
          const next=queued.shift()!;queueState();emit({type:"user_msg",msg_id:next.msg_id,client_msg_id:next.msg_id,prompt:next.prompt});running=true;emit({type:"state",state:"running"});
        }
        }
      } else if (cmd.type === "cancel_queued_query") {
        const index=queued.findIndex(q=>q.msg_id===cmd.msg_id);if(index>=0)queued.splice(index,1);queueState();
      } else if (cmd.type === "query" || cmd.type === "steer") {
        running = true;
        emit({ type: "user_msg", msg_id: cmd.msg_id, client_msg_id: cmd.msg_id, prompt: cmd.prompt });
        emit({ type: "state", state: "running" });
      } else if (cmd.type === "interrupt") {
        running = false;
        emit({ type: "ask_user_closed", ask_id: "ask-fixture", reason: "cancelled" });
        emit({ type: "state", state: "idle" });
      } else if (cmd.type === "answer_question" && acceptAnswers) {
        running = false;
        emit({type:"ask_user_closed",ask_id:cmd.ask_id,reason:"answered"});
        emit({type:"state",state:"idle"});
      } else if (cmd.type === "ping") {
        send({ type: "pong", n: cmd.n });
      }
      if (cmd.cmd_id) send({ type: "command_ack", client_id: cmd.client_id, cmd_id: cmd.cmd_id });
    });
  });
  await page.goto("/tests/mobile-app.html" + (options.fragment ?? ""));
  await expect(page.locator(".composer textarea")).toBeEnabled();
  await expect(page.locator('[data-turn-id="history-23"]')).toBeVisible();
  return {
    commands, emit, holdRosters: (hold=true) => {holdRosters=hold;},
    advanceSilently: () => {
      running = true;
      turns.push({id:"missed-phone-turn", prompt:"另一端开始的任务", done:false,
        detailLoaded:true, detailEventCount:0,
        blocks:[{kind:"text",message_id:"missed-answer",channel:"final",text:"另一端正在回答，手机应自动补载",done:false}]});
    },
    delayCreate: () => {delayCreate=true;}, completeCreate,
    rekeyCreated: () => {if(createdSession) emit({type:"session_rekey",old_key:"tmp-created",session_id:CREATED,cwd:"/repo/sample-project"});},
    refreshRoster: (materialize=false) => {includeCreated=materialize;send({type:"wrapper_reconnected"});},
    laggingState:()=>{running=true;emit({type:"state",state:"idle"});},
    start:()=>{running=true;emit({type:"state",state:"running"});},
    finish:()=>{running=false;emit({type:"state",state:"idle"});const q=editors.has(String(queued[0]?.msg_id)) ? undefined : queued.shift();queueState();if(q){emit({type:"user_msg",msg_id:q.msg_id,client_msg_id:q.msg_id,prompt:q.prompt});running=true;emit({type:"state",state:"running"});}},
    acceptAnswers: () => { acceptAnswers = true; },
    ask: () => {
      running = true;
      emit({ type: "state", state: "running" });
      emit({ type: "ask_user", ask_id: "ask-fixture", header: "继续任务？",
        question: Array.from({ length: 30 }, () => "这是一条很长的提问，用来确认取消按钮不会被内容挤出屏幕。").join("\n"),
        options: [{ label: "继续" }, { label: "换个方案" }], allow_text: true });
    },
  };
}

test("ask cancellation stays reachable and emits interrupt even while an answer is pending", async ({ page }) => {
  const app = await openApp(page);
  await page.setViewportSize({ width: 390, height: 510 });
  app.ask();
  const dialog = page.getByRole("dialog", { name: "操作确认" });
  await expect(dialog).toBeVisible();
  const cancel = page.getByRole("button", { name: "取消并停止任务", exact: true });
  const bounds = await cancel.boundingBox();
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(510);
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(dialog).toContainText("正在发送");
  await expect(cancel).toBeEnabled();
  await cancel.click();
  await expect(dialog).toHaveCount(0);
  const stops = app.commands.filter((cmd) => cmd.type === "interrupt");
  expect(stops).toHaveLength(1);
  expect(stops[0].sid).toBe(SID);
  expect(app.commands.filter((cmd) => cmd.type === "answer_question" && cmd.outcome === "skip")).toHaveLength(0);
});

test("one right-hand stop remains after adding a pending message without interrupting", async ({ page }) => {
  const app=await openApp(page);app.start();
  await expect(page.getByRole('button',{name:'停止',exact:true})).toHaveCount(1);
  expect(await page.getByRole('button',{name:'停止任务',exact:true}).count()).toBe(0);
  const editor=page.locator('.composer textarea');await editor.fill('等当前任务结束再处理这段');
  await page.getByRole('button',{name:'发送',exact:true}).tap();
  await expect(page.getByRole('button',{name:'编辑待发送消息'})).toBeVisible();
  expect(app.commands.filter(cmd=>cmd.type==='interrupt'||cmd.type==='steer')).toHaveLength(0);
  expect(app.commands.filter(cmd=>cmd.type==='query')[0].delivery).toBe('queue');
  await page.getByRole('button',{name:'停止',exact:true}).tap();
  await expect.poll(()=>app.commands.filter(cmd=>cmd.type==='interrupt').length).toBe(1);
});

test("questions arriving during draft editing open and return a scoped answer", async ({ page }) => {
  const app=await openApp(page);
  app.acceptAnswers();
  const editor=page.locator('.composer textarea');
  await editor.fill('这段草稿在回答提问后继续编辑。');
  app.ask();
  const dialog=page.getByRole('dialog',{name:'操作确认'});
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button',{name:'继续',exact:true}).tap();
  await expect(dialog).toHaveCount(0);
  const answers=app.commands.filter(cmd=>cmd.type==='answer_question');
  expect(answers).toHaveLength(1);
  expect(answers[0]).toMatchObject({sid:SID,ask_id:'ask-fixture',answer:'继续'});
  await expect(editor).toHaveValue('这段草稿在回答提问后继续编辑。');
});

test("opt-in viewport probe reports geometry without draft text", async ({ page }) => {
  await openApp(page);
  await page.locator('.composer textarea').fill('这个私密草稿不能出现在诊断数据里');
  const request=page.waitForRequest(r=>r.url().endsWith('/api/client-viewport'));
  await page.evaluate(async()=>{
    history.replaceState(null,'',`${location.pathname}?viewport_probe=1`);
    const modulePath='/src/viewport-probe.ts';
    const module=await import(modulePath);
    module.startViewportProbe();
  });
  const payload=(await request).postDataJSON();
  expect(JSON.stringify(payload)).not.toContain('私密草稿');
  expect(Object.keys(payload)).toHaveLength(19);
  expect(payload.focused).toBe(true);
  expect(payload.composerBottom).toBeGreaterThan(payload.composerTop);
  expect(payload.visualHeight).toBeGreaterThan(0);
});

test("sending from an older reading position reveals the new message in the production app", async ({ page }) => {
  const app = await openApp(page);
  await page.locator(".thread").evaluate((element) => {
    element.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: -5000 }));
    element.scrollTop = 0;
  });
  await expect(page.getByRole("button", { name: /底部|最新/ })).toBeVisible();
  await page.locator(".composer textarea").fill("这是我刚刚发送的新指令");
  await page.getByRole("button", { name: "发送", exact: true }).tap();
  await expect.poll(() => app.commands.some((cmd) => cmd.type === "query" && cmd.prompt === "这是我刚刚发送的新指令")).toBe(true);
  await expect(page.locator(".thread").getByText("这是我刚刚发送的新指令", { exact: true })).toBeVisible();
  await expect.poll(() => page.locator(".thread").evaluate((element) =>
    element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThan(3);
});

test("mobile Enter inserts a newline and focused drafts grow within a bounded editor", async ({ page }) => {
  const app = await openApp(page);
  const editor = page.locator(".composer textarea");
  await editor.fill("第一行");
  await editor.press("End");
  await editor.press("Enter");
  await editor.press("x");
  await expect(editor).toHaveValue("第一行\nx");
  expect(app.commands.some((command) => command.type === "query")).toBe(false);
  await editor.fill("这是一段通过语音录入的长指令。".repeat(100));
  expect(await editor.evaluate(el => el.clientHeight)).toBeLessThanOrEqual(144);
  expect(await editor.evaluate(el => el.clientHeight)).toBeGreaterThan(60);
  await editor.blur();
  await expect(page.locator('.composer')).not.toHaveClass(/editor-focused/);
  expect(await page.locator(".composer .inrow").evaluate((element) => element.clientHeight)).toBeLessThan(70);
});

test("keyboard geometry places the composer and visible model controls at its top", async ({ page }) => {
  await page.addInitScript(() => {
    const viewport = new EventTarget();
    const reading = { height:844,width:390,offsetTop:0,offsetLeft:0,scale:1,pageTop:0,pageLeft:0 };
    for (const key of Object.keys(reading)) Object.defineProperty(viewport,key,{get:()=>reading[key as keyof typeof reading]});
    Object.defineProperty(window,"visualViewport",{value:viewport});
    Object.assign(window,{__keyboard:(height:number,offsetTop:number)=>{
      reading.height=height;reading.offsetTop=offsetTop;viewport.dispatchEvent(new Event("resize"));
    }});
  });
  const app = await openApp(page);
  app.emit({type:"model",model:"dsh:model"});
  app.emit({type:"effort",effort:"high"});
  const editor=page.locator('.composer textarea');
  await editor.tap();
  await editor.fill('根据上面的结果修改这段方案。\n'.repeat(20));
  await page.evaluate(()=>(window as unknown as {__keyboard(h:number,o:number):void}).__keyboard(440,18));
  await expect.poll(()=>page.locator('.composer').evaluate(el=>el.getBoundingClientRect().bottom)).toBe(458);
  await expect(page.locator('html')).toHaveAttribute('data-keyboard-open','true');
  const hint=page.locator('.composer .hint');
  expect(await hint.evaluate(el=>458-el.getBoundingClientRect().bottom)).toBeLessThanOrEqual(5);
  await expect(page.getByRole('button',{name:'选择模型',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'思考强度',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'选择模型',exact:true})).not.toContainText('读取中');
  await expect(page.getByRole('button',{name:'思考强度',exact:true})).not.toContainText('读取中');
  expect(await editor.evaluate(el=>el.clientHeight)).toBeLessThanOrEqual(124);
  expect(await page.locator('.thread').evaluate(el=>el.clientHeight)).toBeGreaterThan(100);
  await page.screenshot({path:`../work/mobile-keyboard-blue-${test.info().project.name}.png`,clip:{x:0,y:0,width:page.viewportSize()!.width,height:458}});
  await page.getByRole('button',{name:'选择模型',exact:true}).tap();
  await expect(page.getByRole('dialog',{name:'选择模型'})).toBeVisible();
});

test("home-screen launch and keyboard transitions show exactly one text layer", async ({ page }) => {
  // Emulate standalone detection and safe-area layout. The native iOS launch
  // itself cannot be driven by Playwright, so keep that distinction explicit.
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "standalone", { value: true });
    const match = window.matchMedia.bind(window);
    window.matchMedia = (query) => {
      const media = match(query);
      if (query === "(display-mode: standalone)") Object.defineProperty(media, "matches", { value: true });
      return media;
    };
  });
  await openApp(page);
  const editor = page.locator(".composer textarea");
  await expect(editor).toHaveAttribute("placeholder", "");
  await expect(page.locator(".composer-preview")).toHaveText("输入指令…");
  await editor.tap();
  await expect(page.locator(".composer-preview")).toBeHidden();
  await expect(editor).not.toHaveAttribute("placeholder", "");
  await editor.fill("语音第一段\n语音第二段");
  await page.setViewportSize({ width: 390, height: 510 });
  await editor.blur();
  await expect(page.locator('.composer')).not.toHaveClass(/editor-focused/);
  await expect(page.locator(".composer-preview")).toHaveText("语音第一段 语音第二段");
  expect(await editor.evaluate(el => getComputedStyle(el).getPropertyValue("-webkit-text-fill-color"))).toMatch(/transparent|rgba\(0, 0, 0, 0\)/);
  await editor.tap();
  await page.getByRole("button", { name: "展开输入区", exact: true }).tap();
  await expect(page.locator(".composer-preview")).toBeHidden();
  await expect(editor).toHaveValue("语音第一段\n语音第二段");
});

test("expanding preserves the native editor and selection while actions stay above the keyboard", async ({ page }) => {
  await openApp(page);
  const editor = page.locator(".composer textarea");
  await editor.fill("先看上下文，再修改中间的一句话。\n第二段保留原样。");
  await editor.evaluate((element) => {
    (element as HTMLTextAreaElement).setSelectionRange(4, 7);
    (window as unknown as { __editor: Element }).__editor = element;
  });
  await page.getByRole("button", { name: "展开输入区", exact: true }).tap();
  await expect(page.locator(".composer")).toHaveClass(/composer-expanded/);
  expect(await editor.evaluate((element) => element === (window as unknown as { __editor: Element }).__editor)).toBe(true);
  await expect.poll(() => editor.evaluate((element) => (element as HTMLTextAreaElement).selectionStart)).toBe(4);
  await page.setViewportSize({ width: 390, height: 510 });
  for (const name of ["发送", "打开会话"]) {
    const button = page.getByRole("button", { name, exact: true });
    await expect.poll(async () => {
      const box = await button.boundingBox();
      return box!.y + box!.height;
    }).toBeLessThanOrEqual(510);
  }
  const sendBox = await page.getByRole("button", { name: "发送", exact: true }).boundingBox();
  const sessionsBox = await page.getByRole("button", { name: "打开会话", exact: true }).boundingBox();
  expect(sessionsBox!.x).toBeLessThan(sendBox!.x);
  expect(sendBox!.x + sendBox!.width).toBeGreaterThan(390 - 25);
  await page.getByRole("button", { name: "收拢输入区", exact: true }).tap();
  await expect(editor).toHaveValue("先看上下文，再修改中间的一句话。\n第二段保留原样。");
});

test("IME commits its final text before one mobile send", async ({ page }) => {
  const app = await openApp(page);
  const editor = page.locator(".composer textarea");
  await editor.fill("临时输入");
  await editor.dispatchEvent("compositionstart");
  await editor.evaluate((element) => element.addEventListener("blur", () => {
    const textarea = element as HTMLTextAreaElement;
    textarea.value = "中文确认后的最终指令";
    textarea.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: textarea.value }));
  }, { once: true }));
  await page.getByRole("button", { name: "发送", exact: true }).tap();
  await expect.poll(() => app.commands.filter((command) => command.type === "query").map((command) => command.prompt)).toEqual(["中文确认后的最终指令"]);
});

test("background output keeps the reading position until an explicit send", async ({ page }) => {
  const app = await openApp(page);
  await page.locator(".thread").evaluate((element) => {
    element.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: -1500 }));
    element.scrollTop -= 1200;
  });
  await expect(page.getByRole("button", { name: "滚动到底部" })).toBeVisible();
  const before = await page.locator(".thread").evaluate((element) => element.scrollTop);
  app.emit({ type: "user_msg", msg_id: "external-turn", prompt: "后台新消息" });
  app.emit({ type: "delta", message_id: "external-text", channel: "final", text: "后台正在生成的新内容。" });
  await page.waitForTimeout(180);
  await expect.poll(async () => Math.abs(await page.locator(".thread").evaluate((element) => element.scrollTop) - before)).toBeLessThan(2);
});

test("right edge swipe opens the activity list and right swipe inside closes it", async ({ page }) => {
  await openApp(page);
  const gesture = async (selector: string, startX: number, endX: number) => {
    await page.locator(selector).evaluate((element, { startX, endX }) => {
      for (const [type, x] of [["touchstart",startX],["touchmove",endX],["touchend",endX]] as const) {
        const point = { identifier: 1, clientX: x, clientY: 240 };
        const event = new Event(type, { bubbles: true, cancelable: true });
        Object.defineProperties(event, { touches: { value: type === "touchend" ? [] : [point] }, changedTouches: { value: [point] } });
        element.dispatchEvent(event);
      }
    }, { startX, endX });
  };
  await gesture(".thread", 390, 280);
  await expect(page.locator(".sessions")).toHaveClass(/show/);
  await expect(page.locator(".scard").last()).toContainText("另一目录的正在运行任务");
  await gesture(".scard >> nth=0", 180, 280);
  await expect(page.locator(".sessions")).not.toHaveClass(/show/);
});

test("pending cards edit, remove and explicitly stop-and-start with the same message id", async ({page})=>{
  const app=await openApp(page);app.start();const input=page.locator('.composer textarea');
  await input.fill('下一条待发送');await page.getByRole('button',{name:'发送',exact:true}).tap();
  expect(await page.locator('.thread').getByText('下一条待发送',{exact:true}).count()).toBe(0);
  await page.getByRole('button',{name:'编辑待发送消息'}).tap();
  const dialog=page.getByRole('dialog',{name:'待发送消息',exact:true});
  const edit=dialog.getByRole('textbox',{name:'编辑排队消息'});await edit.fill('改好的待发送消息');
  await dialog.getByRole('button',{name:'保存修改',exact:true}).tap();
  await expect(page.getByRole('button',{name:'编辑待发送消息'})).toContainText('改好的待发送消息');
  await expect(dialog.getByRole('button',{name:'引导当前任务',exact:true})).toHaveCount(0);
  expect(app.commands.filter(cmd=>cmd.type==='update_queued_query'&&cmd.start_now===true)).toHaveLength(0);
  await expect(dialog).toHaveCount(0);
  const chip=page.getByRole('button',{name:'编辑待发送消息'});
  await chip.dispatchEvent('pointerdown',{button:0,clientX:100,clientY:100});
  await page.waitForTimeout(550);await chip.dispatchEvent('pointerup');
  await dialog.getByRole('button',{name:'引导当前任务',exact:true}).tap();
  await expect(dialog).toHaveCount(0);
  const starts=app.commands.filter(cmd=>cmd.type==='update_queued_query'&&cmd.start_now===true);
  expect(starts).toHaveLength(1);expect(starts[0].msg_id).toBe(app.commands.find(cmd=>cmd.type==='query')!.msg_id);
  expect(app.commands.some(cmd=>cmd.type==='steer')).toBe(false);
  app.finish();await expect(page.locator('.thread').getByText('改好的待发送消息',{exact:true})).toBeVisible();
  await input.fill('这条要删除');await page.getByRole('button',{name:'发送',exact:true}).tap();
  await page.getByRole('button',{name:'编辑待发送消息'}).tap();
  await dialog.getByRole('button',{name:'移除',exact:true}).tap();
  await expect(page.getByRole('button',{name:'编辑待发送消息'})).toHaveCount(0);
});

test("editor expansion is in the upper right and helper/mode buttons are absent",async({page})=>{
  await openApp(page);await page.locator('.composer textarea').fill('需要展开编辑的草稿');
  const box=await page.getByRole('button',{name:'展开输入区',exact:true}).boundingBox();
  const input=await page.locator('.composer textarea').boundingBox();
  expect(box!.x).toBeGreaterThan(300);expect(box!.y).toBeLessThan(input!.y);
  for(const label of ['找词定位','输入设置','引导','排队'])expect(await page.getByRole('button',{name:label,exact:true}).count()).toBe(0);
});

test("desktop keeps Enter send and Shift Enter newline", async ({ page }) => {
  await page.setViewportSize({width:1280,height:800});
  const app=await openApp(page);
  const editor=page.locator(".composer textarea");
  await editor.fill("第一行");await editor.press("End");await editor.press("Shift+Enter");await editor.press("x");
  await expect(editor).toHaveValue("第一行\nx");
  await editor.press("Enter");
  await expect.poll(()=>app.commands.filter(command=>command.type==="query").map(command=>command.prompt)).toEqual(["第一行\nx"]);
});

test("switching sessions preserves each draft and releases the old editor focus", async ({ page }) => {
  await openApp(page);
  const editor=page.locator(".composer textarea");
  await editor.fill("当前会话尚未发送的草稿");
  await page.getByRole("button", {name:"打开会话",exact:true}).tap();
  await page.locator('[data-session-id="dsh@other"]').tap();
  await expect(editor).toHaveValue("");
  expect(await editor.evaluate(element=>element===document.activeElement)).toBe(false);
  await page.getByRole("button", {name:"打开会话",exact:true}).tap();
  await page.locator(`[data-session-id="${SID}"]`).tap();
  await expect(editor).toHaveValue("当前会话尚未发送的草稿");
});

test("production composer appearance stays compact and circular", async ({ page }) => {
  await openApp(page);
  await page.evaluate(()=>document.documentElement.dataset.theme="dark");
  const editor=page.locator(".composer textarea");
  await editor.fill("口述时继续看回复，修改时再展开输入区。");
  await editor.blur();
  await expect(page.locator('.composer')).not.toHaveClass(/editor-focused/);
  const geometry=await page.locator(".composer .inrow").evaluate(element=>({
    height:element.clientHeight,radius:parseFloat(getComputedStyle(element).borderRadius),
    sendRadius:parseFloat(getComputedStyle(element.querySelector(".sendbtn")!).borderRadius),
  }));
  expect(geometry.height).toBeLessThan(70);
  expect(geometry.radius).toBeGreaterThanOrEqual(geometry.height/2);
  expect(geometry.sendRadius).toBeGreaterThanOrEqual(20);
  await page.screenshot({path:`../work/mobile-composer-${test.info().project.name}.png`});
  await editor.tap();
  await page.getByRole("button", {name:"展开输入区",exact:true}).tap();
  await page.screenshot({path:`../work/mobile-editor-${test.info().project.name}.png`});
});


test("project file browser navigates, searches and opens contents without a diff", async ({page}) => {
  const app = await openApp(page);
  await expect(page.locator("button.surface-head-title")).toHaveCount(0);
  await page.getByRole("button",{name:"更多设置",exact:true}).tap();
  await page.getByRole("button",{name:/项目文件/}).tap();
  const browser = page.getByRole("dialog",{name:"项目文件",exact:true});
  await expect(browser).toBeVisible();
  await browser.getByRole("button",{name:"src",exact:true}).tap();
  await expect(browser.getByRole("button",{name:"app.py",exact:true})).toBeVisible();
  await browser.getByRole("button",{name:"返回上级",exact:true}).tap();
  await browser.getByRole("textbox",{name:"搜索项目文件"}).fill("app");
  await expect(browser.getByRole("button",{name:/app.py/})).toBeVisible();
  await browser.getByRole("button",{name:/app.py/}).tap();
  await expect(browser).toHaveCount(0);
  await expect(page.locator(".artifact-panel")).toContainText("preview works");
  const close = page.getByRole('button', {name:'关闭文件预览', exact:true});
  await expect(close.locator('svg')).not.toHaveCount(0);
  await close.tap();
  await expect(page.locator('.artifact-panel')).toHaveCount(0);
  expect(app.commands.filter(c=>c.type === "browse_files").at(-1)?.query).toBe("app");
  expect(app.commands.filter(c=>c.type === "get_file_preview").at(-1)?.path).toBe("src/app.py");
});

test("changed source file chip opens contents when native diff is absent", async ({page}) => {
  const app = await openApp(page);
  app.emit({type:"user_msg",msg_id:"file-turn",prompt:"更新代码"});
  app.emit({type:"tool_use",message_id:"file-message",tool_use_id:"write-1",tool:"write",input:{path:"src/app.py",content:"updated"}});
  app.emit({type:"tool_result",tool_use_id:"write-1",content:"done",is_error:false});
  app.emit({type:"turn_end",result:"completed"});
  await page.getByRole("button",{name:/改动 1 个文件/}).tap();
  const chip=page.getByRole("dialog",{name:"本轮改动文件"}).getByRole("button",{name:/app.py/});
  await expect(chip).toBeEnabled();
  await chip.tap();
  await expect(page.locator(".artifact-panel")).toContainText("preview works");
  const close = page.getByRole('button', {name:'关闭文件预览', exact:true});
  await expect(close.locator('svg')).not.toHaveCount(0);
  await close.tap();
  await expect(page.locator('.artifact-panel')).toHaveCount(0);
  expect(app.commands.filter(c=>c.type === "get_file_preview").at(-1)?.path).toBe("src/app.py");
});


test("new chat inherits cwd and retains focus across delayed creation, rekey and lagging catalogs", async ({page}) => {
  const app=await openApp(page);
  app.delayCreate();
  await page.getByRole("button",{name:"打开会话",exact:true}).tap();
  await page.getByRole("button",{name:"新会话",exact:true}).tap();
  await expect(page.locator(".newchat-cwd")).toContainText("/repo/sample-project");
  await page.locator(".newchat-input").fill("这条消息属于新会话");
  await page.getByRole("button",{name:"开始",exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type === "new_session").length).toBe(1);
  const creation=app.commands.find(c=>c.type === "new_session")!;
  expect(creation.cwd).toBe("/repo/sample-project");
  app.refreshRoster();
  await expect(page.locator(".newchat-input")).toBeVisible();
  app.completeCreate();
  await expect(page.locator(".thread")).toContainText("这条消息属于新会话");
  app.rekeyCreated();
  app.refreshRoster();
  await expect(page.locator(".thread")).toContainText("这条消息属于新会话");
  await expect(page.locator(".newchat-input")).toHaveCount(0);
  await expect(page.locator(".hstat")).toContainText("running");
  await page.locator(".composer textarea").fill("继续这个新会话");
  await page.getByRole("button",{name:"发送",exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type === "query").at(-1)?.sid).toBe("dsh@created-fixture");
  app.refreshRoster(true);
  await expect(page.locator(".surface-head-title")).toContainText("刚创建的对话");
});

test("all visual themes preview instantly, support dark mode and persist after reload", async ({page}) => {
  await openApp(page);
  await page.getByRole("button",{name:"更多设置",exact:true}).tap();
  await page.getByRole("button",{name:/外观与主题/}).tap();
  for(const [name,id] of [["紫青极光","aurora"],["像素游戏机","pixel"],["暖纸","paper"],["DeepSeek 蓝","blue"]]) {
    await page.getByRole("radio",{name,exact:true}).tap();
    await expect(page.locator("html")).toHaveAttribute("data-appearance",id);
    await expect(page.getByRole("radio",{name,exact:true})).toHaveAttribute("aria-checked","true");
    await page.locator(".header-menu-scrim").click({position:{x:2,y:2}});
    await page.screenshot({path:`../work/theme-${id}-${test.info().project.name}.png`});
    await page.getByRole("button",{name:"更多设置",exact:true}).tap();
    await page.getByRole("button",{name:/外观与主题/}).tap();
  }
  await page.getByRole("radio",{name:"像素游戏机",exact:true}).tap();
  await page.getByRole("button",{name:"深色",exact:true}).tap();
  await expect(page.locator("html")).toHaveAttribute("data-theme","dark");
  expect(await page.locator(".composer .inrow").evaluate(el=>getComputedStyle(el).borderRadius)).toBe("10px");
  await page.reload();
  await expect(page.locator(".composer textarea")).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-appearance","pixel");
  await expect(page.locator("html")).toHaveAttribute("data-theme","dark");
  await page.evaluate(() => {
    Object.defineProperty(window.visualViewport!, "height", {value:300, configurable:true});
    window.visualViewport!.dispatchEvent(new Event("resize"));
  });
  await expect.poll(()=>page.locator(".composer").evaluate(el=>el.getBoundingClientRect().bottom)).toBe(300);
  await page.getByRole("button",{name:"更多设置",exact:true}).tap();
  await page.getByRole("button",{name:/外观与主题/}).tap();
  const bounds=await page.locator(".header-menu-card").boundingBox();
  expect(bounds!.y+bounds!.height).toBeLessThanOrEqual(300);
  await page.getByRole("radio",{name:"暖纸",exact:true}).tap();
  await expect(page.locator("html")).toHaveAttribute("data-appearance","paper");
});

test("editor focus and retargeted touch clicks cannot open metadata pickers", async ({page}) => {
  const app=await openApp(page);
  app.emit({type:"model",model:"dsh:model"});
  app.emit({type:"effort",effort:"high"});
  const editor=page.locator(".composer textarea");
  for(const name of ["选择模型","思考强度","点击切换权限模式","上下文占用"]) {
    await editor.dispatchEvent("pointerdown",{pointerId:1,pointerType:"touch",isPrimary:true});
    await editor.focus();
    await editor.fill("正在编辑的草稿");
    // iOS keyboard/focus movement can produce a trailing click on a newly
    // positioned control. The gesture began in the editor, not on that control.
    const control=name === "点击切换权限模式"
      ? page.locator(".composer .hint-mode") : page.getByRole("button",{name,exact:true});
    await control.dispatchEvent("click",{detail:1});
    await expect(page.locator(".sheet.show,.composer .ctx-pop")).toHaveCount(0);
    await expect(editor).toHaveValue("正在编辑的草稿");
  }
  await page.getByRole("button",{name:"选择模型",exact:true}).tap();
  await expect(page.locator(".sheet.show")).toHaveAttribute("aria-label","选择模型");
  await page.locator(".scrim.show").tap({position:{x:5,y:5}});
  // Keyboard/assistive activation does not require a pointerdown.
  const effort=page.getByRole("button",{name:"思考强度",exact:true});
  await effort.focus();await effort.press("Enter");
  await expect(page.locator(".sheet.show")).toHaveAttribute("aria-label","选择思考强度");
  expect(app.commands.some(c=>["set_model","set_effort","set_perm","query","interrupt"].includes(c.type as string))).toBe(false);
});

test("closed model panel cannot paint above a keyboard with a taller layout viewport", async ({page}) => {
  await openApp(page);
  // Real iPhone readings: layout viewport 793, visual viewport 378. A fixed
  // closed sheet can otherwise be shifted upward by the keyboard inset.
  await page.evaluate(()=>Object.defineProperty(document.documentElement,"clientHeight",{value:793,configurable:true}));
  await page.setViewportSize({width:393,height:378});
  await page.locator(".composer textarea").focus();
  await expect(page.locator("html")).toHaveAttribute("data-keyboard-open","true");
  await expect(page.getByRole("dialog",{name:"选择模型",exact:true})).not.toBeInViewport();
  await expect(page.locator(".sheet")).toHaveCount(0);
  await page.getByRole("button",{name:"选择模型",exact:true}).tap();
  const panel=page.locator(".sheet.show");
  await expect(panel).toBeInViewport();
  await expect.poll(()=>panel.evaluate(el=>el.getBoundingClientRect().bottom)).toBeLessThanOrEqual(378);
  const bounds=await panel.boundingBox();
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.y+bounds!.height).toBeLessThanOrEqual(378);
  await page.locator(".scrim.show").tap({position:{x:5,y:5}});
  await expect(page.locator(".sheet")).toHaveCount(0);
});

test("new chat never reveals a closed picker when its keyboard changes viewport geometry", async ({page}) => {
  await openApp(page);
  await page.getByRole("button",{name:"打开会话",exact:true}).tap();
  await page.getByRole("button",{name:"新会话",exact:true}).tap();
  await page.evaluate(()=>Object.defineProperty(document.documentElement,"clientHeight",{value:793,configurable:true}));
  await page.setViewportSize({width:393,height:378});
  const editor=page.locator(".newchat-input");
  await editor.focus();
  await editor.fill("新对话的草稿");
  await expect(page.locator("html")).toHaveAttribute("data-keyboard-open","true");
  await expect(page.locator(".sheet,.scrim.show")).toHaveCount(0);
  await expect(editor).toHaveValue("新对话的草稿");
  await page.getByRole("button",{name:"选择模型",exact:true}).tap();
  const panel=page.getByRole("dialog",{name:"选择模型",exact:true});
  await expect(panel).toBeInViewport();
  await expect.poll(()=>panel.evaluate(el=>el.getBoundingClientRect().bottom)).toBeLessThanOrEqual(378);
  const bounds=await panel.boundingBox();
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  await page.locator(".picker-scrim").tap({position:{x:5,y:5}});
  await expect(page.locator(".sheet")).toHaveCount(0);
  await expect(editor).toHaveValue("新对话的草稿");
});

test("new chat rejects editor clicks retargeted onto metadata but allows deliberate touch and keyboard activation", async ({page}) => {
  const app=await openApp(page);
  await page.getByRole("button",{name:"打开会话",exact:true}).tap();
  await page.getByRole("button",{name:"新会话",exact:true}).tap();
  const editor=page.locator(".newchat-input");
  for(const name of ["选择模型","选择思考强度"]) {
    await editor.dispatchEvent("pointerdown",{pointerId:1,pointerType:"touch",isPrimary:true});
    await editor.focus();
    await editor.fill("保留这份草稿");
    const control=page.getByRole("button",{name,exact:true});
    await control.dispatchEvent("click",{detail:1});
    await expect(page.locator(".sheet")).toHaveCount(0);
    await control.tap();
    await expect(page.getByRole("dialog",{name,exact:true})).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator(".sheet")).toHaveCount(0);
    await expect(editor).toHaveValue("保留这份草稿");
  }
  const control=page.getByRole("button",{name:"选择模型",exact:true});
  await control.focus();await control.press("Enter");
  const picker=page.getByRole("dialog",{name:"选择模型",exact:true});
  await expect(picker).toBeVisible();
  await picker.getByRole("button",{name:"DeepSeek",exact:true}).tap();
  await expect(picker).toHaveCount(0);
  await expect(editor).toHaveValue("保留这份草稿");
  expect(app.commands.some(c=>["new_session","query","interrupt"].includes(c.type as string))).toBe(false);
});

test("settings dialogs dismiss outside or on Escape without applying an action", async ({page}) => {
  const app=await openApp(page);
  app.emit({type:"perm",mode:"full",options:[{id:"full",name:"Full Access",description:"测试权限",danger:true}]});
  for(const name of ["选择模型","思考强度","点击切换权限模式"]) {
    const control=name === "点击切换权限模式" ? page.locator(".composer .hint-mode") : page.getByRole("button",{name,exact:true});
    await control.tap();
    await expect(page.locator(".sheet.show")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator(".sheet")).toHaveCount(0);
  }
  await page.getByRole("button",{name:"上下文占用",exact:true}).tap();
  await expect(page.locator(".ctx-pop")).toBeVisible();
  await page.locator(".thread").tap({position:{x:5,y:5}});
  await expect(page.locator(".ctx-pop")).toHaveCount(0);
  await page.getByRole("button",{name:"上下文占用",exact:true}).tap();
  await page.keyboard.press("Escape");
  await expect(page.locator(".ctx-pop")).toHaveCount(0);
  expect(lockRequests).toEqual([]);
  expect(app.commands.some(c=>["set_model","set_effort","set_perm","query","interrupt"].includes(c.type as string))).toBe(false);
});

const DESIGN_LAYOUTS=[
  ["原生精简","native","native"],
  ["Terminal 工作台","terminal","terminal"],
  ["工程对话","engineering","engineering"],
  ["像素工作台","pixel-workbench","pixel"],
] as const;

async function selectDesign(page: Page,name: string) {
  await page.getByRole("button",{name:"更多设置",exact:true}).tap();
  await page.getByRole("button",{name:/外观与主题/}).tap();
  await page.getByRole("radio",{name,exact:true}).tap();
  await page.locator(".header-menu-scrim").tap({position:{x:2,y:2}});
}

for(const [name,id,layout] of DESIGN_LAYOUTS) {
  test(`design layout ${id} preserves the editor and exposes real settings, sidebar and keyboard actions`,async ({page})=>{
    const app=await openApp(page);
    app.emit({type:"model",model:"dsh:model"});
    app.emit({type:"effort",effort:"high"});
    app.emit({type:"perm",mode:"full",options:[{id:"full",name:"Full Access",danger:true}]});
    const editor=page.locator(".composer textarea");
    await editor.fill("保留这段语音草稿\n第二段需要继续编辑");
    await editor.evaluate(el=>{el.dataset.editorIdentity="original";});
    await selectDesign(page,name);
    await expect(page.locator("html")).toHaveAttribute("data-layout",layout);
    await expect(editor).toHaveAttribute("data-editor-identity","original");
    await expect(editor).toHaveValue("保留这段语音草稿\n第二段需要继续编辑");
    await expect(page.locator(".composer .hint")).toHaveCount(0);
    const bubble=page.locator(".ubub").last();
    if(layout === "terminal") {
      expect(await bubble.evaluate(el=>getComputedStyle(el).borderLeftWidth)).toBe("2px");
      expect(await bubble.evaluate(el=>getComputedStyle(el).borderRadius)).toBe("0px");
    } else if(layout === "pixel") {
      expect(await bubble.evaluate(el=>getComputedStyle(el).borderRadius)).toBe("2px");
      expect(await bubble.evaluate(el=>getComputedStyle(el).borderTopWidth)).toBe("1px");
    } else if(layout === "engineering") {
      expect(await bubble.evaluate(el=>getComputedStyle(el).borderRadius)).toBe("17px");
    }
    await expect(page.locator(".sheet")).toHaveCount(0);
    await page.screenshot({path:`../work/layout-${id}-light-${test.info().project.name}.png`});
    await page.getByRole("button",{name:"更多设置",exact:true}).tap();
    await page.getByRole("button",{name:/外观与主题/}).tap();
    await page.getByRole("button",{name:"深色",exact:true}).tap();
    await page.locator(".header-menu-scrim").tap({position:{x:2,y:2}});
    await page.screenshot({path:`../work/layout-${id}-dark-${test.info().project.name}.png`});
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-appearance",id);
    await expect(page.locator("html")).toHaveAttribute("data-layout",layout);
    await expect(page.locator("html")).toHaveAttribute("data-theme","dark");
    await expect(editor).toBeEnabled();
    app.emit({type:"model",model:"dsh:model"});
    app.emit({type:"effort",effort:"high"});
    app.emit({type:"perm",mode:"full",options:[{id:"full",name:"Full Access",danger:true}]});
    await page.evaluate(()=>Object.defineProperty(document.documentElement,"clientHeight",{value:793,configurable:true}));
    await page.setViewportSize({width:393,height:378});
    await editor.focus();await editor.fill("短草稿");
    await expect(page.locator("html")).toHaveAttribute("data-keyboard-open","true");
    await expect(page.locator(".sheet")).toHaveCount(0);
    await expect.poll(()=>page.locator(".composer").evaluate(el=>el.getBoundingClientRect().height)).toBeLessThanOrEqual(110);
    await expect.poll(()=>page.locator(".composer").evaluate(el=>el.getBoundingClientRect().bottom)).toBe(378);
    await editor.fill(Array.from({length:12},(_,i)=>`长草稿段落 ${i}`).join("\n"));
    await expect.poll(()=>editor.evaluate(el=>el.getBoundingClientRect().height)).toBeLessThanOrEqual(120);
    await page.getByRole("button",{name:"展开输入区",exact:true}).tap();
    await expect(page.getByRole("button",{name:"收拢输入区",exact:true})).toBeInViewport();
    await page.screenshot({path:`../work/layout-${id}-editor-${test.info().project.name}.png`});
    await page.getByRole("button",{name:"收拢输入区",exact:true}).tap();
    await page.getByRole("button",{name:"会话配置",exact:true}).tap();
    const config=page.getByRole("dialog",{name:"会话配置",exact:true});
    await expect(config).toBeVisible();
    await expect(config.getByRole("button",{name:"点击切换权限模式",exact:true})).toBeEnabled();
    await config.getByRole("button",{name:"选择模型",exact:true}).tap();
    await expect(config).toHaveCount(0);
    const models=page.getByRole("dialog",{name:"选择模型",exact:true});
    await models.getByRole("button",{name:"DeepSeek",exact:true}).tap();
    await expect(models).toHaveCount(0);
    await expect.poll(()=>app.commands.filter(c=>c.type === "set_model").at(-1)?.sid).toBe(SID);
    await page.getByRole("button",{name:"打开会话",exact:true}).tap();
    await expect(page.locator(".sessions.show .scard.active .scard-activity")).toBeVisible();
    await expect.poll(()=>page.locator(".sessions.show").evaluate(el=>el.getBoundingClientRect().left)).toBeLessThanOrEqual(40);
    const activeRow=page.locator(".sessions.show .scard.active");
    const timeBounds=await activeRow.locator(".scard-activity").boundingBox();
    const actionBounds=await activeRow.getByRole("button",{name:"更多操作",exact:true}).boundingBox();
    expect(timeBounds!.x+timeBounds!.width).toBeLessThanOrEqual(actionBounds!.x);
    await page.screenshot({path:`../work/layout-${id}-sidebar-${test.info().project.name}.png`});
    await page.getByRole("button",{name:"收起",exact:true}).tap();
    await editor.fill("新界面发送检查");
    await page.getByRole("button",{name:"发送",exact:true}).tap();
    await expect.poll(()=>app.commands.filter(c=>c.type === "query").at(-1)?.prompt).toBe("新界面发送检查");
    await expect(page.locator(".thread")).toContainText("新界面发送检查");
    expect(app.commands.some(c=>c.type === "interrupt")).toBe(false);
  });

  test(`design new chat ${id} retains cwd, default settings and draft through picker and creation`,async ({page})=>{
    const app=await openApp(page);
    await selectDesign(page,name);
    await page.getByRole("button",{name:"打开会话",exact:true}).tap();
    await page.getByRole("button",{name:"新会话",exact:true}).tap();
    const editor=page.locator(".newchat-input");
    await expect(page.locator(".newchat-cwd")).toContainText("/repo/sample-project");
    await page.evaluate(()=>Object.defineProperty(document.documentElement,"clientHeight",{value:793,configurable:true}));
    await page.setViewportSize({width:393,height:378});
    await editor.focus();await editor.fill("新的布局，新会话草稿");
    await expect(page.locator(".sheet")).toHaveCount(0);
    await expect.poll(()=>page.locator(".newchat-editor").evaluate(el=>el.getBoundingClientRect().bottom)).toBeLessThanOrEqual(378);
    await page.getByRole("button",{name:"会话配置",exact:true}).tap();
    const config=page.getByRole("dialog",{name:"会话配置",exact:true});
    await config.getByRole("button",{name:"选择模型",exact:true}).tap();
    const models=page.getByRole("dialog",{name:"选择模型",exact:true});
    await expect(models).toBeInViewport();
    await models.getByRole("button",{name:/^本机默认/}).tap();
    await expect(page.locator(".sheet")).toHaveCount(0);
    await expect(editor).toHaveValue("新的布局，新会话草稿");
    await page.getByRole("button",{name:"展开输入区",exact:true}).tap();
    await expect(page.getByRole("button",{name:"收拢输入区",exact:true})).toBeInViewport();
    await page.getByRole("button",{name:"收拢输入区",exact:true}).tap();
    await page.screenshot({path:`../work/layout-${id}-newchat-${test.info().project.name}.png`});
    await page.getByRole("button",{name:"开始",exact:true}).tap();
    await expect(page.locator(".thread")).toContainText("新的布局，新会话草稿");
    const creation=app.commands.find(c=>c.type === "new_session")!;
    expect(creation.cwd).toBe("/repo/sample-project");
    expect(creation.model ?? null).toBe(null);
    expect(app.commands.some(c=>c.type === "interrupt")).toBe(false);
  });
}

test("desktop keeps the same editor, settings and Enter sending when switching designed layouts",async ({page})=>{
  const app=await openApp(page);
  const editor=page.locator(".composer textarea");
  await editor.fill("桌面端草稿");
  await editor.evaluate(el=>{el.dataset.editorIdentity="desktop-original";});
  for(const [name,,layout] of DESIGN_LAYOUTS) {
    await page.getByRole("button",{name:"更多设置",exact:true}).click();
    await page.getByRole("button",{name:/外观与主题/}).click();
    await page.getByRole("radio",{name,exact:true}).click();
    await page.locator(".header-menu-scrim").click({position:{x:2,y:2}});
    await expect(page.locator("html")).toHaveAttribute("data-layout",layout);
    await expect(editor).toHaveAttribute("data-editor-identity","desktop-original");
    await expect(editor).toHaveValue("桌面端草稿");
    await page.getByRole("button",{name:"会话配置",exact:true}).click();
    const config=page.getByRole("dialog",{name:"会话配置",exact:true});
    await expect(config).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(config).toHaveCount(0);
    await expect(page.getByRole("button",{name:"打开会话",exact:true})).toBeVisible();
  }
  await editor.press("Enter");
  await expect.poll(()=>app.commands.filter(c=>c.type === "query").at(-1)?.prompt).toBe("桌面端草稿");
});

async function stubClipboard(page: Page,text: string,mode:"ready"|"reject"|"pending"="ready") {
  await page.evaluate(({text,mode})=>{
    const state=window as unknown as {clipboardReads:number;clipboardActivation:boolean|null;resolveClipboard?:(text:string)=>void};
    state.clipboardReads=0;
    Object.defineProperty(navigator,"clipboard",{configurable:true,value:{readText:()=>{
      state.clipboardReads++;state.clipboardActivation=navigator.userActivation?.isActive ?? null;
      if(mode==="reject")return Promise.reject(new DOMException("Denied","NotAllowedError"));
      if(mode==="pending")return new Promise<string>(resolve=>{state.resolveClipboard=resolve;});
      return Promise.resolve(text);
    }}});
  },{text,mode});
}
async function openDraftHistory(page: Page) {
  await page.getByRole("button",{name:"输入工具",exact:true}).tap();
  await page.getByRole("button",{name:"草稿历史",exact:true}).tap();
  return page.getByRole("dialog",{name:"草稿历史",exact:true});
}

test("clipboard empty-input action reads on demand and sends exactly once without a business confirmation",async({page})=>{
  const app=await openApp(page);
  const text="来自剪贴板的 prompt\n保留第二段";
  await stubClipboard(page,text,"pending");
  const dialogs:string[]=[];page.on("dialog",dialog=>{dialogs.push(dialog.type());void dialog.dismiss();});
  expect(await page.evaluate(()=>(window as unknown as {clipboardReads:number}).clipboardReads)).toBe(0);
  const paste=page.getByRole("button",{name:"粘贴并发送",exact:true});
  await paste.tap();
  await page.locator(".sendbtn").dispatchEvent("click");
  expect(await page.evaluate(()=>(window as unknown as {clipboardReads:number}).clipboardReads)).toBe(1);
  await page.evaluate(text=>(window as unknown as {resolveClipboard:(text:string)=>void}).resolveClipboard(text),text);
  await expect.poll(()=>app.commands.filter(c=>c.type==="query").length).toBe(1);
  expect(app.commands.find(c=>c.type==="query")?.prompt).toBe(text);
  expect(app.commands.find(c=>c.type==="query")?.sid).toBe(SID);
  expect(await page.evaluate(()=>(window as unknown as {clipboardActivation:boolean|null}).clipboardActivation)).not.toBe(false);
  expect(dialogs).toEqual([]);
  await expect(page.getByRole("button",{name:"停止",exact:true})).toBeEnabled();
  await expect(page.locator(".composer textarea")).toHaveValue("");
  app.finish();
  const history=await openDraftHistory(page);
  await expect(history).toContainText("来自剪贴板的 prompt");
});

test("clipboard keeps stop as the running empty-input action and offers explicit paste-and-queue in tools",async({page})=>{
  const app=await openApp(page);app.start();
  await stubClipboard(page,"等当前任务完成后处理");
  await expect(page.getByRole("button",{name:"停止",exact:true})).toBeEnabled();
  await expect(page.getByRole("button",{name:"粘贴并发送",exact:true})).toHaveCount(0);
  await page.getByRole("button",{name:"输入工具",exact:true}).tap();
  await page.getByRole("button",{name:"粘贴并排队",exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==="query" && c.delivery==="queue").length).toBe(1);
  expect(app.commands.some(c=>c.type==="interrupt")).toBe(false);
  await expect(page.locator(".qchip")).toContainText("等当前任务完成后处理");
  await page.getByRole("button",{name:"停止",exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==="interrupt").length).toBe(1);
});

test("clipboard rejection and empty contents leave the editor intact and send no commands",async({page})=>{
  const app=await openApp(page);
  await stubClipboard(page,"","reject");
  await page.getByRole("button",{name:"粘贴并发送",exact:true}).tap();
  await expect(page.locator(".composer-notice")).toContainText("未能读取剪贴板");
  await expect(page.locator(".composer textarea")).toHaveValue("");
  await stubClipboard(page,"   ");
  await page.getByRole("button",{name:"粘贴并发送",exact:true}).tap();
  await expect(page.locator(".composer-notice")).toContainText("没有可发送的文字");
  expect(app.commands.some(c=>["query","interrupt","new_session"].includes(c.type as string))).toBe(false);
});

test("clipboard permission replies cannot replace newly typed text and can be recovered with confirmation",async({page})=>{
  const app=await openApp(page);await selectDesign(page,"工程对话");
  await stubClipboard(page,"","pending");
  await page.getByRole("button",{name:"粘贴并发送",exact:true}).tap();
  const editor=page.locator(".composer textarea");await editor.fill("期间新写的草稿");
  await page.evaluate(()=>(window as unknown as {resolveClipboard:(text:string)=>void}).resolveClipboard("迟到的剪贴板内容"));
  await expect(page.locator(".composer-notice")).toContainText("未发送");
  await expect(editor).toHaveValue("期间新写的草稿");
  expect(app.commands.some(c=>c.type==="query")).toBe(false);
  const history=await openDraftHistory(page);
  await history.getByRole("button",{name:/迟到的剪贴板内容/}).tap();
  await page.getByRole("button",{name:"恢复到输入框",exact:true}).tap();
  const confirmation=page.getByRole("dialog",{name:"替换当前草稿？",exact:true});
  await expect(confirmation).toBeVisible();await expect(editor).toHaveValue("期间新写的草稿");
  await confirmation.getByRole("button",{name:"确认恢复",exact:true}).tap();
  await expect(editor).toHaveValue("迟到的剪贴板内容");
  expect(app.commands.some(c=>c.type==="query")).toBe(false);
});

test("clipboard permission replies cannot send to a newly selected conversation",async({page})=>{
  const app=await openApp(page);await stubClipboard(page,"","pending");
  await page.getByRole("button",{name:"粘贴并发送",exact:true}).tap();
  await page.getByRole("button",{name:"打开会话",exact:true}).tap();
  await page.locator(".scard").filter({hasText:"另一目录的正在运行任务"}).tap();
  await expect(page.locator(".surface-head-title")).toHaveText("另一目录的正在运行任务");
  await page.evaluate(()=>(window as unknown as {resolveClipboard:(text:string)=>void}).resolveClipboard("属于原会话的剪贴板"));
  await expect(page.locator(".composer-notice")).toContainText("未发送");
  expect(app.commands.some(c=>["query","interrupt"].includes(c.type as string))).toBe(false);
  const history=await openDraftHistory(page);await expect(history).toContainText("属于原会话的剪贴板");
});

test("clipboard first-message action inherits the cwd and remains on the new conversation",async({page})=>{
  const app=await openApp(page);await selectDesign(page,"原生精简");
  await page.getByRole("button",{name:"打开会话",exact:true}).tap();
  await page.getByRole("button",{name:"新会话",exact:true}).tap();
  await stubClipboard(page,"剪贴板直接开始新会话");
  await page.getByRole("button",{name:"粘贴并发送",exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==="new_session").length).toBe(1);
  const cmd=app.commands.find(c=>c.type==="new_session")!;
  expect(cmd.cwd).toBe("/repo/sample-project");expect(cmd.prompt).toBe("剪贴板直接开始新会话");
  await expect(page.locator(".thread")).toContainText("剪贴板直接开始新会话");
  await expect(page.locator(".newchat-input")).toHaveCount(0);
  await expect(page.locator(".composer textarea")).toHaveValue("");
  const history=await openDraftHistory(page);
  await history.getByRole("button",{name:"当前会话",exact:true}).tap();
  await expect(history).toContainText("剪贴板直接开始新会话");
});

test("draft cache restores after reload and clear requires confirmation with recovery history",async({page})=>{
  const app=await openApp(page);await selectDesign(page,"像素工作台");
  const editor=page.locator(".composer textarea");await editor.fill("需要保留的语音草稿\n还有第二段");
  await expect.poll(()=>page.evaluate(async()=>{
    const {browserDraftPersistence}=await import("/src/draft-persistence.ts");
    const state=await browserDraftPersistence().load() as {drafts?:Array<{draft:{input:string}}>};
    return state?.drafts?.some(item=>item.draft.input.includes("需要保留的语音草稿")) ?? false;
  })).toBe(true);
  await page.reload();await expect(editor).toHaveValue("需要保留的语音草稿\n还有第二段");
  await page.getByRole("button",{name:"输入工具",exact:true}).tap();
  await page.getByRole("button",{name:"清空草稿",exact:true}).tap();
  const confirm=page.getByRole("dialog",{name:"清空草稿？",exact:true});await expect(confirm).toBeVisible();
  await confirm.getByRole("button",{name:"取消",exact:true}).tap();
  await expect(editor).toHaveValue("需要保留的语音草稿\n还有第二段");
  await page.getByRole("button",{name:"清空草稿",exact:true}).tap();
  await page.getByRole("button",{name:"确认清空",exact:true}).tap();
  await expect(editor).toHaveValue("");
  const history=await openDraftHistory(page);await expect(history).toContainText("清空前");
  await history.getByRole("button",{name:/需要保留的语音草稿/}).tap();
  await page.getByRole("button",{name:"恢复到输入框",exact:true}).tap();
  await expect(editor).toHaveValue("需要保留的语音草稿\n还有第二段");
  expect(app.commands.some(c=>c.type==="query")).toBe(false);
  await page.screenshot({path:`../work/draft-restored-${test.info().project.name}.png`});
});

test("draft cache saves new-chat dictation across reload and preserves scope when its directory changes",async({page})=>{
  await openApp(page);await selectDesign(page,"原生精简");
  await page.getByRole("button",{name:"打开会话",exact:true}).tap();await page.getByRole("button",{name:"新会话",exact:true}).tap();
  const editor=page.locator(".newchat-input");await editor.fill("这个目录的未提交草稿");
  await expect.poll(()=>page.evaluate(async()=>{
    const {browserDraftPersistence}=await import("/src/draft-persistence.ts");
    const state=await browserDraftPersistence().load() as {drafts?:Array<{draft:{input:string}}>};
    return state?.drafts?.some(item=>item.draft.input==="这个目录的未提交草稿") ?? false;
  })).toBe(true);
  await page.reload();await page.getByRole("button",{name:"打开会话",exact:true}).tap();await page.getByRole("button",{name:"新会话",exact:true}).tap();
  await expect(editor).toHaveValue("这个目录的未提交草稿");
  const history=await openDraftHistory(page);await expect(history).toContainText("这个目录的未提交草稿");
});

test("draft cache commits attachment references atomically and merges independent pages without reviving cleared data",async({page})=>{
  await openApp(page);
  const result=await page.evaluate(async()=>{
    const {browserDraftPersistence}=await import("/src/draft-persistence.ts");
    const one=browserDraftPersistence(),two=browserDraftPersistence();
    await Promise.all([one.load(),two.load()]);
    const at=Date.now();
    const blob="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII=";
    const draft={input:"A",images:[{media_type:"image/png" as const,data:blob}],files:[],pastes:[]};
    const h={id:"attachment-record",key:"a",label:"A",at,reason:"saved" as const,draft};
    await one.save({version:1,drafts:[{key:"a",at,draft}],history:[h]});
    await two.save({version:1,drafts:[{key:"b",at:at+1,draft:{input:"B",images:[],files:[],pastes:[]}}],history:[]});
    await one.save({version:1,drafts:[{key:"a",at:at+2,draft:{...draft,input:"A updated"}}],history:[h]});
    const state=await browserDraftPersistence().load() as {drafts:Array<{key:string;draft:{input:string;images:Array<{data:string}>}}>};
    const db=await new Promise<IDBDatabase>((resolve,reject)=>{
      const r=indexedDB.open("dsh-remote-drafts",2);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);
    });
    const count=await new Promise<number>((resolve,reject)=>{
      const r=db.transaction("attachments","readonly").objectStore("attachments").count();r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);
    });db.close();
    await one.save({version:1,resetAt:at+10,drafts:[],history:[]});
    await two.save({version:1,drafts:[{key:"b",at:at+9,draft:{input:"stale",images:[],files:[],pastes:[]}}],history:[]});
    const cleared=await browserDraftPersistence().load() as {drafts:unknown[];history:unknown[]};
    return {a:state.drafts.find(item=>item.key==="a")?.draft.input,b:state.drafts.find(item=>item.key==="b")?.draft.input,
      image:state.drafts.find(item=>item.key==="a")?.draft.images[0].data,blob,count,remaining:cleared.drafts.length+cleared.history.length};
  });
  expect(result.a).toBe("A updated");expect(result.b).toBe("B");expect(result.image).toBe(result.blob);
  expect(result.count).toBe(1);expect(result.remaining).toBe(0);
});

test("draft cache carries new-chat text when the user deliberately chooses another directory",async({page})=>{
  const app=await openApp(page);
  await page.getByRole("button",{name:"打开会话",exact:true}).tap();await page.getByRole("button",{name:"新会话",exact:true}).tap();
  const editor=page.locator(".newchat-input");await editor.fill("换目录时保留这条 prompt");
  await page.locator(".newchat-cwd").tap();
  const picker=page.getByRole("dialog",{name:"选择本地工作区",exact:true});
  await picker.getByPlaceholder("或粘贴绝对路径…").fill("/repo/Robot");
  await picker.getByRole("button",{name:"在此创建",exact:true}).tap();
  await expect(page.locator(".newchat-cwd")).toContainText("/repo/Robot");
  await expect(editor).toHaveValue("换目录时保留这条 prompt");
  await page.getByRole("button",{name:"开始",exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==="new_session").length).toBe(1);
  expect(app.commands.find(c=>c.type==="new_session")?.cwd).toBe("/repo/Robot");
  expect(app.commands.find(c=>c.type==="new_session")?.prompt).toBe("换目录时保留这条 prompt");
});

test("clipboard reply after logout cannot restore content into the cleared local cache",async({page})=>{
  const app=await openApp(page);await stubClipboard(page,"","pending");
  await page.getByRole("button",{name:"粘贴并发送",exact:true}).tap();
  await page.getByRole("button",{name:"更多设置",exact:true}).tap();
  await page.getByRole("button",{name:/退出登录/}).tap();
  await expect(page.locator(".composer")).toHaveCount(0);
  await page.evaluate(()=>(window as unknown as {resolveClipboard:(text:string)=>void}).resolveClipboard("退出后迟到的剪贴板内容"));
  await expect.poll(()=>page.evaluate(async()=>{
    const {browserDraftPersistence}=await import("/src/draft-persistence.ts");
    const state=await browserDraftPersistence().load() as {drafts:unknown[];history:unknown[]};return state.drafts.length+state.history.length;
  })).toBe(0);
  expect(app.commands.some(c=>["query","new_session","interrupt"].includes(c.type as string))).toBe(false);
});


test("ordinary send still queues when the server is busy before client state catches up", async ({page}) => {
  const app=await openApp(page);app.laggingState();
  await page.locator('.composer textarea').fill('状态尚未同步，也必须排队');
  await page.getByRole('button',{name:'发送',exact:true}).tap();
  await expect(page.getByRole('button',{name:'编辑待发送消息'})).toBeVisible();
  const queries=app.commands.filter(c=>c.type==='query');
  expect(queries).toHaveLength(1);expect(queries[0].delivery).toBe('queue');
  expect(app.commands.filter(c=>c.type==='interrupt'||c.type==='steer'||c.start_now)).toHaveLength(0);
  app.finish();await expect(page.locator('.thread')).toContainText('状态尚未同步，也必须排队');
});

async function edgeSwipe(page:Page,selector:string,startX:number,endX:number){
  await page.locator(selector).evaluate((element,{startX,endX})=>{
    for(const [type,x] of [['touchstart',startX],['touchmove',endX],['touchend',endX]] as const){
      const point={identifier:1,clientX:x,clientY:240};
      const event=new Event(type,{bubbles:true,cancelable:true});
      Object.defineProperties(event,{touches:{value:type==='touchend'?[]:[point]},changedTouches:{value:[point]}});
      element.dispatchEvent(event);
    }
  },{startX,endX});
}

for(const width of [834,1180])test(`tablet ${width} keeps touch Return and both edge gestures in desktop-site Safari`,async({page})=>{
  await page.setViewportSize({width,height:820});const app=await openApp(page);
  await expect(page.locator('html')).toHaveAttribute('data-device-layout','tablet');
  const input=page.locator('.composer textarea');await input.fill('第一行');await input.press('End');await input.press('Enter');await input.press('x');
  await expect(input).toHaveValue('第一行\nx');expect(app.commands.filter(c=>c.type==='query')).toHaveLength(0);
  await expect(page.getByRole('dialog',{name:'选择模型'})).toHaveCount(0);
  await input.blur();await edgeSwipe(page,'.thread',5,120);
  await expect(page.locator('.sessions')).toHaveClass(/show/);
  await expect.poll(async()=>Math.round((await page.locator('.sessions').boundingBox())!.x)).toBe(0);
  await expect(page.locator('.scard').last()).toContainText('另一目录的正在运行任务');
  await page.getByRole('button',{name:'项目目录',exact:true}).tap();
  await expect(page.locator('.scard').last()).toHaveAttribute('data-session-id',SID);
  await edgeSwipe(page,'.sessions',180,60);await expect(page.locator('.sessions')).not.toHaveClass(/show/);
  await edgeSwipe(page,'.thread',width-5,width-120);await expect(page.locator('.sessions')).toHaveClass(/show/);
  await expect(page.locator('.sessions')).toHaveAttribute('data-side','right');
  await expect.poll(async()=>{const r=(await page.locator('.sessions').boundingBox())!;return Math.round(r.x+r.width);}).toBe(width);
  await edgeSwipe(page,'.sessions',width-160,width-40);await expect(page.locator('.sessions')).not.toHaveClass(/show/);
  await page.getByRole('button',{name:'打开会话',exact:true}).tap();await expect(page.locator('.sessions')).toHaveClass(/show/);
  await page.getByRole('button',{name:'新会话',exact:true}).tap();
  const fresh=page.locator('.newchat-input');await fresh.fill('新会话第一行');await fresh.press('End');await fresh.press('Enter');await fresh.press('x');
  await expect(fresh).toHaveValue('新会话第一行\nx');expect(app.commands.filter(c=>c.type==='new_session')).toHaveLength(0);
  await expect(page.getByRole('dialog',{name:'选择模型'})).toHaveCount(0);
});

test('tablet queue editing preserves the job and long press alone exposes guide',async({page})=>{
  const app=await openApp(page);app.start();await page.locator('.composer textarea').fill('稍后做');
  await page.getByRole('button',{name:'发送',exact:true}).tap();
  const chip=page.getByRole('button',{name:'编辑待发送消息'});await chip.tap();
  const dialog=page.getByRole('dialog',{name:'待发送消息',exact:true});
  await expect(dialog.getByRole('button',{name:'引导当前任务'})).toHaveCount(0);
  await dialog.getByRole('textbox',{name:'编辑排队消息'}).fill('改后稍后做');
  await dialog.getByRole('button',{name:'保存修改',exact:true}).tap();
  expect(app.commands.filter(c=>c.type==='interrupt'||c.type==='steer'||c.start_now)).toHaveLength(0);
  await expect(dialog).toHaveCount(0);
  await chip.dispatchEvent('pointerdown',{button:0,clientX:100,clientY:100});await page.waitForTimeout(550);await chip.dispatchEvent('pointerup');
  await expect(dialog.getByRole('button',{name:'引导当前任务'})).toBeVisible();
  const bounds=(await dialog.boundingBox())!;expect(bounds.y).toBeGreaterThanOrEqual(0);expect(bounds.y+bounds.height).toBeLessThanOrEqual(820);
  await dialog.getByRole('button',{name:'引导当前任务'}).tap();
  expect(app.commands.filter(c=>c.type==='update_queued_query'&&c.start_now)).toHaveLength(1);
});

test('tablet focused editor and sheets stay inside the keyboard viewport',async({page})=>{
  await page.addInitScript(()=>{
    const viewport=new EventTarget();const reading={height:820,width:1180,offsetTop:0,scale:1};
    for(const key of Object.keys(reading))Object.defineProperty(viewport,key,{get:()=>reading[key as keyof typeof reading]});
    Object.defineProperty(window,'visualViewport',{value:viewport});
    Object.assign(window,{__keyboard:()=>{reading.height=440;reading.offsetTop=18;viewport.dispatchEvent(new Event('resize'));}});
  });
  await openApp(page);
  await page.locator('.composer textarea').fill('参考上文继续输入\n'.repeat(20));
  await page.locator('.composer textarea').focus();
  await page.evaluate(()=>(window as unknown as {__keyboard():void}).__keyboard());
  await expect.poll(()=>page.locator('.composer').evaluate(el=>el.getBoundingClientRect().bottom)).toBe(458);
  await expect(page.locator('.picker-sheet')).toHaveCount(0);
  await page.getByRole('button',{name:'选择模型',exact:true}).tap();
  const dialog=page.getByRole('dialog',{name:'选择模型',exact:true});await expect(dialog).toBeVisible();
  await expect.poll(async()=>{const r=(await dialog.boundingBox())!;return r.y+r.height;}).toBeLessThanOrEqual(459);
  expect((await dialog.boundingBox())!.y).toBeGreaterThanOrEqual(18);
  await page.screenshot({path:'../work/ipad-keyboard-sheet.png',clip:{x:0,y:0,width:1180,height:458}});
});

test('desktop keeps newest activity and directory sessions at the bottom',async({page})=>{
  await page.goto('/tests/sidebar-browser.html');
  await expect(page.locator('.scard').last()).toHaveAttribute('data-session-id','session-0');
  await expect(page.locator('[data-session-id="session-0"]')).toBeInViewport();
  await page.getByRole('button',{name:'项目目录',exact:true}).click();
  await expect(page.locator('.scard').last()).toHaveAttribute('data-session-id','session-0');
  await expect(page.locator('[data-session-id="session-0"]')).toBeInViewport();
});


test('both agents share activity and directory lists with correctly scoped send and settings',async({page})=>{
  const app=await openApp(page,{codex:true});
  await page.getByRole('button',{name:'打开会话',exact:true}).tap();
  const codex=page.locator('[data-session-id="codex@native"]');await expect(codex).toContainText('Codex');
  await expect(page.locator(`[data-session-id="${SID}"]`)).toContainText('DSH');
  await page.getByRole('button',{name:'项目目录',exact:true}).tap();
  const group=page.locator('.sgroup-wrap').filter({has:codex});
  await expect(group.locator(`[data-session-id="${SID}"]`)).toBeVisible();
  await codex.tap();
  await expect(page.locator('.surface-head-title').first()).toContainText('Codex 项目会话');
  await expect(page.locator('.assistant-role').first()).toHaveText('Codex');
  expect(app.commands.findLast(c=>c.type==='switch_session')).toMatchObject({engine:'codex',session_id:'codex@native'});
  await page.locator('.composer textarea').fill('发给 Codex 的消息');
  await page.getByRole('button',{name:'发送',exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==='query').length).toBe(1);
  expect(app.commands.find(c=>c.type==='query')).toMatchObject({sid:'codex@native',delivery:'queue',prompt:'发给 Codex 的消息'});
  expect(app.commands.filter(c=>c.type==='interrupt'||c.type==='steer')).toHaveLength(0);
  await page.getByRole('button',{name:'打开会话',exact:true}).tap();
  await expect(page.locator(`[data-session-id="${SID}"]`)).toBeVisible();
  await expect(codex).toBeVisible();
});

test('new chat selects Codex while retaining the inherited project and typed draft',async({page})=>{
  const app=await openApp(page,{codex:true});
  await page.getByRole('button',{name:'打开会话',exact:true}).tap();await page.getByRole('button',{name:'新会话',exact:true}).tap();
  const input=page.locator('.newchat-input');await input.fill('切换 Agent 仍保留我的草稿');
  await page.getByRole('radio',{name:'Codex',exact:true}).tap();
  await expect(input).toHaveValue('切换 Agent 仍保留我的草稿');
  await expect(page.locator('.newchat-cwd')).toContainText('sample-project');
  await expect(page.locator('.hstat')).toHaveCount(0);
  await expect(page.locator('.newchat-hint')).toContainText('换行');
  await input.press('End');await input.press('Enter');await input.press('x');
  await expect(input).toHaveValue('切换 Agent 仍保留我的草稿\nx');
  await expect(page.locator('.picker-sheet')).toHaveCount(0);
  await page.getByRole('button',{name:'开始',exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==='new_session').length).toBe(1);
  expect(app.commands.find(c=>c.type==='new_session')).toMatchObject({engine:'codex',cwd:'/repo/sample-project',prompt:'切换 Agent 仍保留我的草稿\nx'});
});


test('both agents retain terminal and pixel structures across engine changes',async({page})=>{
  await openApp(page,{codex:true});
  for(const [name,layout] of [['Terminal 工作台','terminal'],['像素工作台','pixel']] as const){
    await selectDesign(page,name);
    await page.getByRole('button',{name:'打开会话',exact:true}).tap();
    await page.locator('[data-session-id="codex@native"]').tap();
    await expect(page.locator('html')).toHaveAttribute('data-engine','codex');
    await expect(page.locator('html')).toHaveAttribute('data-layout',layout);
    const bubble=page.locator('.ubub').last();
    expect(await bubble.evaluate(el=>getComputedStyle(el).borderRadius)).toBe(layout==='terminal'?'0px':'2px');
    await page.getByRole('button',{name:'打开会话',exact:true}).tap();
    await page.locator(`[data-session-id="${SID}"]`).tap();
  }
});


test('tablet device entry stays readable in portrait and landscape with every layout',async({page})=>{
  await openApp(page);
  for(const width of [834,1180]){
    await page.setViewportSize({width,height:820});
    for(const [name] of DESIGN_LAYOUTS){
      await selectDesign(page,name);
      const trigger=page.getByRole('button',{name:'设备中心',exact:true});
      const label=trigger.locator('span');await expect(label).toHaveText('设备');await expect(label).toBeVisible();
      await expect(trigger).toHaveAttribute('title',/测试设备.*在线/);
      const geometry=await label.evaluate(el=>({client:el.clientWidth,scroll:el.scrollWidth}));
      expect(geometry.client).toBeGreaterThan(18);expect(geometry.scroll).toBeLessThanOrEqual(geometry.client+1);
      await trigger.tap();await expect(page.getByRole('dialog',{name:'设备中心',exact:true})).toBeVisible();
      await page.keyboard.press('Escape');
    }
  }
});

test('both agents have official vector identity marks, independent status dots, and a sidebar type filter',async({page})=>{
  await openApp(page,{codex:true});
  await expect(page.locator('.session-agent-badge')).toHaveAttribute('aria-label','Agent：DSH');
  await page.getByRole('button',{name:'打开会话',exact:true}).tap();
  const dsh=page.locator(`[data-session-id="${SID}"]`),codex=page.locator('[data-session-id="codex@native"]');
  const marks=await Promise.all([dsh,codex].map(row=>row.locator('.agent-logo').evaluate(el=>{
    if(el instanceof HTMLImageElement)return {src:el.src,loaded:el.complete && el.naturalWidth>0,width:el.clientWidth};
    const mask=getComputedStyle(el).maskImage;return {src:mask,loaded:mask!=="none",width:el.clientWidth};
  })));
  expect(marks[0].src).not.toBe(marks[1].src);expect(marks.every(mark=>mark.loaded && mark.width===16)).toBe(true);
  await expect(dsh.locator('.agent-ring')).toHaveCount(0);
  await expect(dsh.locator('.scard-status .sd').first()).toBeVisible();
  const deepseekFilter=page.getByRole('checkbox',{name:'显示 DSH 会话',exact:true});
  const codexFilter=page.getByRole('checkbox',{name:'显示 Codex 会话',exact:true});
  await expect(deepseekFilter).toBeChecked();await expect(codexFilter).toBeChecked();
  await deepseekFilter.uncheck();await expect(dsh).toHaveCount(0);await expect(codex).toBeVisible();
  await page.getByRole('button',{name:'项目目录',exact:true}).tap();await expect(dsh).toHaveCount(0);
  await codexFilter.uncheck();await expect(codex).toHaveCount(0);await expect(page.getByText('无匹配',{exact:true})).toBeVisible();
  await deepseekFilter.check();await expect(dsh).toBeVisible();await expect(codex).toHaveCount(0);
  await codexFilter.check();await expect(codex).toBeVisible();
  await codex.tap();await expect(page.locator('.session-agent-badge')).toHaveAttribute('aria-label','Agent：Codex');
  expect(await page.locator('.session-agent-badge').evaluate(el=>!!el.closest('button'))).toBe(true);
});

test('tablet drawer follows the finger from each edge, restores after cancellation, and settles without a transform',async({page})=>{
  await openApp(page);const width=page.viewportSize()!.width;
  const signal=async(type:string,x:number,y=240)=>page.locator('.thread').evaluate((element,{type,x,y})=>{
    const point={identifier:1,clientX:x,clientY:y};const event=new Event(type,{bubbles:true,cancelable:true});
    Object.defineProperties(event,{touches:{value:type==='touchend'||type==='touchcancel'?[]:[point]},changedTouches:{value:[point]}});element.dispatchEvent(event);
  },{type,x,y});
  for(const side of ['left','right'] as const){
    const start=side==='left'?5:width-5,end=side==='left'?150:width-150;
    await signal('touchstart',start);await signal('touchmove',end);
    await expect(page.locator('.shell')).toHaveAttribute('data-drawer-dragging','true');
    await expect(page.locator('.sessions')).toHaveAttribute('data-side',side);
    // Finger progress is batched into requestAnimationFrame. The drag marker
    // is synchronous; under suite load it can precede the first painted frame.
    await expect.poll(()=>page.locator('.shell').evaluate(el=>Number(el.style.getPropertyValue('--drawer-progress')))).toBeGreaterThan(0);
    const progress=await page.locator('.shell').evaluate(el=>Number(el.style.getPropertyValue('--drawer-progress')));
    expect(progress).toBeGreaterThan(0);expect(progress).toBeLessThan(1);
    const box=(await page.locator('.sessions').boundingBox())!;
    expect(side==='left'?box.x<0:box.x+box.width>width).toBe(true);
    await signal('touchcancel',end);await expect(page.locator('.sessions')).not.toHaveClass(/show/);
    await expect(page.locator('.shell')).not.toHaveAttribute('data-drawer-dragging','true');
    await signal('touchstart',start);await signal('touchmove',end);await signal('touchend',end);
    await expect(page.locator('.sessions')).toHaveClass(/show/);
    await expect.poll(()=>page.locator('.sessions').evaluate(el=>getComputedStyle(el).transform)).toBe('none');
    await page.getByRole('button',{name:'收起',exact:true}).tap();
    await expect(page.locator('.sessions')).not.toHaveClass(/show/);
  }
});

test('tablet reduced motion keeps the same navigation with animations disabled',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await openApp(page);
  await edgeSwipe(page,'.thread',page.viewportSize()!.width-5,page.viewportSize()!.width-150);
  await expect(page.locator('.sessions')).toHaveClass(/show/);
  const motion=await page.locator('.sessions').evaluate(el=>{const s=getComputedStyle(el);return {duration:s.transitionDuration,animation:s.animationName};});
  expect(motion).toEqual({duration:'0s',animation:'none'});
});

test('both agents expose real balance and dynamic quota windows in settings', async ({page},testInfo)=>{
  const app=await openApp(page,{codex:true});
  await page.getByRole('button',{name:'更多设置',exact:true}).tap();
  await page.getByRole('button',{name:/余额与额度/}).tap();
  const panel=page.getByRole('dialog',{name:'余额与额度',exact:true});
  await expect(panel).toContainText('¥136.827');
  await expect(panel).toContainText('每周额度');
  await expect(panel).toContainText('剩余 78%');
  await expect(panel).not.toContainText('5 小时额度');
  await panel.locator('.account-wallet summary').click();
  await expect(panel.locator('.account-wallet-details')).toContainText('今日开销');
  await expect(panel.locator('.account-wallet-details')).toContainText('本周开销');
  await expect(panel.getByRole('link',{name:/查看官方账单/})).toHaveAttribute('href','https://platform.deepseek.com/usage');
  await expect(panel.locator('.account-reset-countdown')).toContainText(/后重置|已到重置时间/);
  await page.waitForFunction(()=>document.querySelector(".account-usage-sheet")?.getAnimations().every(animation=>animation.playState!=="running"));
  if(testInfo.project.name === 'ipad-webkit'){
    const box=await panel.boundingBox();expect(Math.abs(box!.x+box!.width/2-page.viewportSize()!.width/2)).toBeLessThan(2);
  }
  await page.screenshot({path:`../work/remote-acceptance/identity-account-${testInfo.project.name}.png`});
  expect(app.commands.filter(c=>c.type==='get_account_usage')).toHaveLength(2);
  await panel.getByRole('button',{name:'刷新余额与额度'}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==='get_account_usage').length).toBe(4);
  await page.locator('.scrim.show').tap({position:{x:5,y:5}});
  await expect(panel).toHaveCount(0);
});

test('pending editor copies the unsaved full text and closes only after save confirmation',async({page})=>{
  await page.addInitScript(()=>{
    Object.defineProperty(navigator,'clipboard',{value:{writeText:async(text:string)=>{(window as unknown as {__copied:string}).__copied=text;}}});
  });
  await openApp(page).then(app=>app.start());
  await page.locator('.composer textarea').fill('排队原始全文');
  await page.getByRole('button',{name:'发送',exact:true}).tap();
  await page.getByRole('button',{name:'编辑待发送消息'}).tap();
  const dialog=page.getByRole('dialog',{name:'待发送消息',exact:true});
  await dialog.getByRole('textbox',{name:'编辑排队消息'}).fill('修改后尚未保存\n第二段文字');
  await dialog.getByRole('button',{name:'复制排队消息'}).tap();
  await expect(dialog).toContainText('已复制');
  expect(await page.evaluate(()=>(window as unknown as {__copied:string}).__copied)).toBe('修改后尚未保存\n第二段文字');
  await dialog.getByRole('button',{name:'保存修改',exact:true}).tap();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('button',{name:'编辑待发送消息'})).toContainText('修改后尚未保存');
});

test('desktop keeps a visible upper-left sidebar button and a left drawer in every layout',async({page})=>{
  await openApp(page);
  const button=page.getByRole('button',{name:'打开会话侧边栏',exact:true});
  for(const appearance of ['blue','native','terminal','engineering','pixel']){
    await page.evaluate(value=>document.documentElement.dataset.layout=value==='blue'?'classic':value,appearance);
    await expect(button).toBeVisible();
    await button.click();
    await expect(page.locator('.sessions')).toHaveClass(/show/);
    await expect.poll(()=>page.locator('.sessions').evaluate(el=>Math.round(el.getBoundingClientRect().left))).toBe(0);
    await button.click();
    await expect(page.locator('.sessions')).not.toHaveClass(/show/);
  }
});

test('pending save waits for confirmation and a rejected save keeps the draft open',async({page})=>{
  const app=await openApp(page,{deferQueueUpdates:true});app.start();
  await page.locator('.composer textarea').fill('等待发送的消息');
  await page.getByRole('button',{name:'发送',exact:true}).tap();
  await page.getByRole('button',{name:'编辑待发送消息'}).tap();
  const dialog=page.getByRole('dialog',{name:'待发送消息',exact:true});
  const input=dialog.getByRole('textbox',{name:'编辑排队消息'});
  await input.fill('新的完整草稿');
  await dialog.getByRole('button',{name:'保存修改',exact:true}).tap();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button',{name:'正在保存…',exact:true})).toBeDisabled();
  const failed=app.commands.findLast(cmd=>cmd.type==='update_queued_query')!;
  app.emit({type:'queued_query_updated',msg_id:failed.msg_id,request_id:failed.cmd_id,updated:false,error:'保存未完成，请重试'});
  await expect(input).toHaveValue('新的完整草稿');
  await expect(dialog).toContainText('保存未完成，请重试');
  await dialog.getByRole('button',{name:'保存修改',exact:true}).tap();
  const success=app.commands.findLast(cmd=>cmd.type==='update_queued_query')!;
  app.emit({type:'queued_query_updated',msg_id:success.msg_id,request_id:success.cmd_id,updated:true});
  await expect(dialog).toHaveCount(0);
  expect(app.commands.filter(c=>c.type==='interrupt'||c.type==='steer')).toHaveLength(0);
});

test('both agents native Codex thin history exposes full process on a fresh client',async({page})=>{
  const app=await openApp(page,{codex:true,nativeThinHistory:true});
  await page.getByRole('button',{name:'打开会话',exact:true}).tap();
  await page.locator('[data-session-id="codex@native"]').tap();
  await expect(page.locator('.session-agent-badge')).toHaveAttribute('aria-label','Agent：Codex');
  await page.getByText('查看完整过程',{exact:true}).tap();
  await expect.poll(()=>app.commands.some(c=>c.type==='get_turn_detail'&&c.session_id==='codex@native')).toBe(true);
  const thought=page.locator('.process-activity').filter({hasText:'思考'});
  await expect(thought).toBeVisible();
  await thought.locator('summary').first().tap();
  await expect(page.getByText('本机历史提供的思考摘要',{exact:true})).toBeVisible();
});

test('both agents compact spacing can be changed and survives reload',async({page})=>{
  await openApp(page,{codex:true});
  const leading=()=>page.locator('.prose').first().evaluate(el=>{const s=getComputedStyle(el);return Number.parseFloat(s.lineHeight)/Number.parseFloat(s.fontSize);});
  await expect.poll(leading).toBeCloseTo(1.45,2);
  await page.getByRole('button',{name:'更多设置',exact:true}).tap();
  await page.getByRole('button',{name:/外观与主题/}).tap();
  await page.getByRole('group',{name:'聊天行距'}).getByRole('button',{name:'标准',exact:true}).tap();
  await expect.poll(leading).toBeCloseTo(1.62,2);
  await page.keyboard.press('Escape');await page.reload();
  await expect.poll(leading).toBeCloseTo(1.62,2);
});

test('both agents title navigation and device label stay legible in every design',async({page})=>{
  await openApp(page,{codex:true});
  for(const label of ['原生精简','Terminal 工作台','工程对话','像素工作台','DeepSeek 蓝']){
    await page.getByRole('button',{name:'更多设置',exact:true}).tap();
    await page.getByRole('button',{name:/外观与主题/}).tap();
    await page.getByRole('radio',{name:label,exact:true}).tap();
    await page.keyboard.press('Escape');
    await expect(page.locator('.surface-head-title')).toHaveText('移动端测试');
    await expect(page.locator('.device-trigger')).toHaveText('设备');
    expect(await page.locator('.device-trigger>span').evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true);
    await expect(page.locator('.desktop-sidebar-trigger')).toHaveCount(0);
    await page.getByRole('button',{name:'打开会话侧边栏',exact:true}).tap();
    await expect(page.locator('.sessions')).toHaveClass(/show/);
    const card=page.locator(`[data-session-id="${SID}"]`);
    await expect(card.locator('.scard-top')).toContainText('移动端测试');
    await expect(card.locator('.scard-meta')).toContainText('sample-project');
    await expect(card.locator('.scard-meta')).toContainText('DSH');
    await expect(card.locator('.scard-status')).toContainText('当前');
    await expect(card.locator('.scard-status time')).toBeVisible();
    await page.getByRole('button',{name:'收起',exact:true}).tap();
  }
});

test('both agents queued edit appends a file and waits for successful save',async({page})=>{
  const app=await openApp(page,{codex:true});
  app.start();
  const input=page.locator('.composer textarea');await input.fill('原消息');
  await page.getByRole('button',{name:'发送',exact:true}).tap();
  await page.getByRole('button',{name:'编辑待发送消息',exact:true}).tap();
  const dialog=page.getByRole('dialog',{name:'待发送消息',exact:true});
  await expect(dialog.getByRole('textbox',{name:'编辑排队消息'})).toHaveValue('原消息');
  await dialog.getByLabel('添加排队消息附件').setInputFiles({name:'context.txt',mimeType:'text/plain',buffer:Buffer.from('extra context')});
  await expect(dialog).toContainText('context.txt');
  await dialog.getByRole('textbox',{name:'编辑排队消息'}).fill('补充上下文');
  await dialog.getByRole('button',{name:'保存修改',exact:true}).tap();
  await expect(dialog).toHaveCount(0);
  expect(app.commands.findLast(cmd=>cmd.type==='update_queued_query')).toMatchObject({prompt:'补充上下文',add_files:[{filename:'context.txt',data:Buffer.from('extra context').toString('base64')}]});
  expect(app.commands.filter(cmd=>cmd.type==='interrupt'||cmd.type==='steer')).toHaveLength(0);
});

test('both agents many changed files occupy one row and preview dismisses outside',async({page})=>{
  const app=await openApp(page,{codex:true});
  app.emit({type:'user_msg',msg_id:'large-file-turn',prompt:'更新项目文件'});
  for(let i=0;i<32;i++){
    app.emit({type:'tool_use',message_id:'writes',tool_use_id:`write-${i}`,tool:'write',input:{path:`src/file-${i}.py`,content:'updated'}});
    app.emit({type:'tool_result',tool_use_id:`write-${i}`,content:'done',is_error:false});
  }
  app.emit({type:'turn_end',result:'completed'});
  const summary=page.getByRole('button',{name:/改动 32 个文件/});
  await expect(summary).toBeVisible();
  expect((await summary.boundingBox())!.height).toBeLessThan(40);
  await expect(page.locator('.turn-file-chip')).toHaveCount(0);
  await summary.tap();
  const files=page.getByRole('dialog',{name:'本轮改动文件',exact:true});
  await expect(files.locator('.turn-files-sheet-list>button')).toHaveCount(32);
  await files.getByRole('button',{name:/file-0.py/}).tap();
  await expect(page.locator('.artifact-panel')).toContainText('preview works');
  await page.getByRole('button',{name:'打开会话侧边栏',exact:true}).tap();
  await expect(page.locator('.artifact-panel')).toHaveCount(0);
  await expect(page.locator('.sessions')).toHaveClass(/show/);
});

for (const codex of [false, true]) {
  test(`both agents Amo ${codex ? 'Codex' : 'DSH'} whole reply collapses once from the tail and restores all answers`,async({page})=>{
    const app=await openApp(page,{codex});
    if (codex) {
      await page.getByRole('button',{name:'打开会话侧边栏',exact:true}).click();
      await page.locator('.scard').filter({hasText:'Codex 项目会话'}).click();
      await expect(page.locator('.surface-head-title')).toHaveText('Codex 项目会话');
    }
    app.emit({type:'user_msg',msg_id:'collapse-turn',prompt:'处理当前任务'});
    app.emit({type:'process',item_id:'reasoning-footer',kind:'reasoning',phase:'end',status:'succeeded',title:'思考',summary:'思考摘要',detail:'过程内容'});
    app.emit({type:'delta',message_id:'answer-one',text:'第一部分回答',channel:'final'});
    app.emit({type:'delta',message_id:'answer-two',text:'第二部分回答',channel:'final'});
    app.emit({type:'turn_end',result:{subtype:'completed',is_error:false}});
    const turn=page.locator('[data-turn-id="collapse-turn"]');
    await turn.locator('.turn-process-head').click();
    await expect(turn.locator('.process-timeline')).toBeVisible();
    await expect(turn.getByRole('button',{name:'收起本轮回答',exact:true})).toHaveCount(1);
    await turn.getByRole('button',{name:'收起本轮回答',exact:true}).click();
    await expect(turn.locator('.ubub')).toHaveText('处理当前任务');
    await expect(turn.locator('.process-timeline')).toHaveCount(0);
    await expect(turn).not.toContainText('第一部分回答');
    await expect(turn).not.toContainText('第二部分回答');
    await expect(turn.getByRole('button',{name:'展开本轮回答',exact:true})).toBeInViewport();
    await turn.getByRole('button',{name:'展开本轮回答',exact:true}).click();
    await expect(turn).toContainText('第一部分回答');
    await expect(turn).toContainText('第二部分回答');
    await expect(turn.locator('.process-timeline')).toBeVisible();
  });

  test(`both agents Amo ${codex ? 'Codex' : 'DSH'} healthy foreground return repairs missed history without sending a query`,async({page})=>{
    const app=await openApp(page,{codex});
    if (codex) {
      await page.getByRole('button',{name:'打开会话侧边栏',exact:true}).click();
      await page.locator('.scard').filter({hasText:'Codex 项目会话'}).click();
      await expect(page.locator('.surface-head-title')).toHaveText('Codex 项目会话');
    }
    const before=app.commands.filter(cmd=>cmd.type==='get_history').length;
    app.advanceSilently();
    await page.evaluate(()=>{
      Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
    });
    await expect(page.locator('.thread')).toContainText('另一端正在回答，手机应自动补载');
    expect(app.commands.filter(cmd=>cmd.type==='get_history')).toHaveLength(before+1);
    expect(app.commands.some(cmd=>cmd.type==='get_status' && cmd.sid===(codex?'codex@native':SID))).toBe(true);
    expect(app.commands.filter(cmd=>['query','interrupt','new_session'].includes(String(cmd.type)))).toHaveLength(0);
  });
}

test('both agents selecting current full access has no confirmation or command',async({page})=>{
  const app=await openApp(page,{codex:true});
  app.emit({type:'perm',mode:'danger-full-access',options:[{id:'danger-full-access',name:'Full Access',danger:true}]});
  let confirmations=0;page.on('dialog',async dialog=>{confirmations++;await dialog.dismiss();});
  await page.locator('.composer .hint-mode').tap();
  const picker=page.getByRole('dialog',{name:'选择权限模式',exact:true});
  await picker.getByRole('button',{name:'Full Access',exact:true}).tap();
  expect(confirmations).toBe(0);
  expect(app.commands.filter(cmd=>cmd.type==='set_perm')).toHaveLength(0);
});

async function pasteScreenshot(page: Page, selector: string, filesOnly = false) {
  await page.locator(selector).focus();
  return page.locator(selector).evaluate(async (control, filesOnly) => {
    const canvas=document.createElement('canvas');canvas.width=2;canvas.height=2;
    canvas.getContext('2d')!.fillRect(0,0,2,2);
    const blob=await new Promise<Blob>(resolve=>canvas.toBlob(blob=>resolve(blob!), 'image/png'));
    const file=new File([blob],'clipboard.png',{type:'image/png'});
    const transfer=new DataTransfer();transfer.items.add(file);transfer.setData('text/plain','截图说明');
    const event=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:transfer});
    if(filesOnly)Object.defineProperty(event,'clipboardData',{value:{files:[file],items:[],getData:(type:string)=>type==='text/plain'?'截图说明':''}});
    control.dispatchEvent(event);
    return event.defaultPrevented;
  },filesOnly);
}

for(const agent of ['dsh','codex'] as const){
  test(`both agents ${agent} paste Safari files-only screenshots into a draft and send the image`,async({page})=>{
    const app=await openApp(page,{codex:true});
    if(agent==='codex'){
      await page.getByRole('button',{name:'打开会话',exact:true}).tap();
      await page.locator('[data-session-id="codex@native"]').tap();
      await expect(page.locator('.session-agent-badge')).toHaveAttribute('aria-label','Agent：Codex');
    }
    expect(await pasteScreenshot(page,'.composer textarea',true)).toBe(true);
    await expect(page.getByRole('img',{name:'待发送图片 1',exact:true})).toBeVisible();
    await expect(page.locator('.composer textarea')).toHaveValue('截图说明');
    expect(app.commands.filter(c=>c.type==='query')).toHaveLength(0);
    await page.getByRole('button',{name:'发送',exact:true}).tap();
    await expect.poll(()=>app.commands.filter(c=>c.type==='query').length).toBe(1);
    const query=app.commands.findLast(c=>c.type==='query')!;
    expect(query).toMatchObject({prompt:'截图说明',sid:agent==='codex'?'codex@native':SID,images:[{media_type:'image/png'}]});
    expect((query.images as {data:string}[])[0].data.length).toBeGreaterThan(30);
    expect(app.commands.filter(c=>c.type==='interrupt'||c.type==='steer')).toHaveLength(0);
  });
}

test('both agents new conversation accepts pasted images before its first message',async({page})=>{
  const app=await openApp(page);
  await page.getByRole('button',{name:'打开会话',exact:true}).tap();await page.getByRole('button',{name:'新会话',exact:true}).tap();
  expect(await pasteScreenshot(page,'.newchat-input')).toBe(true);
  await expect(page.getByRole('img',{name:'待发送图片 1',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'开始',exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==='new_session').length).toBe(1);
  expect(app.commands.findLast(c=>c.type==='new_session')).toMatchObject({prompt:'截图说明',cwd:'/repo/sample-project',images:[{media_type:'image/png'}]});
});

test('both agents queued editor accepts pasted images and closes after save',async({page})=>{
  const app=await openApp(page,{codex:true});app.start();
  await page.locator('.composer textarea').fill('待发送');await page.getByRole('button',{name:'发送',exact:true}).tap();
  await page.getByRole('button',{name:'编辑待发送消息',exact:true}).tap();
  const dialog=page.getByRole('dialog',{name:'待发送消息',exact:true});
  await pasteScreenshot(page,'.queued-query-editor');
  await expect(dialog.getByRole('img',{name:'新增图片',exact:true})).toBeVisible();
  await dialog.getByRole('button',{name:'保存修改',exact:true}).tap();await expect(dialog).toHaveCount(0);
  expect(app.commands.findLast(c=>c.type==='update_queued_query')).toMatchObject({add_images:[{media_type:'image/png'}]});
  expect(app.commands.filter(c=>c.type==='interrupt'||c.type==='steer')).toHaveLength(0);
});

test('both agents clipboard send reads image content once and sends no duplicate representation',async({page})=>{
  const app=await openApp(page);
  await page.evaluate(()=>{
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{read:async()=>{
      const canvas=document.createElement('canvas');canvas.width=2;canvas.height=2;
      const blob=await new Promise<Blob>(resolve=>canvas.toBlob(blob=>resolve(blob!),'image/png'));
      return [{types:['image/png','text/html'],getType:async()=>blob}];
    }}});
  });
  await page.getByRole('button',{name:'粘贴并发送',exact:true}).tap();
  await expect.poll(()=>app.commands.filter(c=>c.type==='query').length).toBe(1);
  expect(app.commands.findLast(c=>c.type==='query')).toMatchObject({prompt:'',images:[{media_type:'image/png'}]});
  await expect(page.locator('.composer .attach-img')).toHaveCount(0);
});

async function assertRenameSelection(page: Page, title: string){
  const input=page.getByRole('textbox',{name:'会话标题',exact:true});await expect(input).toHaveValue(title);
  const selection=await input.evaluate(el=>{const input=el as HTMLInputElement;return [input.selectionStart,input.selectionEnd,document.activeElement===input];});
  expect(selection).toEqual([0,title.length,true]);
}

test('both agents double tap opens a selected rename dialog without switching sessions',async({page})=>{
  const app=await openApp(page,{codex:true});await page.getByRole('button',{name:'打开会话',exact:true}).tap();
  const switches=app.commands.filter(c=>c.type==='switch_session').length;
  const row=page.locator('[data-session-id="codex@native"]');
  await page.waitForFunction(()=>getComputedStyle(document.querySelector('.sessions')!).transform==='none');
  const rowBox=await row.boundingBox();
  // Two native taps without locator stability polling between them. The
  // browser's real double-tap interval must not include test-runner delays.
  await page.touchscreen.tap(rowBox!.x+rowBox!.width/2,rowBox!.y+rowBox!.height/2);
  await page.touchscreen.tap(rowBox!.x+rowBox!.width/2,rowBox!.y+rowBox!.height/2);
  const dialog=page.getByRole('dialog',{name:'重命名会话',exact:true});await expect(dialog).toBeVisible();
  await assertRenameSelection(page,'Codex 项目会话');
  expect(app.commands.filter(c=>c.type==='switch_session')).toHaveLength(switches);
  await page.getByRole('textbox',{name:'会话标题',exact:true}).fill('新的 Codex 标题');
  await dialog.getByRole('button',{name:'保存',exact:true}).tap();await expect(dialog).toHaveCount(0);
  expect(app.commands.findLast(c=>c.type==='rename_session')).toMatchObject({session_id:'codex@native',engine:'codex',title:'新的 Codex 标题'});
  await expect(page.locator('.sessions')).toHaveClass(/show/);
});

test('both agents menu rename selects the old title and dismisses without changing it',async({page},testInfo)=>{
  const app=await openApp(page);await page.getByRole('button',{name:'打开会话',exact:true}).tap();
  const switches=app.commands.filter(c=>c.type==='switch_session').length;
  const row=page.locator(`[data-session-id="${SID}"]`);
  await row.getByRole('button',{name:'更多操作',exact:true}).tap();await row.getByRole('button',{name:'重命名',exact:true}).tap();
  await assertRenameSelection(page,'移动端测试');
  await page.waitForFunction(()=>document.querySelector(".rename-session-sheet")?.getAnimations().every(animation=>animation.playState!=="running"));
  if(testInfo.project.name === 'ipad-webkit'){
    const box=await page.getByRole('dialog',{name:'重命名会话',exact:true}).boundingBox();
    expect(Math.abs(box!.x+box!.width/2-page.viewportSize()!.width/2)).toBeLessThan(2);
  }
  await page.screenshot({path:`../work/remote-acceptance/identity-rename-${testInfo.project.name}.png`});
  await page.getByRole('textbox',{name:'会话标题',exact:true}).fill('不提交的修改');
  await page.locator('.rename-session-scrim').tap({position:{x:5,y:5}});
  await expect(page.getByRole('dialog',{name:'重命名会话',exact:true})).toHaveCount(0);
  expect(app.commands.filter(c=>c.type==='rename_session')).toHaveLength(0);
  expect(app.commands.filter(c=>c.type==='switch_session')).toHaveLength(switches);
});

test('desktop keeps double click rename independent from IME confirmation and Enter save',async({page})=>{
  const app=await openApp(page);await page.getByRole('button',{name:'打开会话侧边栏',exact:true}).click();
  const switches=app.commands.filter(c=>c.type==='switch_session').length;
  await page.locator(`[data-session-id="${SID}"]`).dblclick();await assertRenameSelection(page,'移动端测试');
  const input=page.getByRole('textbox',{name:'会话标题',exact:true});
  await input.dispatchEvent('compositionstart');await input.fill('中文标题');
  await input.dispatchEvent('keydown',{key:'Enter',code:'Enter',keyCode:229,isComposing:true});
  await expect(page.getByRole('dialog',{name:'重命名会话',exact:true})).toBeVisible();
  expect(app.commands.filter(c=>c.type==='rename_session')).toHaveLength(0);
  expect(app.commands.filter(c=>c.type==='switch_session')).toHaveLength(switches);
  await input.dispatchEvent('compositionend');await page.waitForTimeout(50);await input.press('Enter');
  await expect.poll(()=>app.commands.filter(c=>c.type==='rename_session').length).toBe(1);
  expect(app.commands.findLast(c=>c.type==='rename_session')).toMatchObject({title:'中文标题'});
});


test('both agents clipboard reply during the double-tap window stays a draft and never sends',async({page})=>{
  const app=await openApp(page);await stubClipboard(page,"","pending");
  await page.getByRole('button',{name:'粘贴并发送',exact:true}).tap();
  await page.getByRole('button',{name:'打开会话',exact:true}).tap();
  await page.locator('.scard').filter({hasText:'另一目录的正在运行任务'}).tap();
  await page.evaluate(()=>(window as unknown as {resolveClipboard:(text:string)=>void}).resolveClipboard('导航期间到达的草稿'));
  await expect(page.locator('.surface-head-title')).toHaveText('另一目录的正在运行任务');
  expect(app.commands.some(c=>['query','interrupt'].includes(c.type as string))).toBe(false);
  const history=await openDraftHistory(page);await expect(history).toContainText('导航期间到达的草稿');
});


test('both agents Amo links copy the correct native and remote targets without sending commands',async({page})=>{
  const nativeId='01900000-0000-7000-8000-000000000001';
  const app=await openApp(page,{codex:true,codexId:`codex@${nativeId}`});
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async(text:string)=>{(window as unknown as {copiedLink:string}).copiedLink=text;}}}));
  await page.getByRole('button',{name:'打开会话侧边栏',exact:true}).click();
  const codex=page.locator(`[data-session-id="codex@${nativeId}"]`);
  await codex.getByRole('button',{name:'更多操作',exact:true}).click();
  const menuBox=await codex.locator('.card-menu').boundingBox();
  const listBox=await page.locator('.s-scroll').boundingBox();
  expect(menuBox!.y).toBeGreaterThanOrEqual(listBox!.y);
  expect(menuBox!.y+menuBox!.height).toBeLessThanOrEqual(listBox!.y+listBox!.height);
  await codex.getByRole('button',{name:'复制 Codex 链接',exact:true}).click();
  expect(await page.evaluate(()=>(window as unknown as {copiedLink:string}).copiedLink)).toBe(`codex://threads/${nativeId}`);
  await expect(codex.getByRole('button',{name:'已复制 Codex 链接',exact:true})).toBeVisible();
  await codex.getByRole('button',{name:'复制 Amo 会话链接',exact:true}).click();
  let link=await page.evaluate(()=>(window as unknown as {copiedLink:string}).copiedLink);
  expect(JSON.parse(decodeURIComponent(new URL(link).hash.slice('#notification='.length)))).toMatchObject({machine_id:'default',engine:'codex',session_id:`codex@${nativeId}`});
  await codex.getByRole('button',{name:'更多操作',exact:true}).click();
  const dsh=page.locator(`[data-session-id="${SID}"]`);
  await dsh.getByRole('button',{name:'更多操作',exact:true}).click();
  await expect(dsh.getByRole('button',{name:'复制 Codex 链接',exact:true})).toHaveCount(0);
  await dsh.getByRole('button',{name:'复制 Amo 会话链接',exact:true}).click();
  link=await page.evaluate(()=>(window as unknown as {copiedLink:string}).copiedLink);
  expect(JSON.parse(decodeURIComponent(new URL(link).hash.slice('#notification='.length)))).toMatchObject({machine_id:'default',engine:'dsh',session_id:SID});
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('denied');}}}));
  await dsh.getByRole('button',{name:'已复制 Amo 链接',exact:true}).click();
  await expect(dsh.getByRole('alert')).toContainText('无法写入剪贴板');
  await expect(dsh.getByRole('button',{name:'已复制 Amo 链接',exact:true})).toHaveCount(0);
  expect(app.commands.some(c=>['query','interrupt','rename_session','new_session'].includes(String(c.type)))).toBe(false);
});

test('both agents Amo deep link resolves a Codex session against an authorized fresh catalog',async({page})=>{
  const nativeId='01900000-0000-7000-8000-000000000001';
  const route={machine_id:'default',engine:'codex',space:'code',session_id:`codex@${nativeId}`};
  const app=await openApp(page,{codex:true,codexId:route.session_id,fragment:'#notification='+encodeURIComponent(JSON.stringify(route))});
  await expect(page.locator('.surface-head-title')).toHaveText('Codex 项目会话');
  expect(app.commands.findLast(c=>c.type==='switch_session')).toMatchObject({engine:'codex',session_id:route.session_id});
  expect(app.commands.some(c=>['query','interrupt','new_session'].includes(String(c.type)))).toBe(false);
  expect(new URL(page.url()).hash).toBe('');
});

test('both agents Amo login preserves authentication, password visibility and keyboard access',async({page})=>{
  let logins=0;
  await page.route('**/api/**',async(route)=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/login'){logins++;expect(route.request().postDataJSON()).toMatchObject({password:'test-only'});await route.fulfill({status:401,json:{detail:'Invalid password'}});}
    else await route.fulfill({status:path==='/api/session'?401:200,json:path==='/api/auth-config'?{multi_user:false}:{authenticated:false}});
  });
  await page.goto('/');
  await expect(page.locator('.login-brand')).toContainText('Amo');
  const input=page.getByLabel('访问密码',{exact:true});await input.fill('test-only');
  await page.getByRole('button',{name:'显示密码',exact:true}).click();await expect(input).toHaveAttribute('type','text');
  await page.getByRole('button',{name:'隐藏密码',exact:true}).click();await expect(input).toHaveAttribute('type','password');
  await input.press('Enter');await expect.poll(()=>logins).toBe(1);await expect(page.getByRole('alert')).toBeVisible();
  const mark=page.locator('.login .amo-mark-vector svg');await expect(mark).toBeVisible();
  expect(await mark.locator('path').count()).toBeGreaterThan(0);
  await page.setViewportSize({width:390,height:330});
  await page.getByRole('button',{name:'进入 Amo',exact:true}).scrollIntoViewIfNeeded();
  const button=await page.getByRole('button',{name:'进入 Amo',exact:true}).boundingBox();expect(button!.y+button!.height).toBeLessThanOrEqual(330);
});


test('both agents Amo layout preserves aligned metadata and reachable actions across themes',async({page},testInfo)=>{
  await openApp(page,{codex:true});await page.getByRole('button',{name:'打开会话侧边栏',exact:true}).click();
  for(const [appearance,layout,theme] of [['blue','classic','light'],['blue','classic','dark'],['native','native','light'],['terminal','terminal','dark'],['engineering','engineering','light'],['pixel-workbench','pixel','dark']]){
    await page.evaluate(([appearance,layout,theme])=>{const root=document.documentElement;root.dataset.appearance=appearance;root.dataset.layout=layout;root.dataset.theme=theme;},[appearance,layout,theme]);
    await page.waitForFunction(()=>getComputedStyle(document.querySelector('.sessions')!).transform==='none');
    const row=page.locator(`[data-session-id="${SID}"]`);
    const boxes=await row.evaluate(el=>{
      const box=(selector:string)=>el.querySelector(selector)!.getBoundingClientRect();
      const title=box('.scard-top'),meta=box('.scard-meta'),status=box('.scard-status'),folder=box('.scard-location'),agent=box('.scard-engine'),actions=box('.scard-actions'),card=el.getBoundingClientRect();
      return {aligned:Math.max(title.x,meta.x,status.x)-Math.min(title.x,meta.x,status.x),agentGap:agent.x-(folder.x+folder.width),actionsFit:actions.x+actions.width<=card.x+card.width,cardHeight:card.height};
    });
    expect(boxes.aligned).toBeLessThan(1);expect(boxes.agentGap).toBeLessThan(16);expect(boxes.actionsFit).toBe(true);expect(boxes.cardHeight).toBeLessThan(110);
    await page.screenshot({path:`../work/remote-acceptance/amo-sidebar-${testInfo.project.name}-${appearance}-${theme}.png`});
  }
});

test('both agents Amo live history loads even when Safari cache opening never finishes',async({page})=>{
  const app=await openApp(page,{blockReplayCache:true});
  await expect(page.locator('[data-turn-id="history-23"]')).toBeVisible();
  expect(app.commands.some(cmd=>cmd.type==='hello')).toBe(true);
  expect(app.commands.some(cmd=>cmd.type==='get_history')).toBe(true);
});

test('both agents Amo cache replay and eviction never deserialize every conversation',async({page})=>{
  await openApp(page);
  const result=await page.evaluate(async()=>{
    const modulePath='/src/cache.ts';const cache=await import(modulePath);
    await cache.clearCache();
    const database=await new Promise<IDBDatabase>((resolve,reject)=>{const req=indexedDB.open('cc_remote_cache');req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});
    const payload='缓存测试'.repeat(24000);
    await new Promise<void>((resolve,reject)=>{
      const tx=database.transaction(['sessions','replay'],'readwrite');
      for(let i=0;i<64;i++){
        const id='cache-perf-'+i;const metadata={v:18,sid:id,lastSeq:i+1,revision:'fixture',savedAt:i+1};
        tx.objectStore('sessions').put({...metadata,turns:[{id:'t-'+i,prompt:'测试',done:true,blocks:[{kind:'text',channel:'final',message_id:'a-'+i,text:payload,done:true}]}]},id);
        tx.objectStore('replay').put(metadata,id);
      }
      tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);
    });
    let bodyReads=0;
    const original=IDBObjectStore.prototype.openCursor;
    IDBObjectStore.prototype.openCursor=function(...args){const req=original.apply(this,args);if(this.name==='sessions')req.addEventListener('success',()=>{if(req.result)bodyReads++;});return req;};
    const replay=await cache.loadAllReplayState();
    cache.saveSession('cache-new-a',[{id:'new-a',prompt:'新消息',done:true,blocks:[]}],100,'fixture');
    cache.saveSession('cache-new-b',[{id:'new-b',prompt:'新消息',done:true,blocks:[]}],101,'fixture');
    let saved=false;
    for(let attempt=0;attempt<60;attempt++){
      if((await cache.loadAllReplayState()).cursors['cache-new-b']===101){saved=true;break;}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    const count=await new Promise<number>(resolve=>{const req=database.transaction('sessions').objectStore('sessions').count();req.onsuccess=()=>resolve(req.result);});
    const preserved=await cache.loadSession('cache-perf-2');
    await cache.invalidateSessionCache('cache-new-b');
    const afterInvalidation=await cache.loadAllReplayState();
    await cache.clearCache();const cleared=await cache.loadAllReplayState();
    database.close();IDBObjectStore.prototype.openCursor=original;
    return {seeded:Object.keys(replay.cursors).length,bodyReads,saved,count,preserved:!!preserved,
      invalidated:afterInvalidation.cursors['cache-new-b']===undefined,cleared:Object.keys(cleared.cursors).length};
  });
  expect(result).toEqual({seeded:64,bodyReads:0,saved:true,count:64,preserved:true,invalidated:true,cleared:0});
});

test('desktop keeps a flush sidebar edge across every layout and theme',async({page})=>{
  await openApp(page,{codex:true});
  await page.getByRole('button',{name:'打开会话侧边栏',exact:true}).click();
  await page.waitForFunction(()=>getComputedStyle(document.querySelector('.sessions')!).transform==='none');
  for(const layout of ['classic','native','engineering','terminal','pixel'])for(const theme of ['light','dark']){
    const geometry=await page.evaluate(({layout,theme})=>{
      const root=document.documentElement;root.dataset.layout=layout;root.dataset.appearance=layout==='classic'?'blue':layout;root.dataset.theme=theme;
      const sidebar=document.querySelector('.sessions')!,pane=document.querySelector('.pane')!;
      const style=getComputedStyle(sidebar);
      return {gap:pane.getBoundingClientRect().left-sidebar.getBoundingClientRect().right,left:style.borderLeftWidth,right:style.borderRightWidth,shadow:style.boxShadow};
    },{layout,theme});
    expect(Math.abs(geometry.gap)).toBeLessThan(.5);
    expect(geometry.left).toBe('0px');expect(geometry.right).toBe('0px');expect(geometry.shadow).toBe('none');
  }
});

async function openSessionDrawer(page: Page) {
  if (await page.locator('.sessions').evaluate(el => el.classList.contains('show'))) return;
  const name = test.info().project.name === 'desktop' ? '打开会话侧边栏' : '打开会话';
  await page.getByRole('button', {name, exact:true}).click();
}

test('both agents Amo immediately lists a created chat before either catalog returns',async({page})=>{
  const app=await openApp(page,{codex:true});
  await openSessionDrawer(page);
  await page.getByRole('button',{name:'新会话',exact:true}).click();
  await page.locator('.newchat-input').fill('无需等待目录即可看到新会话');
  app.holdRosters();
  await page.getByRole('button',{name:'开始',exact:true}).click();
  await expect(page.locator('.thread')).toContainText('无需等待目录即可看到新会话');
  if(!await page.locator('.sessions').evaluate(el=>el.classList.contains('show')))
    await openSessionDrawer(page);
  await expect(page.locator('.sessions [data-session-id="tmp-created"]')).toBeVisible();
  app.rekeyCreated();
  await expect(page.locator('.sessions [data-session-id="dsh@created-fixture"]')).toBeVisible();
  await expect(page.locator('.sessions [data-session-id="tmp-created"]')).toHaveCount(0);
  app.holdRosters(false); app.refreshRoster();
  await expect(page.locator('.sessions [data-session-id="dsh@created-fixture"]')).toBeVisible();
});

test('both agents Amo immediately lists a Codex fork while native catalogs lag',async({page})=>{
  const app=await openApp(page,{codex:true,forkable:true});
  await openSessionDrawer(page);
  await page.locator('.sessions [data-session-id="codex@native"]').click();
  await expect(page.locator('html')).toHaveAttribute('data-engine','codex');
  app.holdRosters();
  await page.getByRole('button',{name:'派生',exact:true}).last().click();
  await expect.poll(()=>app.commands.filter(c=>c.type==='fork_session').length).toBe(1);
  if(!await page.locator('.sessions').evaluate(el=>el.classList.contains('show')))
    await openSessionDrawer(page);
  await expect(page.locator('.sessions [data-session-id="codex@forked-fixture"]')).toBeVisible();
  await expect(page.locator('.sessions [data-session-id="codex@forked-fixture"] .scard-title')).toContainText('派生会话');
  await page.locator('.sessions').getByRole('button',{name:'收起',exact:true}).click();
  await page.locator('.composer textarea').fill('继续派生会话');
  await page.getByRole('button',{name:'发送',exact:true}).click();
  await expect.poll(()=>app.commands.filter(c=>c.type==='query').at(-1)?.sid).toBe('codex@forked-fixture');
});

test('both agents Amo Codex asks receive scoped answers and queued guide sends immediately',async({page})=>{
  const app=await openApp(page,{codex:true});
  await openSessionDrawer(page);
  await page.locator('.sessions [data-session-id="codex@native"]').click();
  await expect(page.locator('html')).toHaveAttribute('data-engine','codex');
  app.acceptAnswers();app.ask();
  const question=page.getByRole('dialog',{name:'操作确认'});
  await expect(question).toBeVisible();
  await question.getByRole('button',{name:'继续',exact:true}).click();
  await expect(question).toHaveCount(0);
  expect(app.commands.find(c=>c.type==='answer_question')).toMatchObject({sid:'codex@native',answer:'继续'});
  app.start();
  await page.locator('.composer textarea').fill('排队后立即引导');
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const chip=page.getByRole('button',{name:'编辑待发送消息'});
  await expect(chip).toBeVisible();
  await chip.dispatchEvent('pointerdown',{button:0,clientX:100,clientY:100});
  await page.waitForTimeout(550);await chip.dispatchEvent('pointerup');
  const editor=page.getByRole('dialog',{name:'待发送消息'});
  await editor.getByRole('button',{name:'引导当前任务'}).click();
  await expect.poll(()=>app.commands.filter(c=>c.type==='update_queued_query'&&c.start_now).length).toBe(1);
  await expect(editor).toHaveCount(0);
  expect(app.commands.filter(c=>c.type==='query').at(-1)).toMatchObject({sid:'codex@native',delivery:'queue'});
});

for (const engine of ['dsh','codex'] as const) {
  test(`both agents Amo ${engine} queue refresh restores state and editing pauses dispatch`, async ({page}) => {
    const app=await openApp(page,{codex:true});
    if(engine==='codex') {
      await openSessionDrawer(page);
      await page.locator('[data-session-id="codex@native"] .scard-title').click();
      await expect.poll(()=>app.commands.some(cmd=>cmd.type==='get_history' && cmd.session_id==='codex@native')).toBe(true);
      await expect(page.locator('html')).toHaveAttribute('data-engine','codex');
    }
    app.start();
    await expect(page.locator('.hstat')).toContainText('running');
    await page.locator('.composer textarea').fill('跨端恢复的完整待发送消息');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await expect(page.getByRole('button',{name:'编辑待发送消息'})).toBeVisible();
    // A fresh App has neither a queue reducer nor local pending-send memory.
    if(engine==='codex') await page.evaluate(()=>history.replaceState(null,'','#notification='+encodeURIComponent(JSON.stringify({machine_id:'default',engine:'codex',space:'code',session_id:'codex@native'}))));
    await page.reload();
    await expect(page.getByRole('button',{name:'编辑待发送消息'})).toBeVisible();
    await page.getByRole('button',{name:'编辑待发送消息'}).click();
    const sheet=page.getByRole('dialog',{name:'待发送消息',exact:true});
    await expect(sheet.getByLabel('编辑排队消息')).toHaveValue('跨端恢复的完整待发送消息');
    expect(app.commands.findLast(cmd=>cmd.type==='get_queued_query')).toMatchObject({edit_action:'begin'});
    app.finish();
    await expect(sheet).toBeVisible();
    await expect(page.getByRole('button',{name:'编辑待发送消息'})).toBeVisible();
    await sheet.getByLabel('编辑排队消息').fill('编辑完成后的消息');
    await sheet.getByRole('button',{name:'保存修改',exact:true}).click();
    await expect(sheet).toHaveCount(0);
    await expect(page.getByRole('button',{name:'编辑待发送消息'})).toHaveCount(0);
    await expect(page.locator('.thread')).toContainText('编辑完成后的消息');
  });
}

test('both agents Amo compact footer and running tail have aligned controls',async({page})=>{
  const app=await openApp(page,{codex:true});
  await openSessionDrawer(page);
  const foot=page.locator('.sessions .s-foot');
  const chips=foot.locator('.agent-filters');const create=foot.getByRole('button',{name:'新会话',exact:true});
  const [a,b]=await Promise.all([chips.boundingBox(),create.boundingBox()]);
  expect(Math.abs((a!.y+a!.height/2)-(b!.y+b!.height/2))).toBeLessThan(1);
  expect(b!.width).toBeLessThan(120);
  expect(await page.locator('.s-inner>.agent-filters').count()).toBe(0);
  await page.getByRole('button',{name:'收起',exact:true}).click();
  app.emit({type:'user_msg',msg_id:'stable-tail',prompt:'检查思考时输入'});
  app.emit({type:'state',state:'running'});
  app.emit({type:'process',item_id:'stable-thought',kind:'reasoning',phase:'start',status:'running',title:'思考',summary:'保持可见的思考摘要'});
  const row=page.locator('[data-turn-id="stable-tail"] .turn-tail-controls');
  await expect(row.getByRole('button',{name:'收起本轮回答'})).toBeVisible();
  const [status,fold]=await Promise.all([row.locator('.turn-working').boundingBox(),row.locator('button').boundingBox()]);
  expect(Math.abs((status!.y+status!.height/2)-(fold!.y+fold!.height/2))).toBeLessThan(1);
  const input=page.locator('.composer textarea');await input.focus();await page.waitForTimeout(400);
  await page.evaluate(()=>{
    const state={samples:[] as number[],running:true};
    (window as unknown as {typingSamples:typeof state}).typingSamples=state;
    const sample=()=>{if(!state.running)return;state.samples.push(document.querySelector('[data-turn-id="stable-tail"]')!.getBoundingClientRect().top);requestAnimationFrame(sample);};sample();
  });
  await input.pressSequentially('测试输入不应反复改变同一行的高度',{delay:35});
  const samples=await page.evaluate(()=>{const state=(window as unknown as {typingSamples:{samples:number[],running:boolean}}).typingSamples;state.running=false;return state.samples;});
  expect(Math.max(...samples)-Math.min(...samples)).toBeLessThan(2);
  const device=page.getByRole('button',{name:'设备中心',exact:true});
  expect(await device.evaluate(el=>getComputedStyle(el).borderTopWidth)).toBe('0px');
});

test('desktop keeps a shared sidebar boundary throughout opening and closing',async({page})=>{
  await openApp(page);
  await page.evaluate(()=>document.querySelector('.shell')!.classList.remove('sidebar-open'));
  await page.waitForTimeout(400);
  const samples=await page.evaluate(async()=>{
    const shell=document.querySelector('.shell')!;const sidebar=document.querySelector('.sessions')!;const pane=document.querySelector('.pane')!;
    const gaps:number[]=[];
    for(const opening of [true,false]){
      shell.classList.toggle('sidebar-open',opening);
      const start=performance.now();
      while(performance.now()-start<350){await new Promise(requestAnimationFrame);gaps.push(pane.getBoundingClientRect().left-sidebar.getBoundingClientRect().right);}
    }
    return gaps;
  });
  expect(Math.max(...samples.map(Math.abs))).toBeLessThan(.5);
});
