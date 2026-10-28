import asyncio
import os
from pathlib import Path

import pytest

from scripts.cwiki_upload import (
  apply_login_cli_config,
  build_comment_storage_body,
  CwikiUploadError,
  CWIKI_COOKIE_SOURCE_ENV,
  CWIKI_UPLOAD_CONFIG_FILE_ENV,
  CWIKI_MANUAL_COOKIE_ENV,
  CWIKI_USE_MANUAL_COOKIE_ENV,
  build_parser,
  build_storage_body,
  collect_local_references,
  get_user_config_path,
  load_login_config,
  resolve_cookie_source,
  resolve_comment_text,
  resolve_space_and_parent,
  resolve_target_page_id,
  resolve_title,
  normalize_cli_error,
  publish_comment_only,
  publish_markdown,
  run_preflight_check,
  write_workspace_env_values,
)
from scripts.markdown_storage import describe_prettify_scheme


SKILL_ROOT = Path(__file__).resolve().parent.parent


def test_start_chrome_debug_script_detaches_chrome_from_parent_shell() -> None:
  script = (SKILL_ROOT / "start-chrome-debug.sh").read_text(encoding="utf-8")
  assert script.startswith("#!/usr/bin/env bash")
  assert 'DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}"' in script
  assert 'nohup "$CHROME_BIN"' in script
  assert "</dev/null >/dev/null 2>&1 &" in script
  assert 'disown "$CHROME_PID"' in script
  assert "cleanup_stale_chrome_singletons()" in script
  assert "SingletonLock SingletonCookie SingletonSocket" in script
  assert 'open -na "$CHROME_APP_NAME" --args' in script
  assert 'CHROME_START_URL="${CHROME_START_URL:-about:blank}"' in script


def test_parser_supports_apply_update_and_restart_chrome_flags() -> None:
  parser = build_parser()
  args = parser.parse_args(
    [
      "./docs/spec.md",
      "--space-key",
      "AI",
      "--parent-page-id",
      "123456",
      "--apply",
      "--update",
      "--prettify",
      "--prettify-plan-file",
      "./docs/prettify-plan.json",
      "--restart-chrome",
      "--cookie-source",
      "chrome",
      "--save-login-config",
    ]
  )

  assert args.markdown_path == "./docs/spec.md"
  assert args.space_key == "AI"
  assert args.parent_page_id == "123456"
  assert args.apply is True
  assert args.update is True
  assert args.prettify is True
  assert args.prettify_plan_file == "./docs/prettify-plan.json"
  assert args.restart_chrome is True
  assert args.cookie_source == "chrome"
  assert args.save_login_config == "user"


def test_parser_supports_target_page_id_and_page_url() -> None:
  parser = build_parser()
  args = parser.parse_args(
    [
      "./docs/spec.md",
      "--page-id",
      "649300169",
      "--page-url",
      "https://cwiki.guazi.com/display/CTOBTECH/Skills+Hub",
    ]
  )

  assert args.page_id == "649300169"
  assert args.page_url == "https://cwiki.guazi.com/display/CTOBTECH/Skills+Hub"


def test_parser_supports_comment() -> None:
  parser = build_parser()
  args = parser.parse_args([
    "./docs/spec.md",
    "--page-id",
    "649300169",
    "--comment",
    "这是一条发布评论",
  ])

  assert args.page_id == "649300169"
  assert args.comment == "这是一条发布评论"


def test_parser_supports_comment_file() -> None:
  parser = build_parser()
  args = parser.parse_args([
    "./docs/spec.md",
    "--page-id",
    "649300169",
    "--comment-file",
    "./docs/comment.md",
  ])

  assert args.page_id == "649300169"
  assert args.comment_file == "./docs/comment.md"


def test_parser_supports_apply_comment_only() -> None:
  parser = build_parser()
  args = parser.parse_args([
    "--page-id",
    "649300169",
    "--comment-file",
    "./docs/comment.md",
    "--apply-comment-only",
  ])

  assert args.markdown_path is None
  assert args.page_id == "649300169"
  assert args.comment_file == "./docs/comment.md"
  assert args.apply_comment_only is True


def test_parser_allows_check_without_markdown_argument() -> None:
  parser = build_parser()
  args = parser.parse_args(["--check"])

  assert args.markdown_path is None
  assert args.check is True


def test_resolve_space_and_parent_prefers_cli_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv("CONFLUENCE_SPACE_KEY", "ENV_SPACE")
  monkeypatch.setenv("CONFLUENCE_PARENT_PAGE_ID", "999999")

  space_key, parent_page_id = resolve_space_and_parent(space_key="CLI_SPACE", parent_page_id="123456")

  assert space_key == "CLI_SPACE"
  assert parent_page_id == "123456"


def test_resolve_space_and_parent_uses_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv("CONFLUENCE_SPACE_KEY", "ENV_SPACE")
  monkeypatch.setenv("CONFLUENCE_PARENT_PAGE_ID", "999999")

  space_key, parent_page_id = resolve_space_and_parent(space_key=None, parent_page_id=None)

  assert space_key == "ENV_SPACE"
  assert parent_page_id == "999999"


