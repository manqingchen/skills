import argparse
import asyncio
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import httpx

from .chrome_cookie import CHROME_DEBUG_PORT_DEFAULT, get_cookie_header_for_domain
from .common import (
  CWIKI_COOKIE_SOURCE_ENV,
  CWIKI_MANUAL_COOKIE_ENV,
  CWIKI_UPLOAD_CONFIG_FILE_ENV,
  CWIKI_USE_MANUAL_COOKIE_ENV,
  CheckResult,
  CwikiUploadError,
  GUAZI_SSO_SKILL_ROOT_ENV,
  LocalReference,
  MermaidBlock,
)
from .confluence_client import (
  CONFLUENCE_BASE_URL_DEFAULT,
  CONFLUENCE_DOMAIN_DEFAULT,
  create_footer_comment,
  create_page,
  find_child_page_by_title,
  find_page_by_space_and_title,
  get_page_by_id,
  list_attachment_titles,
  update_page,
  upload_attachment,
)
from .local_refs import collect_local_references
from .markdown_storage import (
  build_comment_storage_body,
  build_storage_body,
  extract_mermaid_blocks,
)
from .publisher import (
  PublisherDeps,
  publish_comment_only as _publish_comment_only,
  publish_markdown as _publish_markdown,
  render_mermaid_with_kroki,
  resolve_existing_page,
)
from .target_resolver import (
  resolve_comment_text,
  resolve_space_and_parent,
  resolve_space_and_title_from_page_url,
  resolve_target_page_id,
  resolve_title,
)
from .workspace_config import (
  apply_login_cli_config,
  get_user_config_path,
  get_workspace_root,
  load_env_file,
  load_login_config,
  load_workspace_env,
  write_env_values,
  write_workspace_env_values,
)

_render_mermaid_with_kroki = render_mermaid_with_kroki


def get_start_chrome_debug_script_path() -> Path:
  return Path(__file__).resolve().parent.parent / "start-chrome-debug.sh"


def is_debug_port_open(port: int) -> bool:
  try:
    with socket.create_connection(("127.0.0.1", port), timeout=1):
      return True
  except OSError:
    return False


def ensure_chrome_debugging(*, allow_restart: bool) -> None:
  debug_port = int(os.getenv("CHROME_DEBUG_PORT", str(CHROME_DEBUG_PORT_DEFAULT)))
  if is_debug_port_open(debug_port):
    return

  if not allow_restart:
    raise CwikiUploadError(
      "E_CHROME_DEBUG_PORT_UNAVAILABLE",
      f"Chrome 调试端口 {debug_port} 未开启。",
      "请先手动开启 Chrome 远程调试端口，或显式传入 --restart-chrome 允许脚本重启 Chrome。",
    )

  script_path = get_start_chrome_debug_script_path()
  if not script_path.exists():
    raise CwikiUploadError(
      "E_CHROME_DEBUG_SCRIPT_MISSING",
      f"未找到 Chrome 调试脚本：{script_path}",
      "请确认 skill 目录完整，或手动开启 Chrome 远程调试端口后重试。",
    )
  subprocess.run(["bash", str(script_path)], check=True)


