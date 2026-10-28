import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from bs4 import BeautifulSoup


CONFLUENCE_BASE_URL_DEFAULT = "https://cwiki.guazi.com"
CONFLUENCE_DOMAIN_DEFAULT = ".guazi.com"


def _decode_display_segment(raw: str) -> str:
  return unquote(raw).replace("+", " ").strip()


def extract_page_locator(page_id_or_url: str) -> dict[str, str]:
  page_id_match = re.search(r"pageId=(\d+)", page_id_or_url)
  if page_id_match:
    return {"page_id": page_id_match.group(1)}

  stripped = page_id_or_url.strip()
  if stripped.isdigit():
    return {"page_id": stripped}

  path = urlsplit(stripped).path if "://" in stripped else stripped
  display_match = re.search(r"/display/([^/]+)/([^/?#]+)", path)
  if display_match:
    space_key = _decode_display_segment(display_match.group(1))
    title = _decode_display_segment(display_match.group(2))
    if space_key and title:
      return {"space_key": space_key, "title": title}

  raise ValueError("无法解析 pageId，请输入有效的 URL 或 pageId")


def extract_page_id(page_id_or_url: str) -> str:
  locator = extract_page_locator(page_id_or_url)
  page_id = locator.get("page_id")
  if not page_id:
    raise ValueError("无法解析 pageId，请输入有效的 URL 或 pageId")
  return page_id


def extract_referenced_attachment_names(storage_html: str) -> set[str]:
  referenced: set[str] = set()

  for filename in re.findall(r'ri:filename="([^"]+)"', storage_html, flags=re.IGNORECASE):
    referenced.add(unquote(filename))

  soup = BeautifulSoup(f"<body>{storage_html}</body>", "lxml")
  for tag in soup.find_all(["a", "img"]):
    value = tag.get("href") or tag.get("src")
    if not value:
      continue
    path = urlsplit(value).path
    if "/download/attachments/" not in path:
      continue
    filename = Path(unquote(path)).name
    if filename:
      referenced.add(filename)

  return referenced


