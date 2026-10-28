import json
import os
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from .confluence_client import fetch_comment_detail, list_comment_replies, list_comments


CWIKI_FETCH_COMMENTS_ENV = "CWIKI_DOWNLOAD_FETCH_COMMENTS"


def is_comment_fetch_enabled() -> bool:
  raw = os.getenv(CWIKI_FETCH_COMMENTS_ENV, "").strip().lower()
  if not raw:
    return True
  return raw not in {"0", "false", "no", "off"}


def _html_to_text(html_value: str) -> str:
  """将评论 HTML 转成纯文本；如果完全没有可见文字则返回空串。

  这样可以在后续阶段基于 bodyText 是否为空来过滤掉“只有链接/图片占位”的评论。
  """
  soup = BeautifulSoup(f"<body>{html_value}</body>", "lxml")
  text = soup.get_text("\n", strip=True)
  text = re.sub(r"\n{3,}", "\n\n", text)
  return text.strip()


def _extract_comment_author(comment: dict) -> str:
  author = comment.get("version", {}).get("by", {}).get("displayName")
  if isinstance(author, str) and author.strip():
    return author.strip()
  return "未知"


def _extract_comment_times(comment: dict) -> tuple[str, str]:
  created = comment.get("version", {}).get("when")
  updated = comment.get("version", {}).get("when")
  created_s = created.strip() if isinstance(created, str) else ""
  updated_s = updated.strip() if isinstance(updated, str) else ""
  return created_s, updated_s


def _classify_comment_type(comment: dict) -> str:
  extensions = comment.get("extensions") or {}
  location = str(extensions.get("location") or "").strip().lower()
  if location == "footer":
    return "page"
  if location == "inline":
    return "inline"
  for key in ("inline", "inlineComment", "inline-comment", "annotation"):
    if extensions.get(key):
      return "inline"
  return "page"


def _extract_inline_context(raw: dict) -> tuple[str, str, str]:
  extensions = raw.get("extensions") or {}
  inline_props = extensions.get("inlineProperties") or {}
  if not isinstance(inline_props, dict):
    return "", "", ""

  section_path = ""
  quote = ""
  marker_ref = ""
  for key in ("path", "sectionPath", "headingPath", "ancestorTitles", "markerRef"):
    value = inline_props.get(key)
    if isinstance(value, str) and value.strip():
      if key == "markerRef":
        marker_ref = value.strip()
      else:
        section_path = value.strip()
      break
    if isinstance(value, list) and value:
      section_path = " > ".join(str(item).strip() for item in value if str(item).strip())
      if section_path:
        break

  for key in ("text", "selection", "originalSelection", "selectedText", "excerpt"):
    value = inline_props.get(key)
    if isinstance(value, str) and value.strip():
      quote = value.strip()
      break

  return (section_path or ""), (quote or ""), (marker_ref or "")


def _normalize_comment(*, raw: dict) -> dict:
  comment_id = str(raw.get("id") or "").strip()
  created_at, updated_at = _extract_comment_times(raw)
  storage_html = (((raw.get("body") or {}).get("storage") or {}).get("value")) or ""
  section_path, quote, marker_ref = _extract_inline_context(raw)
  return {
    "id": comment_id,
    "type": _classify_comment_type(raw),
    "author": _extract_comment_author(raw),
    "createdAt": created_at,
    "updatedAt": updated_at,
    "sectionPath": section_path,
    "markerRef": marker_ref,
    "quote": quote,
    "bodyText": _html_to_text(storage_html),
    "raw": raw,
  }


def _build_heading_title_to_paths(markdown: str) -> dict[str, list[str]]:
  title_to_paths: dict[str, list[str]] = {}
  stack: list[str] = []
  for raw_line in markdown.splitlines():
    line = raw_line.rstrip()
    m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
    if not m:
      continue
    level = len(m.group(1))
    title = m.group(2).strip()
    if not title:
      continue
    if level <= len(stack):
      stack = stack[: level - 1]
    while len(stack) < level - 1:
      stack.append("")
    if level == len(stack) + 1:
      stack.append(title)
    else:
      stack[level - 1] = title
    path = " > ".join(part for part in stack if part)
    title_to_paths.setdefault(title, []).append(path)
  return title_to_paths


