import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { APPEARANCES, type Appearance } from "../appearance";
import { Icon } from "../icons";
import type { NotificationMode } from "../notification-mode";
import type { PushBindingState } from "../push";

interface Props {
  theme: "light" | "dark";
  appearance?: Appearance;
  onAppearance?: (appearance: Appearance) => void;
  notificationMode: NotificationMode;
  notificationBinding: PushBindingState;
  notificationAvailable: boolean;
  onNotificationMode: (mode: NotificationMode) => Promise<boolean>;
  onToggleTheme: () => void;
  onLogout: () => void;
  onBrowseFiles?: () => void;
  onAccountUsage?: () => void;
  chatSpacing?: "compact" | "standard" | "relaxed";
  onChatSpacing?: (spacing: "compact" | "standard" | "relaxed") => void;
}

interface Position {
  top: number;
  right: number;
}

const MODE_LABELS: Record<NotificationMode, string> = {
  off: "关闭",
  generic: "通用提醒",
  session: "显示会话名称",
};

export function HeaderMenu({
  theme,
  appearance = "blue",
  onAppearance,
  notificationMode,
  notificationBinding,
  notificationAvailable,
  onNotificationMode,
  onToggleTheme,
  onBrowseFiles,
  onAccountUsage,
  chatSpacing = "compact", onChatSpacing,
  onLogout,
}: Props) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const firstRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<"main" | "notifications" | "appearance">("main");
  const [changingMode, setChangingMode] = useState(false);
  const [position, setPosition] = useState<Position>({ top: 58, right: 8 });

  const close = () => setOpen(false);
  const show = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      setPosition({
        top: Math.min(rect.bottom + 8, window.innerHeight - 24),
        right: Math.max(8, window.innerWidth - rect.right),
      });
    }
    setPage("main");
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(cardRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])',
      ) ?? []).filter((element) => !element.hasAttribute("hidden"));
      if (focusable.length === 0) {
        event.preventDefault();
        cardRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first
          || !cardRef.current?.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", close);
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const focusFrame = window.requestAnimationFrame(() => firstRef.current?.focus());
    return () => window.cancelAnimationFrame(focusFrame);
  }, [open, page]);

  const selectMode = async (mode: NotificationMode) => {
    if (changingMode || mode === notificationMode) return;
    setChangingMode(true);
    try {
      await onNotificationMode(mode);
    } finally {
      setChangingMode(false);
    }
  };

  const style = {
    "--header-menu-top": `${position.top}px`,
    "--header-menu-right": `${position.right}px`,
  } as CSSProperties;
  const bindingLabel = notificationMode === "off"
    ? "已关闭"
    : notificationBinding === "remote"
      ? "后台 Push"
      : notificationBinding === "binding"
        ? "正在同步"
        : "页面后台提醒";

  return (
    <>
      <button ref={triggerRef} type="button" className="iconbtn header-menu-trigger"
        data-lock-horizontal-swipe onClick={() => open ? close() : show()}
        aria-label="更多设置" aria-haspopup="dialog" aria-expanded={open}>
        <Icon name="dots" />
      </button>
      {open && typeof document !== "undefined" && createPortal(
        <div className="header-menu-scrim" data-lock-horizontal-swipe
          onClick={(event) => {
            if (event.target === event.currentTarget) close();
          }}>
          <section ref={cardRef} className="header-menu-card" style={style}
            role="dialog" aria-modal="true" aria-label="应用设置" tabIndex={-1}
            onPointerDown={(event) => event.stopPropagation()}>
            {page === "main" ? (
              <>
                <header className="header-menu-title">
                  <b>设置</b>
                </header>
                <div className="header-menu-items">
                  {onAccountUsage && <button type="button" className="header-menu-item"
                    onClick={() => { close(); onAccountUsage(); }}>
                    <Icon name="balance" size={18} />
                    <span><b>余额与额度</b><small>DSH 余额 · Codex 会员额度</small></span>
                  </button>}
                  {onBrowseFiles && <button type="button" className="header-menu-item"
                    onClick={() => { close(); onBrowseFiles(); }}>
                    <Icon name="folder-open" size={18} />
                    <span><b>项目文件</b><small>浏览与搜索当前目录</small></span>
                  </button>}
                  <button ref={firstRef}
                    type="button" className="header-menu-item"
                    onClick={() => setPage("notifications")}>
                    <Icon name="notify" size={18} />
                    <span><b>通知</b><small>{MODE_LABELS[notificationMode]} · {bindingLabel}</small></span>
                    <Icon name="chevron-right" size={16} />
                  </button>
                  <button type="button" className="header-menu-item"
                    onClick={() => setPage("appearance")}>
                    <Icon name={theme === "dark" ? "sun" : "moon"} size={18} />
                    <span><b>外观与主题</b><small>{APPEARANCES.find(item => item.id === appearance)?.name} · {theme === "dark" ? "深色" : "浅色"}</small></span>
                    <Icon name="chevron-right" size={16} />
                  </button>
                  <button type="button" className="header-menu-item danger"
                    onClick={onLogout}>
                    <Icon name="logout" size={18} />
                    <span><b>退出登录</b><small>清除本机缓存并断开连接</small></span>
                  </button>
                </div>
              </>
            ) : page === "appearance" ? (
              <>
                <header className="header-menu-title">
                  <button ref={firstRef} type="button" className="header-menu-back"
                    onClick={() => setPage("main")} aria-label="返回设置"><Icon name="back" size={17} /></button>
                  <b>外观与主题</b>
                </header>
                <div className="appearance-modes" role="group" aria-label="明暗模式">
                  <button aria-pressed={theme === "light"} onClick={() => {if(theme !== "light") onToggleTheme();}}><Icon name="sun" size={16}/>浅色</button>
                  <button aria-pressed={theme === "dark"} onClick={() => {if(theme !== "dark") onToggleTheme();}}><Icon name="moon" size={16}/>深色</button>
                </div>
                <div className="chat-spacing-setting">
                  <span>聊天行距</span>
                  <div role="group" aria-label="聊天行距">
                    {(["compact", "standard", "relaxed"] as const).map(value => <button key={value}
                      aria-pressed={chatSpacing === value} onClick={() => onChatSpacing?.(value)}>
                      {{compact:"紧凑",standard:"标准",relaxed:"宽松"}[value]}
                    </button>)}
                  </div>
                </div>
                <div className="appearance-groups">
                  {[true, false].map(designed => <section key={String(designed)} className="appearance-group">
                    <h3>{designed ? "界面设计" : "原有主题"}</h3>
                    <div className="appearance-options" role="radiogroup" aria-label={designed ? "界面设计" : "原有主题"}>
                  {APPEARANCES.filter(item => !!item.layout === designed).map(item => { const colors = theme === "dark" ? item.darkColors : item.colors; return <button key={item.id} type="button" role="radio"
                    aria-checked={appearance === item.id} aria-label={item.name}
                    className={`appearance-option ${item.id}`} onClick={() => onAppearance?.(item.id)}>
                    <span className="appearance-preview" style={{background:colors[0], color:colors[1]}} aria-hidden="true">
                      <span className="appearance-preview-pixels" style={{background:colors[1],boxShadow:`8px 0 ${colors[2]},0 8px ${colors[2]},8px 8px ${colors[1]}`}}/>
                      <i style={{background:colors[1]}}/><i style={{background:colors[2]}}/>
                      {item.layout && <span className="appearance-preview-composer"/>}
                    </span>
                    <span className="appearance-copy"><b>{item.name}</b><small>{item.description}</small></span>
                    <span className="appearance-check" aria-hidden="true">{appearance === item.id && <Icon name="check" size={15}/>}</span>
                  </button>; })}
                    </div>
                  </section>)}
                </div>
                <p className="appearance-note">即时预览，自动记住你的选择</p>
              </>
            ) : (
              <>
                <header className="header-menu-title">
                  <button ref={firstRef} type="button" className="header-menu-back"
                    onClick={() => setPage("main")} aria-label="返回设置">
                    <Icon name="back" size={17} />
                  </button>
                  <b>完成通知</b>
                </header>
                <p className="header-menu-help">
                  通用提醒不会把会话名称或编号发送给 Push 服务；会话提醒可从通知精确打开对应对话。
                </p>
                {!notificationAvailable && (
                  <p className="header-menu-warning">当前浏览器不支持系统通知。</p>
                )}
                <div className="header-menu-modes" role="radiogroup" aria-label="通知隐私模式">
                  {(["off", "generic", "session"] as const).map((mode) => (
                    <button key={mode} type="button" role="radio"
                      aria-checked={notificationMode === mode}
                      disabled={changingMode || (!notificationAvailable && mode !== "off")}
                      onClick={() => void selectMode(mode)}>
                      <span className="header-menu-radio" aria-hidden="true" />
                      <span>
                        <b>{MODE_LABELS[mode]}</b>
                        <small>{mode === "off"
                          ? "不发送系统通知"
                          : mode === "generic"
                            ? "仅显示完成、失败或中断"
                            : "显示安全截断的会话名称，并支持精确跳转"}</small>
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </section>
        </div>,
        document.body,
      )}
    </>
  );
}