def test_resolve_space_and_parent_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.delenv("CONFLUENCE_SPACE_KEY", raising=False)
  monkeypatch.delenv("CONFLUENCE_PARENT_PAGE_ID", raising=False)

  with pytest.raises(CwikiUploadError) as error:
    resolve_space_and_parent(space_key=None, parent_page_id=None)

  assert error.value.code == "E_MISSING_TARGET"


def test_resolve_target_page_id_prefers_page_id() -> None:
  assert resolve_target_page_id(page_id="649300169", page_url=None) == "649300169"


def test_resolve_target_page_id_extracts_page_id_from_url_query() -> None:
  assert (
    resolve_target_page_id(
      page_id=None,
      page_url="https://cwiki.guazi.com/pages/viewpage.action?pageId=649300169",
    )
    == "649300169"
  )


def test_resolve_target_page_id_allows_display_url_without_page_id() -> None:
  assert (
    resolve_target_page_id(page_id=None, page_url="https://cwiki.guazi.com/display/CTOBTECH/Skills+Hub")
    is None
  )


def test_resolve_target_page_id_rejects_invalid_page_id() -> None:
  with pytest.raises(CwikiUploadError) as error:
    resolve_target_page_id(page_id="abc", page_url=None)
  assert error.value.code == "E_INVALID_PAGE_ID"


def test_resolve_cookie_source_prefers_manual_cookie_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv(CWIKI_USE_MANUAL_COOKIE_ENV, "true")
  monkeypatch.setenv(CWIKI_MANUAL_COOKIE_ENV, "sid=abc;token=123")
  assert resolve_cookie_source() == "manual"


