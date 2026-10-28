import base64
import hashlib
import re
import textwrap

from markdown_it import MarkdownIt

from .common import LocalReference, MermaidBlock, escape_html
from .prettify_storage import describe_prettify_scheme, prettify_storage_html
from .structured_tables import extract_table_html_placeholders, preprocess_llm_structured_tables


_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", flags=re.MULTILINE)

_MACRO_TONES = {
  "info": {
    "borderColor": "#99D7B2",
    "bgColor": "#E5F5EC",
    "titleColor": "#FFFFFF",
    "titleBGColor": "#66C58C",
  },
  "success": {
    "borderColor": "#99D7B2",
    "bgColor": "#E5F5EC",
    "titleColor": "#FFFFFF",
    "titleBGColor": "#66C58C",
  },
  "warning": {
    "borderColor": "#FFB432",
    "bgColor": "#FFF7E0",
    "titleColor": "#FFFFFF",
    "titleBGColor": "#FFCB7A",
  },
  "danger": {
    "borderColor": "#FF5461",
    "bgColor": "#FFECEF",
    "titleColor": "#FFFFFF",
    "titleBGColor": "#FF98A0",
  },
  "neutral": {
    "borderColor": "#D8DDE6",
    "bgColor": "#F5F7FA",
    "titleColor": "#303741",
    "titleBGColor": "#EFF2F6",
  },
}


def _remove_fenced_blocks(markdown_text: str) -> str:
  return re.sub(r"```[\s\S]*?```", "", markdown_text)


def _strip_markdown_inline(text: str) -> str:
  cleaned = re.sub(r"`([^`]+)`", r"\1", text)
  cleaned = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", cleaned)
  cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
  cleaned = cleaned.replace("*", "").replace("_", "").replace("~", "")
  return cleaned.strip()


def _extract_headings(markdown_text: str) -> list[tuple[int, str]]:
  text_without_code = _remove_fenced_blocks(markdown_text)
  headings: list[tuple[int, str]] = []
  for match in _HEADING_PATTERN.finditer(text_without_code):
    level = len(match.group(1))
    title = _strip_markdown_inline(match.group(2))
    if title:
      headings.append((level, title))
  return headings


def _build_toc_macro(headings: list[tuple[int, str]]) -> str:
  levels = [level for level, _title in headings if level >= 2]
  if len(levels) < 2:
    return ""

  min_level = max(2, min(levels))
  max_level = min(6, max(levels))
  return (
    '<ac:structured-macro ac:name="toc">'
    f'<ac:parameter ac:name="minLevel">{min_level}</ac:parameter>'
    f'<ac:parameter ac:name="maxLevel">{max_level}</ac:parameter>'
    '<ac:parameter ac:name="style">none</ac:parameter>'
    "</ac:structured-macro>"
  )


def _insert_toc_macro(html: str, headings: list[tuple[int, str]]) -> str:
  if 'ac:name="toc"' in html:
    return html

  toc_macro = _build_toc_macro(headings)
  if not toc_macro:
    return html

  first_h1 = re.search(r"<h1[^>]*>[\s\S]*?</h1>", html, flags=re.IGNORECASE)
  if first_h1:
    return html[: first_h1.end()] + "\n" + toc_macro + "\n" + html[first_h1.end():]
  return toc_macro + "\n" + html


def _preprocess_mermaid_fences(markdown_text: str) -> str:
  def repl(match: re.Match[str]) -> str:
    body = match.group(1).strip("\n")
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    filename = f"mermaid-{digest}"
    macro = (
      '<ac:structured-macro ac:name="mermaid-cloud">'
      '<ac:parameter ac:name="toolbar">bottom</ac:parameter>'
      f'<ac:parameter ac:name="filename">{filename}</ac:parameter>'
      '<ac:parameter ac:name="format">svg</ac:parameter>'
      '<ac:parameter ac:name="zoom">fit</ac:parameter>'
      '<ac:parameter ac:name="revision">1</ac:parameter>'
      "</ac:structured-macro>"
    )
    encoded = base64.b64encode(macro.encode("utf-8")).decode("ascii")
    return f"CWIKI_MERMAID_EMBED:{encoded}"

  return re.sub(r"```mermaid\s*\n([\s\S]*?)\n```", repl, markdown_text, flags=re.IGNORECASE)


