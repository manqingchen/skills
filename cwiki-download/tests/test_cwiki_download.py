import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import tomllib

import httpx
from scripts.cwiki_download import ensure_chrome_debugging
from scripts.cwiki_download import apply_login_cli_config
from scripts.cwiki_download import CwikiDownloadError
from scripts.cwiki_download import CWIKI_COOKIE_SOURCE_ENV
from scripts.cwiki_download import CWIKI_DOWNLOAD_CONFIG_FILE_ENV
from scripts.cwiki_download import CWIKI_MANUAL_COOKIE_ENV
from scripts.comments import CWIKI_FETCH_COMMENTS_ENV
from scripts.cwiki_download import CWIKI_USE_MANUAL_COOKIE_ENV
from scripts.cwiki_download import build_parser
from scripts.cwiki_download import build_chrome_start_url
from scripts.cwiki_download import CheckResult
from scripts.cwiki_download import format_check_report
from scripts.cwiki_download import get_asset_relative_url
from scripts.cwiki_download import get_start_chrome_debug_script_path
from scripts.cwiki_download import get_user_config_path
from scripts.cwiki_download import get_workspace_root
from scripts.cwiki_download import load_login_config
from scripts.cwiki_download import normalize_cli_error
from scripts.cwiki_download import prepend_page_metadata
from scripts.cwiki_download import resolve_cli_output_dir
from scripts.cwiki_download import resolve_cookie_source
from scripts.cwiki_download import write_workspace_env_values
from scripts.comments import is_comment_fetch_enabled
from scripts.cwiki_download import run_preflight_check
from scripts.cwiki_download import write_page_bundle
from scripts.confluence_client import (
  extract_page_locator,
  extract_referenced_attachment_download_urls,
  fetch_page,
  filter_referenced_attachments,
  list_attachments,
)
from scripts.macro_transformer import (
  MacroAsset,
  extract_complex_macro_preview_titles,
  extract_storage_macro_preview_titles,
  extract_view_macro_links,
)
from scripts.markdown_renderer import (
  derive_llm_markdown_filename,
  derive_markdown_filename,
  render_storage_to_llm_markdown,
  render_storage_to_markdown,
  resolve_output_dir,
)


SKILL_ROOT = Path(__file__).resolve().parent.parent


def test_only_downloads_attachments_referenced_in_storage_body() -> None:
  storage_html = """
  <ac:image>
    <ri:attachment ri:filename="used-image.png" />
  </ac:image>
  <p>
    <a href="/download/attachments/12345/used-file.pdf">used-file.pdf</a>
  </p>
  """
  attachments = [
    {"title": "used-image.png", "download": "/download/attachments/12345/used-image.png"},
    {"title": "used-file.pdf", "download": "/download/attachments/12345/used-file.pdf"},
    {"title": "draft.docx", "download": "/download/attachments/12345/draft.docx"},
  ]

  filtered = filter_referenced_attachments(storage_html, attachments)

  assert [item["title"] for item in filtered] == ["used-image.png", "used-file.pdf"]


def test_extracts_direct_download_urls_from_html_and_ri_value() -> None:
  html_text = """
  <p>附件地址：</p>
  <ac:link>
    <ri:url ri:value="https://cwiki.guazi.com/download/attachments/612061430/image-2025-12-20_12-57-13.png?version=1&amp;modificationDate=1766549294000&amp;api=v2" />
  </ac:link>
  <p>/download/attachments/12345/inline.txt?api=v2</p>
  """

  urls = extract_referenced_attachment_download_urls(html_text)

  assert "https://cwiki.guazi.com/download/attachments/612061430/image-2025-12-20_12-57-13.png?version=1&modificationDate=1766549294000&api=v2" in urls
  assert "/download/attachments/12345/inline.txt?api=v2" in urls


