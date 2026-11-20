# codearts2api

华为云中国站（huaweicloud.com）**码道 CodeArts Agent** 到 **OpenAI 兼容 API** 的本地网关。

技术栈：Python 3.12、uv、FastAPI。上游认证使用华为云官方 **SDK-HMAC-SHA256** 签名算法。凭证放在**多账号池**中，每个账号可以是：

1. **官方客户端同款浏览器 SSO / OAuth**（推荐，不必粘贴长期 AK/SK）
2. 你自己的华为云 IAM **永久 AK/SK**（以及可选的 STS `X-Security-Token`）

`AK/SK 请写在账号池文件（默认 `data/config.json`）或在 WebUI 里添加，不要放进 `.env`。可以把仓库根目录的 `config.example.json` 复制为 `data/config.json` 后填入密钥。旧的 `CODEARTS_AK` / `CODEARTS_SK` 环境变量仍可作为空池时的一次性导入，但不推荐再使用。

时区、签到 cron、调度开关、账号池策略、上游超时等运行配置同样写在 `data/config.json`，也可在 WebUI 里修改。`.env` 只保留：启动用的 `CA2A_API_KEY`（随后可在 WebUI 更换并持久化）、仅能通过环境变量设置的监听地址 `CA2A_HOST` / `CA2A_PORT`，以及可选的数据目录 `CA2A_DATA_DIR` / `CA2A_AUTH_DIR`。

对话请求按配置的策略在启用账号之间轮换，并在上游返回 401/403/429/5xx 时把该账号放入冷却并改用其他账号（只做故障转移，不是插队或绕过限流）。每日签到和权益领取会**分别对每个已启用账号**执行。

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

从该官方 `agentkernel` 中核对到的中国站生产常量与行为（见下文「OAuth / SSO」和「opengw 权益」）。**官方 IDE / 插件安装包体积很大，本次未完整解包核对其 client_id**；若你使用的是 IDE 而不是 CLI，其 `client_id` 可能不同，可在 `data/config.json` 的 `settings.oauth_client_id` 或 WebUI 中覆盖。默认值 `CodeArts_Tui` 来自官方 CLI。

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

若存在临时安全令牌，加入**已签名**的 `X-Security-Token`。若账号配置了 `project_id`，加入已签名的 `X-Project-Id`。部分官方客户端会在签名完成后再附加 `X-Language`；未参与签名的头不能写入规范请求。可在 `data/config.json` 的 `settings.x_language` 或 WebUI 中配置（默认不发送）。

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

`settings.credential_source`（默认 `auto`）用来筛选账号池类型：`auto` 使用全部启用账号，`oauth` 只用 OAuth 账号，`aksk` 只用 AK/SK 账号（旧值 `env` 仍被接受，并按 `aksk` 处理）。对话使用的具体账号由账号池策略决定（默认 `round_robin`，也可选 `first_available` 或 `random`）。

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

准备上游凭证（任选，也可之后在 WebUI 里继续添加）：

```bash
# 方式 A：官方 CLI 同款浏览器登录（推荐），写入账号池
uv run codearts2api login
# 绑定到指定名称的账号（不存在则创建）
uv run codearts2api login --account 上班
# 无桌面环境、只想打印链接：
uv run codearts2api login --print-only

# 方式 B：在 data/config.json 中填写自己的 IAM AK/SK（以及可选的签到时间等）
mkdir -p data
cp config.example.json data/config.json
# 编辑 accounts 里的 ak / sk，按需修改 settings.checkin_cron 等
```

启动网关：

```bash
uv run codearts2api
# 或
uv run codearts2api serve --host 127.0.0.1 --port 8787
```

默认监听 `127.0.0.1:8787`。浏览器打开控制台：

- http://127.0.0.1:8787/ 或 http://127.0.0.1:8787/ui

页面本身不用带密钥；进入后在登录表单填写 `CA2A_API_KEY`，浏览器只把它存在 sessionStorage，并以 `Authorization: Bearer` 调用管理接口。在「账号池」里可以再添加第二个 AK/SK 或 OAuth 账号。OAuth 回调仍然是官方客户端同款的 `http://127.0.0.1:<port>/oauth/callback`，浏览器必须能访问这台机器的环回地址。

