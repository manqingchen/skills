# cwiki-download

将 Confluence 页面下载为本地 Markdown 的 Python 工具。

## 功能

- 通过 Chrome 远程调试端口读取 Cookie
- 默认通过 `guazi-sso-login` 获取并校验 Wiki Cookie（可切换）
- 调用 Confluence REST API 拉取页面和附件列表
- 只下载正文里实际引用到的附件
- 简单宏转换为 Markdown 友好格式，包括代码块、提示块、状态、展开块以及布局宏中的正文
- 复杂宏降级为本地 SVG 占位图
- `toc` 目录宏默认直接忽略，不输出到导出结果
- 同时输出人类阅读版 `*.md` 与模型阅读版 `*.llm.md`
- 模型版会将嵌套表格、合并单元格表格转换为结构化文本块
- 默认保存到当前工程根目录下的 `docs`
- 页面相关资源统一保存到 `assets/<pageId>/`
- Markdown 文件名使用“标题 + pageId”

## 安装

```bash
npx skills add "https://git.guazi-corp.com/yanliwan/agent-skill.git" --skill cwiki-download
```

## 关联 Skill

- `guazi-sso-login`：<https://git.guazi-corp.com/yanliwan/agent-skill/tree/master/guazi-sso-login>

## 说明：

- `run.sh` 依赖 `uv`
- 若本机未安装 `uv`，脚本会直接报错并提示安装命令，不会自动下载安装
- `--check` 会检查 `uv`、Python 版本、输出目录可写性、登录态来源可用性、Confluence Cookie、Confluence API 基础连通性
- 默认不主动重启当前 Chrome；只有显式传入 `--restart-chrome` 时，才允许调用 `start-chrome-debug.sh`

若 Chrome 远程调试端口未开启，默认会直接报错并提示处理方式；显式传入 `--restart-chrome` 后，工具才会调用 `cwiki-download/start-chrome-debug.sh` 拉起调试模式。默认端口为 `9222`，也支持通过 `CHROME_DEBUG_PORT` 覆盖。

## Skill 对话使用方式

在 AI 对话中使用这个 skill 时，直接提供页面 URL 或 `pageId` 即可，不需要手动写命令。

例如：

```text
用 cwiki-download 拉取这个页面：https://cwiki.guazi.com/pages/viewpage.action?pageId=123456
```

```text
用 cwiki-download 下载 123456
```

如果需要指定输出目录，可以这样说：

```text
用 cwiki-download 拉取这个页面：https://cwiki.guazi.com/pages/viewpage.action?pageId=123456，输出到 ./docs/confluence
```

如果需要指定登录态来源，也可以直接在会话里表达：

```text
这次用 cwiki-download 下载 123456，使用 Chrome 登录态
```

```text
这次用 cwiki-download 下载 123456，使用这个 Cookie：<Cookie>
```

```text
以后都用 SSO 登录态下载 cwiki，先做一次自检并保存配置
```

约束：

- 必须提供页面 URL 或纯数字 `pageId`
- 默认输出到当前工程根目录下的 `docs`
- 默认同时生成 `标题-pageId.md` 和 `标题-pageId.llm.md`
- 默认登录态来源是 `sso`（通过 `guazi-sso-login` 获取 Wiki Cookie）
- 如果只是排查环境问题，可以直接说“用 cwiki-download 做一次自检”
- 如果允许工具重启 Chrome，可以明确说“用 cwiki-download 拉取这个页面，并允许重启 Chrome”

### 推荐会话模板（可直接复制）

最短可用版：

```text
请用 cwiki-download 下载这个页面：123456。
下载后告诉我生成的 md 和 llm.md 文件路径。
```

完整参数版（推荐）：

```text
请用 cwiki-download 拉取这个页面：
https://cwiki.guazi.com/pages/viewpage.action?pageId=123456
输出到 ./docs/confluence，并允许重启 Chrome。
执行前先做一次自检，失败时按错误码给我原因和建议。
```

仅自检版：

```text
请用 cwiki-download 做一次环境自检，不要下载页面。
```

## 参数说明

CLI 参数：