def _is_manual_cookie_enabled() -> bool:
  return os.getenv(CWIKI_USE_MANUAL_COOKIE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_cookie_source() -> str:
  if _is_manual_cookie_enabled() and os.getenv(CWIKI_MANUAL_COOKIE_ENV, "").strip():
    return "manual"

  source = os.getenv(CWIKI_COOKIE_SOURCE_ENV, "").strip().lower()
  if source in {"manual", "chrome", "sso"}:
    return source

  return "sso"


def resolve_guazi_sso_skill_root() -> Path:
  configured = os.getenv(GUAZI_SSO_SKILL_ROOT_ENV, "").strip()
  candidates: list[Path] = []
  if configured:
    candidates.append(Path(configured).expanduser())

  current_skill_root = Path(__file__).resolve().parent.parent
  candidates.extend([
    current_skill_root.parent / "guazi-sso-login",
    Path.home() / ".agents" / "skills" / "guazi-sso-login",
    Path.home() / ".codex" / "skills" / "guazi-sso-login",
  ])

  for candidate in candidates:
    if (candidate / "run.sh").is_file():
      return candidate

  raise CwikiUploadError(
    "E_SSO_LOGIN_UNAVAILABLE",
    "未找到 guazi-sso-login skill，无法获取默认 SSO 登录态。",
    f"请安装 guazi-sso-login，或设置 {GUAZI_SSO_SKILL_ROOT_ENV}，或显式设置 {CWIKI_COOKIE_SOURCE_ENV}=chrome 使用 Chrome 登录态。",
  )


def get_cookie_header_from_sso() -> str:
  skill_root = resolve_guazi_sso_skill_root()
  result = subprocess.run(
    ["bash", str(skill_root / "run.sh"), "wiki", "--validate", "--plain"],
    check=False,
    text=True,
    capture_output=True,
  )
  cookie = result.stdout.strip()
  if result.returncode == 0 and cookie:
    return cookie

  detail = result.stderr.strip() or result.stdout.strip() or f"退出码：{result.returncode}"
  raise CwikiUploadError(
    "E_SSO_LOGIN_FAILED",
    detail,
    "请先在单独会话中使用 guazi-sso-login 初始化登录态，或显式设置 CWIKI_COOKIE_SOURCE=chrome 使用 Chrome 登录态。",
  )


async def resolve_cookie_header(*, allow_chrome_restart: bool) -> str:
  cookie_source = resolve_cookie_source()
  if cookie_source == "manual":
    return os.getenv(CWIKI_MANUAL_COOKIE_ENV, "").strip()

  if cookie_source == "sso":
    return get_cookie_header_from_sso()

  ensure_chrome_debugging(allow_restart=allow_chrome_restart)
  base_url = os.getenv("CONFLUENCE_BASE_URL", CONFLUENCE_BASE_URL_DEFAULT)
  domain = os.getenv("CONFLUENCE_DOMAIN", CONFLUENCE_DOMAIN_DEFAULT)
  return await get_cookie_header_for_domain(
    domain,
    chrome_debug_port=int(os.getenv("CHROME_DEBUG_PORT", str(CHROME_DEBUG_PORT_DEFAULT))),
    confluence_base_url=base_url,
  )


def format_check_report(results: list[CheckResult]) -> str:
  return "\n".join(f"[{item.status}] {item.name}: {item.detail}" for item in results)


async def run_preflight_check() -> list[CheckResult]:
  results: list[CheckResult] = []
  uv_path = shutil.which("uv")
  results.append(CheckResult("uv", "PASS" if uv_path else "FAIL", uv_path or "未检测到 uv"))
  workspace_root = get_workspace_root()
  load_login_config(workspace_root)
  base_url = os.getenv("CONFLUENCE_BASE_URL", CONFLUENCE_BASE_URL_DEFAULT)
  cookie_source = resolve_cookie_source()
  if cookie_source == "manual":
    results.append(CheckResult("chrome_debug_port", "PASS", "手动 Cookie 模式已启用，跳过端口检查"))
    cookie_header = os.getenv(CWIKI_MANUAL_COOKIE_ENV, "").strip()
    results.append(CheckResult("confluence_cookie", "PASS", f"已使用环境变量 {CWIKI_MANUAL_COOKIE_ENV}"))
  elif cookie_source == "sso":
    try:
      sso_root = resolve_guazi_sso_skill_root()
      results.append(CheckResult("sso_login", "PASS", str(sso_root)))
      cookie_header = get_cookie_header_from_sso()
      results.append(CheckResult("confluence_cookie", "PASS", "已通过 guazi-sso-login 获取 Wiki Cookie"))
    except CwikiUploadError as exc:
      results.append(CheckResult("sso_login", "FAIL", exc.message))
      results.append(CheckResult("confluence_cookie", "FAIL", exc.suggestion or str(exc)))
      return results
  else:
    debug_port = int(os.getenv("CHROME_DEBUG_PORT", str(CHROME_DEBUG_PORT_DEFAULT)))
    port_open = is_debug_port_open(debug_port)
    results.append(CheckResult("chrome_debug_port", "PASS" if port_open else "FAIL", f"127.0.0.1:{debug_port}"))

    if not port_open:
      results.append(CheckResult("confluence_cookie", "FAIL", "Chrome 调试端口不可用，无法检查登录态"))
      return results

    domain = os.getenv("CONFLUENCE_DOMAIN", CONFLUENCE_DOMAIN_DEFAULT)
    try:
      cookie_header = await get_cookie_header_for_domain(
        domain,
        chrome_debug_port=debug_port,
        confluence_base_url=base_url,
      )
      results.append(CheckResult("confluence_cookie", "PASS", "已通过 Chrome 调试端口获取 Confluence Cookie"))
    except Exception as exc:
      results.append(CheckResult("confluence_cookie", "FAIL", str(exc)))
      return results

  try:
    async with httpx.AsyncClient(timeout=10) as client:
      response = await client.get(
        f"{base_url.rstrip('/')}/rest/api/group",
        headers={"Cookie": cookie_header, "Content-Type": "application/json"},
      )
      response.raise_for_status()
    results.append(CheckResult("confluence_api", "PASS", f"{base_url.rstrip('/')}/rest/api/group"))
  except Exception as exc:
    results.append(CheckResult("confluence_api", "FAIL", str(exc)))
  return results


def normalize_cli_error(error: Exception) -> CwikiUploadError:
  if isinstance(error, CwikiUploadError):
    return error
  if isinstance(error, httpx.HTTPStatusError):
    status = error.response.status_code
    response_text = ""
    try:
      response_text = error.response.text
    except Exception:
      response_text = ""
    if status == 404:
      return CwikiUploadError("E_PAGE_NOT_FOUND", f"页面不存在：{error.request.url}", "请确认父页面或目标页面仍然存在。")
    if status in {401, 403}:
      return CwikiUploadError("E_PAGE_FORBIDDEN", f"无权访问页面：{error.request.url}", "请确认当前账号在该空间有读写权限。")
    if status == 409:
      return CwikiUploadError("E_VERSION_CONFLICT", f"页面版本冲突：{error.request.url}", "请重新拉取最新版本后再更新。")
    detail = f"Confluence 接口异常：{status}"
    if response_text:
      snippet = response_text.strip()
      if len(snippet) > 500:
        snippet = snippet[:500] + "..."
      detail = f"{detail}，响应：{snippet}"
    return CwikiUploadError("E_CONFLUENCE_HTTP_ERROR", detail, "请稍后重试或检查站点配置。")
  return CwikiUploadError("E_UNEXPECTED", str(error) or error.__class__.__name__, "请查看日志后重试。")


def _build_publisher_deps() -> PublisherDeps:
  return PublisherDeps(
    get_workspace_root=get_workspace_root,
    load_login_config=load_login_config,
    resolve_cookie_header=resolve_cookie_header,
    async_client_factory=httpx.AsyncClient,
    get_page_by_id=get_page_by_id,
    update_page=update_page,
    create_page=create_page,
    find_page_by_space_and_title=find_page_by_space_and_title,
    find_child_page_by_title=find_child_page_by_title,
    list_attachment_titles=list_attachment_titles,
    upload_attachment=upload_attachment,
    create_footer_comment=create_footer_comment,
    render_mermaid_with_kroki=_render_mermaid_with_kroki,
    collect_local_references=collect_local_references,
  )


async def publish_markdown(
  *,
  markdown_path: Path,
  space_key: str | None,
  parent_page_id: str | None,
  target_page_id: str | None,
  target_page_url: str | None,
  title: str | None,
  comment_text: str | None,
  allow_update: bool,
  apply_changes: bool,
  allow_chrome_restart: bool,
  prettify: bool,
  prettify_plan_path: Path | None,
) -> str:
  return await _publish_markdown(
    markdown_path=markdown_path,
    space_key=space_key,
    parent_page_id=parent_page_id,
    target_page_id=target_page_id,
    target_page_url=target_page_url,
    title=title,
    comment_text=comment_text,
    allow_update=allow_update,
    apply_changes=apply_changes,
    allow_chrome_restart=allow_chrome_restart,
    prettify=prettify,
    prettify_plan_path=prettify_plan_path,
    deps=_build_publisher_deps(),
  )


async def publish_comment_only(
  *,
  target_page_id: str | None,
  target_page_url: str | None,
  comment_text: str,
  allow_chrome_restart: bool,
) -> str:
  return await _publish_comment_only(
    target_page_id=target_page_id,
    target_page_url=target_page_url,
    comment_text=comment_text,
    allow_chrome_restart=allow_chrome_restart,
    deps=_build_publisher_deps(),
  )


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="将本地 Markdown 发布到 Confluence")
  parser.add_argument("markdown_path", nargs="?", help="本地 Markdown 文件路径")
  parser.add_argument("--space-key", dest="space_key", help="Confluence Space Key")
  parser.add_argument("--parent-page-id", dest="parent_page_id", help="父页面 ID")
  parser.add_argument("--page-id", dest="page_id", help="目标页面 ID，指定后将直接更新该页面")
  parser.add_argument("--page-url", dest="page_url", help="目标页面链接，支持 viewpage 或 display 链接")
  parser.add_argument("--title", dest="title", help="页面标题，默认使用文件名")
  parser.add_argument("--comment", dest="comment", help="发布成功后在页面底部追加一条评论，支持轻量 Markdown")
  parser.add_argument("--comment-file", dest="comment_file", help="从本地文件读取评论内容，适合长评论")
  parser.add_argument("--check", action="store_true", help="只检查运行环境与登录态，不执行发布")
  parser.add_argument("--apply", action="store_true", help="确认执行创建/更新页面及上传附件")
  parser.add_argument("--apply-comment-only", action="store_true", help="仅向目标页面追加评论，不更新页面正文")
  parser.add_argument("--update", action="store_true", help="允许同名页面更新（默认同名拒绝）")
  parser.add_argument("--prettify", action="store_true", help="按 Markdown 结构为 Wiki 正文应用颜色与区块美化")
  parser.add_argument("--prettify-plan-file", help="Agent 生成的美化方案 JSON 文件；需配合 --prettify 使用")
  parser.add_argument("--restart-chrome", action="store_true", help="调试端口不可用时允许重启 Chrome")
  parser.add_argument("--cookie-source", choices=["sso", "chrome", "manual"], help="登录态来源；默认读取配置，未配置时使用 sso")
  parser.add_argument("--manual-cookie", help="本次使用手动 Cookie；会自动等价为 --cookie-source manual")
  parser.add_argument("--sso-skill-root", help="guazi-sso-login skill 目录；未传时自动查找同项目或全局安装路径")
  parser.add_argument(
    "--save-login-config",
    nargs="?",
    const="user",
    choices=["user", "project"],
    help="保存本次登录态来源配置；默认写入用户级配置，传 project 时写入工作区 .env",
  )
  return parser


