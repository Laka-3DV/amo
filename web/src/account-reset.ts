/** Quotas reset at the provider timestamp, never a locally assumed weekday. */
export function resetCountdown(resetsAt: number, now: number): string {
  const seconds = Math.ceil(resetsAt - now / 1000);
  if (seconds <= 0) return "已到重置时间，请刷新额度";
  const minutes = Math.ceil(seconds / 60);
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor(minutes % 1440 / 60);
  const rest = minutes % 60;
  return `${days ? `${days} 天 ` : ""}${days || hours ? `${hours} 小时 ` : ""}${rest} 分钟后重置`;
}
