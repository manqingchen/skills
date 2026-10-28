from pathlib import Path
from typing import Any

import httpx


CONFLUENCE_BASE_URL_DEFAULT = "https://cwiki.guazi.com"
CONFLUENCE_DOMAIN_DEFAULT = ".guazi.com"


async def list_attachment_titles(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str,
  cookie_header: str,
) -> set[str]:
  titles: set[str] = set()
  start = 0
  while True:
    response = await client.get(
      f"{base_url.rstrip('/')}/rest/api/content/{page_id}/child/attachment",
      params={"limit": 200, "start": start},
      headers={"Cookie": cookie_header, "Content-Type": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    results = data.get("results") or []
    for item in results:
      title = str(item.get("title") or item.get("filename") or "").strip()
      if title:
        titles.add(title)
    next_link = ((data.get("_links") or {}).get("next") if isinstance(data, dict) else None) or ""
    if not next_link:
      break
    start += len(results)
  return titles


async def find_child_page_by_title(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  parent_page_id: str,
  title: str,
  cookie_header: str,
) -> dict[str, Any] | None:
  start = 0
  while True:
    response = await client.get(
      f"{base_url.rstrip('/')}/rest/api/content/{parent_page_id}/child/page",
      params={"limit": 200, "start": start, "expand": "version"},
      headers={"Cookie": cookie_header, "Content-Type": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    results = data.get("results") or []
    for item in results:
      if str(item.get("title") or "").strip() == title:
        return item

    next_link = ((data.get("_links") or {}).get("next") if isinstance(data, dict) else None) or ""
    if not next_link:
      return None
    start += len(results)


async def get_page_by_id(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str,
  cookie_header: str,
) -> dict[str, Any]:
  response = await client.get(
    f"{base_url.rstrip('/')}/rest/api/content/{page_id}",
    params={"expand": "version,space"},
    headers={"Cookie": cookie_header, "Content-Type": "application/json"},
  )
  response.raise_for_status()
  return response.json()


async def find_page_by_space_and_title(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  space_key: str,
  title: str,
  cookie_header: str,
) -> dict[str, Any] | None:
  response = await client.get(
    f"{base_url.rstrip('/')}/rest/api/content",
    params={
      "spaceKey": space_key,
      "title": title,
      "type": "page",
      "limit": 2,
      "expand": "version,space",
    },
    headers={"Cookie": cookie_header, "Content-Type": "application/json"},
  )
  response.raise_for_status()
  results = (response.json() or {}).get("results") or []
  if not results:
    return None
  return results[0]


async def create_page(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  space_key: str,
  parent_page_id: str,
  title: str,
  storage_value: str,
  cookie_header: str,
) -> dict[str, Any]:
  payload = {
    "type": "page",
    "title": title,
    "space": {"key": space_key},
    "ancestors": [{"id": str(parent_page_id)}],
    "body": {"storage": {"value": storage_value, "representation": "storage"}},
  }
  response = await client.post(
    f"{base_url.rstrip('/')}/rest/api/content",
    headers={"Cookie": cookie_header, "Content-Type": "application/json"},
    json=payload,
  )
  response.raise_for_status()
  return response.json()


async def update_page(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str,
  title: str,
  storage_value: str,
  current_version: int,
  cookie_header: str,
) -> dict[str, Any]:
  payload = {
    "id": str(page_id),
    "type": "page",
    "title": title,
    "version": {"number": int(current_version) + 1},
    "body": {"storage": {"value": storage_value, "representation": "storage"}},
  }
  response = await client.put(
    f"{base_url.rstrip('/')}/rest/api/content/{page_id}",
    headers={"Cookie": cookie_header, "Content-Type": "application/json"},
    json=payload,
  )
  response.raise_for_status()
  return response.json()


async def create_footer_comment(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str,
  storage_value: str,
  cookie_header: str,
) -> dict[str, Any]:
  payload = {
    "type": "comment",
    "container": {
      "id": str(page_id),
      "type": "page",
      "status": "current",
    },
    "body": {"storage": {"value": storage_value, "representation": "storage"}},
  }
  response = await client.post(
    f"{base_url.rstrip('/')}/rest/api/content",
    headers={"Cookie": cookie_header, "Content-Type": "application/json"},
    json=payload,
  )
  response.raise_for_status()
  return response.json()


async def upload_attachment(
  *,
  client: httpx.AsyncClient,
  base_url: str,
  page_id: str,
  cookie_header: str,
  file_path: Path,
) -> dict[str, Any]:
  with file_path.open("rb") as fp:
    files = {"file": (file_path.name, fp, "application/octet-stream")}
    response = await client.post(
      f"{base_url.rstrip('/')}/rest/api/content/{page_id}/child/attachment",
      headers={
        "Cookie": cookie_header,
        "X-Atlassian-Token": "no-check",
      },
      files=files,
    )
  response.raise_for_status()
  return response.json()