def test_resolve_cookie_source_defaults_to_sso_when_manual_cookie_missing(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv(CWIKI_USE_MANUAL_COOKIE_ENV, "true")
  monkeypatch.delenv(CWIKI_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.delenv(CWIKI_COOKIE_SOURCE_ENV, raising=False)
  assert resolve_cookie_source() == "sso"


def test_resolve_cookie_source_uses_explicit_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.delenv(CWIKI_USE_MANUAL_COOKIE_ENV, raising=False)
  monkeypatch.setenv(CWIKI_COOKIE_SOURCE_ENV, "chrome")
  assert resolve_cookie_source() == "chrome"


def test_resolve_comment_text_prefers_inline_comment() -> None:
  assert resolve_comment_text(comment_text="内联评论", comment_file=None) == "内联评论"


def test_resolve_comment_text_reads_comment_file(tmp_path: Path) -> None:
  comment_path = tmp_path / "comment.md"
  comment_path.write_text("## 发布完成\n\n请关注回归结果。", encoding="utf-8")

  resolved = resolve_comment_text(comment_text=None, comment_file=str(comment_path))

  assert resolved == "## 发布完成\n\n请关注回归结果。"


def test_resolve_comment_text_rejects_conflict(tmp_path: Path) -> None:
  comment_path = tmp_path / "comment.md"
  comment_path.write_text("发布完成", encoding="utf-8")

  with pytest.raises(CwikiUploadError) as error:
    resolve_comment_text(comment_text="内联评论", comment_file=str(comment_path))

  assert error.value.code == "E_COMMENT_CONFLICT"


def test_resolve_comment_text_rejects_missing_file(tmp_path: Path) -> None:
  missing_path = tmp_path / "missing.md"

  with pytest.raises(CwikiUploadError) as error:
    resolve_comment_text(comment_text=None, comment_file=str(missing_path))

  assert error.value.code == "E_COMMENT_FILE_NOT_FOUND"


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


def test_apply_login_cli_config_sets_and_saves_manual_cookie_to_user_config(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  parser = build_parser()
  args = parser.parse_args(["--check", "--manual-cookie", "sid=abc", "--save-login-config"])
  user_config = tmp_path / "user.env"
  monkeypatch.setenv(CWIKI_UPLOAD_CONFIG_FILE_ENV, str(user_config))
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


def test_apply_login_cli_config_can_save_project_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  parser = build_parser()
  args = parser.parse_args(["--check", "--cookie-source", "chrome", "--save-login-config", "project"])
  monkeypatch.delenv(CWIKI_COOKIE_SOURCE_ENV, raising=False)

  try:
    env_path = apply_login_cli_config(args, workspace_root=tmp_path)

    assert env_path == tmp_path / ".env"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == 'CWIKI_COOKIE_SOURCE="chrome"\n'
  finally:
    os.environ.pop(CWIKI_COOKIE_SOURCE_ENV, None)


def test_load_login_config_allows_project_to_override_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  user_config = tmp_path / "user.env"
  user_config.write_text('CWIKI_COOKIE_SOURCE="sso"\n', encoding="utf-8")
  (tmp_path / ".env").write_text('CWIKI_COOKIE_SOURCE="chrome"\n', encoding="utf-8")
  monkeypatch.setenv(CWIKI_UPLOAD_CONFIG_FILE_ENV, str(user_config))
  monkeypatch.delenv(CWIKI_COOKIE_SOURCE_ENV, raising=False)

  load_login_config(tmp_path)

  assert os.environ[CWIKI_COOKIE_SOURCE_ENV] == "chrome"


def test_load_login_config_keeps_explicit_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  user_config = tmp_path / "user.env"
  user_config.write_text('CWIKI_COOKIE_SOURCE="sso"\n', encoding="utf-8")
  (tmp_path / ".env").write_text('CWIKI_COOKIE_SOURCE="chrome"\n', encoding="utf-8")
  monkeypatch.setenv(CWIKI_UPLOAD_CONFIG_FILE_ENV, str(user_config))
  monkeypatch.setenv(CWIKI_COOKIE_SOURCE_ENV, "manual")

  load_login_config(tmp_path)

  assert os.environ[CWIKI_COOKIE_SOURCE_ENV] == "manual"


def test_preflight_check_uses_group_probe_for_sso(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

  monkeypatch.setattr("scripts.cwiki_upload.shutil.which", lambda name: "/opt/homebrew/bin/uv")
  monkeypatch.setattr("scripts.cwiki_upload.get_workspace_root", lambda: tmp_path)
  monkeypatch.setattr("scripts.cwiki_upload.resolve_guazi_sso_skill_root", lambda: tmp_path / "guazi-sso-login")
  monkeypatch.setattr("scripts.cwiki_upload.get_cookie_header_from_sso", lambda: "GUAZISSO=sso; JSESSIONID=wiki")
  monkeypatch.setattr("scripts.cwiki_upload.httpx.AsyncClient", FakeAsyncClient)
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


def test_resolve_title_uses_file_stem_by_default() -> None:
  assert resolve_title(Path("/tmp/发布说明-v2.md"), title=None) == "发布说明-v2"


def test_resolve_title_allows_override() -> None:
  assert resolve_title(Path("/tmp/发布说明-v2.md"), title="手动标题") == "手动标题"


def test_collect_local_references_only_keeps_relative_paths() -> None:
  markdown_text = """
  ![架构图](./assets/arch.png)
  [接口文档](./assets/api.pdf)
  ![编码图片](./assets/used%20image.png)
  - 附件引用: assets/inline.png
  [远端链接](https://example.com/demo.pdf)
  ![Base64](data:image/png;base64,abc)
  """

  references = collect_local_references(markdown_text)
  values = {(item.kind, item.raw_path) for item in references}

  assert ("image", "./assets/arch.png") in values
  assert ("link", "./assets/api.pdf") in values
  assert ("image", "./assets/used%20image.png") in values
  assert ("link", "assets/inline.png") in values
  assert ("link", "https://example.com/demo.pdf") not in values
  assert ("image", "data:image/png;base64,abc") not in values


def test_build_storage_body_rewrites_local_refs_to_attachment_tags() -> None:
  markdown_text = """
  # 发布说明

  ![架构图](./assets/arch.png)

  [接口文档](./assets/api.pdf)
  """
  references = collect_local_references(markdown_text)
  storage = build_storage_body(markdown_text, references)

  assert "ri:filename=\"arch.png\"" in storage
  assert "ri:filename=\"api.pdf\"" in storage
  assert "<h1>发布说明</h1>" in storage


def test_build_storage_body_adds_toc_macro_when_headings_can_generate_directory() -> None:
  markdown_text = """
  # 发布说明

  ## 背景

  内容

  ## 方案

  内容
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert 'ac:name="toc"' in storage
  assert 'ac:name="minLevel">2' in storage
  assert storage.index('ac:name="toc"') > storage.index("<h1>发布说明</h1>")


def test_build_storage_body_skips_toc_macro_when_directory_is_not_available() -> None:
  markdown_text = """
  # 发布说明

  正文
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert 'ac:name="toc"' not in storage


def test_encoded_reference_filename_is_unquoted_in_attachment_tag() -> None:
  markdown_text = """
  # 标题

  ![编码图片](./assets/used%20image.png)
  """
  references = collect_local_references(markdown_text)
  storage = build_storage_body(markdown_text, references)

  # 附件 filename 应该是解码后的真实文件名
  assert "ri:filename=\"used image.png\"" in storage


def test_build_storage_body_renders_list_and_codeblock() -> None:
  markdown_text = """
  # 标题

  - A
  - B

  ```python
  print("hi")
  ```
  """
  references = collect_local_references(markdown_text)
  storage = build_storage_body(markdown_text, references)

  assert "<div>A</div>" in storage
  assert "<div>B</div>" in storage
  assert 'ac:name="code"' in storage
  assert "print(" in storage
  assert "hi" in storage


def test_confluence_admonition_blocks_are_converted_to_macros() -> None:
  markdown_text = """
  # 页面

  :::info 结论摘要
  这里是 **关键结论**。
  :::

  :::warning 风险提示
  - Skywalking 8.9 不支持 OTLP
  - 需要短期替代方案
  :::
  """

  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert '<ac:structured-macro ac:name="info">' in storage
  assert '<ac:parameter ac:name="title">结论摘要</ac:parameter>' in storage
  assert "<strong>关键结论</strong>" in storage
  assert '<ac:structured-macro ac:name="warning">' in storage
  assert '<ac:parameter ac:name="title">风险提示</ac:parameter>' in storage
  assert "<div>Skywalking 8.9 不支持 OTLP</div>" in storage


def test_confluence_panel_block_supports_company_tone_params() -> None:
  markdown_text = """
  # 页面

  :::panel 问题背景 {tone=warning}
  这里是 **关键背景**。
  :::
  """

  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert '<ac:structured-macro ac:name="panel">' in storage
  assert '<ac:parameter ac:name="title">问题背景</ac:parameter>' in storage
  assert '<ac:parameter ac:name="bgColor">#FFF7E0</ac:parameter>' in storage
  assert '<ac:parameter ac:name="borderColor">#FFB432</ac:parameter>' in storage
  assert '<ac:parameter ac:name="titleBGColor">#FFCB7A</ac:parameter>' in storage
  assert '<ac:parameter ac:name="titleColor">#FFFFFF</ac:parameter>' in storage
  assert "<strong>关键背景</strong>" in storage


def test_confluence_expand_block_supports_code_macro_inside() -> None:
  markdown_text = """
  # 页面

  :::expand 配置详情
  ```bash
  SPAN_STORAGE_TYPE=badger
  ```
  :::
  """

  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert '<ac:structured-macro ac:name="expand">' in storage
  assert '<ac:parameter ac:name="title">配置详情</ac:parameter>' in storage
  assert '<ac:structured-macro ac:name="code">' in storage
  assert "SPAN_STORAGE_TYPE=badger" in storage


def test_confluence_code_fence_supports_title_attrs() -> None:
  markdown_text = """
  # 页面

  ```ts title=instrumentation.ts
  export async function register() {}
  ```
  """

  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert '<ac:structured-macro ac:name="code">' in storage
  assert '<ac:parameter ac:name="language">ts</ac:parameter>' in storage
  assert '<ac:parameter ac:name="title">instrumentation.ts</ac:parameter>' in storage
  assert "export async function register()" in storage


def test_confluence_status_inline_is_converted_to_macro() -> None:
  markdown_text = """
  # 发布

  当前状态：:status[已完成]{color=green}，风险：:status[阻塞]{color=red subtle=false}
  """

  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert storage.count('<ac:structured-macro ac:name="status">') == 2
  assert '<ac:parameter ac:name="title">已完成</ac:parameter>' in storage
  assert '<ac:parameter ac:name="colour">Green</ac:parameter>' in storage
  assert '<ac:parameter ac:name="title">阻塞</ac:parameter>' in storage
  assert '<ac:parameter ac:name="colour">Red</ac:parameter>' in storage
  assert '<ac:parameter ac:name="subtle">false</ac:parameter>' in storage


def test_confluence_columns_blocks_are_converted_to_section_and_column_macros() -> None:
  markdown_text = """
  # 页面

  :::columns
  :::column 50%
  :::panel 当前方案 {tone=info}
  内存模式
  :::
  :::
  :::column {width=50%}
  :::panel 后续方案 {tone=warning}
  Badger 持久化
  :::
  :::
  :::
  """

  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert '<ac:structured-macro ac:name="section">' in storage
  assert storage.count('<ac:structured-macro ac:name="column">') == 2
  assert storage.count('<ac:parameter ac:name="width">50%</ac:parameter>') == 2
  assert '<ac:parameter ac:name="title">当前方案</ac:parameter>' in storage
  assert '<ac:parameter ac:name="title">后续方案</ac:parameter>' in storage
  assert "内存模式" in storage
  assert "Badger 持久化" in storage


def test_confluence_status_inside_panel_paragraph_preserves_paragraph_close() -> None:
  markdown_text = """
  # 页面

  :::panel 当前方案 {tone=info}
  **状态：** :status[适合测试]{color=blue}
  :::
  """

  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert '<p><strong>状态：</strong> <ac:structured-macro ac:name="status">' in storage
  assert '</ac:structured-macro></p></ac:rich-text-body>' in storage


def test_prettify_preserves_confluence_macros_while_styling_regular_tables() -> None:
  markdown_text = """
  # 页面

  :::info 摘要
  | 项 | 值 |
  | --- | --- |
  | 宏内 | 不改 |
  :::

  ## 服务地址

  | 用途 | 地址 |
  | --- | --- |
  | UI | example.com |
  """

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
  )
  info_start = storage.index('<ac:structured-macro ac:name="info">')
  info_end = storage.index("</ac:structured-macro>", info_start)
  info_macro = storage[info_start:info_end]

  assert '<ac:structured-macro ac:name="info">' in storage
  assert "<th>项</th>" in info_macro
  assert 'background-color: #F5F7FA' not in info_macro
  assert '<th style="background-color: #F5F7FA; color: #303741;">用途</th>' in storage[info_end:]


def test_prettify_preserves_nested_confluence_macros() -> None:
  markdown_text = """
  # 页面

  :::expand 配置详情
  ```bash
  SPAN_STORAGE_TYPE=badger
  ```
  :::

  ## 服务地址

  | 用途 | 地址 |
  | --- | --- |
  | UI | example.com |
  """

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
  )
  expand_start = storage.index('<ac:structured-macro ac:name="expand">')
  heading_start = storage.index("<h2", expand_start)
  expand_macro = storage[expand_start:heading_start]

  assert '<ac:structured-macro ac:name="code">' in expand_macro
  assert "SPAN_STORAGE_TYPE=badger" in expand_macro
  assert 'background-color: #F5F7FA' not in expand_macro
  assert '<th style="background-color: #F5F7FA; color: #303741;">用途</th>' in storage[heading_start:]


def test_prettify_restores_more_than_ten_confluence_macros_without_prefix_collision() -> None:
  rows = "\n".join(f"| :status[阶段{i}]{{color=blue}} | :status[结果{i}]{{color=green}} |" for i in range(1, 7))
  markdown_text = f"""# 页面

:::panel 摘要 {{tone=success}}
开头说明。
:::

## 方案演进

| 阶段 | 结果 |
| --- | --- |
{rows}

## 普通表

| 用途 | 地址 |
| --- | --- |
| UI | example.com |
"""

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
  )

  assert storage.count('<ac:structured-macro ac:name="status">') == 12
  assert storage.count('<ac:structured-macro ac:name="panel">') == 1
  assert "CWIKI_PROTECTED_MACRO_" not in storage
  assert '<ac:parameter ac:name="title">阶段6</ac:parameter>' in storage
  assert '<ac:parameter ac:name="title">结果6</ac:parameter>' in storage
  assert "开头说明" in storage


def test_build_storage_body_prettifies_sections_with_restrained_design_tokens() -> None:
  markdown_text = """
  # 发布说明

  ## 风险与问题

  - 存在 **阻塞风险**

  ## 验收结果

  **已通过**。
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text), prettify=True)

  assert storage.count('ac:name="panel"') == 1
  assert 'border-left: 4px solid #FF5461' in storage
  assert '<ac:parameter ac:name="bgColor">#FFECEF</ac:parameter>' in storage
  assert 'border-left: 4px solid #009B3F' in storage
  assert '<strong><span style="color: #009B3F;">已通过</span></strong>' in storage


def test_describe_prettify_scheme_reports_logical_theme_plan() -> None:
  scheme = describe_prettify_scheme("""
  # 发布说明

  ## 背景目标

  内容

  ## 后续计划

  内容
  """)

  assert "背景目标:说明信息(标题强调/#009B3F/脚本兜底)" in scheme
  assert "后续计划:计划推进(标题强调/#006633/脚本兜底)" in scheme


def test_build_storage_body_uses_agent_prettify_plan() -> None:
  markdown_text = """
  # 追踪实践

  ## 起因

  分布式追踪用于定位关键链路。

  ## 自建 Jaeger

  选择 Jaeger all-in-one 降低成本。
  """
  prettify_plan = {
    "operations": [
      {
        "selector": {"type": "section", "title": "起因"},
        "action": "callout",
        "options": {"tone": "info", "emphasis": ["关键链路"]},
      },
      {
        "selector": {"type": "section", "title": "自建 Jaeger"},
        "action": "section_accent",
        "options": {"tone": "success"},
      },
      {
        "selector": {"type": "section", "title": "自建 Jaeger"},
        "action": "keyword_emphasis",
        "options": {"keywords": ["Jaeger"], "color": "#009B3F"},
      },
    ],
  }

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
    prettify_plan=prettify_plan,
  )
  scheme = describe_prettify_scheme(markdown_text, prettify_plan)

  assert storage.count('ac:name="panel"') == 1
  assert 'border-left: 4px solid #009B3F' in storage
  assert 'border-left: 4px solid #009B3F' in storage
  assert '<span style="color: #006633;">关键链路</span>' in storage
  assert '<span style="color: #009B3F;">Jaeger</span>' in storage
  assert "起因:说明信息(轻提示面板/#009B3F/Agent操作)" in scheme
  assert "自建 Jaeger:结果结论(标题强调/#009B3F/Agent操作)" in scheme


def test_agent_prettify_operations_can_drive_generic_layout_and_styles() -> None:
  markdown_text = """
  # Jaeger 服务

  ## 背景

  需要把分布式追踪链路展示清楚。

  ## 服务地址

  **测试环境**

  | 用途 | 地址 |
  | --- | --- |
  | UI | test.example.com |

  **生产环境**

  | 用途 | 地址 |
  | --- | --- |
  | UI | prod.example.com |
  """
  prettify_plan = {
    "operations": [
      {
        "selector": {"type": "section", "title": "背景"},
        "action": "callout",
        "options": {"tone": "info", "emphasis": ["分布式追踪"]},
      },
      {
        "selector": {"type": "section", "title": "背景"},
        "action": "tag",
        "options": {"tone": "info", "keywords": ["分布式追踪"]},
      },
      {
        "selector": {"type": "tables", "scope": "all"},
        "action": "table_style",
        "options": {"headerBg": "#F5F7FA", "headerColor": "#303741"},
      },
      {
        "selector": {"type": "section", "title": "服务地址"},
        "action": "section_accent",
        "options": {"tone": "info"},
      },
      {
        "selector": {"type": "section", "title": "服务地址"},
        "action": "two_column",
        "target": "tables",
        "options": {"headerColors": ["#009B3F", "#FFB432"]},
      },
    ],
  }

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
    prettify_plan=prettify_plan,
  )
  scheme = describe_prettify_scheme(markdown_text, prettify_plan)

  assert storage.count('ac:name="panel"') == 1
  assert "分布式追踪" in storage
  assert 'width: 50%; vertical-align: top; padding-right: 12px' in storage
  assert 'border-radius: 4px; padding: 2px 6px' in storage
  assert '<th style="background-color: #F5F7FA; color: #303741;">用途</th>' in storage
  assert "背景:说明信息(轻提示面板/#009B3F/Agent操作)" in scheme
  assert "服务地址:说明信息(标题强调/#009B3F/Agent操作/paired-tables)" in scheme


def test_table_header_background_uses_sixty_percent_semantic_color() -> None:
  markdown_text = """
  # 页面

  ## 错误表

  | 风险 | 说明 |
  | --- | --- |
  | 协议 | 不兼容 |

  ## 核心表

  | 项 | 值 |
  | --- | --- |
  | 状态 | 已完成 |
  """
  prettify_plan = {
    "operations": [
      {
        "selector": {"type": "section", "title": "核心表"},
        "action": "table_style",
        "options": {"headerBg": "#009B3F"},
      },
    ],
  }

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
    prettify_plan=prettify_plan,
  )

  assert '<th style="background-color: #FF98A0; color: #FFFFFF;">风险</th>' in storage
  assert '<th style="background-color: #66C58C; color: #FFFFFF;">项</th>' in storage


def test_agent_prettify_tags_support_compact_mixed_tones() -> None:
  markdown_text = """
  # 发布说明

  ## 发布范围

  生产环境 灰度 已完成
  """
  prettify_plan = {
    "operations": [
      {
        "selector": {"type": "section", "title": "发布范围"},
        "action": "tag",
        "options": {
          "radius": "3px",
          "keywords": [
            {"text": "生产环境", "tone": "danger"},
            {"text": "灰度", "tone": "warning"},
            {"text": "已完成", "tone": "success"},
          ],
        },
      },
    ],
  }

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
    prettify_plan=prettify_plan,
  )

  assert "background-color: #FFECEF" in storage
  assert "background-color: #FFF7E0" in storage
  assert "background-color: #E5F5EC" in storage
  assert "border-radius: 3px; padding: 2px 6px" in storage


def test_keyword_emphasis_defaults_to_first_occurrence_only() -> None:
  markdown_text = """
  # 追踪实践

  ## 存储

  Badger 默认持久化。Badger 会自动 compaction。Badger 不用手动清理。
  """
  prettify_plan = {
    "operations": [
      {
        "selector": {"type": "section", "title": "存储"},
        "action": "keyword_emphasis",
        "options": {"keywords": ["Badger"], "color": "#006633"},
      },
    ],
  }

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
    prettify_plan=prettify_plan,
  )

  assert storage.count('<span style="color: #006633;">Badger</span>') == 1
  assert storage.count("Badger") == 3


def test_callout_on_long_section_only_wraps_leading_summary() -> None:
  markdown_text = """
  # 追踪实践

  ## 自建 Jaeger

  既然接不了公司的，那就自己搞一个。

  要求很简单：

  1. 原生支持 OTLP
  2. 部署和维护成本低

  ### 架构

  ```text
  app -> jaeger
  ```

  ### 部署

  后续部署说明。
  """
  prettify_plan = {
    "operations": [
      {
        "selector": {"type": "section", "title": "自建 Jaeger"},
        "action": "callout",
        "options": {"tone": "success", "emphasis": ["OTLP"]},
      },
    ],
  }

  storage = build_storage_body(
    markdown_text,
    collect_local_references(markdown_text),
    prettify=True,
    prettify_plan=prettify_plan,
  )
  panel_start = storage.index('<ac:structured-macro ac:name="panel">')
  panel_end = storage.index("</ac:structured-macro>", panel_start)
  panel_html = storage[panel_start:panel_end]

  assert "既然接不了公司的" in panel_html
  assert "原生支持" in panel_html
  assert "架构" not in panel_html
  assert "app -> jaeger" not in panel_html
  assert "架构" in storage[panel_end:]
  assert "app -> jaeger" in storage[panel_end:]


def test_prettify_does_not_turn_neutral_section_into_panel_by_body_words() -> None:
  markdown_text = """
  # 追踪实践

  ## 起因

  这里说明要解决哪个接口慢的问题，但它不是风险提示。

  ## 尝试对接公司 Skywalking：不行

  这里是明确失败结论。
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text), prettify=True)

  assert storage.count('ac:name="panel"') == 1
  assert 'border-left: 4px solid #009B3F' in storage
  assert 'border-left: 4px solid #FF5461' in storage


