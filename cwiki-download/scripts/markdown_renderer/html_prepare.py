import html
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup


def _replace_confluence_images(storage_html: str, attachment_path_map: dict[str, str], page_id: str | None) -> str:
  def replace_image(match: re.Match[str]) -> str:
    inner = match.group(1) or ""

    filename_match = re.search(r'ri:filename="([^"]+)"', inner, flags=re.IGNORECASE)
    if filename_match:
      filename = unquote(filename_match.group(1))
      local_path = attachment_path_map.get(filename)
      src = local_path or (f"assets/{page_id}/{quote(filename)}" if page_id else quote(filename))
      return f'<img src="{src}" alt="{filename}" />'

    url_match = re.search(r'ri:value="([^"]+)"', inner, flags=re.IGNORECASE)
    if url_match:
      return f'<img src="{url_match.group(1)}" alt="" />'

    if page_id:
      return f'<img src="/download/attachments/{page_id}" alt="" />'
    return ""

  return re.sub(r"<ac:image[^>]*>([\s\S]*?)</ac:image>", replace_image, storage_html, flags=re.IGNORECASE)


def _replace_page_links(storage_html: str, page_link_map: dict[str, str] | None) -> str:
  if not page_link_map:
    return storage_html

  def replace_link(match: re.Match[str]) -> str:
    inner = match.group(1) or ""
    title_match = re.search(r'ri:content-title="([^"]+)"', inner, flags=re.IGNORECASE)
    if not title_match:
      return ""
    title = unquote(title_match.group(1))
    href = page_link_map.get(title)
    if not href:
      return html.escape(title)
    return f'<a href="{href}">{html.escape(title)}</a>'

  return re.sub(
    r"<ac:link[^>]*>([\s\S]*?)</ac:link>",
    replace_link,
    storage_html,
    flags=re.IGNORECASE,
  )


def _rewrite_links(html_text: str, attachment_path_map: dict[str, str]) -> str:
  soup = BeautifulSoup(f"<body>{html_text}</body>", "lxml")

  for tag in soup.find_all(["a", "img"]):
    attr = "href" if tag.name == "a" else "src"
    value = tag.get(attr)
    if not value:
      continue
    path = unquote(urlsplit(value).path)
    basename = Path(path).name
    local_path = attachment_path_map.get(basename)
    if local_path:
      tag[attr] = local_path

  return soup.body.decode_contents() if soup.body else html_text


def _normalize_text(value: str) -> str:
  cleaned = value.replace("\xa0", " ")
  cleaned = re.sub(r"\s+", " ", cleaned)
  return cleaned.strip()