def _extract_mermaid_html_placeholders(html: str) -> str:
  def repl_token(match: re.Match[str]) -> str:
    encoded = match.group(1)
    try:
      return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
      return match.group(0)

  replaced = re.sub(
    r"<p>\s*(CWIKI_MERMAID_EMBED:[A-Za-z0-9+/=]+)\s*</p>",
    r"\1",
    html,
    flags=re.IGNORECASE,
  )
  return re.sub(r"CWIKI_MERMAID_EMBED:([A-Za-z0-9+/=]+)", repl_token, replaced)


def _to_cdata_text(text: str) -> str:
  return text.replace("]]>", "]]]]><![CDATA[>")


def _parse_attrs(raw_attrs: str) -> dict[str, str]:
  attrs: dict[str, str] = {}
  pattern = re.compile(r"([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s}]+))")
  for key, double_quoted, single_quoted, bare in pattern.findall(raw_attrs):
    attrs[key.lower()] = (double_quoted or single_quoted or bare).strip()
  return attrs


def _split_title_attrs(raw_title: str) -> tuple[str, dict[str, str]]:
  title = raw_title.strip()
  attrs: dict[str, str] = {}
  match = re.search(r"\s*\{([^}]*)\}\s*$", title)
  if match:
    attrs = _parse_attrs(match.group(1))
    title = title[: match.start()].strip()
  return title, attrs


def _parse_code_fence_info(raw_info: str) -> tuple[str, dict[str, str]]:
  info = raw_info.strip()
  if not info:
    return "text", {}
  if info.startswith("{"):
    return "text", _parse_attrs(info.strip("{}"))
  first, _, rest = info.partition(" ")
  language = first.strip().lower() or "text"
  attrs = _parse_attrs(rest.strip().strip("{}")) if rest.strip() else {}
  return language, attrs


def _preprocess_code_fences(markdown_text: str) -> str:
  def repl(match: re.Match[str]) -> str:
    language, attrs = _parse_code_fence_info(match.group(1) or "")
    title = attrs.get("title", "").strip()
    code = match.group(2).strip("\n")
    title_param = f'<ac:parameter ac:name="title">{escape_html(title)}</ac:parameter>' if title else ""
    macro = (
      '<ac:structured-macro ac:name="code">'
      f'<ac:parameter ac:name="language">{escape_html(language)}</ac:parameter>'
      f"{title_param}"
      "<ac:plain-text-body><![CDATA[\n"
      f"{_to_cdata_text(code)}\n"
      "]]></ac:plain-text-body>"
      "</ac:structured-macro>"
    )
    encoded = base64.b64encode(macro.encode("utf-8")).decode("ascii")
    return f"CWIKI_CODE_EMBED:{encoded}"

  pattern = re.compile(r"```([^\n`]*)\n([\s\S]*?)\n```")
  return re.sub(pattern, repl, markdown_text)


def _extract_code_html_placeholders(html: str) -> str:
  def repl_token(match: re.Match[str]) -> str:
    encoded = match.group(1)
    try:
      return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
      return match.group(0)

  replaced = re.sub(
    r"<p>\s*(CWIKI_CODE_EMBED:[A-Za-z0-9+/=]+)\s*</p>",
    r"\1",
    html,
    flags=re.IGNORECASE,
  )
  return re.sub(r"CWIKI_CODE_EMBED:([A-Za-z0-9+/=]+)", repl_token, replaced)