def test_build_comment_storage_body_supports_markdown() -> None:
  comment_storage = build_comment_storage_body("""
  **已发布**

  ```text
  done
  ```
  """)

  assert "<strong>已发布</strong>" in comment_storage
  assert 'ac:name="code"' in comment_storage
  assert "done" in comment_storage


def test_build_storage_body_renders_pipe_table_as_html_table() -> None:
  markdown_text = """
  | 序号 | 模块 | 描述 |
  | --- | --- | --- |
  | 1 | A | B |
  """
  references = collect_local_references(markdown_text)
  storage = build_storage_body(markdown_text, references)

  assert "<table" in storage
  assert "<th>序号</th>" in storage
  assert "<td>1</td>" in storage


def test_build_storage_body_converts_structured_table_to_html_table() -> None:
  markdown_text = """
  ## 结构化表格 1
  - 表头: 序号 | 模块 | 描述
  - 行 1:
    - 序号: 1
    - 模块: M1
    - 描述:
      - 文本: AAA
      - 子表格:
        - 行 1:
          - 列1: H1
          - 列2: H2
        - 行 2:
          - 列1: V1
          - 列2: V2
  """
  references = collect_local_references(markdown_text)
  storage = build_storage_body(markdown_text, references)

  assert "<table" in storage
  assert "<th>序号</th>" in storage
  assert "<td>1</td>" in storage
  assert storage.count("<table") >= 2
  assert "H1" in storage and "V1" in storage