def test_pyproject_exposes_cwiki_download_console_script() -> None:
  pyproject = tomllib.loads((SKILL_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
  assert pyproject["project"]["scripts"]["cwiki-download"] == "scripts.cwiki_download:main"


def test_run_script_executes_console_script_from_own_directory() -> None:
  run_script = (SKILL_ROOT / "run.sh").read_text(encoding="utf-8")
  assert 'SOURCE="${BASH_SOURCE[0]}"' in run_script
  assert 'while [ -L "$SOURCE" ]; do' in run_script
  assert 'SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"' in run_script
  assert 'if ! command -v uv >/dev/null 2>&1; then' in run_script
  assert 'echo "未检测到 uv，请先安装 uv 后再运行 cwiki-download。"' in run_script
  assert 'echo "安装命令：curl -LsSf https://astral.sh/uv/install.sh | sh"' in run_script
  assert 'uv run --directory "$SCRIPT_DIR" python -m scripts.cwiki_download "$@"' in run_script


def test_parser_supports_check_and_restart_chrome_flags() -> None:
  parser = build_parser()
  args = parser.parse_args([
    "123456",
    "--check",
    "--restart-chrome",
    "--cookie-source",
    "chrome",
    "--save-login-config",
  ])

  assert args.page_id_or_url == "123456"
  assert args.check is True
  assert args.restart_chrome is True
  assert args.cookie_source == "chrome"
  assert args.save_login_config == "user"


def test_parser_allows_check_without_page_argument() -> None:
  parser = build_parser()
  args = parser.parse_args(["--check"])

  assert args.page_id_or_url is None
  assert args.check is True


def test_resolve_cookie_source_prefers_manual_cookie_when_enabled(monkeypatch) -> None:
  monkeypatch.setenv(CWIKI_USE_MANUAL_COOKIE_ENV, "1")
  monkeypatch.setenv(CWIKI_MANUAL_COOKIE_ENV, "sid=abc")
  assert resolve_cookie_source() == "manual"


def test_resolve_cookie_source_defaults_to_sso_when_manual_cookie_missing(monkeypatch) -> None:
  monkeypatch.setenv(CWIKI_USE_MANUAL_COOKIE_ENV, "1")
  monkeypatch.delenv(CWIKI_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.delenv(CWIKI_COOKIE_SOURCE_ENV, raising=False)
  assert resolve_cookie_source() == "sso"


def test_resolve_cookie_source_uses_explicit_chrome(monkeypatch) -> None:
  monkeypatch.delenv(CWIKI_USE_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.setenv(CWIKI_COOKIE_SOURCE_ENV, "chrome")
  assert resolve_cookie_source() == "chrome"


def test_write_workspace_env_values_updates_existing_file(tmp_path: Path) -> None:
  env_path = tmp_path / ".env"
  env_path.write_text("FOO=bar\nCWIKI_COOKIE_SOURCE=\"sso\"\n", encoding="utf-8")

  written = write_workspace_env_values(tmp_path, {
    CWIKI_COOKIE_SOURCE_ENV: "chrome",
    "GUAZI_SSO_SKILL_ROOT": "/tmp/sso skill",
  })

  assert written == env_path
  assert env_path.read_text(encoding="utf-8") == (
    'FOO=bar\n'
    'CWIKI_COOKIE_SOURCE="chrome"\n'
    '\n'
    'GUAZI_SSO_SKILL_ROOT="/tmp/sso skill"\n'
  )


def test_apply_login_cli_config_sets_and_saves_manual_cookie_to_user_config(monkeypatch, tmp_path: Path) -> None:
  parser = build_parser()
  args = parser.parse_args(["--check", "--manual-cookie", "sid=abc", "--save-login-config"])
  user_config = tmp_path / "user.env"
  monkeypatch.setenv(CWIKI_DOWNLOAD_CONFIG_FILE_ENV, str(user_config))
  monkeypatch.delenv(CWIKI_COOKIE_SOURCE_ENV, raising=False)
  monkeypatch.delenv(CWIKI_USE_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.delenv(CWIKI_MANUAL_COOKIE_ENV, raising=False)

  try:
    env_path = apply_login_cli_config(args, workspace_root=tmp_path)

    assert env_path == user_config
    assert get_user_config_path() == user_config
    assert resolve_cookie_source() == "manual"
    assert user_config.read_text(encoding="utf-8") == (
      'CWIKI_USE_MANUAL_COOKIE="1"\n'
      'CWIKI_MANUAL_COOKIE="sid=abc"\n'
      'CWIKI_COOKIE_SOURCE="manual"\n'
    )
  finally:
    os.environ.pop(CWIKI_COOKIE_SOURCE_ENV, None)
    os.environ.pop(CWIKI_USE_MANUAL_COOKIE_ENV, None)
    os.environ.pop(CWIKI_MANUAL_COOKIE_ENV, None)


def test_apply_login_cli_config_can_save_project_config(monkeypatch, tmp_path: Path) -> None:
  parser = build_parser()
  args = parser.parse_args(["--check", "--cookie-source", "chrome", "--save-login-config", "project"])
  monkeypatch.delenv(CWIKI_COOKIE_SOURCE_ENV, raising=False)

  try:
    env_path = apply_login_cli_config(args, workspace_root=tmp_path)

    assert env_path == tmp_path / ".env"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == 'CWIKI_COOKIE_SOURCE="chrome"\n'
  finally:
    os.environ.pop(CWIKI_COOKIE_SOURCE_ENV, None)


def test_load_login_config_allows_project_to_override_user(monkeypatch, tmp_path: Path) -> None:
  user_config = tmp_path / "user.env"
  user_config.write_text('CWIKI_COOKIE_SOURCE="sso"\n', encoding="utf-8")
  (tmp_path / ".env").write_text('CWIKI_COOKIE_SOURCE="chrome"\n', encoding="utf-8")
  monkeypatch.setenv(CWIKI_DOWNLOAD_CONFIG_FILE_ENV, str(user_config))
  monkeypatch.delenv(CWIKI_COOKIE_SOURCE_ENV, raising=False)

  load_login_config(tmp_path)

  assert os.environ[CWIKI_COOKIE_SOURCE_ENV] == "chrome"


def test_load_login_config_keeps_explicit_environment(monkeypatch, tmp_path: Path) -> None:
  user_config = tmp_path / "user.env"
  user_config.write_text('CWIKI_COOKIE_SOURCE="sso"\n', encoding="utf-8")
  (tmp_path / ".env").write_text('CWIKI_COOKIE_SOURCE="chrome"\n', encoding="utf-8")
  monkeypatch.setenv(CWIKI_DOWNLOAD_CONFIG_FILE_ENV, str(user_config))
  monkeypatch.setenv(CWIKI_COOKIE_SOURCE_ENV, "manual")

  load_login_config(tmp_path)

  assert os.environ[CWIKI_COOKIE_SOURCE_ENV] == "manual"


def test_comment_fetch_enabled_by_default(monkeypatch) -> None:
  monkeypatch.delenv(CWIKI_FETCH_COMMENTS_ENV, raising=False)
  assert is_comment_fetch_enabled() is True


def test_comment_fetch_can_be_disabled_by_env(monkeypatch) -> None:
  monkeypatch.setenv(CWIKI_FETCH_COMMENTS_ENV, "false")
  assert is_comment_fetch_enabled() is False


def test_skill_doc_declares_unified_invocation_contract() -> None:
  skill_doc = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
  assert "统一执行要求" in skill_doc
  assert 'SKILL_ROOT="$(dirname "<SKILL.md 的绝对路径>")"' in skill_doc
  assert 'bash "$SKILL_ROOT/run.sh" "123456"' in skill_doc
  assert "--check" in skill_doc
  assert "--restart-chrome" in skill_doc
  assert "默认不主动重启当前 Chrome" in skill_doc


def test_start_chrome_debug_script_uses_env_debug_port_and_portable_shebang() -> None:
  script = (SKILL_ROOT / "start-chrome-debug.sh").read_text(encoding="utf-8")
  assert script.startswith("#!/usr/bin/env bash")
  assert 'DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}"' in script
  assert "CHROME_PATH=" not in script
  assert 'nohup "$CHROME_BIN"' in script
  assert "</dev/null >/dev/null 2>&1 &" in script
  assert 'disown "$CHROME_PID"' in script
  assert "cleanup_stale_chrome_singletons()" in script
  assert "SingletonLock SingletonCookie SingletonSocket" in script
  assert 'open -na "$CHROME_APP_NAME" --args' in script
  assert 'CHROME_START_URL="${CHROME_START_URL:-about:blank}"' in script


def test_relative_cli_output_dir_resolves_against_workspace_root(tmp_path: Path, monkeypatch) -> None:
  monkeypatch.setenv("CWIKI_DOWNLOAD_WORKSPACE_ROOT", str(tmp_path))

  assert resolve_cli_output_dir("./docs/product-specs") == tmp_path / "docs" / "product-specs"


def test_readme_documents_check_mode_and_compatibility_matrix() -> None:
  readme = (SKILL_ROOT / "README.md").read_text(encoding="utf-8")

  assert "兼容性矩阵" in readme
  assert "--check" in readme
  assert "--restart-chrome" in readme
  assert "默认不主动重启当前 Chrome" in readme
  assert "CWIKI_DOWNLOAD_FETCH_COMMENTS" in readme


def test_normalize_cli_error_maps_invalid_input() -> None:
  error = normalize_cli_error(ValueError("无法解析 pageId，请输入有效的 URL 或 pageId"))

  assert isinstance(error, CwikiDownloadError)
  assert error.code == "E_INVALID_PAGE_INPUT"
  assert "pageId" in error.message


def test_extract_page_locator_supports_display_style_url() -> None:
  locator = extract_page_locator("https://cwiki.guazi.com/display/CTOBTECH/Skills+Hub")
  assert locator == {"space_key": "CTOBTECH", "title": "Skills Hub"}


def test_build_chrome_start_url_uses_input_url_or_page_id() -> None:
  base_url = "https://cwiki.guazi.com"

  assert build_chrome_start_url("123456", base_url=base_url) == "https://cwiki.guazi.com/pages/viewpage.action?pageId=123456"
  assert build_chrome_start_url(
    "https://cwiki.guazi.com/pages/viewpage.action?pageId=123456",
    base_url=base_url,
  ) == "https://cwiki.guazi.com/pages/viewpage.action?pageId=123456"
  assert build_chrome_start_url(
    "https://cwiki.guazi.com/display/CTOBTECH/Skills+Hub",
    base_url=base_url,
  ) == "https://cwiki.guazi.com/display/CTOBTECH/Skills+Hub"


def test_fetch_page_queries_rest_content_by_title_and_space_key() -> None:
  class FakeResponse:
    def __init__(self, payload: dict) -> None:
      self.payload = payload

    def raise_for_status(self) -> None:
      return None

    def json(self) -> dict:
      return self.payload

  class FakeClient:
    def __init__(self) -> None:
      self.calls: list[dict[str, object]] = []

    async def get(self, url: str, params: dict, headers: dict) -> FakeResponse:
      self.calls.append({"url": url, "params": params, "headers": headers})
      return FakeResponse({"results": [{"id": "12345", "title": "Skills Hub"}]})

  client = FakeClient()
  page = asyncio.run(
    fetch_page(
      client=client,  # type: ignore[arg-type]
      base_url="https://cwiki.guazi.com",
      page_id=None,
      page_title="Skills Hub",
      space_key="CTOBTECH",
      cookie_header="sid=1",
    )
  )

  assert page["id"] == "12345"
  assert client.calls[0]["url"] == "https://cwiki.guazi.com/rest/api/content"
  assert client.calls[0]["params"] == {
    "title": "Skills Hub",
    "spaceKey": "CTOBTECH",
    "expand": "body.storage,body.view,version,space,metadata.labels,_links",
    "limit": 1,
  }


def test_normalize_cli_error_maps_http_404_to_page_not_found() -> None:
  request = httpx.Request("GET", "https://cwiki.guazi.com/rest/api/content/123")
  response = httpx.Response(404, request=request)

  error = normalize_cli_error(httpx.HTTPStatusError("not found", request=request, response=response))

  assert error.code == "E_PAGE_NOT_FOUND"
  assert "404" in error.message


def test_format_check_report_renders_status_lines() -> None:
  report = format_check_report(
    [
      CheckResult(name="uv", status="PASS", detail="/opt/homebrew/bin/uv"),
      CheckResult(name="chrome_debug_port", status="FAIL", detail="127.0.0.1:9222"),
    ]
  )

  assert "[PASS] uv: /opt/homebrew/bin/uv" in report
  assert "[FAIL] chrome_debug_port: 127.0.0.1:9222" in report


def test_prefers_env_output_dir_and_falls_back_to_workspace_docs(tmp_path: Path, monkeypatch) -> None:
  workspace_root = tmp_path / "workspace"
  workspace_root.mkdir()
  custom_dir = tmp_path / "custom-docs"

  monkeypatch.setenv("CWIKI_DOWNLOAD_SAVE_DIR", str(custom_dir))
  assert resolve_output_dir(workspace_root) == custom_dir

  monkeypatch.delenv("CWIKI_DOWNLOAD_SAVE_DIR")
  assert resolve_output_dir(workspace_root) == workspace_root / "docs"


def test_uses_page_title_for_markdown_filename() -> None:
  assert derive_markdown_filename("订单中心 / 发布说明", "647421114") == "订单中心-发布说明-647421114.md"


def test_uses_page_title_for_llm_markdown_filename() -> None:
  assert derive_llm_markdown_filename("订单中心 / 发布说明", "647421114") == "订单中心-发布说明-647421114.llm.md"


def test_renders_simple_macro_and_complex_macro_placeholder() -> None:
  storage_html = """
  <ac:structured-macro ac:name="code">
    <ac:parameter ac:name="language">python</ac:parameter>
    <ac:plain-text-body><![CDATA[print("hello")]]></ac:plain-text-body>
  </ac:structured-macro>
  <ac:structured-macro ac:name="jira">
    <ac:parameter ac:name="key">ABC-123</ac:parameter>
  </ac:structured-macro>
  """

  markdown, macro_assets = render_storage_to_markdown(
    storage_html=storage_html,
    attachment_path_map={},
    macro_asset_prefix="assets/macros",
  )

  assert "```python" in markdown
  assert 'print("hello")' in markdown
  assert "ABC-123" in markdown
  assert macro_assets == []


def test_renders_code_macro_title_without_losing_code() -> None:
  storage_html = """
  <ac:structured-macro ac:name="code">
    <ac:parameter ac:name="language">typescript</ac:parameter>
    <ac:parameter ac:name="title">instrumentation.ts</ac:parameter>
    <ac:plain-text-body><![CDATA[export async function register() {}]]></ac:plain-text-body>
  </ac:structured-macro>
  """

  markdown, macro_assets = render_storage_to_markdown(
    storage_html=storage_html,
    attachment_path_map={},
    macro_asset_prefix="assets/macros",
  )

  assert '```typescript title="instrumentation.ts"' in markdown
  assert "export async function register() {}" in markdown
  assert macro_assets == []


def test_renders_nested_section_column_panel_and_status_without_losing_content() -> None:
  storage_html = """
  <ac:structured-macro ac:name="section">
    <ac:rich-text-body>
      <ac:structured-macro ac:name="column">
        <ac:parameter ac:name="width">50%</ac:parameter>
        <ac:rich-text-body>
          <ac:structured-macro ac:name="panel">
            <ac:parameter ac:name="title">内存模式</ac:parameter>
            <ac:rich-text-body>
              <p><strong>配置文件：</strong> <code>config-memory.yaml</code></p>
              <p><strong>状态：</strong>
                <ac:structured-macro ac:name="status">
                  <ac:parameter ac:name="title">适合测试</ac:parameter>
                  <ac:parameter ac:name="colour">Blue</ac:parameter>
                </ac:structured-macro>
              </p>
            </ac:rich-text-body>
          </ac:structured-macro>
        </ac:rich-text-body>
      </ac:structured-macro>
      <ac:structured-macro ac:name="column">
        <ac:parameter ac:name="width">50%</ac:parameter>
        <ac:rich-text-body>
          <ac:structured-macro ac:name="panel">
            <ac:parameter ac:name="title">Badger 持久化</ac:parameter>
            <ac:rich-text-body>
              <p><strong>配置文件：</strong> <code>config-badger.yaml</code></p>
              <p><strong>状态：</strong>
                <ac:structured-macro ac:name="status">
                  <ac:parameter ac:name="title">推荐生产</ac:parameter>
                  <ac:parameter ac:name="colour">Green</ac:parameter>
                </ac:structured-macro>
              </p>
            </ac:rich-text-body>
          </ac:structured-macro>
        </ac:rich-text-body>
      </ac:structured-macro>
    </ac:rich-text-body>
  </ac:structured-macro>
  """

  markdown, macro_assets = render_storage_to_markdown(
    storage_html=storage_html,
    attachment_path_map={},
    macro_asset_prefix="assets/macros",
  )

  assert "**内存模式**" in markdown
  assert "配置文件：" in markdown
  assert "`config-memory.yaml`" in markdown
  assert "[适合测试]" in markdown
  assert "**Badger 持久化**" in markdown
  assert "`config-badger.yaml`" in markdown
  assert "[推荐生产]" in markdown
  assert "复杂宏" not in markdown
  assert macro_assets == []


def test_renders_complex_macro_with_preview_image_when_available() -> None:
  storage_html = """
  <ac:structured-macro ac:name="easymind"></ac:structured-macro>
  <ac:structured-macro ac:name="drawio"></ac:structured-macro>
  """

  markdown, macro_assets = render_storage_to_markdown(
    storage_html=storage_html,
    attachment_path_map={},
    macro_asset_prefix="assets/647421114",
    complex_macro_preview_paths={
      "easymind": ["assets/647421114/mindmap-preview.png"],
      "drawio": ["assets/647421114/drawio-preview.png"],
    },
  )

  assert "![复杂宏-easymind](assets/647421114/mindmap-preview.png)" in markdown
  assert "![复杂宏-drawio](assets/647421114/drawio-preview.png)" in markdown
  assert macro_assets == []


def test_renders_heading_drawio_macro_preview_on_next_line() -> None:
  storage_html = """
  <h3>1.1 寄售tab展示条件
    <ac:structured-macro ac:name="drawio"></ac:structured-macro>
  </h3>
  """

  markdown, macro_assets = render_storage_to_markdown(
    storage_html=storage_html,
    attachment_path_map={},
    macro_asset_prefix="assets/668596043",
    complex_macro_preview_paths={
      "drawio": ["assets/668596043/门店寄售tab.png"],
    },
  )

  assert "### 1.1 寄售tab展示条件" in markdown
  assert "![复杂宏-drawio](assets/668596043/门店寄售tab.png)" in markdown
  assert "### 1.1 寄售tab展示条件复杂宏-drawio" not in markdown
  assert macro_assets == []


def test_renders_view_file_macro_as_local_attachment_link() -> None:
  storage_html = """
  <h3>1.2 原型示意
    <ac:structured-macro ac:name="view-file">
      <ac:parameter ac:name="name">
        <ri:attachment ri:filename="门店寄售独立Tab — 交互原型0615.html" />
      </ac:parameter>
      <ac:parameter ac:name="height">250</ac:parameter>
    </ac:structured-macro>
  </h3>
  """

  markdown, macro_assets = render_storage_to_markdown(
    storage_html=storage_html,
    attachment_path_map={
      "门店寄售独立Tab — 交互原型0615.html": "assets/668596043/%E9%97%A8%E5%BA%97.html",
    },
    macro_asset_prefix="assets/668596043",
  )

  assert "### 1.2 原型示意 [查看 HTML 附件](assets/668596043/%E9%97%A8%E5%BA%97.html)" in markdown
  assert "复杂宏-view-file" not in markdown
  assert macro_assets == []


def test_renders_self_closing_mermaid_cloud_with_preview_when_available() -> None:
  markdown, macro_assets = render_storage_to_markdown(
    storage_html='<ac:structured-macro ac:name="mermaid-cloud" />',
    attachment_path_map={},
    macro_asset_prefix="assets/123",
    complex_macro_preview_paths={
      "mermaid-cloud": ["assets/123/diagram-preview.svg"],
    },
  )

  assert "![复杂宏-mermaid-cloud](assets/123/diagram-preview.svg)" in markdown
  assert macro_assets == []


def test_ignores_toc_macro_instead_of_rendering_notice() -> None:
  markdown, macro_assets = render_storage_to_markdown(
    storage_html='<ac:structured-macro ac:name="toc" />',
    attachment_path_map={},
    macro_asset_prefix="assets/123",
  )

  assert markdown == ""
  assert macro_assets == []


def test_renders_widget_macro_as_link_when_url_parameter_exists() -> None:
  markdown, macro_assets = render_storage_to_markdown(
    storage_html="""
    <p>UI地址：
      <ac:structured-macro ac:name="widget">
        <ac:parameter ac:name="url">
          <ri:url ri:value="https://www.figma.com/design/dgcseRDKKcQvP5jNnRBTDu/demo?node-id=1-2" />
        </ac:parameter>
      </ac:structured-macro>
    </p>
    """,
    attachment_path_map={},
    macro_asset_prefix="assets/123",
  )

  assert "<https://www.figma.com/design/dgcseRDKKcQvP5jNnRBTDu/demo?node-id=1-2>" in markdown
  assert macro_assets == []


def test_renders_jira_macro_as_link_when_view_link_available() -> None:
  markdown, macro_assets = render_storage_to_markdown(
    storage_html="""
    <ac:structured-macro ac:name="jira">
      <ac:parameter ac:name="key">SCC-40725</ac:parameter>
    </ac:structured-macro>
    """,
    attachment_path_map={},
    macro_asset_prefix="assets/123",
    macro_link_map={
      "jira": {
        "SCC-40725": "https://cjira.guazi-corp.com/browse/SCC-40725",
      }
    },
  )

  assert "[SCC-40725](https://cjira.guazi-corp.com/browse/SCC-40725)" in markdown
  assert macro_assets == []


def test_renders_embedded_page_link_from_view_mapping() -> None:
  markdown, _ = render_storage_to_markdown(
    storage_html="""
    <p>wiki地址：<ac:link><ri:page ri:space-key="C2CPM" ri:content-title="DCN支持车主车商议价" /></ac:link></p>
    """,
    attachment_path_map={},
    macro_asset_prefix="assets/123",
    page_link_map={
      "DCN支持车主车商议价": "/pages/viewpage.action?pageId=643230190",
    },
  )

  assert "[DCN支持车主车商议价](/pages/viewpage.action?pageId=643230190)" in markdown


def test_falls_back_to_html_block_for_nested_table() -> None:
  markdown, _ = render_storage_to_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th>标题</th>
          <th>内容</th>
        </tr>
        <tr>
          <td>外层</td>
          <td>
            <table>
              <tbody>
                <tr><td>内层1</td></tr>
              </tbody>
            </table>
          </td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={},
    macro_asset_prefix="assets/123",
  )

  assert "```html" not in markdown
  assert "<table>" in markdown
  assert "内层1" in markdown


def test_falls_back_to_html_block_for_colspan_table() -> None:
  markdown, _ = render_storage_to_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th colspan="2">标题</th>
        </tr>
        <tr>
          <td>A</td>
          <td>B</td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={},
    macro_asset_prefix="assets/123",
  )

  assert "```html" not in markdown
  assert 'colspan="2"' in markdown


def test_renders_nested_table_as_structured_block_for_llm_markdown() -> None:
  llm_markdown, _ = render_storage_to_llm_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th>标题</th>
          <th>内容</th>
        </tr>
        <tr>
          <td>外层</td>
          <td>
            <table>
              <tbody>
                <tr><td>内层1</td></tr>
              </tbody>
            </table>
          </td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={},
    macro_asset_prefix="assets/123",
  )

  assert "<table>" not in llm_markdown
  assert "## 结构化表格 1" in llm_markdown
  assert "- 表头: 标题 | 内容" in llm_markdown
  assert "  - 内容:" in llm_markdown
  assert "    - 子表格:" in llm_markdown
  assert "内层1" in llm_markdown


def test_renders_wrapped_nested_table_for_llm_markdown() -> None:
  llm_markdown, _ = render_storage_to_llm_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th>模块</th>
          <th>描述</th>
        </tr>
        <tr>
          <td>外层模块</td>
          <td>
            <div class="content-wrapper">
              <ul>
                <li>详见下表</li>
                <li>
                  <table>
                    <tbody>
                      <tr>
                        <th>分类</th>
                        <th>描述</th>
                      </tr>
                      <tr>
                        <td>C2买家出价</td>
                        <td>展示价格</td>
                      </tr>
                    </tbody>
                  </table>
                </li>
              </ul>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={},
    macro_asset_prefix="assets/123",
  )

  assert "## 结构化表格 1" in llm_markdown
  assert "  - 描述:" in llm_markdown
  assert "    - 文本: 详见下表" in llm_markdown
  assert "    - 子表格:" in llm_markdown
  assert "分类 | 描述" in llm_markdown
  assert "C2买家出价" in llm_markdown
  assert "展示价格" in llm_markdown


def test_renders_colspan_metadata_for_llm_markdown() -> None:
  llm_markdown, _ = render_storage_to_llm_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th colspan="2">标题</th>
        </tr>
        <tr>
          <td>A</td>
          <td>B</td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={},
    macro_asset_prefix="assets/123",
  )

  assert "colspan=2" in llm_markdown
  assert "<table>" not in llm_markdown