_SUPPORTED_ADMONITION_MACROS = {
  "info": "info",
  "note": "note",
  "warning": "warning",
  "warn": "warning",
  "tip": "tip",
  "panel": "panel",
}

_SUPPORTED_BLOCK_MACROS = {
  **_SUPPORTED_ADMONITION_MACROS,
  "expand": "expand",
}

_STATUS_COLOR_ALIASES = {
  "green": "Green",
  "success": "Green",
  "done": "Green",
  "complete": "Green",
  "yellow": "Yellow",
  "warning": "Yellow",
  "warn": "Yellow",
  "orange": "Yellow",
  "red": "Red",
  "danger": "Red",
  "error": "Red",
  "blue": "Blue",
  "info": "Blue",
  "grey": "Grey",
  "gray": "Grey",
  "default": "Grey",
}


def _encode_macro_placeholder(macro: str) -> str:
  encoded = base64.b64encode(macro.encode("utf-8")).decode("ascii")
  return f"CWIKI_MACRO_EMBED:{encoded}"


def _decode_macro_placeholders(html: str) -> str:
  def repl_token(match: re.Match[str]) -> str:
    encoded = match.group(1)
    try:
      return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
      return match.group(0)

  replaced = re.sub(
    r"<p>\s*(CWIKI_MACRO_EMBED:[A-Za-z0-9+/=]+)\s*</p>",
    r"\1",
    html,
    flags=re.IGNORECASE,
  )
  return re.sub(r"CWIKI_MACRO_EMBED:([A-Za-z0-9+/=]+)", repl_token, replaced)


def _render_macro_body(markdown_text: str) -> str:
  prepared = _preprocess_confluence_macro_blocks(markdown_text.strip("\n"))
  html = _render_markdown_to_html(prepared)
  html = _extract_mermaid_html_placeholders(html)
  html = _extract_code_html_placeholders(html)
  html = _decode_macro_placeholders(html)
  html = extract_table_html_placeholders(html)
  return _flatten_lists_for_storage_xhtml(html).strip()


def _build_rich_text_macro(name: str, body_html: str, title: str = "", attrs: dict[str, str] | None = None) -> str:
  title_param = f'<ac:parameter ac:name="title">{escape_html(title)}</ac:parameter>' if title else ""
  extra_params = ""
  if name == "panel":
    tone = (attrs or {}).get("tone", "neutral").strip().lower()
    tone_params = _MACRO_TONES.get(tone, _MACRO_TONES["neutral"])
    extra_params = "".join(
      f'<ac:parameter ac:name="{key}">{value}</ac:parameter>'
      for key, value in tone_params.items()
    )
  return (
    f'<ac:structured-macro ac:name="{name}">'
    f"{title_param}"
    f"{extra_params}"
    "<ac:rich-text-body>"
    f"{body_html}"
    "</ac:rich-text-body>"
    "</ac:structured-macro>"
  )


def _build_column_macro(body_html: str, width: str = "") -> str:
  width_param = f'<ac:parameter ac:name="width">{escape_html(width)}</ac:parameter>' if width else ""
  return (
    '<ac:structured-macro ac:name="column">'
    f"{width_param}"
    "<ac:rich-text-body>"
    f"{body_html}"
    "</ac:rich-text-body>"
    "</ac:structured-macro>"
  )


def _build_section_macro(columns_html: str) -> str:
  return (
    '<ac:structured-macro ac:name="section">'
    "<ac:rich-text-body>"
    f"{columns_html}"
    "</ac:rich-text-body>"
    "</ac:structured-macro>"
  )