def enrich_inline_section_paths(*, comment_threads: list[dict], markdown: str) -> None:
  title_to_paths = _build_heading_title_to_paths(markdown)

  def walk(node: dict) -> None:
    if str(node.get("type") or "") == "inline":
      quote = str(node.get("quote") or "").strip()
      if quote and quote in title_to_paths:
        node["sectionPath"] = title_to_paths[quote][0]
      elif quote:
        normalized = re.sub(r"[\\s\\u3000]+", " ", quote).strip()
        if normalized in title_to_paths:
          node["sectionPath"] = title_to_paths[normalized][0]

      if not str(node.get("sectionPath") or "").strip():
        marker_ref = str(node.get("markerRef") or "").strip()
        if marker_ref:
          node["sectionPath"] = f"markerRef={marker_ref}"

    replies = node.get("replies") or []
    if isinstance(replies, list):
      for r in replies:
        if isinstance(r, dict):
          walk(r)

  for root in comment_threads:
    if isinstance(root, dict):
      walk(root)


def _collect_inline_comments(comment_threads: list[dict]) -> list[dict]:
  entries: list[dict] = []

  def walk(node: dict) -> None:
    if str(node.get("type") or "") == "inline":
      entries.append(
        {
          "id": str(node.get("id") or "").strip(),
          "quote": str(node.get("quote") or "").strip(),
          "sectionPath": str(node.get("sectionPath") or "").strip(),
          "markerRef": str(node.get("markerRef") or "").strip(),
        }
      )
    replies = node.get("replies") or []
    if isinstance(replies, list):
      for item in replies:
        if isinstance(item, dict):
          walk(item)

  for root in comment_threads:
    if isinstance(root, dict):
      walk(root)
  return entries


def inject_inline_markers_into_human_markdown(markdown: str, *, comment_threads: list[dict]) -> tuple[str, list[str]]:
  inline_entries = _collect_inline_comments(comment_threads)
  updated = markdown
  index_lines: list[str] = []

  for entry in inline_entries:
    cid = entry.get("id", "")
    quote = entry.get("quote", "")
    section_path = entry.get("sectionPath", "")
    if not cid:
      continue
    badge = f"〔评论#{cid}〕"
    if badge in updated:
      index_lines.append(f"- `{cid}`：已存在锚点")
      continue

    inserted = False
    if quote and quote in updated:
      updated = updated.replace(quote, f"{quote}{badge}", 1)
      inserted = True

    if not inserted and section_path:
      leaf_title = section_path.split(">")[-1].strip()
      for heading_prefix in ("# ", "## ", "### ", "#### ", "##### ", "###### "):
        needle = f"{heading_prefix}{leaf_title}"
        if leaf_title and needle in updated:
          updated = updated.replace(needle, f"{needle} {badge}", 1)
          inserted = True
          break

    if inserted:
      index_lines.append(f"- `{cid}`：正文已标注")
    else:
      index_lines.append(f"- `{cid}`：近似定位（未找到原文精确位置）")

  return updated, index_lines


def inject_inline_markers_into_llm_markdown(markdown: str, *, comment_threads: list[dict]) -> tuple[str, list[str]]:
  inline_entries = _collect_inline_comments(comment_threads)
  updated = markdown
  map_lines: list[str] = []

  for entry in inline_entries:
    cid = entry.get("id", "")
    quote = entry.get("quote", "")
    section_path = entry.get("sectionPath", "")
    marker_ref = entry.get("markerRef", "")
    if not cid:
      continue

    marker_token = f"<!-- cwiki-marker:{marker_ref or 'none'} comment:{cid} -->"
    inserted = False
    if quote and quote in updated:
      updated = updated.replace(quote, f"{quote} {marker_token}", 1)
      inserted = True
    elif section_path:
      leaf_title = section_path.split(">")[-1].strip()
      for heading_prefix in ("# ", "## ", "### ", "#### ", "##### ", "###### "):
        needle = f"{heading_prefix}{leaf_title}"
        if leaf_title and needle in updated:
          updated = updated.replace(needle, f"{needle}\n{marker_token}", 1)
          inserted = True
          break

    status = "exact" if inserted else "approx"
    map_lines.append(
      f"- commentId={cid} | markerRef={marker_ref or 'none'} | status={status} | path={section_path or 'unknown'} | quote={quote or 'none'}"
    )

  return updated, map_lines