def test_structured_table_without_header_and_with_rowspan_colspan() -> None:
  markdown_text = """
  ## 结构化表格 2
  - 行 1:
    - 列1: A [rowspan=2]
    - 列2: B
  - 行 2:
    - 列1: C
    - 列2: D [colspan=2]
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))

  assert "<table" in storage
  assert "<th>列1</th>" in storage and "<th>列2</th>" in storage
  assert 'rowspan="2"' in storage
  assert 'colspan="2"' in storage


def test_structured_table_empty_marker_is_rendered_as_empty_cell() -> None:
  markdown_text = """
  ## 结构化表格 2
  - 行 1:
    - 列1: (空) [rowspan=2]
    - 列2: A
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))
  assert '<td rowspan="2"></td>' in storage
  assert "(空)" not in storage


def test_pipe_table_strikethrough_is_rendered() -> None:
  markdown_text = """
  | 序号 | 模块 |
  | --- | --- |
  | ~~3~~ | ~~车辆退出门店寄售~~ |
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))
  assert "<s>3</s>" in storage
  assert "<s>车辆退出门店寄售</s>" in storage


def test_inline_subtable_with_named_columns_has_rows() -> None:
  markdown_text = """
  ## 结构化表格 1
  - 表头: 序号 | 模块 | 描述
  - 行 1:
    - 序号: 1
    - 模块: M1
    - 描述:
      - 子表格:
        - 表头: 分类 | 示意 | 描述
        - 行 1:
          - 分类: C2买家出价
          - 示意: C2发起还价- C1未操作
          - 描述: 示例描述
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))
  assert "<th>分类</th>" in storage
  assert "<td>C2买家出价</td>" in storage
  assert "<td>C2发起还价- C1未操作</td>" in storage


