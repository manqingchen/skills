# online-release

把「上线到预发」这段跑到无需界面：打 `-online` tag → 等流水线构建 → 发起部署审批 → 等审批 → 部署预发。全程直接调 Medusa 发布平台的 GraphQL，不启动浏览器。

## 安装

技能放在 `~/Documents/workspace/skills/online-release`，通过软链给各 agent 目录：

```bash
ln -s ~/Documents/workspace/skills/online-release ~/.zcode/skills/online-release
ln -s ~/Documents/workspace/skills/online-release ~/.agents/skills/online-release
```

## 凭据

平台认 cookie `GUAZISSO`，由 `guazi-sso-login` 用本机缓存的账号密码换取：

```bash
/usr/bin/python3 ~/.zcode/skills/guazi-sso-login/scripts/guazi_sso_login.py sso --validate --plain
```

也可以直接给：`export MEDUSA_COOKIE="GUAZISSO=xxx"`。

## 常用

```bash
node scripts/mr-master.mjs                  # 预演：把当前分支合进 master
node scripts/mr-master.mjs --yes            # 建 MR 并合并（幂等）
node scripts/release.mjs --jira OS-43558 --status      # 只读看状态
node scripts/release.mjs --tag <tag> --jira OS-43558 --dry-run   # 演练，不提交
node scripts/release.mjs --jira OS-43558 --yes         # 正式发布：pull → 合 master → push → 自动建 MR → tag → 审批 → 部署
node scripts/release.mjs --jira OS-43558 --yes --force # 没有新提交也强制重发（默认会跳过）
node scripts/release.mjs --tag <tag> --jira OS-43558 --yes    # 中途失败后用它续跑（跳过仓库动作/已建镜像/已过审批）
node scripts/release.mjs --jira OS-43558 --yes --stop-before-deploy  # 停在部署前
node scripts/watch-release.mjs --task <id> --merge-master --yes      # 等发布流程结束→自动合 master
```

发布前会自动体检：平台发布记录里若有「发布中」任务，会核对其代码是否已进 master，没进就拦住不让你发。

发布动作就是在当前分支上：`git pull` → `git pull origin master` → `git push` → 建 MR → `git tag <分支>-<时间戳>-online` 并推送，
然后去 Medusa 等镜像。tag 名与本地 `git tag-online` 一致，打 tag 前默认跑 `pnpm tsc`（`--no-tsc` 跳过）。
MR 由脚本自动创建（只建不合，已有 open MR 复用，标题取主功能 feat 提交、描述带 Jira/cwiki 链接）；
`--no-mr` 可跳过。`mr-master.mjs` 负责「合并」，会复用已建好的 open MR。

重发是安全的：脚本先看本分支自上次发布（最近的 `<分支>-<时间戳>-online` tag）以来有没有新的非合并提交，
没有就跳过（`NO_CHANGES`，`--force` 可强制）；有的话正常重发，并在新版本审批通过后、建新任务之前
把**本分支**上次留下的进行中任务取消掉（只认本分支 tag，不碰别人），否则部署包被占着。

发完记得收尾：任务在平台点掉前一直 `running` 占着部署包（挡住别人的 `DEPLOY_BUSY`），而它的 commit 没进 master
又会让别人发布的体检报 `IN_PROGRESS_UNMERGED`——所以发到预发后尽快跑一次 `mr-master.mjs --yes`。

完整参数与失败码见 [SKILL.md](SKILL.md)。

## 自测

```bash
node scripts/selftest.mjs
```

## 文件

| 文件 | 作用 |
| --- | --- |
| `scripts/release.mjs` | 主流程 CLI（tag → 等构建 → 审批 → 部署） |
| `scripts/mr-master.mjs` | 建 MR 并合并到 master（幂等，默认预演） |
| `scripts/watch-release.mjs` | 轮询发布任务状态，结束后可自动合 master |
| `scripts/medusa.mjs` | GraphQL 操作定义、客户端、状态判定 |
| `scripts/session.mjs` | 通过 guazi-sso-login 取 GUAZISSO |
| `scripts/mock-server.mjs` / `scripts/selftest.mjs` | 离线 mock 与五用例回归 |
