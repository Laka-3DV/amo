import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/index.css";
import "../src/motion.css";
import { SessionsSidebar } from "../src/components/SessionsSidebar";
import { ChatView } from "../src/components/ChatView";
import { Composer } from "../src/components/Composer";
import { ComposerDraftStore } from "../src/composer-drafts";
import type { Turn } from "../src/domain/conversation";
import type { SessionInfo } from "../src/protocol";
import { useMobileViewport } from "../src/use-mobile-viewport";

const turns: Turn[] = Array.from({ length: 20 }, (_, index) => ({
  id: `turn-${index}`, prompt: `问题 ${index}`, done: true,
  blocks: [{ kind: "text", message_id: `text-${index}`, channel: "final",
    text: Array.from({ length: 4 }, () => `回答 ${index}，用于检查滚动与键盘后的布局。`).join("\n\n"),
    done: true }],
}));

export function SidebarFixture() {
  useMobileViewport();
  const chat = new URLSearchParams(window.location.search).has("chat");
  const [open, setOpen] = useState(!chat);
  const [draftStore] = useState(() => new ComposerDraftStore());
  const [selected, setSelected] = useState("");
  const [active, setActive] = useState<string | null>(
    new URLSearchParams(location.search).has("current") ? "session-0" : null);
  const [tick, setTick] = useState(0);
  const large = new URLSearchParams(location.search).has("large");
  const [seed] = useState(() => (): SessionInfo[] =>
    Array.from({ length: large ? 500 : 40 }, (_, index) => ({
      session_id: `session-${index}`,
      summary: `会话 ${index}${large && index % 3 === 0 ? "，这是一个较长的会话标题，检查自动换行以及动态高度测量，保证滚动时点到的条目与实际响应一致" : ""}`,
      cwd: `/workspace/project-${Math.floor(index / (new URLSearchParams(location.search).has("dense") ? 8 : 4))}`,
      last_modified: String(1000 - index),
      engine: "dsh",
      state: "idle",
    })));
  const [sessions, setSessions] = useState<SessionInfo[]>(() =>
    new URLSearchParams(window.location.search).has("empty") ? [] : seed());
  useEffect(() => {
    Object.assign(window, { __sidebar: {
      refresh: (id: string) => setSessions((current) => current.map((session) =>
        session.session_id === id
          ? { ...session, last_modified: "9000", state: "running" }
          : session)),
      setOpen,
      populate: () => setSessions(seed()),
      add: () => setSessions((current) => [...current, {
        session_id: "new-session", summary: "新会话", cwd: "/workspace/new",
        last_modified: "9999", engine: "dsh", state: "idle",
      }]),
      create: () => {
        setSessions(current => [...current, {
          session_id: "created-active", summary: "立即出现的新会话",
          cwd: "/workspace/project-100", last_modified: "10000",
          engine: "dsh", state: "running",
        }]);
        setActive("created-active");
      },
      tick: () => setTick(value => value + 1),
    } });
  }, [seed]);
  const noop = () => {};
  return <div className={`shell${open ? " sidebar-open" : ""}`}>
    <div className="pane">
      <output data-testid="selected">{selected}</output>
      <output data-testid="tick">{tick}</output>
      {chat && <>
        <ChatView sid="fixture" turns={turns} engine="dsh" />
        <Composer draftKey="fixture" draftStore={draftStore} engine="dsh"
          state="idle" connState="connected" wrapperOnline sendMode="steer"
          setSendMode={noop} queue={[]} pendingSend={null} failedDeferred={[]}
          unconfirmedQueued={[]} unconfirmedReplaceable={[]}
          queueCapacity={{}} replaceQueueCapacity={{}}
          model="deepseek-flash" effort="high" perm="danger-full-access"
          permOptions={null} editPrompt={null} onEditConsumed={noop}
          onSendQuery={() => true} onInterrupt={noop}
          onEnqueue={() => true} onSetPending={() => true} onRemoveQueued={noop}
          onInspectQueued={noop} onSetModel={noop} onSetEffort={noop}
          onSetPerm={noop} onClear={noop} onContext={noop} contextReport={null} />
      </>}
    </div>
    <SessionsSidebar open={open} engine="dsh" space="code" sessions={sessions}
      liveStates={Object.fromEntries(sessions.map(session => [session.session_id, session.state ?? "idle"]))}
      completionBadges={{}}
      activeSessionId={active} onSelect={setSelected} onNew={noop}
      onNewInDir={noop} onClose={() => setOpen(false)} onSpaceChange={noop}
      onRename={noop} onArchive={noop}
      onPin={(session, pinned) => setSessions((current) => current.map((s) =>
        s.session_id === session.session_id ? { ...s, pinned } : s))}
      onDelete={noop} />
  </div>;
}

createRoot(document.getElementById("root")!).render(<SidebarFixture />);