def filter_referenced_attachments(storage_html: str, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
  referenced = extract_referenced_attachment_names(storage_html)
  filtered: list[dict[str, Any]] = []

  for item in attachments:
    title = str(item.get("title") or item.get("filename") or "").strip()
    if title and title in referenced:
      filtered.append(item)

  return filtered


def extract_referenced_attachment_download_urls(html_text: str) -> set[str]:
  download_urls: set[str] = set()

  def _maybe_add_download_url(value: str) -> None:
    normalized = html.unescape(value or "").strip()
    if not normalized:
      return
    if "/download/attachments/" not in normalized:
      return
    download_urls.add(normalized)

  soup = BeautifulSoup(f"<body>{html_text}</body>", "lxml")
  for tag in soup.find_all(True):
    for attr in ("href", "src", "ri:value"):
      value = tag.get(attr)
      if isinstance(value, str):
        _maybe_add_download_url(value)

  for matched in re.findall(r'https?://[^\s"<>\']+/download/attachments/[^\s"<>\']+', html_text, flags=re.IGNORECASE):
    _maybe_add_download_url(matched)
  for matched in re.findall(r'/download/attachments/[^\s"<>\']+', html_text, flags=re.IGNORECASE):
    _maybe_add_download_url(matched)

  return download_urls


async def fetch_page(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str | None,
  page_title: str | None,
  space_key: str | None,
  cookie_header: str,
) -> dict[str, Any]:
  expand_value = "body.storage,body.view,version,space,metadata.labels,_links"
  headers = {"Cookie": cookie_header, "Content-Type": "application/json"}
  if page_id:
    response = await client.get(
      f"{base_url.rstrip('/')}/rest/api/content/{page_id}",
      params={"expand": expand_value},
      headers=headers,
    )
    response.raise_for_status()
    return response.json()

  if page_title and space_key:
    response = await client.get(
      f"{base_url.rstrip('/')}/rest/api/content",
      params={"title": page_title, "spaceKey": space_key, "expand": expand_value, "limit": 1},
      headers=headers,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    if isinstance(results, list) and results:
      return results[0]
    raise ValueError(f"无法根据标题和空间定位页面：spaceKey={space_key}, title={page_title}")

  raise ValueError("缺少页面定位参数，请提供 pageId 或 display 链接")


async def list_attachments(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str,
  cookie_header: str,
) -> list[dict[str, Any]]:
  attachments: list[dict[str, Any]] = []
  start = 0

  while True:
    response = await client.get(
      f"{base_url.rstrip('/')}/rest/api/content/{page_id}/child/attachment",
      params={"limit": 999, "start": start},
      headers={"Cookie": cookie_header, "Content-Type": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    attachments.extend(data.get("results") or [])

    next_link = ((data.get("_links") or {}).get("next") if isinstance(data, dict) else None) or ""
    if not next_link:
      break

    start_match = re.search(r"[?&]start=(\d+)", str(next_link))
    if not start_match:
      break
    start = int(start_match.group(1))

  return attachments


async def list_comments(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str,
  cookie_header: str,
) -> list[dict[str, Any]]:
  comments: list[dict[str, Any]] = []
  start = 0
  expand_value = "body.storage,version,container,extensions,ancestors,_links"

  while True:
    response = await client.get(
      f"{base_url.rstrip('/')}/rest/api/content/{page_id}/child/comment",
      params={"limit": 500, "start": start, "expand": expand_value},
      headers={"Cookie": cookie_header, "Content-Type": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    comments.extend(data.get("results") or [])

    next_link = ((data.get("_links") or {}).get("next") if isinstance(data, dict) else None) or ""
    if not next_link:
      break

    start_match = re.search(r"[?&]start=(\d+)", str(next_link))
    if not start_match:
      break
    start = int(start_match.group(1))

  return comments


async def list_comment_replies(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  comment_id: str,
  cookie_header: str,
) -> list[dict[str, Any]]:
  replies: list[dict[str, Any]] = []
  start = 0
  expand_value = "body.storage,version,container,extensions,ancestors,_links"

  while True:
    response = await client.get(
      f"{base_url.rstrip('/')}/rest/api/content/{comment_id}/child/comment",
      params={"limit": 500, "start": start, "expand": expand_value},
      headers={"Cookie": cookie_header, "Content-Type": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    replies.extend(data.get("results") or [])

    next_link = ((data.get("_links") or {}).get("next") if isinstance(data, dict) else None) or ""
    if not next_link:
      break

    start_match = re.search(r"[?&]start=(\d+)", str(next_link))
    if not start_match:
      break
    start = int(start_match.group(1))

  return replies


async def fetch_comment_detail(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  comment_id: str,
  cookie_header: str,
) -> dict[str, Any]:
  response = await client.get(
    f"{base_url.rstrip('/')}/rest/api/content/{comment_id}",
    params={"expand": "body.storage,version,container,extensions,extensions.inlineProperties,extensions.resolution,ancestors,_links,children.comment"},
    headers={"Cookie": cookie_header, "Content-Type": "application/json"},
  )
  response.raise_for_status()
  payload = response.json()
  return payload if isinstance(payload, dict) else {}


def get_attachment_download_path(attachment: dict[str, Any]) -> str | None:
  links = attachment.get("_links") or {}
  download_path = links.get("download")
  if isinstance(download_path, str) and download_path:
    return download_path
  return None


async def download_attachment(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  attachment: dict[str, Any],
  cookie_header: str,
  destination: Path,
) -> None:
  download_path = get_attachment_download_path(attachment)
  if not download_path:
    raise ValueError("附件缺少下载地址")

  response = await client.get(
    f"{base_url.rstrip('/')}{download_path}",
    headers={"Cookie": cookie_header},
  )
  response.raise_for_status()
  destination.write_bytes(response.content)
