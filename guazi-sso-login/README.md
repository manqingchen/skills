# guazi-sso-login

用于获取瓜子 SSO Cookie、cwiki.guazi.com Wiki 登录态和 CHDSSO token 的 skill。

## 安装

```bash
npx skills add "https://git.guazi-corp.com/yanliwan/agent-skill.git" --skill guazi-sso-login
```

## 会话中怎么用

用户通常不需要手动执行命令。直接在会话里说明要做什么即可：

- “初始化瓜子 SSO 登录态”
- “用 guazi-sso-login 登录一下”
- “帮我获取 cwiki 的登录态”
- “帮我获取测试环境 chdsso”
- “cwiki-download 未登录时，先用 SSO 登录再继续”
- “强制刷新 Wiki Cookie”

GUAZISSO/WIKI 首次使用时，用户应单独调用本 skill 的初始化能力。进入初始化流程后，agent 会收集：

- `userName`
- `password`
- `applyPhone`

然后 agent 会调用本 skill 的 `init` 能力写入本地缓存。后续其他 skill 会自动复用缓存，不需要用户重复输入。

CHDSSO 首次使用时，用户应单独调用本 skill 的 `init-chdsso` 能力。进入初始化流程后，agent 会按环境收集：

- `env`，默认 `test`
- `phone`
- `code`

`phone` 和 `code` 会按环境分别缓存，避免测试、预发、线上互相覆盖。

## 能力

- 获取 SSO Cookie：`GUAZISSO=<token>`
- 获取 Wiki Cookie：可直接用于 `cwiki.guazi.com` 请求头
- 获取 CHDSSO token：可直接用于请求头 `chdsso: <token>`，默认 `test`
- 支持当天缓存，默认优先使用缓存
- 支持校验 Wiki Cookie，失效时自动用本地凭据刷新
- 支持校验 CHDSSO token，失效时自动用对应环境的本地凭据刷新
- 支持用户级凭据缓存，便于其他 skill 复用

## 命令与参数

命令：

- `init`：写入本地登录凭据缓存，不请求远端登录接口。
- `wiki`：获取 cwiki.guazi.com 可用 Cookie；默认优先读当天缓存。
- `sso`：获取指定环境的 `GUAZISSO=<token>`。
- `chdsso`：获取指定环境的 CHDSSO token；默认环境 `test`。
- `check`：检查缓存文件、凭据缓存和当天 Cookie 记录。
- `clear-credentials`：清除本地缓存的登录凭据。
- `init-chdsso`：按环境写入 CHDSSO 的 `phone`、`code` 凭据缓存，不请求远端登录接口。

通用参数：

- `--user-name`：登录用户名。
- `--password`：登录密码。
- `--apply-phone`：登录手机号。
- `--plain`：只输出 Cookie 字符串，便于其他 skill 作为环境变量或请求头使用。
- `--force-refresh`：忽略当天 Cookie 缓存，强制重新登录并刷新缓存。
- `--refresh-reason`：刷新原因；包含“失效、过期、invalid、expired、refresh、重新登录”等关键词时会触发强制刷新。
- `--validate`：`wiki` 专用；返回缓存前先请求 cwiki 接口校验 Cookie，有效则返回缓存，失效则自动用本地凭据刷新。

`sso` 专用参数：

- `--env`：目标环境，支持 `test`、`pre`、`online`，默认 `online`。

`chdsso` / `init-chdsso` 专用参数：

- `--env`：目标环境，支持 `test`、`pre`、`online`，默认 `test`。
- `--phone`：CHDSSO 登录手机号。
- `--code`：CHDSSO 登录验证码。
- `--validate`：`chdsso` 专用；返回缓存前先请求 CHDSSO 校验接口，有效则返回缓存，失效则自动用对应环境的凭据刷新。

环境变量：

- `GUAZI_SSO_USER_NAME`：用户名。
- `GUAZI_SSO_PASSWORD`：登录密码。
- `GUAZI_SSO_APPLY_PHONE`：手机号。
- `GUAZI_CHDSSO_PHONE`：CHDSSO 手机号。
- `GUAZI_CHDSSO_CODE`：CHDSSO 验证码。
- `GUAZI_CHDSSO_<ENV>_PHONE`：指定环境 CHDSSO 手机号，例如 `GUAZI_CHDSSO_TEST_PHONE`。
- `GUAZI_CHDSSO_<ENV>_CODE`：指定环境 CHDSSO 验证码，例如 `GUAZI_CHDSSO_TEST_CODE`。
- `GUAZI_SSO_CACHE_DIR`：缓存目录，默认 `~/.agents/cache/guazi-sso-login`。
- `GUAZI_SSO_CACHE_FILE`：缓存文件名或绝对路径，默认 `sso-cache.json`。

凭据和 Cookie 默认缓存到：

```text
~/.agents/cache/guazi-sso-login/sso-cache.json
```

## 给其他 skill 使用

其他 skill 遇到未登录、Cookie 失效、`E_NO_COOKIE` 或需要 `CWIKI_MANUAL_COOKIE` 时，可以调用本 skill 获取 Wiki Cookie。

推荐会话流程：

1. 其他 skill 调用本 skill 获取 Wiki Cookie。
2. 如果返回 `E_MISSING_CREDENTIALS`，其他 skill 应暂停自己的流程，提示用户单独调用 `guazi-sso-login` 的 `init`。
3. 用户完成初始化后，重新发起或继续原流程。

用户不需要知道底层命令。

## 底层调用

下面是 agent 或排障时使用的底层调用方式，不是普通用户的主要入口。

初始化凭据缓存：

```bash
bash ./run.sh init \
  --user-name "<userName>" \
  --password "<password>" \
  --apply-phone "<applyPhone>"
```

初始化 CHDSSO 凭据缓存：

```bash
bash ./run.sh init-chdsso \
  --env test \
  --phone "<phone>" \
  --code "<code>"
```

获取 Wiki Cookie：

```bash
bash ./run.sh wiki --validate --plain
```

获取 CHDSSO token：

```bash
bash ./run.sh chdsso --env test --validate --plain
```

获取 SSO Cookie：

```bash
bash ./run.sh sso --env online --plain
```

强制刷新：

```bash
bash ./run.sh wiki --force-refresh --plain
```

检查状态：

```bash
bash ./run.sh check
```

更多 agent 执行约束见 `SKILL.md`。