def test_inline_subtable_attachment_ref_renders_as_image_tag() -> None:
  markdown_text = """
  ## 结构化表格 1
  - 表头: 序号 | 模块 | 描述
  - 行 1:
    - 序号: 1
    - 模块: M1
    - 描述:
      - 子表格:
        - 表头: 分类 | 示意 | 描述
        - 行 1:
          - 分类: C2买家出价
          - 示意: C2发起还价- C1未操作
            - 附件引用: assets/642350937/image-2025-12-20_12-57-13.png
          - 描述: 示例描述
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))
  assert 'ri:filename="image-2025-12-20_12-57-13.png"' in storage
  assert "<ac:image>" in storage


def test_multi_attachment_refs_in_single_line_render_multiple_images() -> None:
  markdown_text = """
  ## 结构化表格 1
  - 表头: 序号 | 模块 | 描述
  - 行 1:
    - 序号: 1
    - 模块: M1
    - 描述:
      - 附件引用: assets/642350937/a.png；assets/642350937/b.png;assets/642350937/c.png
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))
  assert storage.count("<ac:image>") >= 3
  assert 'ri:filename="a.png"' in storage
  assert 'ri:filename="b.png"' in storage
  assert 'ri:filename="c.png"' in storage


def test_raw_html_table_in_markdown_is_preserved() -> None:
  markdown_text = """
  <table>
    <tbody>
      <tr><th>A</th><th>B</th></tr>
      <tr><td>1</td><td>2</td></tr>
    </tbody>
  </table>
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))
  assert "<table>" in storage
  assert "<th>A</th>" in storage
  assert "<td>1</td>" in storage


def test_mermaid_fence_is_converted_to_mermaid_cloud_macro() -> None:
  markdown_text = """
  # Mermaid 示例

  ```mermaid
  graph TD
    A[Start] --> B[End]
  ```
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))
  assert 'ac:name="mermaid-cloud"' in storage
  assert 'ac:name="toolbar"' in storage
  assert 'ac:name="filename"' in storage
  assert 'ac:name="format"' in storage
  assert 'ac:name="zoom"' in storage
  assert 'ac:name="revision"' in storage