def test_llm_structured_table_keeps_attachment_reference_in_cell() -> None:
  llm_markdown, _ = render_storage_to_llm_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th colspan="2">说明</th>
        </tr>
        <tr>
          <td>流程图</td>
          <td><a href="/download/attachments/123/flow.png?api=v2">点击查看</a></td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={"flow.png": "assets/123/flow.png"},
    macro_asset_prefix="assets/123",
  )

  assert "## 结构化表格 1" in llm_markdown
  assert "附件引用: assets/123/flow.png" in llm_markdown


def test_llm_nested_structured_table_keeps_attachment_reference_in_subtable() -> None:
  llm_markdown, _ = render_storage_to_llm_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th>模块</th>
          <th>详情</th>
        </tr>
        <tr>
          <td>外层</td>
          <td>
            <table>
              <tbody>
                <tr>
                  <th>附件</th>
                </tr>
                <tr>
                  <td><a href="/download/attachments/123/spec.pdf">规范文档</a></td>
                </tr>
              </tbody>
            </table>
          </td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={"spec.pdf": "assets/123/spec.pdf"},
    macro_asset_prefix="assets/123",
  )

  assert "## 结构化表格 1" in llm_markdown
  assert "子表格" in llm_markdown
  assert "附件引用: assets/123/spec.pdf" in llm_markdown


