// Login failure copy. A rejected entry point must never be reported as a wrong
// password: that is the failure mode behind repeated "远程又登录不上了" reports
// where the credential was correct the whole time.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  loginFailure,
  loginNetworkFailure,
} from "../src/login-errors.ts";

const single = { multiUser: false, host: "amo.example.com:8443" };

// 401 keeps the existing wording.
assert.deepEqual(
  loginFailure(401, { error: "invalid" }, single),
  { message: "密码错误"},
);
assert.equal(
  loginFailure(401, {}, { multiUser: true, host: "h" }).message,
  "账号或密码错误",
);

// 403 with a rejected origin names a working entry instead of blaming the user.
const rejected = loginFailure(403, {
  error: "origin_rejected",
  allowed_origins: ["http://127.0.0.1:8765", "https://amo.example.com:8443"],
}, single);
assert.match(rejected.message, /当前入口不被中继接受/);
assert.match(rejected.message, /127\.0\.0\.1:8765/,
  "the message must name an origin the relay actually accepts");

// A 403 without that hint still must not say "密码错误".
const bare403 = loginFailure(403, { error: "forbidden" }, single);
assert.doesNotMatch(bare403.message, /密码/);
assert.match(bare403.message, /403/);

// Rate limiting and server faults each say what they are.
assert.match(loginFailure(429, {}, single).message, /频繁/);
assert.match(loginFailure(500, {}, single).message, /服务端异常/);
assert.match(loginFailure(503, {}, single).message, /503/);

// A request that never reached the relay is a network problem, not a password.
const offline = loginNetworkFailure("amo.example.com:8443");
assert.match(offline, /连不上中继/);
assert.match(offline, /amo\.example\.com:8443/);
assert.doesNotMatch(offline, /密码/);

// The form must route every failure through these helpers.
const form = readFileSync(
  resolve(process.cwd(), "src/components/LoginForm.tsx"), "utf8");
assert.match(form, /loginFailure\(r\.status, payload/);
assert.match(form, /loginNetworkFailure\(window\.location\.host\)/);
assert.doesNotMatch(form, /setError\("网络错误"\)/,
  "a network failure must name the cause and the entry, not just say 网络错误");

console.log("login-errors tests passed");
