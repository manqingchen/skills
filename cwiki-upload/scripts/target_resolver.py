import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .common import CwikiUploadError


def resolve_space_and_parent(*, space_key: str | None, parent_page_id: str | None) -> tuple[str, str]:
  resolved_space_key = (space_key or os.getenv("CONFLUENCE_SPACE_KEY", "")).strip()
  resolved_parent_page_id = (parent_page_id or os.getenv("CONFLUENCE_PARENT_PAGE_ID", "")).strip()
  if not resolved_space_key or not resolved_parent_page_id:
    raise CwikiUploadError(
      "E_MISSING_TARGET",
      "缺少 spaceKey 或 parentPageId，且未在环境变量中配置默认值。",
      "请显式传入 --space-key 和 --parent-page-id，或设置 CONFLUENCE_SPACE_KEY / CONFLUENCE_PARENT_PAGE_ID。",
    )
  if not resolved_parent_page_id.isdigit():
    raise CwikiUploadError(
      "E_INVALID_PARENT_PAGE_ID",
      f"parentPageId 非法：{resolved_parent_page_id}",
      "请传入纯数字 parentPageId。",
    )
  return resolved_space_key, resolved_parent_page_id


def resolve_target_page_id(*, page_id: str | None, page_url: str | None) -> str | None:
  resolved_page_id = (page_id or "").strip()
  resolved_page_url = (page_url or "").strip()
  if resolved_page_id:
    if not resolved_page_id.isdigit():
      raise CwikiUploadError(
        "E_INVALID_PAGE_ID",
        f"pageId 非法：{resolved_page_id}",
        "请传入纯数字 pageId。",
      )
    return resolved_page_id
  if not resolved_page_url:
    return None
  parsed = urlparse(resolved_page_url)
  query_page_id = parse_qs(parsed.query).get("pageId", [""])[0].strip()
  if query_page_id:
    if not query_page_id.isdigit():
      raise CwikiUploadError(
        "E_INVALID_PAGE_URL",
        f"页面链接中的 pageId 非法：{query_page_id}",
        "请提供包含合法 pageId 的链接，或直接传入 --page-id。",
      )
    return query_page_id
  return None


def resolve_space_and_title_from_page_url(page_url: str) -> tuple[str, str] | None:
  parsed = urlparse(page_url.strip())
  path = parsed.path.strip("/")
  if not path.lower().startswith("display/"):
    return None
  parts = path.split("/", 2)
  if len(parts) < 3:
    return None
  space_key = parts[1].strip()
  page_title = unquote(parts[2]).replace("+", " ").strip()
  if not space_key or not page_title:
    return None
  return space_key, page_title


def resolve_title(markdown_path: Path, title: str | None) -> str:
  resolved = (title or markdown_path.stem).strip()
  resolved = re.sub(r"\s+", " ", resolved)
  if not resolved:
    raise CwikiUploadError("E_INVALID_TITLE", "页面标题不能为空。", "请检查文件名或显式传入 --title。")
  return resolved[:255]


def resolve_comment_text(*, comment_text: str | None, comment_file: str | None) -> str | None:
  inline_comment = (comment_text or "").strip()
  file_path_raw = (comment_file or "").strip()
  if inline_comment and file_path_raw:
    raise CwikiUploadError(
      "E_COMMENT_CONFLICT",
      "不能同时传入 --comment 和 --comment-file。",
      "请二选一：短评论用 --comment，长评论用 --comment-file。",
    )
  if inline_comment:
    return comment_text
  if not file_path_raw:
    return None
  comment_path = Path(file_path_raw).expanduser()
  if not comment_path.exists():
    raise CwikiUploadError(
      "E_COMMENT_FILE_NOT_FOUND",
      f"未找到评论文件：{comment_path}",
      "请确认 --comment-file 指向的本地文件存在。",
    )
  if not comment_path.is_file():
    raise CwikiUploadError(
      "E_INVALID_COMMENT_FILE",
      f"评论文件不是普通文件：{comment_path}",
      "请传入一个可读取的文本文件路径。",
    )
  return comment_path.read_text(encoding="utf-8")


__all__ = [
  "resolve_comment_text",
  "resolve_space_and_parent",
  "resolve_space_and_title_from_page_url",
  "resolve_target_page_id",
  "resolve_title",
]
