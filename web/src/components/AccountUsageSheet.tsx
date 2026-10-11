import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "../icons";
import type { AccountUsageReport, EngineInfo, StatusRateWindow } from "../protocol";
import { AgentBadge } from "./AgentBadge";
import { resetCountdown } from "../account-reset";

type Agent = "dsh" | "codex";
interface Props {
  engines: EngineInfo[];
  reports: Partial<Record<Agent, AccountUsageReport>>;
  onRequest: (engine: Agent) => string | null;
  onClose: () => void;
  serviceReady: boolean;
}
function windowLabel(window: StatusRateWindow): string {
  const minutes = window.window_duration_mins;
  if (!minutes) return "额度窗口";
  if (minutes === 10080) return "每周额度";
  if (minutes % 1440 === 0) return `${minutes / 1440} 天额度`;
  if (minutes % 60 === 0) return `${minutes / 60} 小时额度`;
  return `${minutes} 分钟额度`;
}
const money = (value: string) => Number(value).toLocaleString(undefined, {maximumFractionDigits: 4});

export function AccountUsageSheet({engines, reports, onRequest, onClose, serviceReady}: Props) {
  const requestRef = useRef(onRequest);
  requestRef.current = onRequest;
  const [requests, setRequests] = useState<Partial<Record<Agent, string | null>>>({});
  const [expired, setExpired] = useState(false);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const tick = () => { if (!document.hidden) setNow(Date.now()); };
    const timer = window.setInterval(tick, 60_000);
    document.addEventListener("visibilitychange", tick);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", tick); };
  }, []);
  const availableRef = useRef(engines);
  availableRef.current = engines;
  const availability = (["dsh", "codex"] as const).map(agent =>
    engines.some(item => item.id === agent && item.available) ? agent : "").join(":");
  const refresh = useCallback(() => {
    const next: Partial<Record<Agent, string | null>> = {};
    for (const engine of ["dsh", "codex"] as const) {
      if (availableRef.current.some(item => item.id === engine && item.available)) next[engine] = requestRef.current(engine);
    }
    setExpired(false); setRequests(next);
  }, []);
  useEffect(() => { refresh(); }, [refresh, serviceReady, availability]);
  useEffect(() => {
    const timeout = window.setTimeout(() => setExpired(true), 30000);
    return () => window.clearTimeout(timeout);
  }, [requests]);
  useEffect(() => {
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); }
    };
    window.addEventListener("keydown", dismiss);
    return () => window.removeEventListener("keydown", dismiss);
  }, [onClose]);
  return <>
    <div className="scrim show" onClick={onClose} />
    <section className="sheet show account-usage-sheet" role="dialog" aria-modal="true" aria-label="余额与额度">
      <div className="sheet-grip" />
      <header className="queued-query-head">
        <b className="sheet-title">余额与额度</b>
        <div className="queued-query-head-actions">
          <button type="button" className="iconbtn" onClick={refresh} aria-label="刷新余额与额度"><Icon name="refresh" /></button>
          <button type="button" className="iconbtn" onClick={onClose} aria-label="关闭余额与额度"><Icon name="close" /></button>
        </div>
      </header>
      <div className="account-usage-body">
        {(["dsh", "codex"] as const).map(engine => {
          const request = requests[engine];
          const report = reports[engine]?.request_id === request ? reports[engine] : undefined;
          const connected = engines.some(item => item.id === engine && item.available);
          const loading = request && !report && !expired;
          const reason = !serviceReady ? "额度读取服务尚未就绪，请稍后刷新"
            : !connected ? "这台设备未连接该 Agent"
            : !request ? "请求未发送，请检查连接后刷新"
            : !report ? "暂未收到结果，请刷新重试"
            : report.reason === "signed_out" ? "本机未登录 DeepSeek 账户；API Key 余额请在对应平台查看"
            : report.reason === "api_key" ? "当前使用 API Key，会员额度不适用；费用请在 API 平台查看"
            : report.reason === "not_provided" ? "该账户未提供可读取的余额或额度"
            : "暂时无法读取，请稍后刷新";
          return <article className="account-usage-card" key={engine}>
            <header><AgentBadge engine={engine} />{report?.plan && <span className="account-plan">{report.plan}</span>}</header>
            {loading ? <p role="status">正在读取…</p> : !report?.available ? <p role="status">{reason}</p> : engine === "dsh" ? <>
              {report.balances.map(balance => <details key={balance.currency} className="account-wallet">
                <summary aria-label={`${balance.currency} 余额详情`}>
                  <span>可用余额</span><strong title={`${balance.total} ${balance.currency}`}>{balance.currency === "CNY" ? "¥" : "$"}{money(balance.total)}</strong>
                  <Icon name="chev" size={14} />
                </summary>
                <div className="account-wallet-details">
                  <div><span>充值余额</span><b>{money(balance.recharge)} {balance.currency}</b></div>
                  <div><span>赠金余额</span><b>{money(balance.bonus)} {balance.currency}</b></div>
                  <div><span>今日开销</span><span>未提供</span></div>
                  <div><span>本周开销</span><span>未提供</span></div>
                  <p>当前 DSH 账户接口未提供消费明细，可在官方账单中查看。</p>
                  <a href="https://platform.deepseek.com/usage" target="_blank" rel="noopener noreferrer">查看官方账单 <Icon name="chevron-right" size={12} /></a>
                </div>
              </details>)}
              <p className="account-usage-note">DeepSeek 登录账户余额；其他 API Key 或服务商的余额可能不同。</p>
            </> : <>
              {report.limits.map((limit, index) => <div key={`${limit.limit_id}:${index}`}>
                {report.limits.length > 1 && <small>{limit.limit_name || limit.limit_id || "Codex"}</small>}
                {([limit.primary, limit.secondary]).filter((item): item is StatusRateWindow => !!item).map((window, i) => {
                  const remaining = window.used_percent == null ? null : Math.max(0, 100 - window.used_percent);
                  return <div className="account-limit" key={i}>
                    <div><span>{windowLabel(window)}</span><strong>{remaining === null ? "未提供比例" : `剩余 ${remaining}%`}</strong></div>
                    {remaining !== null && <div className="account-limit-track" role="progressbar" aria-label={windowLabel(window)} aria-valuemin={0} aria-valuemax={100} aria-valuenow={remaining}><i style={{width:`${remaining}%`}} /></div>}
                    {window.resets_at != null && <>
                      <small className="account-reset-countdown">{resetCountdown(window.resets_at, now)}</small>
                      <small>重置时间 {new Date(window.resets_at * 1000).toLocaleString()}</small>
                    </>}
                  </div>;
                })}
              </div>)}
              <p className="account-usage-note">按账户返回的额度窗口显示；剩余额度不等同于余额。</p>
            </>}
            {report && <small>读取于 {new Date(report.ts * 1000).toLocaleTimeString()}</small>}
          </article>;
        })}
      </div>
    </section>
  </>;
}
