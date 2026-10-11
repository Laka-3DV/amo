# Install Amo with an Agent / 让 Agent 安装 Amo

Start with [English README](../README.md) or [中文 README](../README.zh-CN.md). Work in a new clone of `https://github.com/Laka-3DV/amo`, not in an existing live deployment. Use the same source revision for relay and connectors.

## Copyable prompt — English

```text
Install Amo from https://github.com/Laka-3DV/amo into a new directory.
Read README.md, docs/INSTALL_AGENT.md and deploy/README.md.
Determine whether I want a local trial, to join an existing relay, or to deploy
my own HTTPS relay. Ask only for the missing choices and connection details.
Use Python 3.12+ and the documented role-specific requirements. Build the web
client only if I am running a relay. Use `python -m amo` for all entry points.
Use my existing local agent login. Do not copy or expose native account files.
Generate fresh secrets with `python -m amo init` only for my own relay; use
my account's short-lived device pairing code when joining an existing relay.
Keep runtime files in private directories and out of Git. Never substitute
someone else's server, IP, password or token into the installation.
Verify the build, health endpoint and authenticated device connection without
sending model prompts. Ask before changing existing public services or DNS.
On macOS, use the documented LaunchAgent when I request background operation;
never restart an existing connector while tasks or queued messages are active.
Report the access URL, source revision, private configuration locations,
verification results and any remaining manual login steps. Do not print
credentials in logs or commit them to a repository.
```

## 可复制 prompt — 中文

```text
请将 https://github.com/Laka-3DV/amo 克隆到新目录，安装 Amo。
先读 README.zh-CN.md、docs/INSTALL_AGENT.md 和 deploy/README.md。
先确认我需要本地试用、接入已有中继，还是部署自己的 HTTPS 中继，只询问缺失信息。
使用 Python 3.12+ 和文档中的角色依赖；仅在运行中继时构建网页。
统一使用 `python -m amo` 命令，沿用我自己的本机 Agent 登录，不复制或暴露账号文件。
部署我自己的中继时使用 `python -m amo init` 生成新凭据；接入已有中继时使用属于
我自己账号的短时设备配对码。私人配置和运行数据不要进入 Git，不要复用别人的
服务器、IP、密码或 token。验证构建、健康接口和设备连接，不要发送模型任务。
改动已有公网服务或 DNS 前先让我确认。如果我需要 macOS 后台运行，配置文档里的
LaunchAgent；已有连接器还在执行任务或有待发送消息时不要重启。
最后报告访问地址、源码版本、私人配置位置、验证结果及仍需我手动登录的步骤。
日志里不要打印凭据，也不要把它们提交到仓库。
```

## Mode A: local trial / 本地试用

1. Confirm Python 3.12+ and Node 22.12+ (24 recommended). Inspect installed local agent versions. Native logins belong to the user.
2. Clone the public repository into a new directory. Install `requirements.txt` in `.venv`, then run `npm --prefix web ci` and `npm --prefix web run build`.
3. Run `python -m amo init`. It refuses to overwrite existing configuration. The generated login password stays in `.env`.
4. Run `python -m amo relay` and `python -m amo connector` in separate terminals. Use loopback HTTP only for this local mode.
5. Verify `/healthz`, then sign in locally and check that the intended device and native agents are visible. Do not send a model task as part of setup verification.

## Mode B: existing relay / 接入已有中继

1. Ask for the relay HTTPS origin and a one-time pairing code from the user's own authorized account. Server administrator credentials and other users' passwords are unnecessary.
2. Install `requirements-wrapper.txt` in a new virtual environment; no web build or server installation is needed.
3. Run the documented `python -m amo pair` command with `PYTHON_DOTENV_DISABLED=1`. Verify `~/.amo/device.json` is private (`0600`); do not display its token.
4. Run `PYTHON_DOTENV_DISABLED=1 python -m amo connector`, or install the macOS service as documented. Confirm the device appears under the user's own account.
5. If account isolation is not configured, ask the relay administrator to set up independent accounts and machine access lists. Do not grant wildcard access to other users' devices.

## Mode C: new HTTPS relay / 部署新中继

1. Ask for the user's server access and intended domain, and whether an existing reverse proxy must be preserved.
2. Follow [deployment instructions](../deploy/README.md#https-relay), using a dedicated service account and private configuration outside the source tree.
3. Generate credentials locally, set `PUBLIC_ORIGIN` to the user's HTTPS origin, and keep the Python relay bound to loopback behind TLS.
4. Configure the connector separately on the agent computer. Never expose DSH's local carrier or Codex's stdio transport as public services.
5. Verify TLS, login, Origin checks and device routing before handing over. Independent users require separate web accounts, device tokens and explicit access lists.

## Setup verification

- `python -m amo version` reports the installed source version.
- A local build succeeds; `/healthz` reports a connected connector when one is running.
- The browser can authenticate and see only devices authorized for its account.
- Native login/configuration stays on the agent computer.
- `.env`, `device.json`, databases, logs, native history and screenshots are absent from `git ls-files`.
- The supplied example domain is replaced by the user's own origin; a public deployment uses HTTPS/WSS.

No model task is needed to complete these setup checks. An optional end-to-end agent test should use a new disposable conversation with the user's authorization.
