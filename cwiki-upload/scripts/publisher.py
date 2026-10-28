import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from .common import CwikiUploadError, LocalReference
from .confluence_client import CONFLUENCE_BASE_URL_DEFAULT
from .markdown_storage import (
  build_comment_storage_body,
  build_storage_body,
  describe_prettify_scheme,
  extract_mermaid_blocks,
)
from .target_resolver import resolve_space_and_title_from_page_url, resolve_target_page_id, resolve_title


@dataclass(slots=True)
class PublisherDeps:
  get_workspace_root: object
  load_login_config: object
  resolve_cookie_header: object
  async_client_factory: object
  get_page_by_id: object
  update_page: object
  create_page: object
  find_page_by_space_and_title: object
  find_child_page_by_title: object
  list_attachment_titles: object
  upload_attachment: object
  create_footer_comment: object
  render_mermaid_with_kroki: object
  collect_local_references: object


async def render_mermaid_with_kroki(
  *,
  client: httpx.AsyncClient,
  source: str,
  output_format: str,
) -> bytes | None:
  try:
    resp = await client.post(
      f"https://kroki.io/mermaid/{output_format}",
      headers={"Content-Type": "text/plain; charset=utf-8"},
      content=source.encode("utf-8"),
      timeout=20,
    )
    if resp.status_code != 200:
      return None
    return resp.content
  except Exception:
    return None


