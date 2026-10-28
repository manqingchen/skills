# cwiki-upload

将本地 Markdown 发布到 Confluence 的 Python 工具。

## 功能

- 默认通过 `guazi-sso-login` 获取并校验 Wiki Cookie（可切换）
- 根据 `spaceKey + parentPageId` 创建子页面
- 支持按 `pageId` 直接更新指定页面
- 支持按页面链接更新（`viewpage.action?pageId=...` 或 `display/<space>/<title>`）
- 支持在页面发布成功后于页面底部追加评论
- 支持仅向指定页面追加评论，不更新页面正文
- Markdown 存在可生成目录的二级及以下标题时，自动在 Wiki 正文中插入 Confluence 目录宏
- 支持 `--prettify` 按 Markdown 结构美化 Wiki；Agent 会自动生成方案 JSON
- 页面同名默认拒绝覆盖，显式 `--update` 才允许更新
- 页面标题默认使用 Markdown 文件名，可用 `--title` 覆盖
- 只上传正文中实际引用到的本地相对路径资源（图片与本地文件链接）
- 默认 dry-run，显式 `--apply` 才会真正写入 Confluence

## 架构说明

- `scripts/markdown_storage.py`：负责 Markdown 到 Confluence storage XHTML 的基础转换，包括代码块、Mermaid、表格、附件、目录宏等。
- `scripts/prettify_storage.py`：负责在已生成的 storage XHTML 基础上做美化后处理。
- `BEAUTIFY.md`：记录 Agent 使用的美化策略；普通用户不需要阅读。

## 安装

```bash
npx skills add "https://git.guazi-corp.com/yanliwan/agent-skill.git" --skill cwiki-upload
```

## 关联 Skill

- `guazi-sso-login`：<https://git.guazi-corp.com/yanliwan/agent-skill/tree/master/guazi-sso-login>

## 对话中使用（触发 skill）

在对话里直接给出上传目标即可，助手会按 skill 规则先 dry-run，再在你确认后执行 `--apply`。例如：

```text
请用 cwiki-upload 把 ./docs/release-note.md 发布到 Confluence，
spaceKey=AI，parentPageId=123456
```

需要覆盖同名页面时，明确说明“允许更新同名页面”，助手才会走 `--update`。

也可以直接指定目标页面更新，例如：

```text
请用 cwiki-upload 把 ./docs/Skills Hub-645245807.llm.md 更新到 pageId=649300169。
```

或：

```text
请用 cwiki-upload 把 ./docs/Skills Hub-645245807.llm.md 更新到
https://cwiki.guazi.com/display/CTOBTECH/Skills+Hub
```

发布成功后追加评论：

```text
请用 cwiki-upload 把 ./docs/Skills Hub-645245807.llm.md 更新到 pageId=649300169，
并在页面底部追加评论：本次内容已同步，若有问题请直接回复本评论。
```

如果评论较长，也可以从本地文件读取：

```text
请用 cwiki-upload 把 ./docs/Skills Hub-645245807.llm.md 更新到 pageId=649300169，
并把 ./docs/comment.md 作为页面底部评论一起发布。
```

如果只想追加评论、不更新页面正文：

```text
请用 cwiki-upload 给 pageId=649300169 追加一条页尾评论，
评论内容来自 ./docs/comment.md，不要更新页面正文。
```

如果需要指定登录态来源，也可以直接在会话里表达：

```text
这次用 cwiki-upload 发布 ./docs/release-note.md，使用 Chrome 登录态
```

```text
这次用 cwiki-upload 发布 ./docs/release-note.md，使用这个 Cookie：<Cookie>
```

```text
以后都用 SSO 登录态上传 cwiki，先做一次自检并保存配置
```

### 推荐对话模板（可直接复制）

最短可用版（依赖环境变量中的 `CONFLUENCE_SPACE_KEY` 和 `CONFLUENCE_PARENT_PAGE_ID`）：

```text
请用 cwiki-upload 发布 ./docs/release-note.md 到 Confluence。
先给我 dry-run 摘要，确认后再执行写入。
```

完整参数版（推荐）：

```text
请用 cwiki-upload 发布 ./docs/release-note.md 到 Confluence，
spaceKey=AI，parentPageId=123456，title=2026-03-发布说明。
先 dry-run，不要直接写入；等我确认后再 apply。
```

同名更新版（仅在你确认要覆盖时使用）：

```text
请用 cwiki-upload 发布 ./docs/release-note.md 到 Confluence，
spaceKey=AI，parentPageId=123456。
如果检测到同名页面，允许 update；先给 dry-run 摘要，我确认后再 apply。
```

## 参数说明

CLI 参数：