def _preprocess_columns_blocks(markdown_text: str) -> str:
  lines = markdown_text.splitlines(keepends=True)
  output: list[str] = []
  index = 0

  while index < len(lines):
    if not re.match(r"^:::columns[ \t]*$", lines[index].strip()):
      output.append(lines[index])
      index += 1
      continue

    index += 1
    columns: list[tuple[str, str]] = []
    valid = True
    closed = False
    while index < len(lines):
      stripped = lines[index].strip()
      if stripped == ":::":
        closed = True
        index += 1
        break

      column_match = re.match(r"^:::column(?:[ \t]+([^{}\n]+))?(?:[ \t]+\{([^}]*)\})?[ \t]*$", stripped)
      if not column_match:
        valid = False
        break

      width = (column_match.group(1) or "").strip()
      attrs = _parse_attrs(column_match.group(2) or "")
      if not width:
        width = attrs.get("width", "")
      index += 1
      body_lines: list[str] = []
      nested_depth = 0
      while index < len(lines):
        inner_stripped = lines[index].strip()
        if inner_stripped == ":::":
          if nested_depth == 0:
            break
          nested_depth -= 1
          body_lines.append(lines[index])
          index += 1
          continue
        if re.match(r"^:::(?!column\b|columns\b)[a-zA-Z][a-zA-Z0-9_-]*(?:[ \t].*)?$", inner_stripped):
          nested_depth += 1
        body_lines.append(lines[index])
        index += 1
      if index >= len(lines):
        valid = False
        break
      index += 1
      columns.append((width, "".join(body_lines)))

    if not valid or not closed or not columns:
      output.append(":::columns\n")
      for width, body in columns:
        output.append(f":::column {width}\n")
        output.append(body)
        output.append(":::\n")
      continue

    columns_html = "".join(
      _build_column_macro(_render_macro_body(body), width)
      for width, body in columns
    )
    output.append(_encode_macro_placeholder(_build_section_macro(columns_html)))
    output.append("\n")

  return "".join(output)


def _preprocess_confluence_macro_blocks(markdown_text: str) -> str:
  pattern = re.compile(
    r"^:::(?P<name>[a-zA-Z][a-zA-Z0-9_-]*)(?:[ \t]+(?P<title>[^\n]+))?[ \t]*\n"
    r"(?P<body>[\s\S]*?)"
    r"^:::[ \t]*$",
    flags=re.MULTILINE,
  )

  def repl(match: re.Match[str]) -> str:
    raw_name = match.group("name").strip().lower()
    macro_name = _SUPPORTED_BLOCK_MACROS.get(raw_name)
    if not macro_name:
      return match.group(0)
    title, attrs = _split_title_attrs(match.group("title") or "")
    body = match.group("body")
    body_html = _render_macro_body(body)
    macro = _build_rich_text_macro(macro_name, body_html, title, attrs)
    return _encode_macro_placeholder(macro)

  return pattern.sub(repl, _preprocess_columns_blocks(markdown_text))


def _normalize_status_colour(raw_value: str) -> str:
  return _STATUS_COLOR_ALIASES.get(raw_value.strip().lower(), "Grey")


def _preprocess_status_inline(markdown_text: str) -> str:
  pattern = re.compile(r":status\[([^\]]+)\](?:\{([^}]*)\})?")

  def repl(match: re.Match[str]) -> str:
    title = match.group(1).strip()
    attrs = _parse_attrs(match.group(2) or "")
    colour = _normalize_status_colour(attrs.get("colour") or attrs.get("color") or attrs.get("tone") or "Grey")
    subtle = attrs.get("subtle", "true").lower()
    macro = (
      '<ac:structured-macro ac:name="status">'
      f'<ac:parameter ac:name="title">{escape_html(title)}</ac:parameter>'
      f'<ac:parameter ac:name="colour">{colour}</ac:parameter>'
      f'<ac:parameter ac:name="subtle">{"true" if subtle != "false" else "false"}</ac:parameter>'
      "</ac:structured-macro>"
    )
    return _encode_macro_placeholder(macro)

  return pattern.sub(repl, markdown_text)