def test_llm_normal_table_textual_attachment_name_includes_relative_path() -> None:
  llm_markdown, _ = render_storage_to_llm_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th>模块</th>
          <th>描述</th>
        </tr>
        <tr>
          <td>未登录说明</td>
          <td>未登录情况说明：image-2025-12-5_11-23-10.png</td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={"image-2025-12-5_11-23-10.png": "assets/123/image-2025-12-5_11-23-10.png"},
    macro_asset_prefix="assets/123",
  )

  assert "未登录情况说明：" in llm_markdown
  assert "assets/123/image-2025-12-5_11-23-10.png" in llm_markdown


def test_markdown_normal_table_textual_attachment_name_includes_relative_path() -> None:
  markdown, _ = render_storage_to_markdown(
    storage_html="""
    <table>
      <tbody>
        <tr>
          <th>模块</th>
          <th>描述</th>
        </tr>
        <tr>
          <td>未登录说明</td>
          <td>未登录情况说明：image-2025-12-5_11-23-10.png</td>
        </tr>
      </tbody>
    </table>
    """,
    attachment_path_map={"image-2025-12-5_11-23-10.png": "assets/123/image-2025-12-5_11-23-10.png"},
    macro_asset_prefix="assets/123",
  )

  assert "未登录情况说明：" in markdown
  assert "![image-2025-12-5_11-23-10.png](assets/123/image-2025-12-5_11-23-10.png)" in markdown


