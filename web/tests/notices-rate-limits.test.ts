import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import type { Notice, ServerEvent, StatusReport } from "../src/protocol.ts";

const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const {
    createRuntime, initialState, reduce, MAX_SESSION_NOTICES,
  } = await harness.ssrLoadModule("/src/reducer.ts");
  const { NoticeStack } = await harness.ssrLoadModule(
    "/src/components/NoticeStack.tsx");
  const { statusNotices } = await harness.ssrLoadModule(
    "/src/notice-presentation.ts");
  const {
    ReconnectBanner, TRANSIENT_BANNER_TTL_MS,
  } = await harness.ssrLoadModule("/src/components/ReconnectBanner.tsx");
  const { ErrorBoundary } = await harness.ssrLoadModule(
    "/src/ErrorBoundary.tsx");
  const {
    presentCommandProblem, presentHistoricalTurnProblem, presentTurnProblem,
  } = await harness.ssrLoadModule("/src/problem-presentation.ts");
  const sid = "notice-session";
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 10, ts: 10, sid, ...body,
  } as ServerEvent);
  let state = {
    ...initialState,
    banner: "machine reconnected — syncing…",
    focusedSid: sid,
    runtimes: { [sid]: createRuntime() },
  };

  // Per-session retention is bounded and duplicate ids replace/move instead of
  // growing the list.  Notice reduction must not mutate the reconnect banner.
  for (let index = 0; index < MAX_SESSION_NOTICES + 3; index += 1) {
    state = reduce(state, { type: "event", event: event({
      type: "notice",
      notice_id: `notice-${index}`,
      severity: "warning",
      category: "runtime",
      title: `warning ${index}`,
      message: "bounded message",
    }) });
  }
  assert.equal(state.runtimes[sid].notices.length, MAX_SESSION_NOTICES);
  assert.equal(state.runtimes[sid].notices[0].notice_id, "notice-3");
  state = reduce(state, { type: "event", event: event({
    type: "notice",
    notice_id: "notice-3",
    severity: "info",
    category: "deprecation",
    title: "updated",
    message: "same id",
  }) });
  assert.equal(state.runtimes[sid].notices.length, MAX_SESSION_NOTICES);
  assert.equal(state.runtimes[sid].notices.at(-1)?.title, "updated");
  assert.equal(state.banner, "machine reconnected — syncing…");

  state = reduce(state, {
    type: "dismiss_notice", sid, noticeId: "notice-3",
  });
  assert.equal(state.runtimes[sid].notices.some(
    (notice: Notice) => notice.notice_id === "notice-3"), false);

  state = reduce(state, {
    type: "command_error", detail: "详细过程已过期，请刷新会话后重试",
  });
  const staleDismiss = reduce(state, {
    type: "dismiss_banner", banner: "older warning",
  });
  assert.equal(staleDismiss.banner, "详细过程已过期，请刷新会话后重试",
    "an old timer must not dismiss a newer banner");
  state = reduce(state, {
    type: "dismiss_banner", banner: "详细过程已过期，请刷新会话后重试",
  });
  assert.equal(state.banner, undefined);

  const report = event({
    type: "status_report",
    thread: { thread_id: sid, status: "idle", active_flags: [] },
    runtime: {},
    context: {},
    account: null,
    rate_limits: [{
      limit_id: "codex", limit_name: "Codex", plan_type: "pro",
      primary: { used_percent: 40, resets_at: 900, window_duration_mins: 300 },
    }],
    usage: null,
    component_errors: [],
  }) as StatusReport;
  state = reduce(state, { type: "event", event: report });
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: null,
    primary: { used_percent: 0, resets_at: 901, window_duration_mins: null },
    secondary: null,
  }) });
  let merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.primary?.used_percent, 40,
    "same-period live updates must not regress an authoritative percentage");
  assert.equal(merged?.primary?.resets_at, 901,
    "one-second provider reset jitter still belongs to the current period");
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: null,
    primary: {
      used_percent: 0, resets_at: 18_900, window_duration_mins: 300,
    },
    secondary: null,
  }) });
  merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.primary?.used_percent, 0,
    "a confirmed new quota period may lower the used percentage");
  assert.equal(merged?.primary?.resets_at, 18_900);
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: null,
    primary: { used_percent: 100, resets_at: 900, window_duration_mins: 300 },
    secondary: null,
  }) });
  merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.primary?.used_percent, 0,
    "a late snapshot from the previous period must be ignored");
  assert.equal(merged?.primary?.resets_at, 18_900);
  state = reduce(state, { type: "event", event: report });
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: "rate_limit_reached",
    primary: { used_percent: 100, resets_at: null, window_duration_mins: null },
    secondary: null,
  }) });
  merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.limit_name, "Codex");
  assert.equal(merged?.plan_type, "pro");
  assert.equal(merged?.primary?.used_percent, 100);
  assert.equal(merged?.primary?.resets_at, 900);
  assert.equal(merged?.rate_limit_reached_type, "rate_limit_reached");
  assert.equal(Object.hasOwn(merged ?? {}, "credits"), false);
  assert.equal(Object.hasOwn(merged ?? {}, "individualLimit"), false);

  // A delayed status response must not replace the newest in-flight read.
  state = reduce(state, {
    type: "begin_status_request", sid, requestId: "status-new",
  });
  const staleStatus = {
    ...report, request_id: "status-old",
    rate_limits: [{
      limit_id: "codex", primary: { used_percent: 100 },
    }],
  } as StatusReport;
  state = reduce(state, { type: "event", event: staleStatus });
  assert.equal(state.runtimes[sid].statusRequestId, "status-new");
  assert.equal(state.runtimes[sid].statusReport?.rate_limits[0]?.primary?.used_percent, 100,
    "stale status response must not replace the installed report");
  const uncorrelatedStatus = { ...staleStatus, request_id: undefined } as StatusReport;
  state = reduce(state, { type: "event", event: uncorrelatedStatus });
  assert.equal(state.runtimes[sid].statusRequestId, "status-new");
  assert.equal(state.runtimes[sid].statusReport?.rate_limits[0]?.primary?.used_percent, 100,
    "uncorrelated status response must not replace an in-flight request");
  const currentStatus = {
    ...report, request_id: "status-new",
    rate_limits: [{
      limit_id: "codex", primary: { used_percent: 0 },
    }],
  } as StatusReport;
  state = reduce(state, { type: "event", event: currentStatus });
  assert.equal(state.runtimes[sid].statusRequestId, null);
  assert.equal(state.runtimes[sid].statusReport?.rate_limits[0]?.primary?.used_percent, 0,
    "the matching status response installs and completes the request");

  const officialDiagnostic = event({
    type: "notice", notice_id: "official-notice-private-diagnostic",
    severity: "warning", category: "runtime",
    title: "Codex runtime warning",
    message: "provider crash at /private/token; see wrapper logs",
    detail: "Traceback: secret",
  }) as Notice;
  const officialConversationMarkup = renderToStaticMarkup(createElement(
    NoticeStack, { notices: [officialDiagnostic], onDismiss: () => {} }));
  assert.equal(officialConversationMarkup, "",
    "official app-server diagnostics must not interrupt the transcript");
  const safeStatusNotices = statusNotices([officialDiagnostic]);
  assert.equal(safeStatusNotices.length, 1);
  assert.doesNotMatch(safeStatusNotices.map((notice: Notice) =>
    [notice.title, notice.message, notice.detail ?? ""].join(" ")).join(" "),
    /crash|warning|wrapper|private|traceback|secret/i);

  const hiddenDiagnostic = "provider crash at /private/token; see wrapper logs";
  assert.equal(presentTurnProblem({ code: "cc_crash", message: hiddenDiagnostic }),
    "本次回复未完成，请重试。");
  for (const safeMessage of [
    "网络异常，连接失败，请重新尝试。",
    "网络连接异常，请检查网络后重试。",
    "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。",
    "请求过于频繁或当前额度受限，请稍后重试。",
    "请求超时，请重新尝试。",
    "Codex 上游服务暂时不可用，请稍后重试。",
  ]) {
    assert.equal(
      presentTurnProblem({ code: "cc_crash", message: safeMessage }),
      safeMessage,
    );
  }
  assert.equal(presentCommandProblem({ code: "internal", message: hiddenDiagnostic }),
    "操作未完成，请稍后重试。");
  assert.doesNotMatch(
    presentCommandProblem({ code: "protocol", message: hiddenDiagnostic }),
    /crash|wrapper|private|protocol/i);
  assert.equal(
    presentCommandProblem({
      code: "protocol",
      message: "思考强度请求与当前会话引擎不匹配。",
    }),
    "当前会话已切换，请重新打开后再修改设置。",
  );
  assert.equal(
    presentCommandProblem({
      code: "protocol",
      message: "protocol version mismatch: expected 39",
    }),
    "页面版本已更新，请刷新后重试。",
  );
  assert.equal(presentHistoricalTurnProblem("error"), "该轮未正常结束");
  const unsupportedDshImage = "当前 DSH 模型不支持图片输入。";
  assert.equal(
    presentTurnProblem({ code: "bad_prompt", message: unsupportedDshImage }),
    unsupportedDshImage,
  );
  assert.equal(
    presentCommandProblem({ code: "not_steerable", message: unsupportedDshImage }),
    unsupportedDshImage,
  );
  assert.equal(
    presentHistoricalTurnProblem(unsupportedDshImage),
    unsupportedDshImage,
  );
  assert.equal(
    presentHistoricalTurnProblem(
      "provider crash at /private/token; Authorization: Bearer secret",
    ),
    "该轮未正常结束",
  );
  assert.equal(
    presentHistoricalTurnProblem("网络连接异常，请检查网络后重试。"),
    "网络连接异常，请检查网络后重试。",
  );
  assert.equal(
    presentHistoricalTurnProblem(
      "Codex 登录已失效或当前账号无权限，请重新登录后重试。",
    ),
    "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。",
    "a cached pre-upgrade auth failure is migrated to provider-neutral copy",
  );

  const markup = renderToStaticMarkup(createElement(NoticeStack, {
    notices: state.runtimes[sid].notices,
    onDismiss: () => {},
  }));
  assert.match(markup, /notice-stack/);
  assert.equal((markup.match(/notice-dismiss/g) ?? []).length,
    state.runtimes[sid].notices.length);
  assert.doesNotMatch(markup, /\bwarning\b|crash|traceback/i,
    "conversation notices must use product copy instead of diagnostics");


  assert.equal(TRANSIENT_BANNER_TTL_MS, 6_000);
  const transientBanner = renderToStaticMarkup(createElement(ReconnectBanner, {
    banner: "详细过程已过期，请刷新会话后重试",
    replaying: false,
    truncated: false,
    busy: false,
    onDismiss: () => {},
  }));
  assert.match(transientBanner, /banner-dismiss/);
  assert.match(transientBanner, /关闭提示/);
  const connectionBanner = renderToStaticMarkup(createElement(ReconnectBanner, {
    banner: "machine offline — waiting for reconnect",
    replaying: false,
    truncated: false,
    busy: true,
    onDismiss: () => {},
  }));
  assert.match(connectionBanner, /banner-dismiss/,
    "every persistent banner remains explicitly dismissible");
  const stickyBanner = renderToStaticMarkup(createElement(ReconnectBanner, {
    banner: "正在连接…",
    sticky: true,
    onReconnect: () => {},
    replaying: false,
    truncated: false,
    busy: true,
    onDismiss: () => {},
  }));
  assert.match(stickyBanner, /重连/,
    "a disconnected phone must get a manual reconnect action");
  assert.match(stickyBanner, /banner-action/);
  assert.match(stickyBanner, /banner-dismiss/,
    "the sticky connection banner stays dismissible as well as actionable");
  const reconnectableBanner = renderToStaticMarkup(createElement(ReconnectBanner, {
    replaying: false,
    truncated: false,
    busy: true,
    onDismiss: () => {},
  }));
  assert.equal(reconnectableBanner, "",
    "a non-sticky banner with no text renders nothing");
  const reconnectBannerSource = readFileSync(resolve(
    process.cwd(), "src/components/ReconnectBanner.tsx"), "utf8");
  assert.match(reconnectBannerSource,
    /window\.setTimeout\([\s\S]*TRANSIENT_BANNER_TTL_MS/,
    "transient banners must schedule their own dismissal");
  assert.match(reconnectBannerSource, /window\.clearTimeout\(timer\)/,
    "a changed or unmounted banner must cancel its stale timer");

  const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
  assert.ok(appSource.indexOf("<ReconnectBanner") < appSource.indexOf("<NoticeStack"),
    "NoticeStack must remain below, not replace, ReconnectBanner");

  const boundary = new ErrorBoundary({ children: createElement("div") });
  boundary.state = { error: new Error(hiddenDiagnostic) };
  const boundaryMarkup = renderToStaticMarkup(boundary.render());
  assert.match(boundaryMarkup, /页面需要重新载入/);
  assert.match(boundaryMarkup, /重新载入/);
  assert.doesNotMatch(boundaryMarkup, /crash|wrapper|private|stack/i,
    "the recovery screen must not expose the render exception");
} finally {
  await harness.close();
}

console.log("notice and live rate-limit tests passed");