def test_typescript_fence_is_converted_to_code_macro() -> None:
  markdown_text = """
  # 代码示例

  ```typescript
  const sum = (a: number, b: number) => a + b;
  ```
  """
  storage = build_storage_body(markdown_text, collect_local_references(markdown_text))
  assert 'ac:name="code"' in storage
  assert 'ac:name="language">typescript' in storage
  assert "const sum = (a: number, b: number) => a + b;" in storage
  assert "<ac:plain-text-body><![CDATA[" in storage


def test_normalize_cli_error_includes_response_text_for_http_400() -> None:
  import httpx

  request = httpx.Request("PUT", "https://cwiki.guazi.com/rest/api/content/1")
  response = httpx.Response(400, request=request, content=b'{"message":"Bad Request","reason":"x"}')
  error = normalize_cli_error(httpx.HTTPStatusError("bad request", request=request, response=response))

  assert error.code == "E_CONFLUENCE_HTTP_ERROR"
  assert "400" in error.message
  assert "Bad Request" in error.message


def test_publish_markdown_creates_footer_comment_when_requested(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  markdown_path = tmp_path / "发布说明.md"
  markdown_path.write_text("# 标题\n\n正文\n", encoding="utf-8")

  class FakeAsyncClient:
    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
      return None

  captured: dict[str, object] = {}

  monkeypatch.setattr("scripts.cwiki_upload.get_workspace_root", lambda: tmp_path)
  monkeypatch.setattr("scripts.cwiki_upload.load_login_config", lambda workspace_root: None)
  monkeypatch.setattr("scripts.cwiki_upload.resolve_cookie_header", _async_return("sid=1"))
  monkeypatch.setattr("scripts.cwiki_upload.httpx.AsyncClient", FakeAsyncClient)
  monkeypatch.setattr("scripts.cwiki_upload.find_child_page_by_title", _async_return(None))
  monkeypatch.setattr(
    "scripts.cwiki_upload.create_page",
    _async_return({"id": "123", "title": "发布说明", "version": {"number": 1}}),
  )
  monkeypatch.setattr("scripts.cwiki_upload.list_attachment_titles", _async_return(set()))
  monkeypatch.setattr("scripts.cwiki_upload.upload_attachment", _async_return({}))
  monkeypatch.setattr("scripts.cwiki_upload._render_mermaid_with_kroki", _async_return(None))

  async def fake_create_footer_comment(**kwargs):
    captured.update(kwargs)
    return {"id": "comment-1"}

  monkeypatch.setattr("scripts.cwiki_upload.create_footer_comment", fake_create_footer_comment)

  result = asyncio.run(
    publish_markdown(
      markdown_path=markdown_path,
      space_key="AI",
      parent_page_id="123456",
      target_page_id=None,
      target_page_url=None,
      title=None,
      comment_text="发布完成\n\n- 时间：刚刚",
      allow_update=False,
      apply_changes=True,
      allow_chrome_restart=False,
      prettify=False,
      prettify_plan_path=None,
    )
  )

  assert "comment: created" in result
  assert captured["page_id"] == "123"
  assert captured["cookie_header"] == "sid=1"
  assert "<li>" not in str(captured["storage_value"])
  assert "发布完成" in str(captured["storage_value"])


def test_publish_comment_only_creates_footer_comment(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  class FakeAsyncClient:
    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
      return None

  captured: dict[str, object] = {}

  monkeypatch.setattr("scripts.cwiki_upload.get_workspace_root", lambda: tmp_path)
  monkeypatch.setattr("scripts.cwiki_upload.load_login_config", lambda workspace_root: None)
  monkeypatch.setattr("scripts.cwiki_upload.resolve_cookie_header", _async_return("sid=1"))
  monkeypatch.setattr("scripts.cwiki_upload.httpx.AsyncClient", FakeAsyncClient)
  monkeypatch.setattr(
    "scripts.cwiki_upload.get_page_by_id",
    _async_return({"id": "649300169", "title": "测试上传", "version": {"number": 32}}),
  )

  async def fake_create_footer_comment(**kwargs):
    captured.update(kwargs)
    return {"id": "comment-1"}

  monkeypatch.setattr("scripts.cwiki_upload.create_footer_comment", fake_create_footer_comment)

  result = asyncio.run(
    publish_comment_only(
      target_page_id="649300169",
      target_page_url=None,
      comment_text="评论-only\n\n- 已完成",
      allow_chrome_restart=False,
    )
  )

  assert "mode: apply-comment-only" in result
  assert "pageId: 649300169" in result
  assert "title: 测试上传" in result
  assert "commentId: comment-1" in result
  assert captured["page_id"] == "649300169"
  assert captured["cookie_header"] == "sid=1"
  assert "评论-only" in str(captured["storage_value"])


def test_publish_comment_only_requires_comment() -> None:
  with pytest.raises(CwikiUploadError) as error:
    asyncio.run(
      publish_comment_only(
        target_page_id="649300169",
        target_page_url=None,
        comment_text="   ",
        allow_chrome_restart=False,
      )
    )

  assert error.value.code == "E_MISSING_COMMENT"


def _async_return(value):
  async def _inner(*args, **kwargs):  # noqa: ANN002, ANN003
    return value

  return _inner


def test_build_storage_body_is_well_formed_xml_fragment() -> None:
  import xml.etree.ElementTree as ET

  markdown_text = """
  # 标题

  - A
    - B

  | H1 | H2 |
  | --- | --- |
  | 1 | 2 |
  """
  refs = collect_local_references(markdown_text)
  storage = build_storage_body(markdown_text, refs)
  wrapped = (
    '<xml xmlns:ac="http://atlassian.com/content" xmlns:ri="http://atlassian.com/resource">'
    + storage
    + "</xml>"
  )
  ET.fromstring(wrapped)


def test_llm_doc_storage_is_well_formed_xml_fragment() -> None:
  import xml.etree.ElementTree as ET

  docs_dir = Path("/Users/ella/T-space/agent-skill/docs")
  candidates = sorted(docs_dir.glob("*门店寄售支持线上议价、认价-642350937.llm.md"))
  if not candidates:
    pytest.skip("缺少用于回归测试的 llm 文档样本")
  doc_path = candidates[-1]
  text = doc_path.read_text(encoding="utf-8")
  refs = collect_local_references(text)
  storage = build_storage_body(text, refs)
  wrapped = (
    '<xml xmlns:ac="http://atlassian.com/content" xmlns:ri="http://atlassian.com/resource">'
    + storage
    + "</xml>"
  )
  ET.fromstring(wrapped)
