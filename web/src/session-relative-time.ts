import { sessionActivityTime } from "./session-order.ts";

export function activityTimestamp(value?: string | null): number | null {
  const stamp=sessionActivityTime(value);
  return Number.isFinite(stamp) && stamp > 0 && Number.isFinite(new Date(stamp).getTime()) ? stamp : null;
}

export function relativeActivity(value: string | null | undefined, now = Date.now()): string {
  return relativeActivityAt(activityTimestamp(value), now);
}

export function relativeActivityAt(stamp: number | null, now = Date.now()): string {
  if(stamp === null || !Number.isFinite(stamp) || stamp <= 0 || !Number.isFinite(new Date(stamp).getTime())) return "时间未知";
  const elapsed=Math.max(0,now-stamp);
  if(elapsed < 60_000) return "刚刚";
  if(elapsed < 3_600_000) return `${Math.floor(elapsed/60_000)} 分钟前`;
  if(elapsed < 86_400_000) return `${Math.floor(elapsed/3_600_000)} 小时前`;
  return `${Math.floor(elapsed/86_400_000)} 天前`;
}
