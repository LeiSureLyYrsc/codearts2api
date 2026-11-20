# codearts2api

华为云中国站（huaweicloud.com）**码道 CodeArts Agent** 到 **OpenAI 兼容 API** 的本地网关。

技术栈：Python 3.12、uv、FastAPI。上游认证使用华为云官方 **SDK-HMAC-SHA256** 签名算法，凭证来源可以是：

1. **官方客户端同款浏览器 SSO / OAuth**（推荐，不必粘贴长期 AK/SK）
2. 你自己的华为云 IAM **永久 AK/SK**（以及可选的 STS `X-Security-Token`）

项目内支持官方「每日签到得积分」活动、进程内定时签到，以及动态拉取模型列表。本仓库全部为原创实现，没有 fork 或拷贝其他网关项目。

## 分析与实现依据（仅官方材料）

本项目只依据华为云官方文档，以及从华为云官方下载渠道取得的官方客户端安装包中的配置/内嵌接口常量。公开 API 目录没写清的路径，若在官方客户端中能核对到，会在下文标明「来自官方客户端」；若官方文档和官方客户端都无法核实，则只留 hook，不自行编造。

### 官方文档

- [码道 API 概览](https://support.huaweicloud.com/api-codeartsagent/codeartsagent-api-0002.html)：公开目录目前只有用量 / commit / 代码生成同步，**不包含**对话、模型列表或签到接口。
- [每日签到得积分](https://support.huaweicloud.com/offers-codeartsagent/codeartsagent_offers_0004.html)：活动时间 2026-10-27 20:00:00 至 2026-12-31 23:59:59（UTC+8）；体验版 100、标准版 150、高级/旗舰 200 积分，当日有效；仅已升级为积分计费的个人版用户可参与。
- [API 签名指南（机制）](https://support.huaweicloud.com/devg-apisign/api-sign-algorithm-001.html) 与 [Python 签名指导](https://support.huaweicloud.com/devg-apisign/api-sign-sdk-python.html)：SDK-HMAC-SHA256。
- [系统内置模型配置](https://support.huaweicloud.com/usermanual-codeartsagent/codeartsagent_ug_0072.html)：官方用户指南中的内置模型说明（如 GLM-5.2）。
- [码道 IDE 快速启动 / 登录](https://support.huaweicloud.com/usermanual-codeartsagent/codeartsagent_ug_0002.html)：客户端会跳转外部浏览器打开华为云登录页，用户登录后在授权页「确认授权」。
- [码道 CLI 授权](https://support.huaweicloud.com/usermanual-cli/codeartsagent_cli_0026.html)：官方提供「网页端登录授权」和「AK/SK 鉴权授权」两种方式；浏览器未自动打开时可复制终端中的登录链接。
- [配置账号 30 天免登录](https://support.huaweicloud.com/usermanual-enterprise/codeartsagent_enterprise_0013.html)：IAM 用户相关权限为 `signin::authorizeOAuth2Access`、`signin::createOauth2Token`。
- [配置 SSO 登录](https://support.huaweicloud.com/usermanual-enterprise/codeartsagent_enterprise_0019.html)：企业联邦用户的 SAML/OIDC 控制台 SSO（与本网关实现的官方客户端浏览器授权不是同一条路径）。
- IAM 访问密钥：<https://console.huaweicloud.com/iam/#/mine/accessKey>
- 官方下载页：<https://codearts.huaweicloud.com/download.html>
- 官方门户：<https://codearts.huaweicloud.com/portal>

### 官方客户端（已核对的安装包）

为核实公开目录未收录的登录与网关地址，本项目查看了官方码道 CLI 安装脚本与 26.11.11 Linux x64 安装包（官方 OBS，不是第三方镜像）：

- `https://cnnorth4-cloudide-marketplace.obs.cn-north-4.myhuaweicloud.com/codearts/cli_tui/install_script/install.sh`
- `https://codearts-agent-obs-cdn.huaweicloud.com/codearts/cli_tui/latest/codearts-install-26.11.11-linux-x64.tar.gz`

从该官方 `agentkernel` 中核对到的中国站生产常量与行为（见下文「OAuth / SSO」和「opengw 权益」）。**官方 IDE / 插件安装包体积很大，本次未完整解包核对其 client_id**；若你使用的是 IDE 而不是 CLI，其 `client_id` 可能不同，可用 `CA2A_OAUTH_CLIENT_ID` 覆盖。默认值 `CodeArts_Tui` 来自官方 CLI。

官网下载页前端脚本中还有门户会话接口 `https://codearts.huaweicloud.com/portal/snap-manager/v1/sso/user`，这是网页门户读当前登录用户用的，本网关不走浏览器 Cookie，因此不使用它。

## 原理

### 1. 公开 API 目录 vs 官方客户端网关

公开 API 目录只覆盖 collector-agents-access 上的用量同步。对话、模型列表和签到需要走官方客户端使用的 **snap-access 网关**，用 IAM/STS AK/SK 做 SDK-HMAC-SHA256 签名：

```
https://snap-access.cn-north-4.myhuaweicloud.com
```

该基址出现在官方 CLI 的生产配置中。下列路径在公开 API 目录中**没有**文档，可能随官方客户端变更：

| 用途 | 方法 | 路径 | 核实情况 |
| --- | --- | --- | --- |
| 动态模型列表 | `GET` | `/v1/model/builtin` | 官方 CLI 内嵌此路径 |
| 对话补全 | `POST` | `/api/v2/chat/completions` | 公开目录未收录；按 OpenAI Chat Completions 形态转发（未在本次 CLI 包中检索到完全相同的 `/api/v2/chat/completions` 字符串，调用前请用自己的账号验证） |
| 当前用户 | `GET` | `/snap-manager/v1/current/user` | 官方 CLI 登录后会签名调用 |
| 账号 / 套餐 | `GET` | `/snap-manager/v1/statistics/plugin` | 官方 CLI 内嵌此路径 |
| 活动列表 / 领取 / 确认 | `GET /v1/ops/delivery?channel=IDE`，`POST /v1/ops/claim`，`POST /v1/ops/confirm` | 官方活动说明确认有「每日领取」；**这三条路径未在本次 CLI 包中检索到**（CLI 可能不含 IDE 签到）。实现按任务约定保留，需用官方 IDE/插件自行复核 |
| 限时模型权益 | `POST` | `{opengw}/api/v1/benefit/claim` 空 JSON `{}` | 官方 CLI：生产基址 `https://opengw.developer.huaweicloud.com`，`error_code == "0000"` 为成功 |

用量同步类公开 API 不是 MVP 范围，本项目未实现。本项目不实现浏览器 Cookie 门户登录，也不读取或解密 IDE 本地凭据库。

### 2. 签名认证

实现依据官方签名文档，而不是任何第三方 SDK 源码拷贝：

1. 构造规范请求 CanonicalRequest（方法、规范化 URI、查询串、规范化头、SignedHeaders、请求体 SHA-256）。计算签名时 URI 必须以 `/` 结尾；实际发请求时可以不带这个尾斜杠。
2. 待签字符串：`SDK-HMAC-SHA256` + `X-Sdk-Date`（UTC `YYYYMMDDTHHMMSSZ`）+ CanonicalRequest 的 SHA-256。
3. `signature = Hex(HMAC-SHA256(SK, StringToSign))`。
4. `Authorization: SDK-HMAC-SHA256 Access={AK}, SignedHeaders=..., Signature=...`。

若存在临时安全令牌，加入**已签名**的 `X-Security-Token`。若配置了 `CODEARTS_PROJECT_ID`，加入已签名的 `X-Project-Id`。部分官方客户端会在签名完成后再附加 `X-Language`；未参与签名的头不能写入规范请求。可用 `CA2A_X_LANGUAGE` 配置（默认不发送）。

### 3. OAuth / SSO（官方 CLI 浏览器授权）

官方用户指南只写到「打开浏览器登录并确认授权」。具体授权 URL、换票和续期步骤来自官方 CLI 26.11.11 的实现，概述如下。

1. 生成 PKCE：`code_verifier` 为 64 字节的 hex（128 个字符），`code_challenge` 为不带填充的 base64url(SHA-256(verifier))，方法字面量为 `SHA-256`。另生成 `ticket_id`（32 字节 hex）。
2. 生成 ES256 / P-256 的 DPoP 密钥对。官方 CLI 在调用令牌接口时携带 `DPoP` 头（JWT typ 为 `dpop+jwt`，声明含 `htm`/`htu`/`iat`/`jti`）。
3. 在 `127.0.0.1` 上启动回调服务，路径为 `/oauth/callback`。官方 CLI 使用端口 `0`（由操作系统分配）。
4. 打开授权地址（官方 CLI 的 `client_id` 为 `CodeArts_Tui`）：

   `https://codearts.huaweicloud.com/portal/authorize?locale=zh-cn&client_id=CodeArts_Tui&port=<回调端口>&code_challenge=<S256>&code_challenge_method=SHA-256&ticket_id=<hex>`

5. 浏览器登录并确认授权后，门户会跳回本机回调：
   - 既无 `code` 也无 `secret`：视为探活，返回 200。
   - 带 `secret` 与 `redirect`：先 307 到 `redirect`，再轮询 `GET https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager/v1/login/ticket?ticket_id=&secret=`（官方 CLI 最多 10 次、间隔 1 秒）。票据体字段为 `credential.access` / `secret` / `securitytoken` / `expires_at`。
   - 带 `code`：`POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens`，表单为 `client_id`、`code`、`code_verifier`、`grant_type=authorization_code`、`redirect_uri=http://127.0.0.1:<port>/oauth/callback`，请求头带 `DPoP`。响应中的 `credentials`（`access_key_id` / `secret_access_key` / `security_token` / `expiration`）以及 `refresh_token` 为可用的临时凭证。
6. 之后用同一把 DPoP 私钥和 `grant_type=refresh_token` 调用同一令牌地址续期（可带 `x-agent-user-account`）。临时凭证写入本地 `auths/session.json`（权限 0600），**不要提交到仓库**。过期前约 10 分钟会自动刷新（与官方 CLI 的 600_000 ms 偏差一致）。

门户回调地址被官方客户端写成环回地址，因此授权必须在能访问本机 `127.0.0.1:<port>` 的浏览器中完成。在远程服务器上登录时，请在自己电脑上做 SSH 本地端口转发，或直接在有浏览器的那台机器上执行 `uv run codearts2api login`。`--print-only` 只是不自动打开浏览器、把授权 URL 打印出来，仍然在本机等待回调。

凭证优先级（`CA2A_CREDENTIAL_SOURCE`，默认 `auto`）：已登录且有效（或可刷新）的 OAuth 会话优先；否则使用环境变量里的 IAM AK/SK。设为 `env` 则只用环境变量，设为 `oauth` 则必须先登录。

### 4. 动态模型列表

`GET /v1/models` 从上游 `GET /v1/model/builtin` 动态拉取，默认缓存 60 秒，`?refresh=true` 可强制刷新。只对外暴露 `enable=true` 的模型；若存在 `display_enabled` 字段，也必须为 true。对话请求使用裸模型 id（例如 `GLM-5.2`）；若客户端传来 `codearts/` 前缀，会去掉后再转给上游。

### 5. 每日签到

官方活动「每日签到得积分」面向已升级积分计费的个人版用户。本网关会先查套餐：`package.is_credit_package === true` 视为积分账号，否则返回 `ineligible`，不领取。

若活动列表与领取确认路径与官方 IDE 一致，流程为：查询套餐 → 拉取活动并匹配类型 `USER_LOGIN`（也可配置为领取所有 claimable 活动）→ `claim`；若响应 `id` 不是 null，必须再 `confirm`，否则积分可能一直处于待确认。已领取返回 `already_claimed`，不视为错误。这三条 ops 路径**尚未在官方 CLI 包中核实**，请以官方 IDE 实际行为为准。

定时签到使用进程内 APScheduler，时区默认 `Asia/Shanghai`，默认每天 08:00，启动时默认也会执行一次。重复执行是安全的。

限时模型权益与每日签到分开：`POST /admin/benefit-claim` 按官方 CLI 调用 opengw 的 `/api/v1/benefit/claim`。

## 安装与启动

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
cd codearts2api
uv sync
cp .env.example .env
# 编辑 .env，至少设置本地网关密钥 CA2A_API_KEY
```

两种准备上游凭证的方式（二选一即可）：

```bash
# 方式 A：官方 CLI 同款浏览器登录（推荐）
uv run codearts2api login
# 无桌面环境、只想打印链接：
uv run codearts2api login --print-only

# 方式 B：填写自己的 IAM AK/SK
# 在 .env 中设置 CODEARTS_AK、CODEARTS_SK，可选 CODEARTS_SECURITY_TOKEN
```

启动网关：

```bash
uv run codearts2api
# 或
uv run codearts2api serve --host 127.0.0.1 --port 8787
```

默认监听 `127.0.0.1:8787`。其他命令：`uv run codearts2api status`、`uv run codearts2api logout`。

```bash
uv run pytest
```

## Docker 与 GHCR

镜像由 GitHub Actions 在推送到 `main` 或手动触发工作流时构建，并发布到 GitHub Container Registry：

[`ghcr.io/leisurelyyrsc/codearts2api:latest`](https://github.com/LeiSureLyYrsc/codearts2api/pkgs/container/codearts2api)

`docker-compose.yml` 已指向该 `latest` 标签。先复制 `.env.example` 为 `.env`，至少设置 `CA2A_API_KEY`，以及 IAM AK/SK（容器内不方便走官方浏览器回调登录）。容器监听 `0.0.0.0:8787`，默认只映射到宿主机的 `127.0.0.1:8787`。

```bash
cp .env.example .env
# 编辑 .env 后：
docker compose pull
docker compose up -d
```

首次拉取需要仓库里的 Actions 至少成功跑完一次。镜像随公开仓库发布；若 `docker compose pull` 返回 401，到 GitHub Packages 设置里确认 `codearts2api` 包已设为 public。OAuth 会话目录挂载在 `./auths`。

## 配置说明

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `CODEARTS_AK` / `CODEARTS_SK` | IAM 访问密钥（OAuth 登录时可不填） | 空 |
| `CODEARTS_SECURITY_TOKEN` | 可选 STS 令牌，作为已签名的 `X-Security-Token` | 空 |
| `CODEARTS_PROJECT_ID` | 可选，已签名的 `X-Project-Id` | 空 |
| `CA2A_CREDENTIAL_SOURCE` | `auto` / `oauth` / `env` | `auto` |
| `CA2A_AUTH_DIR` | OAuth 会话目录 | `auths` |
| `CA2A_OAUTH_CLIENT_ID` | 官方 CLI 为 `CodeArts_Tui` | `CodeArts_Tui` |
| `CA2A_OAUTH_PORTAL_URL` | 官方门户 | `https://codearts.huaweicloud.com/portal` |
| `CA2A_OAUTH_STS_URL` | 官方 CLI 使用的 STS | `https://sts.cn-north-4.myhuaweicloud.com` |
| `CA2A_OAUTH_CALLBACK_PORT` | 本机回调端口，0 表示系统分配 | `0` |
| `SNAP_BASE_URL` | snap-access 网关 | `https://snap-access.cn-north-4.myhuaweicloud.com` |
| `OPENGW_BASE_URL` | 官方 CLI 中的权益网关 | `https://opengw.developer.huaweicloud.com` |
| `CA2A_HOST` / `CA2A_PORT` | 本地监听 | `127.0.0.1` / `8787` |
| `CA2A_API_KEY` 或 `CODEARTS2API_API_KEY` | 保护 `/v1/*` 与 `/admin/*` | 空（未配置则受保护接口返回 503） |
| `CA2A_CHECKIN_CRON` | 5 段 cron | `0 8 * * *` |
| `CA2A_TIMEZONE` | 调度时区 | `Asia/Shanghai` |
| `CA2A_CHECKIN_RUN_ON_STARTUP` | 启动时立即签到 | `true` |
| `CA2A_CAMPAIGN_TYPE` | 默认定向匹配的活动类型 | `USER_LOGIN` |
| `CA2A_CHECKIN_CLAIM_ALL` | 是否同时领取其他可领取活动 | `false` |
| `CA2A_MODEL_CACHE_TTL` | 模型列表缓存秒数 | `60` |
| `CA2A_LOG_LEVEL` | 日志级别 | `INFO` |

日志为结构化 JSON，不会输出 AK/SK/令牌，并对日志中的密钥值脱敏。

## 接口与调用示例

健康检查（无需密钥）：

```bash
curl -s http://127.0.0.1:8787/healthz
```

受保护接口需要 `Authorization: Bearer <CA2A_API_KEY>`。

```bash
# 通过网关发起官方浏览器登录（返回授权 URL；回调仍在网关进程的 127.0.0.1）
curl -s http://127.0.0.1:8787/admin/login   -H "Authorization: Bearer $CA2A_API_KEY"
curl -s http://127.0.0.1:8787/admin/login/status   -H "Authorization: Bearer $CA2A_API_KEY"

curl -s http://127.0.0.1:8787/v1/models   -H "Authorization: Bearer $CA2A_API_KEY"
curl -s "http://127.0.0.1:8787/v1/models?refresh=true"   -H "Authorization: Bearer $CA2A_API_KEY"

curl -s http://127.0.0.1:8787/v1/chat/completions   -H "Authorization: Bearer $CA2A_API_KEY"   -H "Content-Type: application/json"   -d '{"model":"GLM-5.2","messages":[{"role":"user","content":"你好"}]}'

curl -sN http://127.0.0.1:8787/v1/chat/completions   -H "Authorization: Bearer $CA2A_API_KEY"   -H "Content-Type: application/json"   -d '{"model":"codearts/GLM-5.2","stream":true,"messages":[{"role":"user","content":"你好"}]}'

curl -s -X POST http://127.0.0.1:8787/admin/checkin   -H "Authorization: Bearer $CA2A_API_KEY"   -H "Content-Type: application/json"   -d '{"all_claimable": false}'

curl -s http://127.0.0.1:8787/admin/status   -H "Authorization: Bearer $CA2A_API_KEY"
```

`GET /admin/checkin` 与 `GET /admin/status` 内容相同。

## 限制与注意事项

- 对话、模型列表、签到、OAuth 换票所使用的部分路径**未写入公开 API 目录**。已在官方 CLI 中核对的会在上文标明；未核对的需要你用自己的官方客户端复核。接口可能随官方版本调整。
- 必须使用**你自己的**华为云账号（浏览器登录或自行创建的 AK/SK）。不要分享密钥或 `auths/session.json`。
- 对话会消耗账号积分。签到积分仅当天有效，数量以官方活动说明为准。
- 上游响应字段没有稳定的公开 schema。本网关在字段已接近 OpenAI 形态时做忠实转发，并对明显包装字段做适配，但不会臆造工具调用等行为。
- 未实现 IDE 本地凭据解密、插队或绕过限流。
- 自动化测试不调用真实华为云接口。接入前请用自己的账号联调登录、模型列表和对话。

## 许可证

MIT