def _build_comment_threads(normalized: list[dict]) -> list[dict]:
  nodes = {item.get("id"): {**item, "replies": []} for item in normalized if item.get("id")}
  roots: list[dict] = []

  parent_map: dict[str, str] = {}
  for item in normalized:
    raw = item.get("raw") or {}
    cid = item.get("id")
    if not cid:
      continue
    ancestors = raw.get("ancestors")
    if not isinstance(ancestors, list) or not ancestors:
      continue
    parent_id = ""
    for anc in reversed(ancestors):
      if not isinstance(anc, dict):
        continue
      if str(anc.get("type") or "").strip() == "comment":
        parent_id = str(anc.get("id") or "").strip()
        break
    if parent_id:
      parent_map[cid] = parent_id

  for cid, node in nodes.items():
    parent_id = parent_map.get(cid, "")
    parent = nodes.get(parent_id) if parent_id else None
    if parent is not None:
      parent["replies"].append(node)
    else:
      roots.append(node)

  def _sort_tree(n: dict) -> None:
    n["replies"].sort(key=lambda x: (x.get("createdAt") or "", x.get("id") or ""))
    for r in n["replies"]:
      _sort_tree(r)

  roots.sort(key=lambda x: (x.get("createdAt") or "", x.get("id") or ""))
  for r in roots:
    _sort_tree(r)
  return roots


async def fetch_comment_threads(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str,
  cookie_header: str,
) -> tuple[list[dict], str]:
  try:
    top_level_comments = await list_comments(client=client, base_url=base_url, page_id=page_id, cookie_header=cookie_header)
    all_comments: list[dict] = []
    stack = [item for item in top_level_comments if isinstance(item, dict)]
    seen_ids: set[str] = set()

    while stack:
      raw_comment = stack.pop(0)
      comment_id = str(raw_comment.get("id") or "").strip()
      if not comment_id or comment_id in seen_ids:
        continue
      seen_ids.add(comment_id)

      try:
        detail = await fetch_comment_detail(
          client=client,
          base_url=base_url,
          comment_id=comment_id,
          cookie_header=cookie_header,
        )
        merged = {**raw_comment, **detail}
      except Exception:
        merged = raw_comment

      all_comments.append(merged)

      try:
        replies = await list_comment_replies(
          client=client,
          base_url=base_url,
          comment_id=comment_id,
          cookie_header=cookie_header,
        )
        for reply in replies:
          if isinstance(reply, dict):
            stack.append(reply)
      except Exception:
        continue

    normalized = [_normalize_comment(raw=item) for item in all_comments if isinstance(item, dict)]

    # 过滤掉拿不到具体文本内容的页脚评论（例如仅包含链接/图片等结构，无正文文字）
    filtered = [
      item
      for item in normalized
      if item.get("type") == "inline" or str(item.get("bodyText") or "").strip()
    ]

    return _build_comment_threads(filtered), ""
  except Exception as exc:
    return [], str(exc)


def write_comments_json(*, asset_dir: Path, page_id: str, threads: list[dict]) -> None:
  asset_dir.mkdir(parents=True, exist_ok=True)
  (asset_dir / "comments.json").write_text(
    json.dumps(
      {
        "pageId": page_id,
        "count": sum(1 for _ in _iter_comment_nodes(threads)),
        "threads": threads,
      },
      ensure_ascii=False,
      indent=2,
    )
    + "\n",
    encoding="utf-8",
  )


def _iter_comment_nodes(threads: list[dict]):
  for node in threads:
    if not isinstance(node, dict):
      continue
    yield node
    replies = node.get("replies") or []
    if isinstance(replies, list):
      yield from _iter_comment_nodes([r for r in replies if isinstance(r, dict)])

