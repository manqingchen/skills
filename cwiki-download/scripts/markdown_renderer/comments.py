from typing import Any, Dict, List, Tuple


def _render_comment_node_to_lines(node: Dict[str, Any], *, indent: str) -> List[str]:
  cid = str(node.get("id") or "").strip() or "unknown"
  author = str(node.get("author") or "").strip() or "未知"
  created_at = str(node.get("createdAt") or "").strip()
  section_path = str(node.get("sectionPath") or "").strip()
  marker_ref = str(node.get("markerRef") or "").strip()
  quote = str(node.get("quote") or "").strip()
  body = str(node.get("bodyText") or "").strip()
  body = body or "（空）"

  head = f"{indent}- **{cid}** · **{author}**" + (f" · {created_at}" if created_at else "")
  lines = [head]
  if section_path:
    lines.append(f"{indent}  - 位置：`{section_path}`")
  if marker_ref:
    lines.append(f"{indent}  - markerRef：`{marker_ref}`")
  if quote:
    lines.append(f"{indent}  - 原文：{quote.replace(chr(10), ' ')}")
  lines.append(f"{indent}  - 评论：{body.replace(chr(10), ' ')}")
  replies = node.get("replies") or []
  if isinstance(replies, list) and replies:
    lines.append(f"{indent}  - **回复**")
    for r in replies:
      if isinstance(r, dict):
        lines.extend(_render_comment_node_to_lines(r, indent=f"{indent}    "))
  return lines


def _split_comment_threads(threads: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  page_threads: List[Dict[str, Any]] = []
  inline_threads: List[Dict[str, Any]] = []
  for t in threads:
    if not isinstance(t, dict):
      continue
    if str(t.get("type") or "").strip() == "inline":
      inline_threads.append(t)
    else:
      page_threads.append(t)
  return page_threads, inline_threads


def append_comments_section_to_markdown(markdown: str, *, threads: List[Dict[str, Any]] | None, error: str = "") -> str:
  safe_threads = threads or []
  page_threads, inline_threads = _split_comment_threads(safe_threads)

  if not error and not page_threads and not inline_threads:
    return markdown.rstrip()

  rendered = markdown.rstrip()
  rendered += "\n\n---\n\n## 评论（Page Comments）\n\n"
  if error:
    rendered += f"> 评论拉取失败：{error}\n\n"
    rendered += "（未拉取到评论内容）\n"
    return rendered.strip()

  if not page_threads:
    rendered += "（无）\n"
  else:
    for node in page_threads:
      rendered += "\n".join(_render_comment_node_to_lines(node, indent="")) + "\n"

  # 只有存在行内评论时才追加对应章节，避免出现“空的 Inline Comments 区块”
  if inline_threads:
    rendered += "\n## 评论（Inline Comments）\n\n"
    rendered += "<details><summary>展开查看行内评论</summary>\n\n"
    for node in inline_threads:
      rendered += "\n".join(_render_comment_node_to_lines(node, indent="")) + "\n"
    rendered += "\n</details>\n"
  return rendered.strip()


def append_comments_section_to_llm_markdown(markdown: str, *, threads: List[Dict[str, Any]] | None, error: str = "") -> str:
  safe_threads = threads or []
  page_threads, inline_threads = _split_comment_threads(safe_threads)

  if not error and not page_threads and not inline_threads:
    return markdown.rstrip()

  rendered = markdown.rstrip()
  rendered += "\n\n---\n\n## 评论（Page Comments）\n\n"
  if error:
    rendered += f"[COMMENT_FETCH_ERROR] {error}\n"
    return rendered.strip()

  if not page_threads:
    rendered += "（无）\n"
  else:
    for node in page_threads:
      rendered += "\n".join(_render_comment_node_to_lines(node, indent="")) + "\n"

  # 只有存在行内评论时才追加对应章节
  if inline_threads:
    rendered += "\n## 评论（Inline Comments）\n\n"
    for node in inline_threads:
      rendered += "\n".join(_render_comment_node_to_lines(node, indent="")) + "\n"
  return rendered.strip()