async def resolve_existing_page(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  target_page_id: str | None,
  target_page_url: str | None,
  cookie_header: str,
  deps: PublisherDeps,
) -> dict:
  if target_page_id:
    return await deps.get_page_by_id(
      client=client,
      base_url=base_url,
      page_id=target_page_id,
      cookie_header=cookie_header,
    )

  if not target_page_url:
    raise CwikiUploadError(
      "E_MISSING_COMMENT_TARGET",
      "评论-only 模式缺少目标页面。",
      "请传入 --page-id 或 --page-url。",
    )

  resolved_page_id = resolve_target_page_id(page_id=None, page_url=target_page_url)
  if resolved_page_id:
    return await deps.get_page_by_id(
      client=client,
      base_url=base_url,
      page_id=resolved_page_id,
      cookie_header=cookie_header,
    )

  from_url = resolve_space_and_title_from_page_url(target_page_url)
  if not from_url:
    raise CwikiUploadError(
      "E_INVALID_PAGE_URL",
      f"无法从页面链接解析 pageId：{target_page_url}",
      "请使用包含 pageId 的链接，或使用 /display/<space>/<title> 形式链接，或直接传入 --page-id。",
    )
  target_space_key, target_title = from_url
  existing = await deps.find_page_by_space_and_title(
    client=client,
    base_url=base_url,
    space_key=target_space_key,
    title=target_title,
    cookie_header=cookie_header,
  )
  if not existing:
    raise CwikiUploadError(
      "E_PAGE_NOT_FOUND",
      f"未找到页面：{target_page_url}",
      "请确认链接有效，或直接传入 --page-id。",
    )
  return existing


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
  deps: PublisherDeps,
) -> str:
  workspace_root = deps.get_workspace_root()
  deps.load_login_config(workspace_root)

  if not markdown_path.exists():
    raise CwikiUploadError("E_MARKDOWN_NOT_FOUND", f"未找到 Markdown 文件：{markdown_path}", "请确认输入路径存在。")

  markdown_text = markdown_path.read_text(encoding="utf-8")
  prettify_plan: dict[str, Any] | None = None
  if prettify_plan_path:
    if not prettify_plan_path.exists():
      raise CwikiUploadError(
        "E_PRETTIFY_PLAN_NOT_FOUND",
        f"未找到美化方案文件：{prettify_plan_path}",
        "请确认 --prettify-plan-file 指向有效的 JSON 文件。",
      )
    try:
      loaded_plan = json.loads(prettify_plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
      raise CwikiUploadError(
        "E_PRETTIFY_PLAN_INVALID",
        f"美化方案 JSON 解析失败：{exc}",
        "请让 Agent 重新生成合法 JSON 后重试。",
      ) from exc
    if not isinstance(loaded_plan, dict):
      raise CwikiUploadError(
        "E_PRETTIFY_PLAN_INVALID",
        "美化方案根节点必须是 JSON object。",
        "请让 Agent 重新生成包含 operations 字段的 JSON 对象。",
      )
    prettify_plan = loaded_plan

  mermaid_blocks = extract_mermaid_blocks(markdown_text)
  references = deps.collect_local_references(markdown_text)
  resolved_title = resolve_title(markdown_path, title)
  valid_references: list[LocalReference] = []
  missing_assets: list[str] = []
  for item in references:
    primary = (markdown_path.parent / item.raw_path).resolve()
    if primary.exists():
      item.absolute_path = primary
      valid_references.append(item)
      continue
    decoded = unquote(item.raw_path)
    fallback = (markdown_path.parent / decoded).resolve()
    if fallback.exists():
      item.absolute_path = fallback
      valid_references.append(item)
      continue
    missing_assets.append(item.raw_path)
  missing_assets = sorted(set(missing_assets))

  storage_value = build_storage_body(
    markdown_text,
    valid_references,
    prettify=prettify,
    prettify_plan=prettify_plan,
  )
  comment_storage_value = build_comment_storage_body(comment_text) if comment_text else None
  mode = "apply" if apply_changes else "dry-run"
  summary_lines = [
    f"mode: {mode}",
    f"title: {resolved_title}",
    f"assets: {len(valid_references)}",
    f"comment: {'yes' if comment_text else 'no'}",
    f"prettify: {'yes' if prettify else 'no'}",
  ]
  if prettify:
    summary_lines.append(f"prettify_source: {'agent-plan' if prettify_plan else 'script-fallback'}")
    summary_lines.append(f"prettify_scheme: {describe_prettify_scheme(markdown_text, prettify_plan)}")
  if target_page_id:
    summary_lines.append(f"targetPageId: {target_page_id}")
  elif target_page_url:
    summary_lines.append(f"targetPageUrl: {target_page_url}")
  else:
    summary_lines.append(f"spaceKey: {space_key}")
    summary_lines.append(f"parentPageId: {parent_page_id}")
  if missing_assets:
    summary_lines.append(f"missing_assets: {len(missing_assets)}")
    summary_lines.append("missing_asset_paths: " + "；".join(missing_assets))

  if not apply_changes:
    return "\n".join(summary_lines)

  base_url = os.getenv("CONFLUENCE_BASE_URL", CONFLUENCE_BASE_URL_DEFAULT)
  cookie_header = await deps.resolve_cookie_header(allow_chrome_restart=allow_chrome_restart)

  async with deps.async_client_factory(timeout=60) as client:
    existing: dict | None = None
    if target_page_id:
      existing = await deps.get_page_by_id(
        client=client,
        base_url=base_url,
        page_id=target_page_id,
        cookie_header=cookie_header,
      )
      page_id = str(existing.get("id") or target_page_id)
      version_number = int(((existing.get("version") or {}).get("number")) or 1)
      effective_title = (title or str(existing.get("title") or "")).strip() or resolved_title
      result = await deps.update_page(
        client=client,
        base_url=base_url,
        page_id=page_id,
        title=effective_title,
        storage_value=storage_value,
        current_version=version_number,
        cookie_header=cookie_header,
      )
    else:
      if target_page_url:
        from_url = resolve_space_and_title_from_page_url(target_page_url)
        if not from_url:
          raise CwikiUploadError(
            "E_INVALID_PAGE_URL",
            f"无法从页面链接解析 pageId：{target_page_url}",
            "请使用包含 pageId 的链接，或使用 /display/<space>/<title> 形式链接，或直接传入 --page-id。",
          )
        target_space_key, target_title = from_url
        existing = await deps.find_page_by_space_and_title(
          client=client,
          base_url=base_url,
          space_key=target_space_key,
          title=target_title,
          cookie_header=cookie_header,
        )
        if not existing:
          raise CwikiUploadError(
            "E_PAGE_NOT_FOUND",
            f"未找到页面：{target_page_url}",
            "请确认链接有效，或直接传入 --page-id。",
          )
        page_id = str(existing.get("id") or "")
        version_number = int(((existing.get("version") or {}).get("number")) or 1)
        effective_title = (title or str(existing.get("title") or "")).strip() or resolved_title
        result = await deps.update_page(
          client=client,
          base_url=base_url,
          page_id=page_id,
          title=effective_title,
          storage_value=storage_value,
          current_version=version_number,
          cookie_header=cookie_header,
        )
      else:
        existing = await deps.find_child_page_by_title(
          client=client,
          base_url=base_url,
          parent_page_id=str(parent_page_id),
          title=resolved_title,
          cookie_header=cookie_header,
        )
        if existing and not allow_update:
          version_number = (((existing.get("version") or {}).get("number")) or 1)
          raise CwikiUploadError(
            "E_PAGE_ALREADY_EXISTS",
            f"父页面下已存在同名页面：{resolved_title}（pageId={existing.get('id')}，version={version_number}）",
            "默认不允许覆盖。请显式传入 --update 并在对话中确认后执行。",
          )

        if existing:
          page_id = str(existing.get("id"))
          version_number = int(((existing.get("version") or {}).get("number")) or 1)
          result = await deps.update_page(
            client=client,
            base_url=base_url,
            page_id=page_id,
            title=resolved_title,
            storage_value=storage_value,
            current_version=version_number,
            cookie_header=cookie_header,
          )
        else:
          result = await deps.create_page(
            client=client,
            base_url=base_url,
            space_key=str(space_key),
            parent_page_id=str(parent_page_id),
            title=resolved_title,
            storage_value=storage_value,
            cookie_header=cookie_header,
          )

    page_id = str(result.get("id") or "")
    existing_titles = await deps.list_attachment_titles(
      client=client,
      base_url=base_url,
      page_id=page_id,
      cookie_header=cookie_header,
    )
    for item in valid_references:
      if item.absolute_path is None:
        continue
      if item.file_name in existing_titles:
        continue
      await deps.upload_attachment(
        client=client,
        base_url=base_url,
        page_id=page_id,
        cookie_header=cookie_header,
        file_path=item.absolute_path,
      )
    if mermaid_blocks:
      with tempfile.TemporaryDirectory(prefix="cwiki-upload-mermaid-") as temp_dir:
        temp_path = Path(temp_dir)
        for block in mermaid_blocks:
          source_file = temp_path / block.filename
          source_file.write_text(block.source + "\n", encoding="utf-8")
          if source_file.name not in existing_titles:
            await deps.upload_attachment(
              client=client,
              base_url=base_url,
              page_id=page_id,
              cookie_header=cookie_header,
              file_path=source_file,
            )

          svg_name = f"{block.filename}.svg"
          if svg_name not in existing_titles:
            svg_bytes = await deps.render_mermaid_with_kroki(client=client, source=block.source, output_format="svg")
            if svg_bytes:
              svg_file = temp_path / svg_name
              svg_file.write_bytes(svg_bytes)
              await deps.upload_attachment(
                client=client,
                base_url=base_url,
                page_id=page_id,
                cookie_header=cookie_header,
                file_path=svg_file,
              )

          png_name = f"{block.filename}.png"
          if png_name not in existing_titles:
            png_bytes = await deps.render_mermaid_with_kroki(client=client, source=block.source, output_format="png")
            if png_bytes:
              png_file = temp_path / png_name
              png_file.write_bytes(png_bytes)
              await deps.upload_attachment(
                client=client,
                base_url=base_url,
                page_id=page_id,
                cookie_header=cookie_header,
                file_path=png_file,
              )
    if comment_storage_value:
      await deps.create_footer_comment(
        client=client,
        base_url=base_url,
        page_id=page_id,
        storage_value=comment_storage_value,
        cookie_header=cookie_header,
      )
  result_lines = [
    "mode: apply",
    f"pageId: {page_id}",
    f"title: {resolved_title}",
    f"assets: {len(valid_references)}",
    f"prettify: {'yes' if prettify else 'no'}",
  ]
  if prettify:
    result_lines.append(f"prettify_source: {'agent-plan' if prettify_plan else 'script-fallback'}")
    result_lines.append(f"prettify_scheme: {describe_prettify_scheme(markdown_text, prettify_plan)}")
  if comment_text:
    result_lines.append("comment: created")
  if missing_assets:
    result_lines.append(f"missing_assets: {len(missing_assets)}")
    result_lines.append("missing_asset_paths: " + "；".join(missing_assets))
  return "\n".join(result_lines)


async def publish_comment_only(
  *,
  target_page_id: str | None,
  target_page_url: str | None,
  comment_text: str,
  allow_chrome_restart: bool,
  deps: PublisherDeps,
) -> str:
  if not comment_text.strip():
    raise CwikiUploadError(
      "E_MISSING_COMMENT",
      "评论-only 模式缺少评论内容。",
      "请传入 --comment 或 --comment-file。",
    )

  workspace_root = deps.get_workspace_root()
  deps.load_login_config(workspace_root)
  base_url = os.getenv("CONFLUENCE_BASE_URL", CONFLUENCE_BASE_URL_DEFAULT)
  cookie_header = await deps.resolve_cookie_header(allow_chrome_restart=allow_chrome_restart)
  comment_storage_value = build_comment_storage_body(comment_text)

  async with deps.async_client_factory(timeout=60) as client:
    existing = await resolve_existing_page(
      client=client,
      base_url=base_url,
      target_page_id=target_page_id,
      target_page_url=target_page_url,
      cookie_header=cookie_header,
      deps=deps,
    )
    page_id = str(existing.get("id") or target_page_id or "")
    page_title = str(existing.get("title") or "").strip()
    result = await deps.create_footer_comment(
      client=client,
      base_url=base_url,
      page_id=page_id,
      storage_value=comment_storage_value,
      cookie_header=cookie_header,
    )

  return "\n".join([
    "mode: apply-comment-only",
    f"pageId: {page_id}",
    f"title: {page_title}",
    f"commentId: {result.get('id', '')}",
    "comment: created",
  ])


__all__ = [
  "PublisherDeps",
  "publish_comment_only",
  "publish_markdown",
  "render_mermaid_with_kroki",
  "resolve_existing_page",
]
