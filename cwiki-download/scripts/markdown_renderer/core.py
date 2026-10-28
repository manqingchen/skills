import re
from pathlib import Path
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup
from markdownify import markdownify as md

from ..macro_transformer import MacroAsset, transform_macros
from .html_prepare import _replace_confluence_images, _replace_page_links, _rewrite_links
from .tables import _extract_complex_tables, _extract_complex_tables_for_llm


def _prepare_storage_html(
  *,
  storage_html: str,
  attachment_path_map: Dict[str, str],
  macro_asset_prefix: str,
  page_id: str | None = None,
  complex_macro_preview_paths: Dict[str, List[str]] | None = None,
  macro_link_map: Dict[str, Dict[str, str]] | None = None,
  page_link_map: Dict[str, str] | None = None,
) -> Tuple[str, Dict[str, str], List[MacroAsset]]:
  transformed = _replace_page_links(storage_html, page_link_map)
  transformed, markdown_blocks, macro_assets = transform_macros(
    transformed,
    macro_asset_prefix,
    attachment_path_map=attachment_path_map,
    complex_macro_preview_paths=complex_macro_preview_paths,
    macro_link_map=macro_link_map,
  )
  transformed = _replace_confluence_images(transformed, attachment_path_map, page_id=page_id)
  transformed = _rewrite_links(transformed, attachment_path_map)
  transformed = _move_heading_images_to_following_blocks(transformed)
  return transformed, markdown_blocks, macro_assets


def _move_heading_images_to_following_blocks(html_text: str) -> str:
  soup = BeautifulSoup(f"<body>{html_text}</body>", "lxml")
  body = soup.body
  if body is None:
    return html_text

  for heading in body.find_all(re.compile(r"^h[1-6]$")):
    images = list(heading.find_all("img"))
    if not images:
      continue

    insert_after = heading
    for image in images:
      image.extract()
      paragraph = soup.new_tag("p")
      paragraph.append(image)
      insert_after.insert_after(paragraph)
      insert_after = paragraph

  return "".join(str(child) for child in body.contents)


def _append_attachment_paths_for_plain_names(markdown: str, attachment_path_map: Dict[str, str]) -> str:
  if not attachment_path_map:
    return markdown

  image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
  filename_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

  rendered = markdown
  for filename, local_path in sorted(attachment_path_map.items(), key=lambda item: len(item[0]), reverse=True):
    if not filename or not local_path:
      continue

    for token in {filename, filename.replace("_", r"\_")}:
      pattern = re.compile(re.escape(token))

      def replace_plain_name(match: re.Match[str]) -> str:
        source = match.string
        start, end = match.span()
        before = source[max(0, start - 2):start]
        after = source[end:end + 2]
        if start > 0 and source[start - 1] in filename_chars:
          return match.group(0)
        if end < len(source) and source[end] in filename_chars:
          return match.group(0)
        if start > 0 and source[start - 1] == "/":
          return match.group(0)
        if after == "](" and before in {"[", "!["}:
          return match.group(0)
        ext = Path(filename).suffix.lower()
        if ext in image_exts:
          return f"![{filename}]({local_path})"
        suffix = f" ({local_path})"
        if source[end:end + len(suffix)] == suffix:
          return match.group(0)
        return f"{match.group(0)}{suffix}"

      rendered = pattern.sub(replace_plain_name, rendered)

  return rendered


def render_storage_to_markdown(
  *,
  storage_html: str,
  attachment_path_map: Dict[str, str],
  macro_asset_prefix: str,
  page_id: str | None = None,
  complex_macro_preview_paths: Dict[str, List[str]] | None = None,
  macro_link_map: Dict[str, Dict[str, str]] | None = None,
  page_link_map: Dict[str, str] | None = None,
) -> Tuple[str, List[MacroAsset]]:
  transformed, markdown_blocks, macro_assets = _prepare_storage_html(
    storage_html=storage_html,
    attachment_path_map=attachment_path_map,
    macro_asset_prefix=macro_asset_prefix,
    page_id=page_id,
    complex_macro_preview_paths=complex_macro_preview_paths,
    macro_link_map=macro_link_map,
    page_link_map=page_link_map,
  )
  transformed, table_blocks = _extract_complex_tables(transformed)

  markdown = md(
    transformed,
    heading_style="ATX",
    table_infer_header=True,
    keep_inline_images_in=["td", "th"],
  ).strip()

  for token, block in markdown_blocks.items():
    markdown = markdown.replace(token, block)
  for token, block in table_blocks.items():
    markdown = markdown.replace(token, block)

  markdown = _append_attachment_paths_for_plain_names(markdown, attachment_path_map)
  markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
  return markdown, macro_assets


def render_storage_to_llm_markdown(
  *,
  storage_html: str,
  attachment_path_map: Dict[str, str],
  macro_asset_prefix: str,
  page_id: str | None = None,
  complex_macro_preview_paths: Dict[str, List[str]] | None = None,
  macro_link_map: Dict[str, Dict[str, str]] | None = None,
  page_link_map: Dict[str, str] | None = None,
) -> Tuple[str, List[MacroAsset]]:
  transformed, markdown_blocks, macro_assets = _prepare_storage_html(
    storage_html=storage_html,
    attachment_path_map=attachment_path_map,
    macro_asset_prefix=macro_asset_prefix,
    page_id=page_id,
    complex_macro_preview_paths=complex_macro_preview_paths,
    macro_link_map=macro_link_map,
    page_link_map=page_link_map,
  )
  transformed, table_blocks = _extract_complex_tables_for_llm(
    transformed,
    attachment_path_map=attachment_path_map,
  )

  markdown = md(
    transformed,
    heading_style="ATX",
    table_infer_header=True,
    keep_inline_images_in=["td", "th"],
  ).strip()

  for token, block in markdown_blocks.items():
    markdown = markdown.replace(token, block)
  for token, block in table_blocks.items():
    markdown = markdown.replace(token, block)

  markdown = _append_attachment_paths_for_plain_names(markdown, attachment_path_map)
  markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
  return markdown, macro_assets
