import base64
import re

from .common import escape_html
from .local_refs import _build_image_html_from_paths, _extract_local_paths_from_text


def _parse_structured_table_block(lines: list[str], start_idx: int) -> tuple[str, int] | None:
  header_line = lines[start_idx].strip()
  if not header_line.startswith("## 结构化表格"):
    return None
  i = start_idx + 1
  while i < len(lines) and lines[i].strip() == "":
    i += 1
  table_header: list[str] = []
  if i < len(lines) and "- 表头:" in lines[i]:
    header_payload = lines[i].split("表头:", 1)[1].strip()
    table_header = [cell.strip() for cell in header_payload.split("|") if cell.strip()]
    i += 1

  rows: list[dict[str, tuple[str, str]]] = []

  def parse_span(text: str) -> tuple[str, str]:
    clean = text.strip()
    attrs: list[str] = []
    # 支持类似 "[rowspan=2]" / "[colspan=8]"
    for kind, num in re.findall(r"\[(rowspan|colspan)=(\d+)\]", clean):
      attrs.append(f'{kind}="{num}"')
    clean = re.sub(r"\s*\[(rowspan|colspan)=\d+\]\s*", " ", clean).strip()
    if clean == "(空)":
      clean = ""
    return clean, " ".join(attrs)

  def parse_inline_subtable(block_lines: list[str]) -> str:
    sub_headers: list[str] | None = None
    sub_rows: list[dict[str, str]] = []
    j = 0
    while j < len(block_lines) and block_lines[j].strip() == "":
      j += 1
    if j < len(block_lines) and "表头:" in block_lines[j]:
      payload = block_lines[j].split("表头:", 1)[1].strip()
      sub_headers = [c.strip() for c in payload.split("|") if c.strip()]
      j += 1

    current_cells: dict[str, str] = {}
    last_key: str | None = None
    while j < len(block_lines):
      line = block_lines[j]
      stripped = line.strip()
      if stripped.startswith("- 行 "):
        if current_cells:
          sub_rows.append(current_cells)
          current_cells = {}
          last_key = None
        j += 1
        continue
      cell_match = re.match(r"^- ([^:]+):\s*(.*)$", stripped)
      if cell_match:
        key_clean, _ = parse_span(cell_match.group(1).strip())
        value_clean, _ = parse_span(cell_match.group(2).strip())
        inline_paths = _extract_local_paths_from_text(value_clean)
        if inline_paths:
          image_html = _build_image_html_from_paths(inline_paths)
          if image_html:
            if last_key and last_key in current_cells:
              base = current_cells.get(last_key, "")
              current_cells[last_key] = f"{base}<br/>{image_html}" if base else image_html
            else:
              current_cells[key_clean] = image_html
          j += 1
          continue
        current_cells[key_clean] = value_clean
        last_key = key_clean
      j += 1
    if current_cells:
      sub_rows.append(current_cells)

    if sub_headers is None and sub_rows:
      first_keys = list(sub_rows[0].keys())
      if all(re.match(r"^列\d+$", key) for key in first_keys):
        sub_headers = sorted(first_keys, key=lambda k: int(k[1:]))
      else:
        sub_headers = first_keys

    if not sub_headers:
      return ""
    th = "".join(f"<th>{escape_html(h)}</th>" for h in sub_headers)
    trs = []
    for row in sub_rows:
      cells: list[str] = []
      for header in sub_headers:
        value = row.get(header, "")
        if "<table" in value or "<ac:image" in value or "<ac:link" in value or "<br/>" in value:
          cells.append(f"<td>{value}</td>")
        else:
          cells.append(f"<td>{escape_html(value)}</td>")
      trs.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"

  def parse_cell_value(idx: int, base_indent: int) -> tuple[str, int]:
    parts: list[str] = []
    j = idx
    while j < len(lines):
      raw = lines[j]
      if raw.strip() == "":
        j += 1
        continue
      indent = len(raw) - len(raw.lstrip(" "))
      if indent <= base_indent:
        break
      stripped = raw.strip()
      if stripped.startswith("- 文本:"):
        parts.append(escape_html(stripped.split("文本:", 1)[1].strip()))
        j += 1
        continue
      kv_match = re.match(r"^- ([^:]+):\s*(.*)$", stripped)
      if kv_match:
        kv_value = kv_match.group(2).strip()
        inline_paths = _extract_local_paths_from_text(kv_value)
        if inline_paths:
          image_html = _build_image_html_from_paths(inline_paths)
          if image_html:
            parts.append(image_html)
          j += 1
          continue
      if stripped.startswith("- 子表格:"):
        j += 1
        sub_lines: list[str] = []
        while j < len(lines):
          raw2 = lines[j]
          if raw2.strip() == "":
            sub_lines.append(raw2)
            j += 1
            continue
          indent2 = len(raw2) - len(raw2.lstrip(" "))
          if indent2 <= indent:
            break
          sub_lines.append(raw2.strip())
          j += 1
        sub_html = parse_inline_subtable(sub_lines)
        if sub_html:
          parts.append(sub_html)
        continue
      parts.append(escape_html(stripped.lstrip("- ").strip()))
      j += 1
    return "<br/>".join(parts), j

  while i < len(lines):
    stripped = lines[i].strip()
    if stripped.startswith("## ") and i != start_idx:
      break
    if stripped.startswith("- 行 "):
      row_block_indent = len(lines[i]) - len(lines[i].lstrip(" "))
      i += 1
      row: dict[str, tuple[str, str]] = {}
      while i < len(lines):
        raw = lines[i]
        if raw.strip() == "":
          i += 1
          continue
        raw_indent = len(raw) - len(raw.lstrip(" "))
        if (raw.strip().startswith("- 行 ") and raw_indent <= row_block_indent) or raw.strip().startswith("## "):
          break
        indent = len(raw) - len(raw.lstrip(" "))
        matched = re.match(r"^- ([^:]+):\s*(.*)$", raw.strip())
        if not matched:
          i += 1
          continue
        key, key_attr = parse_span(matched.group(1).strip())
        value = matched.group(2).strip()
        value, value_attr = parse_span(value)
        merged_attr = " ".join(part for part in [key_attr, value_attr] if part)
        if value:
          row[key] = (value, merged_attr)
          i += 1
          continue
        cell_html, next_i = parse_cell_value(i + 1, indent)
        row[key] = (cell_html, merged_attr)
        i = next_i
      rows.append(row)
      continue
    i += 1

  if not table_header and rows:
    first_keys = list(rows[0].keys())
    if all(re.match(r"^列\d+$", key) for key in first_keys):
      table_header = sorted(first_keys, key=lambda k: int(k[1:]))
    else:
      table_header = first_keys
  if not table_header:
    return None

  thead = "".join(f"<th>{escape_html(h)}</th>" for h in table_header)
  tr_html: list[str] = []
  for row in rows:
    tds: list[str] = []
    for header in table_header:
      value, attrs = row.get(header, ("", ""))
      attr_chunk = f" {attrs}" if attrs else ""
      if "<table" in value or "<br/>" in value or "<ac:image" in value or "<ac:link" in value:
        tds.append(f"<td{attr_chunk}>{value}</td>")
      else:
        tds.append(f"<td{attr_chunk}>{escape_html(value)}</td>")
    tr_html.append(f"<tr>{''.join(tds)}</tr>")
  html = f"<table><thead><tr>{thead}</tr></thead><tbody>{''.join(tr_html)}</tbody></table>"
  return html, i


def preprocess_llm_structured_tables(markdown_text: str) -> str:
  lines = markdown_text.splitlines()
  out: list[str] = []
  i = 0
  while i < len(lines):
    parsed = _parse_structured_table_block(lines, i)
    if parsed is None:
      out.append(lines[i])
      i += 1
      continue
    table_html, next_i = parsed
    encoded = base64.b64encode(table_html.encode("utf-8")).decode("ascii")
    out.append(f"CWIKI_TABLE_EMBED:{encoded}")
    i = next_i
  return "\n".join(out)


def extract_table_html_placeholders(html: str) -> str:
  def repl_token(match: re.Match[str]) -> str:
    encoded = match.group(1)
    try:
      return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
      return match.group(0)

  replaced = re.sub(r"CWIKI_TABLE_EMBED:([A-Za-z0-9+/=]+)", repl_token, html)
  replaced = re.sub(r"<p>\s*(<table[\s\S]*?</table>)\s*</p>", r"\1", replaced, flags=re.IGNORECASE)
  return replaced


__all__ = [
  "extract_table_html_placeholders",
  "preprocess_llm_structured_tables",
]