def test_prepends_page_metadata_to_markdown() -> None:
  markdown = prepend_page_metadata(
    "# 文档内容",
    page={
      "version": {
        "number": 12,
        "when": "2026-05-14T09:30:00.000+08:00",
      }
    },
  )

  assert markdown.startswith("当前版本：12\n更新时间：2026-05-14T09:30:00.000+08:00\n\n# 文档内容")


def test_write_page_bundle_outputs_markdown_and_macro_assets(tmp_path: Path) -> None:
  markdown_path = write_page_bundle(
    output_dir=tmp_path,
    page_id="647421114",
    page_title="发布说明",
    markdown="# 文档内容",
    llm_markdown="# 模型版文档",
    macro_assets=[MacroAsset(filename="jira-1.svg", content="<svg></svg>")],
  )

  assert markdown_path == tmp_path / "发布说明-647421114.md"
  assert markdown_path.read_text(encoding="utf-8") == "# 文档内容"
  assert (tmp_path / "发布说明-647421114.llm.md").read_text(encoding="utf-8") == "# 模型版文档"
  assert (tmp_path / "assets" / "647421114" / "jira-1.svg").read_text(encoding="utf-8") == "<svg></svg>"


def test_rewrites_encoded_attachment_name_to_local_path() -> None:
  markdown, _ = render_storage_to_markdown(
    storage_html="""
    <ac:image>
      <ri:attachment ri:filename="used%20image.png" />
    </ac:image>
    <p><a href="/download/attachments/123/used%20image.png?api=v2">附件</a></p>
    """,
    attachment_path_map={"used image.png": "assets/123/used-image.png"},
    macro_asset_prefix="assets/123",
  )

  assert "assets/123/used-image.png" in markdown
  assert "attachments/used%20image.png" not in markdown


