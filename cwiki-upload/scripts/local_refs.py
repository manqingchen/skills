import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from .common import LocalReference, escape_html


def _is_remote_or_data_path(path: str) -> bool:
  lowered = path.lower().strip()
  if lowered.startswith("data:"):
    return True
  parsed = urlparse(lowered)
  return parsed.scheme in {"http", "https"}


def _clean_path(raw_path: str) -> str:
  stripped = raw_path.strip().strip("<>").strip()
  if not stripped:
    return ""
  return stripped.split()[0]


def _split_attachment_paths(raw: str) -> list[str]:
  parts = re.split(r"[；;]+", raw)
  return [part.strip() for part in parts if part.strip()]


def _extract_local_paths_from_text(raw: str) -> list[str]:
  parts = re.split(r"[\s；;，,]+", raw)
  candidates: list[str] = []
  for part in parts:
    token = part.strip().strip("<>[](){}\"'")
    token = token.rstrip("。；;，,")
    token = token.split("?", 1)[0].split("#", 1)[0]
    token = _clean_path(token)
    if not token:
      continue
    if _is_remote_or_data_path(token) or token.startswith("/"):
      continue
    if "/" not in token or "." not in token:
      continue
    # 避免把 HTML 片段、属性片段误识别为文件路径
    if any(ch in token for ch in ['<', '>', '=', '"', "'", "`", "(", ")"]):
      continue
    if not re.fullmatch(r"[A-Za-z0-9_./%+\-\u4e00-\u9fff]+", token):
      continue
    candidates.append(token)
  dedup: list[str] = []
  seen: set[str] = set()
  for item in candidates:
    if item in seen:
      continue
    seen.add(item)
    dedup.append(item)
  return dedup


def _build_image_html_from_paths(paths: list[str]) -> str:
  image_html_list: list[str] = []
  for path_item in paths:
    file_name = Path(unquote(path_item)).name if path_item else ""
    if file_name:
      image_html_list.append(f'<ac:image><ri:attachment ri:filename="{escape_html(file_name)}" /></ac:image>')
  return "<br/>".join(image_html_list)


def collect_local_references(markdown_text: str) -> list[LocalReference]:
  references: list[LocalReference] = []
  image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
  link_pattern = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
  for matched in image_pattern.finditer(markdown_text):
    raw = _clean_path(matched.group(2))
    if not raw or _is_remote_or_data_path(raw) or raw.startswith("/"):
      continue
    decoded_name = Path(unquote(raw)).name
    references.append(
      LocalReference(
        kind="image",
        label=matched.group(1).strip(),
        raw_path=raw,
        file_name=decoded_name,
      )
    )
  for matched in link_pattern.finditer(markdown_text):
    raw = _clean_path(matched.group(2))
    if not raw or _is_remote_or_data_path(raw) or raw.startswith("/"):
      continue
    decoded_name = Path(unquote(raw)).name
    references.append(
      LocalReference(
        kind="link",
        label=matched.group(1).strip(),
        raw_path=raw,
        file_name=decoded_name,
      )
    )
  # 通用兜底：从文本中提取本地路径（不限字段名）
  for raw in _extract_local_paths_from_text(markdown_text):
    decoded_name = Path(unquote(raw)).name
    references.append(
      LocalReference(
        kind="link",
        label="inline_ref",
        raw_path=raw,
        file_name=decoded_name,
      )
    )
  dedup: dict[tuple[str, str], LocalReference] = {}
  for item in references:
    key = (item.kind, item.raw_path)
    if key not in dedup:
      dedup[key] = item
  return list(dedup.values())


__all__ = [
  "_build_image_html_from_paths",
  "_clean_path",
  "_extract_local_paths_from_text",
  "_is_remote_or_data_path",
  "_split_attachment_paths",
  "collect_local_references",
]
