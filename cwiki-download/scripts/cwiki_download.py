import argparse
import asyncio
from dataclasses import dataclass
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

from .chrome_cookie import CHROME_DEBUG_PORT_DEFAULT, get_cookie_header_for_domain
from .comments import (
  CWIKI_FETCH_COMMENTS_ENV,
  enrich_inline_section_paths,
  fetch_comment_threads,
  inject_inline_markers_into_human_markdown,
  inject_inline_markers_into_llm_markdown,
  is_comment_fetch_enabled,
  write_comments_json,
)
from .confluence_client import (
  CONFLUENCE_BASE_URL_DEFAULT,
  CONFLUENCE_DOMAIN_DEFAULT,
  download_attachment,
  extract_page_locator,
  extract_referenced_attachment_download_urls,
  fetch_page,
  filter_referenced_attachments,
  list_attachments,
)
from .macro_transformer import (
  MacroAsset,
  extract_complex_macro_preview_titles,
  extract_storage_macro_preview_titles,
  extract_view_macro_links,
)
from .markdown_renderer import (
  derive_llm_markdown_filename,
  derive_markdown_filename,
  render_storage_to_llm_markdown,
  render_storage_to_markdown,
  resolve_output_dir,
)

CWIKI_USE_MANUAL_COOKIE_ENV = "CWIKI_USE_MANUAL_COOKIE"
CWIKI_MANUAL_COOKIE_ENV = "CWIKI_MANUAL_COOKIE"
CWIKI_COOKIE_SOURCE_ENV = "CWIKI_COOKIE_SOURCE"
GUAZI_SSO_SKILL_ROOT_ENV = "GUAZI_SSO_SKILL_ROOT"
CWIKI_DOWNLOAD_CONFIG_FILE_ENV = "CWIKI_DOWNLOAD_CONFIG_FILE"


@dataclass(slots=True)
class CwikiDownloadError(Exception):
  code: str
  message: str
  suggestion: str = ""

  def __str__(self) -> str:
    return f"{self.code}: {self.message}"


@dataclass(slots=True)
class CheckResult:
  name: str
  status: str
  detail: str


def sanitize_asset_filename(filename: str) -> str:
  cleaned = re.sub(r'[\\/:*?"<>|]+', "-", filename).strip()
  cleaned = cleaned.strip(". ") or "attachment"
  return cleaned


def get_asset_relative_url(page_id: str, filename: str) -> str:
  return f"assets/{page_id}/{quote(filename)}"


def build_page_metadata_header(page: dict) -> str:
  version = page.get("version") if isinstance(page.get("version"), dict) else {}
  number = str(version.get("number") or "").strip()
  updated_at = str(version.get("when") or "").strip()
  return "\n".join(
    [
      f"当前版本：{number or '未知'}",
      f"更新时间：{updated_at or '未知'}",
    ]
  )


def prepend_page_metadata(markdown: str, *, page: dict) -> str:
  header = build_page_metadata_header(page)
  body = markdown.lstrip()
  if not body:
    return header
  return f"{header}\n\n{body}"


def get_workspace_root() -> Path:
  env_root = os.getenv("CWIKI_DOWNLOAD_WORKSPACE_ROOT")
  if env_root:
    return Path(env_root).expanduser()
  try:
    result = subprocess.run(
      ["git", "rev-parse", "--show-toplevel"],
      check=True,
      capture_output=True,
      text=True,
    )
    git_root = result.stdout.strip()
    if git_root:
      return Path(git_root)
  except Exception:
    pass
  return Path.cwd()


def resolve_cli_output_dir(output_dir: str | None) -> Path | None:
  if not output_dir:
    return None

  path = Path(output_dir).expanduser()
  if path.is_absolute():
    return path
  return get_workspace_root() / path


def get_user_config_path() -> Path:
  configured = os.getenv(CWIKI_DOWNLOAD_CONFIG_FILE_ENV, "").strip()
  if configured:
    return Path(configured).expanduser()
  return Path.home() / ".agents" / "config" / "cwiki-download.env"