def extract_mermaid_blocks(markdown_text: str) -> list[MermaidBlock]:
  blocks: list[MermaidBlock] = []
  for match in re.finditer(r"```mermaid\s*\n([\s\S]*?)\n```", markdown_text, flags=re.IGNORECASE):
    source = match.group(1).strip("\n")
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    blocks.append(MermaidBlock(filename=f"mermaid-{digest}", source=source))
  return blocks


def _render_markdown_to_html(markdown_text: str) -> str:
  md = MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": False})
  md.enable("table")
  md.enable("strikethrough")
  return md.render(markdown_text)


def _rewrite_local_refs_in_html(html: str, references: list[LocalReference]) -> str:
  processed = html
  for item in references:
    escaped_path = re.escape(item.raw_path)
    if item.kind == "image":
      processed = re.sub(
        rf"<img([^>]*?)\s+src=[\"']{escaped_path}[\"']([^>]*?)>",
        f"<ac:image><ri:attachment ri:filename=\"{item.file_name}\" /></ac:image>",
        processed,
        flags=re.IGNORECASE,
      )
    else:
      processed = re.sub(
        rf"<a([^>]*?)\s+href=[\"']{escaped_path}[\"']([^>]*?)>(.*?)</a>",
        f"<ac:link><ri:attachment ri:filename=\"{item.file_name}\" />"
        f"<ac:plain-text-link-body><![CDATA[\\3]]></ac:plain-text-link-body></ac:link>",
        processed,
        flags=re.IGNORECASE | re.DOTALL,
      )
  return processed


def _filter_storage_html_minimal(html: str) -> str:
  cleaned = re.sub(r"<\s*script[^>]*>.*?<\s*/\s*script\s*>", "", html, flags=re.IGNORECASE | re.DOTALL)
  cleaned = re.sub(r"<\s*style[^>]*>.*?<\s*/\s*style\s*>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
  return cleaned.strip()


def _flatten_lists_for_storage_xhtml(html: str) -> str:
  flattened = re.sub(r"</?(ul|ol)(\s[^>]*)?>", "", html, flags=re.IGNORECASE)
  flattened = re.sub(r"<li(\s[^>]*)?>", "<div>", flattened, flags=re.IGNORECASE)
  flattened = re.sub(r"</li>", "</div>", flattened, flags=re.IGNORECASE)
  open_count = len(re.findall(r"<div>", flattened))
  close_count = len(re.findall(r"</div>", flattened))
  if open_count > close_count:
    flattened = flattened + ("\n" + "</div>") * (open_count - close_count)
  return flattened


def build_storage_body(
  markdown_text: str,
  references: list[LocalReference],
  *,
  include_toc: bool = True,
  prettify: bool = False,
  prettify_plan: dict[str, object] | None = None,
) -> str:
  normalized = textwrap.dedent(markdown_text).lstrip("\n")
  headings = _extract_headings(normalized)
  normalized = _preprocess_mermaid_fences(normalized)
  normalized = _preprocess_code_fences(normalized)
  normalized = _preprocess_status_inline(normalized)
  normalized = _preprocess_confluence_macro_blocks(normalized)
  preprocessed = preprocess_llm_structured_tables(normalized)
  html = _render_markdown_to_html(preprocessed)
  html = _extract_mermaid_html_placeholders(html)
  html = _extract_code_html_placeholders(html)
  html = _decode_macro_placeholders(html)
  html = extract_table_html_placeholders(html)
  html = _flatten_lists_for_storage_xhtml(html)
  storage = _rewrite_local_refs_in_html(html, references)
  if include_toc:
    storage = _insert_toc_macro(storage, headings)
  if prettify:
    storage = prettify_storage_html(storage, prettify_plan)
  return _filter_storage_html_minimal(storage)


def build_comment_storage_body(comment_text: str) -> str:
  return build_storage_body(comment_text, [], include_toc=False)


__all__ = [
  "build_comment_storage_body",
  "build_storage_body",
  "describe_prettify_scheme",
  "extract_mermaid_blocks",
]