其他命令：`uv run codearts2api status`（列出账号池，不输出密钥）、`uv run codearts2api logout`（可加 `--account`）。

```bash
uv run pytest
```

## Docker 与 GHCR

镜像由 GitHub Actions 在推送到 `main` 或手动触发工作流时构建，并发布到 GitHub Container Registry：

[`ghcr.io/leisurelyyrsc/codearts2api:latest`](https://github.com/LeiSureLyYrsc/codearts2api/pkgs/container/codearts2api)

`docker-compose.yml` 已指向该 `latest` 标签。先复制 `.env.example` 为 `.env`，**必须**设置 `CA2A_API_KEY`。AK/SK 与定时签到等运行配置请写入挂载的 `data/config.json`（可从 `config.example.json` 复制）；也可以先启动，再在 WebUI 里添加账号或修改签到时间。容器监听 `0.0.0.0:8787`，默认只映射到宿主机的 `127.0.0.1:8787`。控制台在 http://127.0.0.1:8787/ 。

```bash
cp .env.example .env
# 编辑 .env 后：
docker compose pull
docker compose up -d
```

首次拉取需要仓库里的 Actions 至少成功跑完一次。镜像随公开仓库发布；若 `docker compose pull` 返回 401，到 GitHub Packages 设置里确认 `codearts2api` 包已设为 public。`./data`（WebUI 配置与账号池）和 `./auths`（OAuth 会话）都会挂载到容器中以保持持久化。容器内的 OAuth 回调仍然指向容器自己的 127.0.0.1，通常需要在宿主机做端口转发，或在有浏览器的机器上用 CLI 登录后把 `auths/` 拷进去。

## 配置说明

推荐把账号凭证和运行配置写在 `data/config.json`（可先复制仓库根目录的 `config.example.json`），也可在 WebUI「账号池 / 运行配置」里修改。`.env` 只保留密钥启动默认值和进程级参数。

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `CA2A_API_KEY` 或 `CODEARTS2API_API_KEY` | 保护 `/v1/*` 与 `/admin/*` 的启动默认值；也可在 WebUI 更换并写入 `data/config.json` | 空（未配置则受保护接口返回 503） |
| `CA2A_HOST` / `CA2A_PORT` | 本地监听（仅环境变量，不能在 WebUI 或配置文件中修改） | `127.0.0.1` / `8787` |
| `CA2A_DATA_DIR` | 账号池与运行配置目录（`config.json`，权限 0600） | `data` |
| `CA2A_AUTH_DIR` | OAuth 会话目录 | `auths` |

为保持兼容，旧的 `CODEARTS_AK` / `CODEARTS_SK`（以及可选的 `CODEARTS_SECURITY_TOKEN`、`CODEARTS_PROJECT_ID`）在账号池为空时仍会导入为名为 `env` 的账号；若该账号还在，启动时会与当前环境变量同步。新部署请把密钥写进 `data/config.json` 的 `accounts`。其余曾出现在 `.env` 中的运行项（签到 cron、时区、超时等）仍可被环境变量覆盖，但一旦写入 `data/config.json` 或在 WebUI 中保存，以配置文件为准。

### `data/config.json`

`settings` 中的常用项：

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `timezone` | 调度时区 | `Asia/Shanghai` |
| `checkin_cron` | 5 段 cron | `0 8 * * *` |
| `checkin_run_on_startup` | 启动时立即签到 | `true` |
| `scheduler_enabled` | 是否启用进程内定时签到 | `true` |
| `campaign_type` | 默认定向匹配的活动类型 | `USER_LOGIN` |
| `checkin_claim_all` | 是否同时领取其他可领取活动 | `false` |
| `model_cache_ttl` | 模型列表缓存秒数 | `60` |
| `snap_base_url` | snap-access 网关 | `https://snap-access.cn-north-4.myhuaweicloud.com` |
| `opengw_base_url` | 官方 CLI 中的权益网关 | `https://opengw.developer.huaweicloud.com` |
| `x_language` | 签名后再附加的 `X-Language`，默认不发送 | 空 |
| `log_level` | 日志级别 | `INFO` |
| `http_timeout` / `http_stream_timeout` | 上游 HTTP 超时（秒）；流式读取可更长 | `60` / `300` |
| `credential_source` | `auto` / `oauth` / `aksk`（筛选账号池类型） | `auto` |
| `pool_strategy` | `round_robin` / `first_available` / `random` | `round_robin` |
| `cooldown_seconds` | 上游 401/403/429/5xx 后的账号冷却秒数 | `60` |
| `oauth_client_id` | 官方 CLI 为 `CodeArts_Tui` | `CodeArts_Tui` |
| `oauth_portal_url` | 官方门户 | `https://codearts.huaweicloud.com/portal` |
| `oauth_sts_url` | 官方 CLI 使用的 STS | `https://sts.cn-north-4.myhuaweicloud.com` |
| `oauth_locale` | 授权页语言 | `zh-cn` |
| `oauth_callback_port` | 本机回调端口，0 表示系统分配 | `0` |
| `api_key` | 可选，WebUI 保存后的网关密钥；未写入时使用环境变量 | 空 |

`accounts` 数组存放上游凭证。AK/SK 账号使用 `"kind": "aksk"`，字段包括 `ak`、`sk`，以及可选的 `security_token`、`project_id`。OAuth 账号使用 `"kind": "oauth"`，会话文件放在 `CA2A_AUTH_DIR`。

日志为结构化 JSON，不会输出 AK/SK/令牌，并对日志中的密钥值脱敏。在 WebUI 中修改时区、cron 或调度开关时，进程内 APScheduler 会重新安排，无需重启。

## 多号池与 WebUI

1. 打开 http://127.0.0.1:8787/ ，输入 `CA2A_API_KEY`。
2. 在「账号池」中添加第二个账号：AK/SK 直接填写密钥；OAuth 账号保存后再点「OAuth 登录」，在能访问 `127.0.0.1` 的浏览器里打开授权地址。
3. 「签到与状态」可对全部或单个账号触发每日签到 / 权益领取，并查看调度器的下次运行时间。上次签到结果会写入 `data/config.json`，重启后仍可在页面看到。
4. 管理接口：`GET/PUT /admin/config`，`GET/POST /admin/accounts`，`PATCH/DELETE /admin/accounts/{id}`，`POST /admin/accounts/{id}/login`，`POST /admin/accounts/{id}/test`（用已有的官方套餐统计路径做探测）。响应中的 SK / security_token / refresh_token / 完整 AK 一律脱敏。

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

curl -s http://127.0.0.1:8787/admin/accounts   -H "Authorization: Bearer $CA2A_API_KEY"
```

`GET /admin/checkin` 与 `GET /admin/status` 内容相同，并会附带各账号的签到结果。

## 限制与注意事项

- 对话、模型列表、签到、OAuth 换票所使用的部分路径**未写入公开 API 目录**。已在官方 CLI 中核对的会在上文标明；未核对的需要你用自己的官方客户端复核。接口可能随官方版本调整。
- 必须使用**你自己的**华为云账号（浏览器登录或自行创建的 AK/SK）。不要分享密钥或 `auths/session.json`。
- 对话会消耗账号积分。签到积分仅当天有效，数量以官方活动说明为准。
- 上游响应字段没有稳定的公开 schema。本网关在字段已接近 OpenAI 形态时做忠实转发，并对明显包装字段做适配，但不会臆造工具调用等行为。
- 未实现 IDE 本地凭据解密、插队或绕过限流。
- 自动化测试不调用真实华为云接口。接入前请用自己的账号联调登录、模型列表和对话。

## 许可证

MIT