- `<page_input>`：页面输入，支持完整页面 URL 或纯数字 `pageId`
- `--output-dir <DIR>`：指定输出目录；不传时默认输出到工程根目录下的 `docs`
- `--check`：仅执行环境和登录态自检，不下载页面
- `--restart-chrome`：允许脚本在调试端口不可用时重启 Chrome 并拉起调试模式
- `--cookie-source <sso|chrome|manual>`：指定登录态来源；未配置时默认 `sso`
- `--manual-cookie "<Cookie>"`：本次使用手动 Cookie（会自动等价为 `--cookie-source manual`）
- `--sso-skill-root <DIR>`：显式指定 `guazi-sso-login` skill 根目录
- `--save-login-config [user|project]`：保存本次登录态配置；不带值或传 `user` 写入用户级配置，传 `project` 写入工作区 `.env`

对话常用语义参数：

- “下载这个页面：123456” => `<page_input>=123456`
- “下载这个页面：https://...pageId=123456” => `<page_input>=完整 URL`
- “输出到 ./docs/confluence” => `--output-dir ./docs/confluence`
- “先自检 / 只自检” => `--check`
- “允许重启 Chrome” => `--restart-chrome`
- “这次用 Chrome 登录态” => `--cookie-source chrome`
- “这次用这个 Cookie：<Cookie>” => `--manual-cookie "<Cookie>"`
- “以后都用 Chrome 登录态” => `--cookie-source chrome --save-login-config`
- “只对当前项目生效” => `--save-login-config project`

参数优先级：

- 显式 CLI 参数（例如 `--cookie-source`、`--manual-cookie`）和调用方显式环境变量优先级最高
- 其次是项目级 `.env`
- 其次是用户级配置文件（默认 `~/.agents/config/cwiki-download.env`）
- 最后才使用默认值（默认输出目录 `docs`、默认登录态来源 `sso`、默认调试端口 `9222`）

## 结构化错误输出

命令失败时，会统一输出结构化错误信息，格式如下：

```text
[ERROR] E_XXX
原因：...
建议：...
```

常见错误码：

- `E_INVALID_PAGE_INPUT`：页面 URL 或 `pageId` 非法
- `E_CHROME_DEBUG_PORT_UNAVAILABLE`：Chrome 调试端口未开启，且当前未允许自动重启
- `E_NO_COOKIE`：未能从当前 Chrome 登录态拿到 Confluence Cookie
- `E_SSO_LOGIN_UNAVAILABLE`：未找到可用的 `guazi-sso-login` skill
- `E_SSO_LOGIN_FAILED`：`guazi-sso-login` 登录态校验失败或未初始化
- `E_LOGIN_CONFIG_EMPTY`：传了 `--save-login-config` 但没有可保存的登录态参数
- `E_PAGE_NOT_FOUND`：页面不存在或接口返回 `404`
- `E_PAGE_FORBIDDEN`：当前账号无权限访问页面
- `E_CONFLUENCE_HTTP_ERROR`：Confluence 接口返回其他非成功状态

## Chrome 重启策略

- 默认不主动重启当前 Chrome
- 只有显式传入 `--restart-chrome` 时，才允许关闭并重启 Chrome 以开启远程调试端口
- 如果你不希望影响当前浏览器会话，请先手动使用调试模式启动 Chrome，再执行下载
- `start-chrome-debug.sh` 仍然只负责“已明确允许后”的启动/重启动作

## 常用问题排查

### 1) `curl http://127.0.0.1:9222/json/version` 返回 404

通常表示 `9222` 端口被非 Chrome 进程占用，或当前 Chrome 不是以调试参数启动。

排查命令：

```bash
lsof -nP -iTCP:9222 -sTCP:LISTEN
```

如果监听进程不是 `Google Chrome`，请先释放该端口或改用其他端口（例如 `CHROME_DEBUG_PORT=9223`）。

---

### 2) Chrome 已运行时，重启脚本后调试端口仍不可用

这通常是 Chrome 复用已有进程导致，新增启动参数没有生效。当前 `start-chrome-debug.sh` 已内置“优雅退出 + 强制结束 + 调试接口就绪校验”流程，建议直接使用：