def load_env_file(env_path: Path, *, override: bool = False, protected_keys: set[str] | None = None) -> None:
  """加载 env 文件到当前进程环境变量。

  - 默认仅在变量尚未存在于 os.environ 时写入
  - override=True 时允许覆盖前一层配置，但不会覆盖 protected_keys
  - 只支持最常见的 KEY=VALUE 格式，忽略空行和以 # 开头的行
  """
  protected_keys = protected_keys or set()
  if not env_path.is_file():
    return

  try:
    content = env_path.read_text(encoding="utf-8")
  except Exception:
    return

  for raw_line in content.splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue
    if "=" not in line:
      continue
    key, value = line.split("=", 1)
    key = key.strip()
    if not key or key.startswith("#"):
      continue
    if key in os.environ and (not override or key in protected_keys):
      continue
    value = value.strip().strip("\"'")
    os.environ[key] = value


def load_workspace_env(workspace_root: Path) -> None:
  load_env_file(workspace_root / ".env")


def load_login_config(workspace_root: Path) -> None:
  protected_keys = set(os.environ)
  load_env_file(get_user_config_path(), protected_keys=protected_keys)
  load_env_file(workspace_root / ".env", override=True, protected_keys=protected_keys)


def _quote_env_value(value: str) -> str:
  escaped = value.replace("\\", "\\\\").replace('"', '\\"')
  return f'"{escaped}"'


def write_env_values(env_path: Path, updates: dict[str, str]) -> Path:
  lines: list[str] = []
  if env_path.is_file():
    try:
      lines = env_path.read_text(encoding="utf-8").splitlines()
    except Exception:
      lines = []

  remaining = dict(updates)
  output_lines: list[str] = []
  for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
      output_lines.append(line)
      continue
    key = stripped.split("=", 1)[0].strip()
    if key in remaining:
      output_lines.append(f"{key}={_quote_env_value(remaining.pop(key))}")
    else:
      output_lines.append(line)

  if remaining and output_lines and output_lines[-1].strip():
    output_lines.append("")
  for key, value in remaining.items():
    output_lines.append(f"{key}={_quote_env_value(value)}")

  env_path.parent.mkdir(parents=True, exist_ok=True)
  env_path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
  return env_path


def write_workspace_env_values(workspace_root: Path, updates: dict[str, str]) -> Path:
  return write_env_values(workspace_root / ".env", updates)


def apply_login_cli_config(args: argparse.Namespace, *, workspace_root: Path) -> Path | None:
  updates: dict[str, str] = {}

  if getattr(args, "cookie_source", None):
    os.environ[CWIKI_COOKIE_SOURCE_ENV] = args.cookie_source
    updates[CWIKI_COOKIE_SOURCE_ENV] = args.cookie_source

  if getattr(args, "sso_skill_root", None):
    sso_skill_root = str(Path(args.sso_skill_root).expanduser())
    os.environ[GUAZI_SSO_SKILL_ROOT_ENV] = sso_skill_root
    updates[GUAZI_SSO_SKILL_ROOT_ENV] = sso_skill_root

  if getattr(args, "manual_cookie", None):
    os.environ[CWIKI_USE_MANUAL_COOKIE_ENV] = "1"
    os.environ[CWIKI_MANUAL_COOKIE_ENV] = args.manual_cookie
    os.environ[CWIKI_COOKIE_SOURCE_ENV] = "manual"
    updates[CWIKI_USE_MANUAL_COOKIE_ENV] = "1"
    updates[CWIKI_MANUAL_COOKIE_ENV] = args.manual_cookie
    updates[CWIKI_COOKIE_SOURCE_ENV] = "manual"

  save_scope = getattr(args, "save_login_config", None)
  if save_scope:
    if not updates:
      raise CwikiDownloadError(
        "E_LOGIN_CONFIG_EMPTY",
        "没有可保存的登录态配置。",
        "请同时传入 --cookie-source、--manual-cookie 或 --sso-skill-root。",
      )
    if save_scope == "project":
      return write_workspace_env_values(workspace_root, updates)
    return write_env_values(get_user_config_path(), updates)

  return None


def get_start_chrome_debug_script_path() -> Path:
  return Path(__file__).resolve().parent.parent / "start-chrome-debug.sh"