def main() -> None:
  parser = build_parser()
  args = parser.parse_args()
  workspace_root = get_workspace_root()
  load_login_config(workspace_root)
  try:
    saved_env_path = apply_login_cli_config(args, workspace_root=workspace_root)
    if args.check:
      results = asyncio.run(run_preflight_check())
      print(format_check_report(results))
      if saved_env_path:
        print(f"[INFO] login_config_saved: {saved_env_path}")
      if any(item.status == "FAIL" for item in results):
        raise SystemExit(1)
      return

    target_page_id = resolve_target_page_id(page_id=args.page_id, page_url=args.page_url)
    resolved_comment_text = resolve_comment_text(comment_text=args.comment, comment_file=args.comment_file)

    if args.apply_comment_only:
      if not target_page_id and not args.page_url:
        raise CwikiUploadError(
          "E_MISSING_COMMENT_TARGET",
          "评论-only 模式缺少目标页面。",
          "请传入 --page-id 或 --page-url。",
        )
      if not resolved_comment_text:
        raise CwikiUploadError(
          "E_MISSING_COMMENT",
          "评论-only 模式缺少评论内容。",
          "请传入 --comment 或 --comment-file。",
        )
      result = asyncio.run(
        publish_comment_only(
          target_page_id=target_page_id,
          target_page_url=args.page_url,
          comment_text=resolved_comment_text,
          allow_chrome_restart=args.restart_chrome,
        )
      )
    else:
      if not args.markdown_path:
        parser.error("缺少 markdown_path")
      prettify_plan_path = Path(args.prettify_plan_file).expanduser() if args.prettify_plan_file else None
      if prettify_plan_path and not args.prettify:
        raise CwikiUploadError(
          "E_PRETTIFY_PLAN_WITHOUT_PRETTIFY",
          "传入了 --prettify-plan-file 但未启用 --prettify。",
          "请同时传入 --prettify，或移除 --prettify-plan-file。",
        )

      space_key: str | None = None
      parent_page_id: str | None = None
      if target_page_id is None and not args.page_url:
        space_key, parent_page_id = resolve_space_and_parent(
          space_key=args.space_key,
          parent_page_id=args.parent_page_id,
        )
      result = asyncio.run(
        publish_markdown(
          markdown_path=Path(args.markdown_path).expanduser(),
          space_key=space_key,
          parent_page_id=parent_page_id,
          target_page_id=target_page_id,
          target_page_url=args.page_url,
          title=args.title,
          comment_text=resolved_comment_text,
          allow_update=args.update,
          apply_changes=args.apply,
          allow_chrome_restart=args.restart_chrome,
          prettify=args.prettify,
          prettify_plan_path=prettify_plan_path,
        )
      )
    if saved_env_path:
      print(f"[INFO] login_config_saved: {saved_env_path}")
    print(result)
  except SystemExit:
    raise
  except Exception as error:
    cli_error = normalize_cli_error(error)
    print(f"[ERROR] {cli_error.code}", file=sys.stderr)
    print(f"原因：{cli_error.message}", file=sys.stderr)
    if cli_error.suggestion:
      print(f"建议：{cli_error.suggestion}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
  main()