```bash
bash ./run.sh --check --restart-chrome
```

或实际下载时带上 `--restart-chrome`。

---

### 3) 端口看起来已开启，但工具仍报 `E_CHROME_DEBUG_PORT_UNAVAILABLE`

工具不只检查端口监听，还会检查 CDP 接口可用性。请确认以下地址可以返回 JSON：

```bash
curl -fsS http://127.0.0.1:9222/json/version
```

若失败，通常是 Chrome 启动参数未生效或端口被占用。

---

### 4) 不想影响日常 Chrome 会话，如何降低风险

- 使用独立端口：`CHROME_DEBUG_PORT=9223`
- 保持监听地址为本机回环：`127.0.0.1`（不要改为 `0.0.0.0`）
- 必要时使用独立配置目录启动调试实例，避免和日常实例抢占进程状态

---

### 5) 每次都要重启，是否可以只做环境自检

可以。先执行：

```bash
bash ./run.sh --check
```

如果仅有调试端口问题，再执行：

```bash
bash ./run.sh --check --restart-chrome
```

这样可以把问题定位在 Chrome/CDP，而不是下载逻辑本身。

## 兼容性矩阵

| 项目                | 要求        | 说明                                                                |
| ------------------- | ----------- | ------------------------------------------------------------------- |
| Python              | `>=3.11`    | 依赖 `tomllib` 等标准库能力                                         |
| `uv`                | 必需        | 缺失时直接报错，不自动安装                                          |
| `guazi-sso-login`   | 默认需要    | 默认鉴权来源；可切换为 Chrome/手动 Cookie                           |
| Chrome              | 可选        | 仅在 `cookie-source=chrome` 时需要                                  |
| Chrome 远程调试端口 | 默认 `9222` | 仅在 `cookie-source=chrome` 时需要，可通过 `CHROME_DEBUG_PORT` 覆盖 |
| Confluence 登录态   | 必需        | 需具备可用 Wiki Cookie（SSO/Chrome/手动）                           |

## 环境变量

- `CWIKI_DOWNLOAD_SAVE_DIR`：自定义保存目录，优先级高于默认 `docs`
- `CWIKI_DOWNLOAD_WORKSPACE_ROOT`：显式指定工程根目录
- `CONFLUENCE_BASE_URL`：Confluence 站点地址
- `CONFLUENCE_DOMAIN`：Cookie 过滤域名
- `CHROME_DEBUG_PORT`：Chrome 远程调试端口，默认 `9222`
- `CWIKI_COOKIE_SOURCE`：登录态来源，支持 `sso` / `chrome` / `manual`；默认 `sso`
- `CWIKI_USE_MANUAL_COOKIE`：是否启用手动 Cookie 模式（`1/true/yes/on` 为开启）
- `CWIKI_MANUAL_COOKIE`：手动传入的完整 Cookie Header 字符串
- `GUAZI_SSO_SKILL_ROOT`：显式指定 `guazi-sso-login` skill 目录
- `CWIKI_DOWNLOAD_CONFIG_FILE`：用户级登录配置文件路径（默认 `~/.agents/config/cwiki-download.env`）
- `CWIKI_DOWNLOAD_FETCH_COMMENTS`：是否拉取评论并追加评论章节（默认开启；`0/false/no/off` 为关闭）

额外说明：

- 如果未显式设置 `CWIKI_DOWNLOAD_WORKSPACE_ROOT`，工具会把**调用 `run.sh` 时的工作目录**视为“工程根目录”，并在这里查找 `.env`
- 若根目录存在 `.env` 文件，会在进程启动时自动加载其中的 `KEY=VALUE` 到环境变量
- 可通过 `--save-login-config` 将登录态参数保存到用户级配置（默认）或项目级 `.env`

鉴权优先级说明：

- 当 `CWIKI_USE_MANUAL_COOKIE` 开启且 `CWIKI_MANUAL_COOKIE` 非空时，优先使用手动 Cookie 鉴权
- 否则读取 `CWIKI_COOKIE_SOURCE`：`sso` / `chrome` / `manual`
- `CWIKI_COOKIE_SOURCE` 未设置时，默认使用 `sso`