def build_chrome_start_url(page_id_or_url: str, *, base_url: str) -> str:
  stripped = page_id_or_url.strip()
  if not stripped:
    return base_url.rstrip("/")

  parsed = urlsplit(stripped)
  if parsed.scheme and parsed.netloc:
    return stripped

  locator = extract_page_locator(stripped)
  page_id = locator.get("page_id")
  if page_id:
    return f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}"

  if stripped.startswith("/"):
    return f"{base_url.rstrip('/')}{stripped}"
  return base_url.rstrip("/")


def is_debug_port_open(port: int) -> bool:
  try:
    with socket.create_connection(("127.0.0.1", port), timeout=1):
      return True
  except OSError:
    return False


def ensure_chrome_debugging(*, allow_restart: bool, chrome_start_url: str | None = None) -> None:
  debug_port = int(os.getenv("CHROME_DEBUG_PORT", str(CHROME_DEBUG_PORT_DEFAULT)))
  if is_debug_port_open(debug_port):
    return

  if not allow_restart:
    raise CwikiDownloadError(
      "E_CHROME_DEBUG_PORT_UNAVAILABLE",
      f"Chrome 调试端口 {debug_port} 未开启。",
      "请先手动开启 Chrome 远程调试端口，或显式传入 --restart-chrome 允许脚本重启 Chrome。",
    )

  script_path = get_start_chrome_debug_script_path()
  if not script_path.exists():
    raise CwikiDownloadError(
      "E_CHROME_DEBUG_SCRIPT_MISSING",
      f"未找到 Chrome 调试脚本：{script_path}",
      "请确认 skill 目录完整，或手动开启 Chrome 远程调试端口后重试。",
    )
  env = os.environ.copy()
  if chrome_start_url:
    env["CHROME_START_URL"] = chrome_start_url
  subprocess.run(["bash", str(script_path)], check=True, env=env)


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
  candidates = []
  if configured:
    candidates.append(Path(configured).expanduser())

  current_skill_root = Path(__file__).resolve().parent.parent
  candidates.extend([
    current_skill_root.parent / "guazi-sso-login",
    Path.home() / ".agents" / "skills" / "guazi-sso-login",
    Path.home() / ".codex" / "skills" / "guazi-sso-login",
  ])

  for candidate in candidates:
    run_sh = candidate / "run.sh"
    if run_sh.is_file():
      return candidate

  raise CwikiDownloadError(
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
  raise CwikiDownloadError(
    "E_SSO_LOGIN_FAILED",
    detail,
    "请先完成 guazi-sso-login 首次登录，或显式设置 CWIKI_COOKIE_SOURCE=chrome 使用 Chrome 登录态。",
  )


async def resolve_cookie_header(*, allow_chrome_restart: bool, chrome_start_url: str | None = None) -> str:
  cookie_source = resolve_cookie_source()
  if cookie_source == "manual":
    return os.getenv(CWIKI_MANUAL_COOKIE_ENV, "").strip()

  if cookie_source == "sso":
    return get_cookie_header_from_sso()

  ensure_chrome_debugging(allow_restart=allow_chrome_restart, chrome_start_url=chrome_start_url)
  base_url = os.getenv("CONFLUENCE_BASE_URL", CONFLUENCE_BASE_URL_DEFAULT)
  domain = os.getenv("CONFLUENCE_DOMAIN", CONFLUENCE_DOMAIN_DEFAULT)
  return await get_cookie_header_for_domain(
    domain,
    chrome_debug_port=int(os.getenv("CHROME_DEBUG_PORT", str(CHROME_DEBUG_PORT_DEFAULT))),
    confluence_base_url=base_url,
  )


def write_page_bundle(
  *,
  output_dir: Path,
  page_id: str,
  page_title: str,
  markdown: str,
  llm_markdown: str,
  macro_assets: list[MacroAsset],
) -> Path:
  output_dir.mkdir(parents=True, exist_ok=True)
  asset_dir = output_dir / "assets" / page_id
  asset_dir.mkdir(parents=True, exist_ok=True)

  for asset in macro_assets:
    (asset_dir / asset.filename).write_text(asset.content, encoding="utf-8")

  markdown_path = output_dir / derive_markdown_filename(page_title, page_id)
  llm_markdown_path = output_dir / derive_llm_markdown_filename(page_title, page_id)
  markdown_path.write_text(markdown, encoding="utf-8")
  llm_markdown_path.write_text(llm_markdown, encoding="utf-8")
  return markdown_path


def _is_target_dir_writable(target_dir: Path) -> bool:
  probe_dir = target_dir if target_dir.exists() else next((parent for parent in [target_dir, *target_dir.parents] if parent.exists()), target_dir.parent)
  return os.access(probe_dir, os.W_OK)


async def run_preflight_check(output_dir: Path | None = None) -> list[CheckResult]:
  results: list[CheckResult] = []
  uv_path = shutil.which("uv")
  results.append(CheckResult("uv", "PASS" if uv_path else "FAIL", uv_path or "未检测到 uv"))

  python_ok = sys.version_info >= (3, 11)
  results.append(
    CheckResult(
      "python",
      "PASS" if python_ok else "FAIL",
      f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
  )

  workspace_root = get_workspace_root()
  load_workspace_env(workspace_root)
  target_dir = output_dir or resolve_output_dir(workspace_root)
  writable = _is_target_dir_writable(target_dir)
  results.append(CheckResult("output_dir", "PASS" if writable else "FAIL", str(target_dir)))

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
    except CwikiDownloadError as exc:
      results.append(CheckResult("sso_login", "FAIL", exc.message))
      results.append(CheckResult("confluence_cookie", "FAIL", exc.suggestion or str(exc)))
      return results
  else:
    debug_port = int(os.getenv("CHROME_DEBUG_PORT", str(CHROME_DEBUG_PORT_DEFAULT)))
    port_open = is_debug_port_open(debug_port)
    results.append(
      CheckResult(
        "chrome_debug_port",
        "PASS" if port_open else "FAIL",
        f"127.0.0.1:{debug_port}",
      )
    )

    if not port_open:
      results.append(
        CheckResult(
          "confluence_cookie",
          "FAIL",
          "Chrome 调试端口不可用，无法检查登录态",
        )
      )
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


def format_check_report(results: list[CheckResult]) -> str:
  return "\n".join(f"[{item.status}] {item.name}: {item.detail}" for item in results)


def normalize_cli_error(error: Exception) -> CwikiDownloadError:
  if isinstance(error, CwikiDownloadError):
    return error

  if isinstance(error, ValueError) and "pageId" in str(error):
    return CwikiDownloadError("E_INVALID_PAGE_INPUT", str(error), "请提供完整页面 URL 或纯数字 pageId。")

  if isinstance(error, httpx.HTTPStatusError):
    status = error.response.status_code
    if status == 404:
      return CwikiDownloadError("E_PAGE_NOT_FOUND", f"Confluence 页面不存在或接口返回 404：{error.request.url}", "请确认 pageId 是否正确，且页面仍存在。")
    if status in {401, 403}:
      return CwikiDownloadError("E_PAGE_FORBIDDEN", f"无权访问该页面，接口返回 {status}：{error.request.url}", "请确认当前 Chrome 已登录且账号对该页面有权限。")
    return CwikiDownloadError("E_CONFLUENCE_HTTP_ERROR", f"Confluence 接口返回 {status}：{error.request.url}", "请稍后重试，或检查 Confluence 站点与登录态。")

  message = str(error)
  if "Cookie" in message:
    return CwikiDownloadError("E_NO_COOKIE", message, "请确认 Chrome 已登录 Confluence，且调试端口可用。")
  if "Chrome" in message or "DevTools" in message:
    return CwikiDownloadError("E_CHROME_DEBUG_PORT_UNAVAILABLE", message, "请确认 Chrome 远程调试端口已开启，或显式传入 --restart-chrome。")
  return CwikiDownloadError("E_UNEXPECTED", message or error.__class__.__name__, "请查看错误信息后重试。")


async def download_page(
  page_id_or_url: str,
  output_dir: Path | None = None,
  *,
  allow_chrome_restart: bool,
) -> Path:
  workspace_root = get_workspace_root()
  load_workspace_env(workspace_root)
  target_dir = output_dir or resolve_output_dir(workspace_root)

  base_url = os.getenv("CONFLUENCE_BASE_URL", CONFLUENCE_BASE_URL_DEFAULT)
  page_locator = extract_page_locator(page_id_or_url)
  chrome_start_url = build_chrome_start_url(page_id_or_url, base_url=base_url)
  cookie_header = await resolve_cookie_header(
    allow_chrome_restart=allow_chrome_restart,
    chrome_start_url=chrome_start_url,
  )

  async with httpx.AsyncClient(timeout=60) as client:
    page = await fetch_page(
      client=client,
      base_url=base_url,
      page_id=page_locator.get("page_id"),
      page_title=page_locator.get("title"),
      space_key=page_locator.get("space_key"),
      cookie_header=cookie_header,
    )
    page_id = str(page.get("id") or page_locator.get("page_id") or "").strip()
    if not page_id:
      raise ValueError("无法从 Confluence 响应中获取页面 pageId")

    # 评论：拉取并落盘为结构化 JSON（失败不阻断正文导出）
    comment_threads: list[dict] = []
    comment_error: str = ""
    if is_comment_fetch_enabled():
      comment_threads, comment_error = await fetch_comment_threads(
        client=client,
        base_url=base_url,
        page_id=page_id,
        cookie_header=cookie_header,
      )
      if comment_threads:
        write_comments_json(asset_dir=target_dir / "assets" / page_id, page_id=page_id, threads=comment_threads)
    attachments = await list_attachments(client=client, base_url=base_url, page_id=page_id, cookie_header=cookie_header)

    storage_html = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    view_html = ((page.get("body") or {}).get("view") or {}).get("value") or ""
    page_link_map, macro_link_map = extract_view_macro_links(view_html)
    complex_macro_preview_titles = extract_complex_macro_preview_titles(view_html)
    for macro_name, titles in extract_storage_macro_preview_titles(storage_html).items():
      complex_macro_preview_titles.setdefault(macro_name, []).extend(
        title for title in titles if title not in complex_macro_preview_titles.get(macro_name, [])
      )
    preview_title_set = {
      title
      for titles in complex_macro_preview_titles.values()
      for title in titles
    }
    referenced_attachments = filter_referenced_attachments(storage_html, attachments)
    referenced_titles = {str(item.get("title") or "").strip() for item in referenced_attachments}
    referenced_titles.update(preview_title_set)
    referenced_attachments = [
      item for item in attachments
      if str(item.get("title") or "").strip() in referenced_titles
    ]

    asset_dir = target_dir / "assets" / page_id
    asset_dir.mkdir(parents=True, exist_ok=True)

    attachment_path_map: dict[str, str] = {}
    for attachment in referenced_attachments:
      title = str(attachment.get("title") or "").strip()
      if not title:
        continue
      local_name = sanitize_asset_filename(title)
      destination = asset_dir / local_name
      await download_attachment(
        client=client,
        base_url=base_url,
        attachment=attachment,
        cookie_header=cookie_header,
        destination=destination,
      )
      attachment_path_map[title] = get_asset_relative_url(page_id, local_name)

    direct_attachment_urls = extract_referenced_attachment_download_urls(storage_html)
    direct_attachment_urls.update(extract_referenced_attachment_download_urls(view_html))
    for raw_url in sorted(direct_attachment_urls):
      if raw_url.startswith(("http://", "https://")):
        download_url = raw_url
      else:
        download_url = f"{base_url.rstrip('/')}/{raw_url.lstrip('/')}"

      filename = Path(unquote(urlsplit(download_url).path)).name.strip()
      if not filename:
        continue
      if filename in attachment_path_map:
        continue

      local_name = sanitize_asset_filename(filename)
      destination = asset_dir / local_name
      response = await client.get(
        download_url,
        headers={"Cookie": cookie_header},
      )
      response.raise_for_status()
      destination.write_bytes(response.content)
      attachment_path_map[filename] = get_asset_relative_url(page_id, local_name)

    complex_macro_preview_paths = {
      macro_name: [
        attachment_path_map[title]
        for title in titles
        if title in attachment_path_map
      ]
      for macro_name, titles in complex_macro_preview_titles.items()
    }

  markdown, macro_assets = render_storage_to_markdown(
    storage_html=storage_html,
    attachment_path_map=attachment_path_map,
    macro_asset_prefix=f"assets/{page_id}",
    page_id=page_id,
    complex_macro_preview_paths=complex_macro_preview_paths,
    macro_link_map=macro_link_map,
    page_link_map=page_link_map,
  )
  llm_markdown, _ = render_storage_to_llm_markdown(
    storage_html=storage_html,
    attachment_path_map=attachment_path_map,
    macro_asset_prefix=f"assets/{page_id}",
    page_id=page_id,
    complex_macro_preview_paths=complex_macro_preview_paths,
    macro_link_map=macro_link_map,
    page_link_map=page_link_map,
  )

  # 将评论章节追加到文末（如果评论拉取失败，也会追加失败提示，便于排查）
  from .markdown_renderer import (  # 局部导入避免循环依赖
    append_comments_section_to_llm_markdown,
    append_comments_section_to_markdown,
  )

  if is_comment_fetch_enabled():
    enrich_inline_section_paths(comment_threads=comment_threads, markdown=markdown)
    markdown, human_index_lines = inject_inline_markers_into_human_markdown(markdown, comment_threads=comment_threads)
    llm_markdown, llm_map_lines = inject_inline_markers_into_llm_markdown(llm_markdown, comment_threads=comment_threads)

    markdown = append_comments_section_to_markdown(
      markdown,
      threads=comment_threads,
      error=comment_error,
    )
    llm_markdown = append_comments_section_to_llm_markdown(
      llm_markdown,
      threads=comment_threads,
      error=comment_error,
    )

    if human_index_lines:
      markdown += "\n\n## 行内评论锚点索引\n\n" + "\n".join(human_index_lines)
    if llm_map_lines:
      llm_markdown += "\n\n## Inline Marker Map\n\n" + "\n".join(llm_map_lines)

  page_title = str(page.get("title") or page_id)
  markdown = prepend_page_metadata(markdown, page=page)
  llm_markdown = prepend_page_metadata(llm_markdown, page=page)

  return write_page_bundle(
    output_dir=target_dir,
    page_id=page_id,
    page_title=page_title,
    markdown=markdown,
    llm_markdown=llm_markdown,
    macro_assets=macro_assets,
  )


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="下载 Confluence 页面并保存为 Markdown")
  parser.add_argument("page_id_or_url", nargs="?", help="Confluence 页面 URL 或 pageId")
  parser.add_argument(
    "--output-dir",
    dest="output_dir",
    help="可选的输出目录，默认使用工作区 docs 或环境变量 CWIKI_DOWNLOAD_SAVE_DIR",
  )
  parser.add_argument(
    "--check",
    action="store_true",
    help="只检查运行环境、登录态和基础连通性，不执行下载",
  )
  parser.add_argument(
    "--restart-chrome",
    action="store_true",
    help="当 Chrome 调试端口未开启时，允许脚本自动重启 Chrome",
  )
  parser.add_argument(
    "--cookie-source",
    choices=["sso", "chrome", "manual"],
    help="登录态来源；默认读取配置，未配置时使用 sso",
  )
  parser.add_argument(
    "--manual-cookie",
    help="本次使用手动 Cookie；会自动等价为 --cookie-source manual",
  )
  parser.add_argument(
    "--sso-skill-root",
    help="guazi-sso-login skill 目录；未传时自动查找同项目或全局安装路径",
  )
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
  output_dir = resolve_cli_output_dir(args.output_dir)
  try:
    saved_env_path = apply_login_cli_config(args, workspace_root=workspace_root)
    if args.check:
      results = asyncio.run(run_preflight_check(output_dir=output_dir))
      print(format_check_report(results))
      if saved_env_path:
        print(f"[INFO] login_config_saved: {saved_env_path}")
      if any(item.status == "FAIL" for item in results):
        raise SystemExit(1)
      return

    if not args.page_id_or_url:
      parser.error("缺少 pageId 或页面 URL")

    markdown_path = asyncio.run(
      download_page(
        args.page_id_or_url,
        output_dir=output_dir,
        allow_chrome_restart=args.restart_chrome,
      )
    )
    if saved_env_path:
      print(f"[INFO] login_config_saved: {saved_env_path}")
    print(markdown_path)
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