def test_rewrites_link_by_exact_filename_not_substring() -> None:
  markdown, _ = render_storage_to_markdown(
    storage_html="""
    <p><a href="/download/attachments/123/report-final.png">下载</a></p>
    """,
    attachment_path_map={
      "final.png": "assets/123/final.png",
      "report-final.png": "assets/123/report-final.png",
    },
    macro_asset_prefix="assets/123",
  )

  assert "[下载](assets/123/report-final.png)" in markdown
  assert "assets/123/final.png" not in markdown


def test_builds_encoded_asset_relative_url_for_markdown_preview() -> None:
  assert get_asset_relative_url("647421114", "思维导图 - Tue Mar 24 2026 - 15时03分30秒.png") == (
    "assets/647421114/%E6%80%9D%E7%BB%B4%E5%AF%BC%E5%9B%BE%20-%20Tue%20Mar%2024%202026%20-%2015%E6%97%B603%E5%88%8630%E7%A7%92.png"
  )


def test_detects_git_root_as_workspace_root(tmp_path: Path, monkeypatch) -> None:
  workspace_root = tmp_path / "workspace"
  nested_dir = workspace_root / "nested" / "tool"
  nested_dir.mkdir(parents=True)
  subprocess.run(["git", "init"], cwd=workspace_root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

  monkeypatch.chdir(nested_dir)

  assert get_workspace_root() == workspace_root


def test_uses_local_start_chrome_debug_script(monkeypatch) -> None:
  commands: list[list[str]] = []
  envs: list[dict[str, str] | None] = []

  def fake_run(command: list[str], check: bool, **kwargs):  # type: ignore[no-untyped-def]
    commands.append(command)
    envs.append(kwargs.get("env"))
    return None

  monkeypatch.setattr("scripts.cwiki_download.subprocess.run", fake_run)
  monkeypatch.setattr("scripts.cwiki_download.is_debug_port_open", lambda port: False)

  ensure_chrome_debugging(allow_restart=True, chrome_start_url="https://cwiki.guazi.com/pages/viewpage.action?pageId=123")

  assert commands == [["bash", str(get_start_chrome_debug_script_path())]]
  assert envs[0]["CHROME_START_URL"] == "https://cwiki.guazi.com/pages/viewpage.action?pageId=123"


def test_refuses_to_restart_chrome_without_explicit_flag(monkeypatch) -> None:
  monkeypatch.setattr("scripts.cwiki_download.is_debug_port_open", lambda port: False)

  try:
    ensure_chrome_debugging(allow_restart=False)
  except CwikiDownloadError as error:
    assert error.code == "E_CHROME_DEBUG_PORT_UNAVAILABLE"
    assert "restart-chrome" in error.suggestion
  else:
    raise AssertionError("expected CwikiDownloadError")


def test_preflight_check_fails_cookie_check_when_debug_port_is_closed(monkeypatch, tmp_path: Path) -> None:
  monkeypatch.setattr("scripts.cwiki_download.shutil.which", lambda name: "/opt/homebrew/bin/uv")
  monkeypatch.setattr("scripts.cwiki_download.is_debug_port_open", lambda port: False)
  monkeypatch.setattr("scripts.cwiki_download.get_workspace_root", lambda: tmp_path)
  monkeypatch.delenv(CWIKI_USE_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.delenv(CWIKI_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.setenv(CWIKI_COOKIE_SOURCE_ENV, "chrome")

  results = asyncio.run(run_preflight_check())

  assert any(item.name == "chrome_debug_port" and item.status == "FAIL" for item in results)
  assert any(item.name == "confluence_cookie" and item.status == "FAIL" for item in results)


def test_preflight_check_uses_group_probe_for_sso(monkeypatch, tmp_path: Path) -> None:
  calls = []

  class FakeResponse:
    def raise_for_status(self) -> None:
      return None

  class FakeAsyncClient:
    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
      return None

    async def get(self, url: str, headers: dict):
      calls.append({"url": url, "headers": headers})
      return FakeResponse()

  monkeypatch.setattr("scripts.cwiki_download.shutil.which", lambda name: "/opt/homebrew/bin/uv")
  monkeypatch.setattr("scripts.cwiki_download.get_workspace_root", lambda: tmp_path)
  monkeypatch.setattr("scripts.cwiki_download.resolve_guazi_sso_skill_root", lambda: tmp_path / "guazi-sso-login")
  monkeypatch.setattr("scripts.cwiki_download.get_cookie_header_from_sso", lambda: "GUAZISSO=sso; JSESSIONID=wiki")
  monkeypatch.setattr("scripts.cwiki_download.httpx.AsyncClient", FakeAsyncClient)
  monkeypatch.delenv(CWIKI_USE_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.delenv(CWIKI_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.setenv(CWIKI_COOKIE_SOURCE_ENV, "sso")

  results = asyncio.run(run_preflight_check())

  assert any(item.name == "sso_login" and item.status == "PASS" for item in results)
  assert any(
    item.name == "confluence_api"
    and item.status == "PASS"
    and item.detail == "https://cwiki.guazi.com/rest/api/group"
    for item in results
  )
  assert calls == [{
    "url": "https://cwiki.guazi.com/rest/api/group",
    "headers": {"Cookie": "GUAZISSO=sso; JSESSIONID=wiki", "Content-Type": "application/json"},
  }]


def test_list_attachments_follows_pagination() -> None:
  class FakeResponse:
    def __init__(self, payload: dict):
      self.payload = payload

    def raise_for_status(self) -> None:
      return None

    def json(self) -> dict:
      return self.payload

  class FakeClient:
    def __init__(self) -> None:
      self.calls: list[dict] = []

    async def get(self, url: str, params: dict, headers: dict) -> FakeResponse:
      self.calls.append(params)
      if params.get("start", 0) == 0:
        return FakeResponse(
          {
            "results": [{"title": "first.png"}],
            "_links": {"next": "/rest/api/content/123/child/attachment?start=1"},
          }
        )
      return FakeResponse({"results": [{"title": "second.png"}], "_links": {}})

  attachments = asyncio.run(
    list_attachments(
      client=FakeClient(),
      base_url="https://cwiki.guazi.com",
      page_id="123",
      cookie_header="sid=1",
    )
  )

  assert [item["title"] for item in attachments] == ["first.png", "second.png"]


def test_extracts_complex_macro_preview_titles_from_view_html() -> None:
  drawio_payload = base64.b64encode(
    json.dumps({"previewName": "测试图.png"}).encode("utf-8")
  ).decode("utf-8")
  view_html = f"""
  <div class="conf-macro output-block" data-macro-name="easymind">
    <a href="/plugins/namymap/view">
      <img src="/download/attachments/647421114/%E6%80%9D%E7%BB%B4%E5%AF%BC%E5%9B%BE.png?api=v2" />
    </a>
  </div>
  <div class="conf-macro output-block" data-macro-name="drawio">
    <div id="drawio-macro-data-1" data-diagramdata="{drawio_payload}"></div>
  </div>
  """

  previews = extract_complex_macro_preview_titles(view_html)

  assert previews == {
    "easymind": ["思维导图.png"],
    "drawio": ["测试图.png"],
  }


def test_extracts_mermaid_preview_titles_from_storage_html() -> None:
  storage_html = """
  <ac:structured-macro ac:name="mermaid-cloud">
    <ac:parameter ac:name="filename">查询出价和订单列表</ac:parameter>
    <ac:parameter ac:name="format">svg</ac:parameter>
  </ac:structured-macro>
  """

  previews = extract_storage_macro_preview_titles(storage_html)

  assert previews == {
    "mermaid-cloud": [
      "查询出价和订单列表.svg",
      "查询出价和订单列表.png",
    ],
  }


def test_extracts_page_and_jira_links_from_view_html() -> None:
  view_html = """
  <p>wiki地址：<a href="/pages/viewpage.action?pageId=643230190">DCN支持车主车商议价</a></p>
  <span class="jira-issue conf-macro output-block" data-jira-key="SCC-40725" data-macro-name="jira">
    <a href="https://cjira.guazi-corp.com/browse/SCC-40725" class="jira-issue-key">SCC-40725</a>
  </span>
  """

  page_links, macro_links = extract_view_macro_links(view_html)

  assert page_links == {
    "DCN支持车主车商议价": "/pages/viewpage.action?pageId=643230190",
  }
  assert macro_links == {
    "jira": {
      "SCC-40725": "https://cjira.guazi-corp.com/browse/SCC-40725",
    }
  }
