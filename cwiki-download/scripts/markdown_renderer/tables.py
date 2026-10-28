import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from .html_prepare import _normalize_text


def _is_complex_table(table) -> bool:  # type: ignore[no-untyped-def]
  if table.find("table"):
    return True

  for cell in table.find_all(["td", "th"]):
    colspan = str(cell.get("colspan") or "").strip()
    rowspan = str(cell.get("rowspan") or "").strip()
    if colspan and colspan != "1":
      return True
    if rowspan and rowspan != "1":
      return True

  return False


def _extract_complex_tables(html_text: str) -> tuple[str, dict[str, str]]:
  soup = BeautifulSoup(f"<body>{html_text}</body>", "lxml")
  table_blocks: dict[str, str] = {}
  table_index = 0

  for table in soup.find_all("table"):
    if table.find_parent("table"):
      continue
    if not _is_complex_table(table):
      continue

    table_index += 1
    token = f"CWIKITABLEBLOCK{table_index}TOKEN"
    table_blocks[token] = f"\n{str(table)}\n"
    table.replace_with(token)

  return (soup.body.decode_contents() if soup.body else html_text), table_blocks


def _extract_cell_text_without_nested_tables(cell) -> str:  # type: ignore[no-untyped-def]
  cell_soup = BeautifulSoup(str(cell), "lxml")
  normalized_cell = cell_soup.find(["td", "th"])
  if normalized_cell is None:
    return ""
  for nested_table in normalized_cell.find_all("table"):
    nested_table.extract()
  return _normalize_text(normalized_cell.get_text(" ", strip=True))


def _extract_cell_attachment_refs_without_nested_tables(
  cell,
  attachment_path_map: dict[str, str] | None = None,
) -> list[str]:  # type: ignore[no-untyped-def]
  cell_soup = BeautifulSoup(str(cell), "lxml")
  normalized_cell = cell_soup.find(["td", "th"])
  if normalized_cell is None:
    return []

  for nested_table in normalized_cell.find_all("table"):
    nested_table.extract()

  refs: set[str] = set()
  path_map = attachment_path_map or {}

  cell_html = str(normalized_cell)
  for filename in re.findall(r'ri:filename="([^"]+)"', cell_html, flags=re.IGNORECASE):
    decoded = unquote(filename).strip()
    if decoded:
      refs.add(path_map.get(decoded, decoded))

  for tag in normalized_cell.find_all(["a", "img"]):
    value = (tag.get("href") or tag.get("src") or "").strip()
    if not value:
      continue
    path = unquote(urlsplit(value).path)
    if "/download/attachments/" not in path and "/assets/" not in path and not path.startswith("assets/"):
      continue
    normalized_path = path.lstrip("/")
    if normalized_path.startswith("assets/"):
      refs.add(normalized_path)
      continue
    filename = Path(path).name.strip()
    if filename:
      refs.add(path_map.get(filename, filename))

  return sorted(refs)


def _build_cell_span_suffix(cell) -> str:  # type: ignore[no-untyped-def]
  span_parts: list[str] = []
  colspan = str(cell.get("colspan") or "").strip()
  rowspan = str(cell.get("rowspan") or "").strip()
  if colspan and colspan != "1":
    span_parts.append(f"colspan={colspan}")
  if rowspan and rowspan != "1":
    span_parts.append(f"rowspan={rowspan}")
  return f" [{' '.join(span_parts)}]" if span_parts else ""


def _find_nested_tables_for_cell(cell) -> list:  # type: ignore[no-untyped-def]
  nested_tables: list = []
  for nested_table in cell.find_all("table"):
    nearest_cell = nested_table.find_parent(["td", "th"])
    if nearest_cell is cell:
      nested_tables.append(nested_table)
  return nested_tables


def _serialize_table_to_llm_lines(
  table,
  table_index: int | None = None,
  indent: str = "",
  attachment_path_map: dict[str, str] | None = None,
) -> list[str]:  # type: ignore[no-untyped-def]
  lines: list[str] = []
  rows = [row for row in table.find_all("tr", recursive=False)]
  if not rows:
    body = table.find("tbody")
    rows = body.find_all("tr", recursive=False) if body else table.find_all("tr")

  header_cells: list = []
  data_rows = rows
  if rows and rows[0].find_all("th", recursive=False):
    header_cells = rows[0].find_all(["th", "td"], recursive=False)
    data_rows = rows[1:]

  headers = [
    f"{_extract_cell_text_without_nested_tables(cell) or f'列{index + 1}'}{_build_cell_span_suffix(cell)}"
    for index, cell in enumerate(header_cells)
  ]

  if table_index is not None:
    lines.append(f"{indent}## 结构化表格 {table_index}")
  else:
    lines.append(f"{indent}- 子表格:")
    indent = f"{indent}  "

  if headers:
    lines.append(f"{indent}- 表头: {' | '.join(headers)}")

  for row_index, row in enumerate(data_rows, start=1):
    cells = row.find_all(["td", "th"], recursive=False)
    if not cells:
      continue
    lines.append(f"{indent}- 行 {row_index}:")
    for cell_index, cell in enumerate(cells):
      header = headers[cell_index] if cell_index < len(headers) else f"列{cell_index + 1}"
      text = _extract_cell_text_without_nested_tables(cell)
      suffix = _build_cell_span_suffix(cell)
      attachment_refs = _extract_cell_attachment_refs_without_nested_tables(cell, attachment_path_map=attachment_path_map)
      nested_tables = _find_nested_tables_for_cell(cell)
      if nested_tables:
        lines.append(f"{indent}  - {header}{suffix}:")
        if text:
          lines.append(f"{indent}    - 文本: {text}")
        if attachment_refs:
          lines.append(f"{indent}    - 附件引用: {'；'.join(attachment_refs)}")
        for nested_table in nested_tables:
          lines.extend(
            _serialize_table_to_llm_lines(
              nested_table,
              indent=f"{indent}    ",
              attachment_path_map=attachment_path_map,
            )
          )
        continue

      lines.append(f"{indent}  - {header}: {text or '(空)'}{suffix}")
      if attachment_refs:
        lines.append(f"{indent}    - 附件引用: {'；'.join(attachment_refs)}")
      for nested_table in nested_tables:
        lines.extend(
          _serialize_table_to_llm_lines(
            nested_table,
            indent=f"{indent}    ",
            attachment_path_map=attachment_path_map,
          )
        )

  return lines


def _extract_complex_tables_for_llm(
  html_text: str,
  attachment_path_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
  soup = BeautifulSoup(f"<body>{html_text}</body>", "lxml")
  table_blocks: dict[str, str] = {}
  table_index = 0

  for table in soup.find_all("table"):
    if table.find_parent("table"):
      continue
    if not _is_complex_table(table):
      continue

    table_index += 1
    token = f"CWIKILLMTABLEBLOCK{table_index}TOKEN"
    table_blocks[token] = (
      "\n"
      + "\n".join(
        _serialize_table_to_llm_lines(
          table,
          table_index=table_index,
          attachment_path_map=attachment_path_map,
        )
      )
      + "\n"
    )
    table.replace_with(token)

  return (soup.body.decode_contents() if soup.body else html_text), table_blocks