- `<markdown_path>`：要发布的本地 Markdown 文件路径（位置参数）
- `--space-key <SPACE_KEY>`：Confluence 空间标识；可被环境变量 `CONFLUENCE_SPACE_KEY` 提供默认值
- `--parent-page-id <PAGE_ID>`：父页面 ID；新页面将作为该父页面的子页面创建，可被 `CONFLUENCE_PARENT_PAGE_ID` 提供默认值
- `--page-id <PAGE_ID>`：目标页面 ID；指定后直接更新该页面（不再依赖 `spaceKey + parentPageId`）
- `--page-url <URL>`：目标页面链接；支持 `viewpage.action?pageId=...` 或 `display/<space>/<title>` 形式
- `--title <TITLE>`：页面标题；不传时默认使用 Markdown 文件名（去扩展名）
- `--comment <TEXT>`：发布成功后在页面底部追加一条评论，支持轻量 Markdown
- `--comment-file <FILE>`：从本地文件读取评论内容，适合长评论；不可与 `--comment` 同时使用
- `--apply-comment-only`：仅向目标页面追加评论，不更新页面正文；需配合 `--page-id`/`--page-url` 与评论参数使用
- `--update`：允许同名页面更新；不传时同名默认拒绝覆盖
- `--prettify`：启用 Wiki 正文美化；Agent 会自动分析 Markdown 并生成美化方案，直接使用 CLI 时无方案文件则使用脚本兜底规则
- `--prettify-plan-file <FILE>`：高级参数，Agent 自动生成并传入；仅支持 `operations` DSL，用于描述标题层级、关键词强调、tag 标签、表格风格、提示面板和布局操作
- `--apply`：执行实际写入（创建/更新/附件上传）；不传时仅 dry-run
- `--check`：仅执行环境和依赖自检，不执行发布
- `--restart-chrome`：仅在需要时允许重启 Chrome 并开启调试端口
- `--cookie-source <sso|chrome|manual>`：指定登录态来源；未配置时默认 `sso`
- `--manual-cookie "<Cookie>"`：本次使用手动 Cookie（会自动等价为 `--cookie-source manual`）
- `--sso-skill-root <DIR>`：显式指定 `guazi-sso-login` skill 根目录
- `--save-login-config [user|project]`：保存本次登录态配置；不带值或传 `user` 写入用户级配置，传 `project` 写入工作区 `.env`

对话常用语义参数：

- “允许更新同名页面” => `--update`
- “更新到 pageId=xxx” => `--page-id xxx`
- “更新到页面链接” => `--page-url <url>`
- “在页面底部追加评论：xxx” => `--comment "xxx"`
- “把 ./docs/comment.md 作为评论发布” => `--comment-file ./docs/comment.md`
- “只追加评论，不更新页面正文” => `--apply-comment-only`
- “美化一下 Wiki 内容 / 使用背景色和突出文字颜色” => `--prettify`，Agent 会自动生成并传入美化方案 JSON
- “确认后再写入 / 再 apply” => 先 dry-run，确认后再 `--apply`
- “先自检” => `--check`
- “这次用 Chrome 登录态” => `--cookie-source chrome`
- “这次用这个 Cookie：<Cookie>” => `--manual-cookie "<Cookie>"`
- “以后都用 Chrome 登录态” => `--cookie-source chrome --save-login-config`
- “只对当前项目生效” => `--save-login-config project`

参数优先级：

- 显式 CLI 参数（例如 `--cookie-source`、`--manual-cookie`）和调用方显式环境变量优先级最高
- 其次是项目级 `.env`
- 其次是用户级配置文件（默认 `~/.agents/config/cwiki-upload.env`）
- 最后才使用默认值（默认登录态来源 `sso`、默认调试端口 `9222`）

## 环境变量

- `CONFLUENCE_SPACE_KEY`：默认 spaceKey（可被 `--space-key` 覆盖）
- `CONFLUENCE_PARENT_PAGE_ID`：默认 parentPageId（可被 `--parent-page-id` 覆盖）
- `CONFLUENCE_BASE_URL`：Confluence 站点地址，默认 `https://cwiki.guazi.com`
- `CONFLUENCE_DOMAIN`：Cookie 匹配域名，默认 `.guazi.com`
- `CHROME_DEBUG_PORT`：Chrome 调试端口，默认 `9222`
- `CWIKI_COOKIE_SOURCE`：登录态来源，支持 `sso` / `chrome` / `manual`；默认 `sso`
- `CWIKI_USE_MANUAL_COOKIE`：是否启用手动 Cookie 模式（`1/true/yes/on` 为开启）
- `CWIKI_MANUAL_COOKIE`：手动传入的完整 Cookie Header 字符串
- `GUAZI_SSO_SKILL_ROOT`：显式指定 `guazi-sso-login` skill 目录
- `CWIKI_UPLOAD_WORKSPACE_ROOT`：显式指定工程根目录
- `CWIKI_UPLOAD_CONFIG_FILE`：用户级登录配置文件路径（默认 `~/.agents/config/cwiki-upload.env`）

额外说明：

- 如果未显式设置 `CWIKI_UPLOAD_WORKSPACE_ROOT`，工具会把调用命令时的当前工作目录视为“工程根目录”，并在这里查找 `.env`
- 可通过 `--save-login-config` 将登录态参数保存到用户级配置（默认）或项目级 `.env`

鉴权优先级说明：

- 当 `CWIKI_USE_MANUAL_COOKIE` 开启且 `CWIKI_MANUAL_COOKIE` 非空时，优先使用手动 Cookie 鉴权
- 否则读取 `CWIKI_COOKIE_SOURCE`：`sso` / `chrome` / `manual`
- `CWIKI_COOKIE_SOURCE` 未设置时，默认使用 `sso`

## 结构化错误输出

```text
[ERROR] E_XXX
原因：...
建议：...
```

常见错误码：

- `E_MARKDOWN_NOT_FOUND`：输入的 Markdown 文件不存在
- `E_MISSING_TARGET`：未提供 `pageId/pageUrl` 且缺少 `spaceKey + parentPageId`
- `E_INVALID_PARENT_PAGE_ID`：`parentPageId` 不是纯数字
- `E_INVALID_PAGE_ID`：`pageId` 不是纯数字
- `E_INVALID_PAGE_URL`：页面链接格式不支持
- `E_PAGE_ALREADY_EXISTS`：同名页面已存在且未传 `--update`
- `E_CHROME_DEBUG_PORT_UNAVAILABLE`：Chrome 调试端口不可用且未允许重启
- `E_SSO_LOGIN_UNAVAILABLE`：未找到可用的 `guazi-sso-login` skill
- `E_SSO_LOGIN_FAILED`：`guazi-sso-login` 登录态校验失败或未初始化
- `E_LOGIN_CONFIG_EMPTY`：传了 `--save-login-config` 但没有可保存的登录态参数
