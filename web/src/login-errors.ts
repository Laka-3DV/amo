/** Login failure copy.
 *
 * A rejected entry point used to be presented as "密码错误", which sent the user
 * looking for a credential problem that did not exist — the failure mode behind
 * several "远程又登录不上了" reports. Every distinguishable cause now says what
 * it is, and an origin rejection names an entry the relay does accept.
 */

export interface LoginFailure {
  message: string;
}

interface Options {
  multiUser: boolean;
  host: string;
}

function wrongCredentials(multiUser: boolean): string {
  return multiUser ? "账号或密码错误" : "密码错误";
}

export function loginFailure(
  status: number, payload: unknown, opts: Options,
): LoginFailure {
  const body = (payload ?? {}) as Record<string, unknown>;
  if (status === 429) {
    return { message: "尝试太频繁，等一分钟再试" };
  }
  if (status === 403) {
    if (body.error === "origin_rejected") {
      const allowed = Array.isArray(body.allowed_origins)
        ? body.allowed_origins.filter((value) => typeof value === "string")
        : [];
      return {
        message: allowed.length
          ? `当前入口不被中继接受，请改用 ${allowed[0]}`
          : "当前入口不被中继接受，请改用中继允许的地址",
      };
    }
    return { message: "登录被拒绝（403），请确认入口地址" };
  }
  if (status === 401) {
    return { message: wrongCredentials(opts.multiUser) };
  }
  if (status >= 500) {
    return { message: `服务端异常（${status}），请稍后重试` };
  }
  return { message: wrongCredentials(opts.multiUser) };
}

/** The request never reached the relay: this is the network, not a password. */
export function loginNetworkFailure(host: string): string {
  return `连不上中继（当前入口 ${host}），请检查手机网络后重试`;
}
