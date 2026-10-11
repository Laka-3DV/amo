import type { ErrorMsg } from "./protocol";

const OWNERSHIP_GUIDANCE = /(本机终端|原生 .*CLI|Codex App|Claude TUI)/;
const INCOMPLETE_CLAUDE_SESSION =
  "Claude 会话历史不完整，无法恢复；可从会话菜单删除该条目。";
const PROVIDER_AUTH_TURN_FAILURE =
  "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。";
const DSH_IMAGE_INPUT_UNSUPPORTED = "当前 DSH 模型不支持图片输入。";
const LEGACY_CODEX_AUTH_TURN_FAILURE =
  "Codex 登录已失效或当前账号无权限，请重新登录后重试。";
const SAFE_TURN_FAILURE_MESSAGES = new Set([
  // Keep the former network copy safe for replayed rows from an older wrapper.
  "网络异常，连接失败，请重新尝试。",
  "网络连接异常，请检查网络后重试。",
  PROVIDER_AUTH_TURN_FAILURE,
  DSH_IMAGE_INPUT_UNSUPPORTED,
  "请求过于频繁或当前额度受限，请稍后重试。",
  "请求超时，请重新尝试。",
  "Codex 上游服务暂时不可用，请稍后重试。",
  "当前 Codex 模型不可用，请选择本机支持的模型后重试。",
]);

function safeTurnFailureMessage(message: string): string | null {
  const trimmed = message.trim();
  if (trimmed === LEGACY_CODEX_AUTH_TURN_FAILURE) {
    return PROVIDER_AUTH_TURN_FAILURE;
  }
  return SAFE_TURN_FAILURE_MESSAGES.has(trimmed) ? trimmed : null;
}

function ownershipMessage(message: string): string | null {
  if (!OWNERSHIP_GUIDANCE.test(message)) return null;
  // Ownership rejections are deliberately authored user copy.  Still remove
  // protocol/implementation nouns that should never become product UI.
  return message
    .replace(/\b(?:error|exception|traceback|crash)\b/gi, "问题")
    .replace(/\bcc_[a-z_]+\b/gi, "")
    .trim();
}

function protocolCommandMessage(message: string): string {
  const trimmed = message.trim();
  if (trimmed === "思考强度请求与当前会话引擎不匹配。"
      || trimmed === "权限模式请求与当前会话引擎不匹配。") {
    return "当前会话已切换，请重新打开后再修改设置。";
  }
  if (trimmed === "当前 DeepSeek Harness API 尚未提供取消归档。") {
    return "DSH 当前暂不支持取消归档。";
  }
  if (trimmed === "当前远程后端不支持该操作，请更新页面后重试。") {
    return "当前设备端暂不支持这项操作，请更新 Amo 后重试。";
  }
  if (/protocol (?:version )?(?:mismatch|upgrade required)/i.test(trimmed)) {
    return "页面版本已更新，请刷新后重试。";
  }
  return "当前操作与会话状态不一致，请重试。";
}

export function presentTurnProblem(error: Pick<ErrorMsg, "code" | "message">): string {
  if (error.message.trim() === DSH_IMAGE_INPUT_UNSUPPORTED) {
    return DSH_IMAGE_INPUT_UNSUPPORTED;
  }
  if (error.code === "busy") {
    return ownershipMessage(error.message)
      ?? "本次消息未发送，会话当前不可写，请稍后重试。";
  }
  if (error.code === "not_running") {
    return "本次消息未发送，会话暂时不可用，请重新进入后重试。";
  }
  if (error.code === "bad_prompt") {
    return "消息内容无法发送，请检查输入或附件后重试。";
  }
  if (error.code === "drain_timeout") {
    return "停止操作未及时完成，会话正在恢复。";
  }
  if (error.code === "cc_crash") {
    return safeTurnFailureMessage(error.message)
      ?? "本次回复未完成，请重试。";
  }
  return "本次回复未完成，请重试。";
}

export function presentCommandProblem(
  error: Pick<ErrorMsg, "code" | "message">,
): string {
  if (error.message.trim() === DSH_IMAGE_INPUT_UNSUPPORTED) {
    return DSH_IMAGE_INPUT_UNSUPPORTED;
  }
  switch (error.code) {
    case "wrapper_offline":
      return "设备正在重新连接…";
    case "invalid_cwd":
      return "所选目录不存在或不可用，请重新选择。";
    case "busy":
      return ownershipMessage(error.message)
        ?? "当前操作暂时无法执行，请稍后重试。";
    case "not_running":
      if (error.message === INCOMPLETE_CLAUDE_SESSION) {
        return INCOMPLETE_CLAUDE_SESSION;
      }
      return "当前会话暂时不可用，请重新进入后重试。";
    case "bad_prompt":
      return "输入内容无效，请检查后重试。";
    case "auth":
      return "当前操作不适用于这个会话。";
    case "protocol":
      return protocolCommandMessage(error.message);
    case "fork_reconciling":
      return "正在确认派生结果，请稍候…";
    default:
      return "操作未完成，请稍后重试。";
  }
}

export function presentHistoricalTurnProblem(message: string): string {
  const normalized = message.trim().toLowerCase();
  if (!normalized || normalized === "error") return "该轮未正常结束";
  return safeTurnFailureMessage(message) ?? "该轮未正常结束";
}
