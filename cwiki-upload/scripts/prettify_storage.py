import re
import textwrap
from typing import Any

from .common import escape_html


_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", flags=re.MULTILINE)

_DESIGN_COLORS = {
  "brand": "#009B3F",
  "brand_hover": "#008436",
  "brand_pressed": "#00824B",
  "brand_disabled": "#59BEB2",
  "danger": "#FF5461",
  "warning": "#FFB432",
  "success": "#009B3F",
  "brand_bg_60": "#66C58C",
  "success_bg_60": "#66C58C",
  "warning_bg_60": "#FFCB7A",
  "danger_bg_60": "#FF98A0",
  "link": "#006633",
  "info": "#009B3F",
  "info_deep": "#006633",
  "plan": "#006633",
  "plan_deep": "#006633",
  "orange": "#FF4D18",
  "teal": "#00998A",
  "gold": "#99522E",
  "info_soft": "#E5F5EC",
  "success_soft": "#E5F5EC",
  "warning_soft": "#FFF7E0",
  "danger_soft": "#FFECEF",
  "plan_soft": "#E5F5EC",
  "orange_soft": "#FFEEE8",
  "text_primary": "#303741",
  "text_secondary": "#5F6670",
  "text_tertiary": "#8F96A0",
  "text_quaternary": "#BEC5CF",
  "border": "#D8DDE6",
  "bg_subtle": "#F5F7FA",
  "bg_warning": "#FFF7E0",
  "bg_danger": "#FFECEF",
  "tag_green": "#99D7B2",
  "tag_teal": "#99D6D0",
  "tag_gray": "#AFB2B7",
  "tag_orange": "#FFB8A3",
  "tag_gold": "#D6BAAB",
}

_BACKGROUND_60_COLORS = {
  _DESIGN_COLORS["brand"]: _DESIGN_COLORS["brand_bg_60"],
  _DESIGN_COLORS["info"]: _DESIGN_COLORS["brand_bg_60"],
  _DESIGN_COLORS["success"]: _DESIGN_COLORS["success_bg_60"],
  _DESIGN_COLORS["warning"]: _DESIGN_COLORS["warning_bg_60"],
  _DESIGN_COLORS["danger"]: _DESIGN_COLORS["danger_bg_60"],
  _DESIGN_COLORS["orange"]: "#FF9474",
}

_PRETTIFY_THEMES = {
  "info": {
    "name": "说明信息",
    "accentColor": _DESIGN_COLORS["info"],
    "titleColor": _DESIGN_COLORS["text_primary"],
    "emphasisColor": _DESIGN_COLORS["info_deep"],
    "bgColor": _DESIGN_COLORS["info_soft"],
    "borderColor": _DESIGN_COLORS["tag_green"],
    "panel": False,
  },
  "success": {
    "name": "结果结论",
    "accentColor": _DESIGN_COLORS["success"],
    "titleColor": _DESIGN_COLORS["text_primary"],
    "emphasisColor": _DESIGN_COLORS["success"],
    "bgColor": _DESIGN_COLORS["success_soft"],
    "borderColor": _DESIGN_COLORS["tag_green"],
    "panel": False,
  },
  "warning": {
    "name": "风险提示",
    "accentColor": _DESIGN_COLORS["warning"],
    "titleColor": _DESIGN_COLORS["text_primary"],
    "emphasisColor": "#B76E00",
    "bgColor": _DESIGN_COLORS["bg_warning"],
    "borderColor": _DESIGN_COLORS["warning"],
    "panel": True,
  },
  "danger": {
    "name": "问题风险",
    "accentColor": _DESIGN_COLORS["danger"],
    "titleColor": _DESIGN_COLORS["text_primary"],
    "emphasisColor": _DESIGN_COLORS["danger"],
    "bgColor": _DESIGN_COLORS["bg_danger"],
    "borderColor": _DESIGN_COLORS["danger"],
    "panel": True,
  },
  "plan": {
    "name": "计划推进",
    "accentColor": _DESIGN_COLORS["plan"],
    "titleColor": _DESIGN_COLORS["text_primary"],
    "emphasisColor": _DESIGN_COLORS["plan_deep"],
    "bgColor": _DESIGN_COLORS["plan_soft"],
    "borderColor": _DESIGN_COLORS["brand_disabled"],
    "panel": False,
  },
  "default": {
    "name": "正文区块",
    "accentColor": _DESIGN_COLORS["border"],
    "titleColor": _DESIGN_COLORS["text_primary"],
    "emphasisColor": _DESIGN_COLORS["text_secondary"],
    "panel": False,
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


def _normalize_theme_name(theme_name: object) -> str:
  if not isinstance(theme_name, str):
    return "default"
  normalized = theme_name.strip().lower()
  aliases = {
    "brand": "info",
    "primary": "info",
    "note": "info",
    "result": "success",
    "done": "success",
    "risk": "warning",
    "warn": "warning",
    "error": "danger",
    "problem": "danger",
    "todo": "plan",
  }
  normalized = aliases.get(normalized, normalized)
  return normalized if normalized in _PRETTIFY_THEMES else "default"


def _is_hex_color(value: object) -> bool:
  return isinstance(value, str) and bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", value.strip()))


def _background_color_for(color: object) -> str:
  if not _is_hex_color(color):
    return _DESIGN_COLORS["bg_subtle"]
  normalized = str(color).strip().upper()
  return _BACKGROUND_60_COLORS.get(normalized, str(color).strip())


def _is_css_size(value: object) -> bool:
  if isinstance(value, int | float):
    return value >= 0
  return isinstance(value, str) and bool(re.fullmatch(r"\d+(?:\.\d+)?(?:px|em|rem|%)", value.strip()))


def _format_css_size(value: object, fallback: str) -> str:
  if isinstance(value, int | float) and value >= 0:
    return f"{value:g}px"
  if _is_css_size(value):
    return str(value).strip()
  return fallback


def _copy_theme(theme: dict[str, object]) -> dict[str, object]:
  return dict(theme)


def _strip_html_tags(html: str) -> str:
  return re.sub(r"<[^>]+>", "", html).strip()


def _resolve_prettify_theme(title: str, body_html: str) -> dict[str, object]:
  title_text = title.lower()
  if re.search(r"问题|异常|失败|错误|阻塞|缺陷|不行|走不通|danger|error", title_text):
    return _PRETTIFY_THEMES["danger"]
  if re.search(r"风险|告警|注意|warning|risk", title_text):
    return _PRETTIFY_THEMES["warning"]
  if re.search(r"计划|排期|待办|后续|下一步|todo|roadmap|plan", title_text):
    return _PRETTIFY_THEMES["plan"]
  if re.search(r"结论|结果|收益|完成|验收|通过|成功|summary|result|done", title_text):
    return _PRETTIFY_THEMES["success"]
  if re.search(r"背景|目标|范围|概述|说明|信息|原则|起因|context|overview|goal", title_text):
    return _PRETTIFY_THEMES["info"]
  if "<table" in body_html.lower():
    return _PRETTIFY_THEMES["info"]
  return _PRETTIFY_THEMES["default"]


def _operation_targets_section(operation: dict[str, Any], title: str) -> bool:
  selector = operation.get("selector")
  if not isinstance(selector, dict):
    return False
  selector_type = str(selector.get("type") or "").strip().lower()
  if selector_type != "section":
    return False
  selector_title = str(selector.get("title") or "").strip()
  return selector_title == title.strip()


def _operation_targets_all_tables(operation: dict[str, Any]) -> bool:
  selector = operation.get("selector")
  if not isinstance(selector, dict):
    return False
  selector_type = str(selector.get("type") or "").strip().lower()
  selector_scope = str(selector.get("scope") or "").strip().lower()
  return selector_type == "tables" and selector_scope in {"all", "*", "global"}


def _plan_operations(prettify_plan: dict[str, Any] | None) -> list[dict[str, Any]]:
  if not prettify_plan:
    return []
  operations = prettify_plan.get("operations")
  if not isinstance(operations, list):
    return []
  return [item for item in operations if isinstance(item, dict)]


def _merge_list_values(base: list[str], values: object) -> list[str]:
  merged = list(base)
  if isinstance(values, list):
    for item in values:
      value = str(item).strip()
      if value and value not in merged:
        merged.append(value)
  return merged


def _tone_to_theme(tone: object) -> str:
  if not isinstance(tone, str):
    return "default"
  tone_name = tone.strip().lower()
  aliases = {
    "primary": "info",
    "info": "info",
    "note": "info",
    "success": "success",
    "result": "success",
    "warning": "warning",
    "warn": "warning",
    "danger": "danger",
    "error": "danger",
    "plan": "plan",
    "default": "default",
  }
  return aliases.get(tone_name, "default")


def _tag_style_from_tone(tone: object) -> dict[str, str]:
  theme_name = _tone_to_theme(tone)
  if theme_name == "success":
    return {"bg": _DESIGN_COLORS["info_soft"], "color": _DESIGN_COLORS["link"], "border": _DESIGN_COLORS["tag_green"], "radius": "4px"}
  if theme_name == "warning":
    return {"bg": _DESIGN_COLORS["warning_soft"], "color": "#8A5A00", "border": _DESIGN_COLORS["warning"], "radius": "4px"}
  if theme_name == "danger":
    return {"bg": _DESIGN_COLORS["danger_soft"], "color": _DESIGN_COLORS["danger"], "border": "#FFB8C0", "radius": "4px"}
  if theme_name == "plan":
    return {"bg": _DESIGN_COLORS["info_soft"], "color": _DESIGN_COLORS["link"], "border": _DESIGN_COLORS["brand_disabled"], "radius": "4px"}
  if theme_name == "info":
    return {"bg": _DESIGN_COLORS["bg_subtle"], "color": _DESIGN_COLORS["text_secondary"], "border": _DESIGN_COLORS["border"], "radius": "4px"}
  return {"bg": _DESIGN_COLORS["bg_subtle"], "color": _DESIGN_COLORS["text_secondary"], "border": _DESIGN_COLORS["border"], "radius": "4px"}


def _apply_tag_style_options(style: dict[str, str], options: dict[str, Any]) -> dict[str, str]:
  if _is_hex_color(options.get("bgColor")):
    style["bg"] = str(options["bgColor"]).strip()
  if _is_hex_color(options.get("color")):
    style["color"] = str(options["color"]).strip()
  if _is_hex_color(options.get("borderColor")):
    style["border"] = str(options["borderColor"]).strip()
  if "radius" in options or "borderRadius" in options:
    style["radius"] = _format_css_size(options.get("radius", options.get("borderRadius")), style.get("radius", "4px"))
  if "padding" in options:
    style["padding"] = str(options["padding"]).strip()[:32]
  if "fontSize" in options:
    style["fontSize"] = _format_css_size(options.get("fontSize"), style.get("fontSize", "12px"))
  return style


def _apply_operation_to_section_plan(section_plan: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
  action = str(operation.get("action") or "").strip().lower()
  options = operation.get("options")
  if not isinstance(options, dict):
    options = {}

  if action in {"callout", "panel"}:
    section_plan["treatment"] = "panel"
    if "tone" in options:
      section_plan["theme"] = _tone_to_theme(options.get("tone"))
    section_plan["emphasisKeywords"] = _merge_list_values(
      [str(item) for item in section_plan.get("emphasisKeywords", []) if str(item).strip()]
      if isinstance(section_plan.get("emphasisKeywords"), list)
      else [],
      options.get("emphasis"),
    )
    for key in ("bgColor", "borderColor", "accentColor", "titleColor", "emphasisColor"):
      if key in options:
        section_plan[key] = options[key]
    for key in ("panelScope", "maxPanelBlocks", "maxPanelChars"):
      if key in options:
        section_plan[key] = options[key]

  elif action in {"section_accent", "heading", "accent"}:
    section_plan["treatment"] = "heading"
    if "tone" in options:
      section_plan["theme"] = _tone_to_theme(options.get("tone"))
    for key in ("accentColor", "titleColor", "emphasisColor"):
      if key in options:
        section_plan[key] = options[key]

  elif action in {"table_style", "tables"}:
    if "tone" in options and "theme" not in section_plan:
      section_plan["theme"] = _tone_to_theme(options.get("tone"))
    if "headerBg" in options:
      section_plan["tableHeaderBg"] = options["headerBg"]
    if "headerColor" in options:
      section_plan["tableHeaderColor"] = options["headerColor"]
    if "tableHeaderBg" in options:
      section_plan["tableHeaderBg"] = options["tableHeaderBg"]
    if "tableHeaderColor" in options:
      section_plan["tableHeaderColor"] = options["tableHeaderColor"]

  elif action in {"two_column", "grid", "comparison"}:
    target = str(operation.get("target") or options.get("target") or "").strip().lower()
    if target in {"tables", "table", ""}:
      section_plan["layout"] = "paired-tables"
      header_colors = options.get("headerColors")
      if isinstance(header_colors, list):
        if len(header_colors) > 0:
          section_plan["leftHeaderBg"] = header_colors[0]
        if len(header_colors) > 1:
          section_plan["rightHeaderBg"] = header_colors[1]
      if "leftHeaderBg" in options:
        section_plan["leftHeaderBg"] = options["leftHeaderBg"]
      if "rightHeaderBg" in options:
        section_plan["rightHeaderBg"] = options["rightHeaderBg"]

  elif action in {"keyword_emphasis", "emphasis"}:
    section_plan["emphasisKeywords"] = _merge_list_values(
      [str(item) for item in section_plan.get("emphasisKeywords", []) if str(item).strip()]
      if isinstance(section_plan.get("emphasisKeywords"), list)
      else [],
      options.get("keywords") or options.get("emphasis"),
    )
    if "color" in options:
      section_plan["emphasisColor"] = options["color"]
    if "maxOccurrencesPerKeyword" in options:
      section_plan["maxKeywordOccurrences"] = options["maxOccurrencesPerKeyword"]

  elif action in {"tag", "tags", "badge", "badges", "tag_keywords"}:
    tag_items = section_plan.get("tags") if isinstance(section_plan.get("tags"), list) else []
    keywords = options.get("keywords") or options.get("tags") or options.get("items")
    if isinstance(keywords, list):
      style = _tag_style_from_tone(options.get("tone"))
      style = _apply_tag_style_options(style, options)
      for keyword in keywords:
        if isinstance(keyword, dict):
          text = str(keyword.get("text") or keyword.get("keyword") or "").strip()
          item_style = _tag_style_from_tone(keyword.get("tone")) if "tone" in keyword else dict(style)
          item_style = _apply_tag_style_options(item_style, options)
          item_style = _apply_tag_style_options(item_style, keyword)
        else:
          text = str(keyword).strip()
          item_style = dict(style)
        if text:
          tag_items.append({"text": text, **item_style})
    section_plan["tags"] = tag_items

  return section_plan


def _section_plan_from_operations(
  prettify_plan: dict[str, Any] | None,
  title: str,
) -> dict[str, Any] | None:
  operations = _plan_operations(prettify_plan)
  if not operations:
    return None

  section_plan = {"title": title}
  matched = False
  for operation in operations:
    if _operation_targets_all_tables(operation) or _operation_targets_section(operation, title):
      matched = True
      section_plan = _apply_operation_to_section_plan(section_plan, operation)
  return section_plan if matched else None


def _theme_from_plan_section(section: dict[str, Any], fallback: dict[str, object]) -> dict[str, object]:
  theme = _copy_theme(_PRETTIFY_THEMES[_normalize_theme_name(section.get("theme"))])
  if section.get("theme") is None:
    theme = _copy_theme(fallback)

  treatment = str(section.get("treatment") or "").strip().lower()
  if treatment in {"panel", "callout"}:
    theme["panel"] = True
  elif treatment in {"heading", "accent", "subtle", "none"}:
    theme["panel"] = False

  if _is_hex_color(section.get("accentColor")):
    theme["accentColor"] = str(section["accentColor"]).strip()
  if _is_hex_color(section.get("titleColor")):
    theme["titleColor"] = str(section["titleColor"]).strip()
  if _is_hex_color(section.get("emphasisColor")):
    theme["emphasisColor"] = str(section["emphasisColor"]).strip()
  if _is_hex_color(section.get("bgColor")):
    theme["bgColor"] = str(section["bgColor"]).strip()
  if _is_hex_color(section.get("borderColor")):
    theme["borderColor"] = str(section["borderColor"]).strip()
  if _is_hex_color(section.get("tableHeaderBg")):
    theme["tableHeaderBg"] = str(section["tableHeaderBg"]).strip()
  if _is_hex_color(section.get("tableHeaderColor")):
    theme["tableHeaderColor"] = str(section["tableHeaderColor"]).strip()

  if theme.get("panel") and "bgColor" not in theme:
    theme["bgColor"] = _DESIGN_COLORS["bg_subtle"]
  if theme.get("panel") and "borderColor" not in theme:
    theme["borderColor"] = theme["accentColor"]
  return theme


def _build_panel_macro(body_html: str, theme: dict[str, object]) -> str:
  return (
    '<ac:structured-macro ac:name="panel">'
    f'<ac:parameter ac:name="bgColor">{theme["bgColor"]}</ac:parameter>'
    f'<ac:parameter ac:name="borderColor">{theme["borderColor"]}</ac:parameter>'
    "<ac:rich-text-body>"
    f"{body_html.strip()}"
    "</ac:rich-text-body>"
    "</ac:structured-macro>"
  )


def _panel_scope(section_plan: dict[str, Any] | None) -> str:
  if not section_plan:
    return "smart"
  scope = str(section_plan.get("panelScope") or section_plan.get("scope") or "smart").strip().lower()
  if scope in {"full", "section", "all"}:
    return "full"
  if scope in {"lead", "summary", "excerpt", "smart"}:
    return scope
  return "smart"


def _positive_int(value: object, fallback: int) -> int:
  if isinstance(value, int) and value > 0:
    return value
  if isinstance(value, str) and value.isdigit() and int(value) > 0:
    return int(value)
  return fallback


def _split_panel_body(body_html: str, section_plan: dict[str, Any] | None) -> tuple[str, str]:
  scope = _panel_scope(section_plan)
  if scope == "full":
    return body_html, ""

  stripped = body_html.lstrip()
  leading_ws = body_html[: len(body_html) - len(stripped)]
  if not stripped:
    return "", body_html

  max_blocks = _positive_int(section_plan.get("maxPanelBlocks") if section_plan else None, 4)
  max_chars = _positive_int(section_plan.get("maxPanelChars") if section_plan else None, 520)
  block_pattern = re.compile(
    r"(?P<block>"
    r"<p[\s\S]*?</p>|"
    r"<div[\s\S]*?</div>|"
    r"<(?:ul|ol|blockquote)[\s\S]*?</(?:ul|ol|blockquote)>|"
    r"<table[\s\S]*?</table>|"
    r"<ac:structured-macro[\s\S]*?</ac:structured-macro>|"
    r"<h[3-6][^>]*>[\s\S]*?</h[3-6]>"
    r")",
    flags=re.IGNORECASE,
  )

  cursor = 0
  selected_end = 0
  selected_blocks = 0
  for match in block_pattern.finditer(stripped):
    if stripped[cursor:match.start()].strip():
      break
    block = match.group("block")
    lowered = block.lower()
    is_heavy_block = lowered.startswith("<table") or lowered.startswith("<ac:structured-macro") or re.match(r"<h[3-6]", lowered)
    if is_heavy_block:
      break
    next_end = match.end()
    if selected_blocks >= max_blocks:
      break
    if selected_blocks > 0 and next_end > max_chars:
      break
    selected_end = next_end
    selected_blocks += 1
    cursor = match.end()
    if scope in {"lead", "summary", "excerpt"} and selected_blocks >= max_blocks:
      break

  if selected_end <= 0:
    return "", body_html

  panel_html = stripped[:selected_end]
  rest_html = leading_ws + stripped[selected_end:]
  return panel_html, rest_html


def _style_heading(title_html: str, theme: dict[str, object]) -> str:
  return (
    "<h2>"
    f'<span style="color: {theme["titleColor"]}; border-left: 4px solid {theme["accentColor"]}; '
    'padding-left: 8px;">'
    f"{title_html}"
    "</span>"
    "</h2>"
  )


def _colorize_keywords_in_text_html(body_html: str, keywords: list[str], color: str, max_occurrences: int = 1) -> str:
  clean_keywords = [item.strip() for item in keywords if item.strip()]
  if not clean_keywords:
    return body_html

  parts = re.split(r"(<[^>]+>)", body_html)
  output: list[str] = []
  replacement_counts = {keyword: 0 for keyword in clean_keywords}
  in_plain_text_body = False
  for part in parts:
    if part.startswith("<"):
      lowered = part.lower()
      if "<ac:plain-text-body" in lowered:
        in_plain_text_body = True
      elif "</ac:plain-text-body" in lowered:
        in_plain_text_body = False
      output.append(part)
      continue

    if in_plain_text_body:
      output.append(part)
      continue

    processed = part
    for keyword in clean_keywords:
      escaped_keyword = escape_html(keyword)
      if not escaped_keyword:
        continue
      remaining = max_occurrences - replacement_counts[keyword] if max_occurrences > 0 else 0
      if remaining <= 0:
        continue
      replacement_counts[keyword] += processed.count(escaped_keyword) if remaining <= 0 else min(processed.count(escaped_keyword), remaining)
      processed = processed.replace(
        escaped_keyword,
        f'<span style="color: {color};">{escaped_keyword}</span>',
        remaining,
      )
    output.append(processed)
  return "".join(output)


def _tag_keywords_in_text_html(body_html: str, tags: list[dict[str, str]]) -> str:
  clean_tags = [item for item in tags if item.get("text")]
  if not clean_tags:
    return body_html

  parts = re.split(r"(<[^>]+>)", body_html)
  output: list[str] = []
  in_plain_text_body = False
  for part in parts:
    if part.startswith("<"):
      lowered = part.lower()
      if "<ac:plain-text-body" in lowered:
        in_plain_text_body = True
      elif "</ac:plain-text-body" in lowered:
        in_plain_text_body = False
      output.append(part)
      continue

    if in_plain_text_body:
      output.append(part)
      continue

    processed = part
    for tag in clean_tags:
      text = escape_html(tag["text"])
      if not text:
        continue
      style = (
        f"background-color: {tag.get('bg', _DESIGN_COLORS['bg_subtle'])}; "
        f"color: {tag.get('color', _DESIGN_COLORS['text_secondary'])}; "
        f"border: 1px solid {tag.get('border', _DESIGN_COLORS['border'])}; "
        f"border-radius: {tag.get('radius', '4px')}; "
        f"padding: {tag.get('padding', '2px 6px')}; "
        f"font-size: {tag.get('fontSize', '12px')}; white-space: nowrap;"
      )
      processed = processed.replace(text, f'<span style="{style}">{text}</span>')
    output.append(processed)
  return "".join(output)


def _prettify_inline_emphasis(
  body_html: str,
  theme: dict[str, object],
  section_plan: dict[str, Any] | None = None,
) -> str:
  emphasis_color = str(theme["emphasisColor"])
  processed = re.sub(
    r"<strong>([\s\S]*?)</strong>",
    f'<strong><span style="color: {emphasis_color};">\\1</span></strong>',
    body_html,
    flags=re.IGNORECASE,
  )
  if section_plan:
    keywords = section_plan.get("emphasisKeywords")
    if isinstance(keywords, list):
      processed = _colorize_keywords_in_text_html(
        processed,
        [str(item) for item in keywords],
        emphasis_color,
        _positive_int(section_plan.get("maxKeywordOccurrences"), 1),
      )
    tags = section_plan.get("tags")
    if isinstance(tags, list):
      processed = _tag_keywords_in_text_html(
        processed,
        [item for item in tags if isinstance(item, dict)],
      )
  return processed


def _resolve_table_style(theme: dict[str, object] | None = None) -> tuple[str, str]:
  if theme and _is_hex_color(theme.get("tableHeaderBg")):
    bg = _background_color_for(theme["tableHeaderBg"])
  elif theme and theme.get("accentColor") in {_DESIGN_COLORS["warning"], _DESIGN_COLORS["danger"]}:
    bg = _background_color_for(theme["accentColor"])
  else:
    bg = _DESIGN_COLORS["bg_subtle"]

  color = str(theme.get("tableHeaderColor")) if theme and _is_hex_color(theme.get("tableHeaderColor")) else _DESIGN_COLORS["text_primary"]
  if bg in {
    _DESIGN_COLORS["brand"],
    _DESIGN_COLORS["brand_hover"],
    _DESIGN_COLORS["brand_pressed"],
    _DESIGN_COLORS["plan"],
    _DESIGN_COLORS["brand_bg_60"],
    _DESIGN_COLORS["success_bg_60"],
    _DESIGN_COLORS["warning_bg_60"],
    _DESIGN_COLORS["danger_bg_60"],
  }:
    color = "#FFFFFF"
  return bg, color


def _prettify_tables(html: str, theme: dict[str, object] | None = None) -> str:
  header_bg, header_color = _resolve_table_style(theme)
  html = re.sub(
    r"<th>",
    f'<th style="background-color: {header_bg}; color: {header_color};">',
    html,
    flags=re.IGNORECASE,
  )
  html = re.sub(
    r"<table>",
    f'<table style="border-color: {_DESIGN_COLORS["border"]};">',
    html,
    flags=re.IGNORECASE,
  )
  return html


def _protect_structured_macros(html: str) -> tuple[str, dict[str, str]]:
  placeholders: dict[str, str] = {}
  token_pattern = re.compile(r"<ac:structured-macro\b[^>]*>|</ac:structured-macro>", flags=re.IGNORECASE)
  parts: list[str] = []
  cursor = 0
  macro_start: int | None = None
  depth = 0

  for match in token_pattern.finditer(html):
    token = match.group(0).lower()
    if token.startswith("<ac:structured-macro"):
      if depth == 0:
        parts.append(html[cursor:match.start()])
        macro_start = match.start()
      depth += 1
      continue

    if depth == 0:
      continue

    depth -= 1
    if depth == 0 and macro_start is not None:
      key = f"CWIKI_PROTECTED_MACRO_{len(placeholders)}"
      placeholders[key] = html[macro_start:match.end()]
      parts.append(key)
      cursor = match.end()
      macro_start = None

  if depth != 0 or macro_start is not None:
    return html, {}

  parts.append(html[cursor:])
  return "".join(parts), placeholders


def _restore_structured_macros(html: str, placeholders: dict[str, str]) -> str:
  restored = html
  for key, macro in sorted(placeholders.items(), key=lambda item: len(item[0]), reverse=True):
    restored = restored.replace(key, macro)
  return restored


def _layout_paired_tables(body_html: str, section_plan: dict[str, Any] | None) -> str:
  if not section_plan:
    return body_html
  layout = str(section_plan.get("layout") or "").strip().lower()
  if layout not in {"paired-tables", "side-by-side-tables", "two-column-tables"}:
    return body_html

  pattern = re.compile(
    r"<p>\s*<strong>(?P<title1>[\s\S]*?)</strong>\s*</p>\s*"
    r"(?P<table1><table[\s\S]*?</table>)\s*"
    r"<p>\s*<strong>(?P<title2>[\s\S]*?)</strong>\s*</p>\s*"
    r"(?P<table2><table[\s\S]*?</table>)",
    flags=re.IGNORECASE,
  )

  def repl(match: re.Match[str]) -> str:
    title1 = match.group("title1")
    title2 = match.group("title2")
    table1 = match.group("table1")
    table2 = match.group("table2")
    left_bg = str(section_plan.get("leftHeaderBg") or _DESIGN_COLORS["brand_bg_60"])
    right_bg = str(section_plan.get("rightHeaderBg") or _DESIGN_COLORS["warning_bg_60"])
    if not _is_hex_color(left_bg):
      left_bg = _DESIGN_COLORS["brand_bg_60"]
    if not _is_hex_color(right_bg):
      right_bg = _DESIGN_COLORS["warning_bg_60"]
    left_bg = _background_color_for(left_bg)
    right_bg = _background_color_for(right_bg)
    left_soft = str(section_plan.get("leftCardBg") or "#F4FAFF")
    right_soft = str(section_plan.get("rightCardBg") or "#FFF8F0")
    if not _is_hex_color(left_soft):
      left_soft = "#F4FAFF"
    if not _is_hex_color(right_soft):
      right_soft = "#FFF8F0"
    return (
      '<table style="border: 0; width: 100%;"><tbody><tr>'
      '<td style="width: 50%; vertical-align: top; padding-right: 12px; border: 0;">'
      f'<div style="background-color: {left_soft}; border: 1px solid #D8E8FA; padding: 10px;">'
      f'<p><span style="background-color: {left_bg}; color: #FFFFFF; padding: 3px 8px; font-size: 12px;">'
      f"<strong>{title1}</strong></span></p>{table1}</div></td>"
      '<td style="width: 50%; vertical-align: top; padding-left: 12px; border: 0;">'
      f'<div style="background-color: {right_soft}; border: 1px solid #F5D9B8; padding: 10px;">'
      f'<p><span style="background-color: {right_bg}; color: #FFFFFF; padding: 3px 8px; font-size: 12px;">'
      f"<strong>{title2}</strong></span></p>{table2}</div></td>"
      "</tr></tbody></table>"
    )

  return pattern.sub(repl, body_html)


def prettify_storage_html(storage_html: str, prettify_plan: dict[str, Any] | None = None) -> str:
  protected_html, protected_macros = _protect_structured_macros(storage_html)
  matches = list(re.finditer(r"(<h2[^>]*>([\s\S]*?)</h2>)", protected_html, flags=re.IGNORECASE))
  if not matches:
    return _restore_structured_macros(_prettify_tables(protected_html), protected_macros)

  chunks: list[str] = []
  cursor = 0
  for index, match in enumerate(matches):
    chunks.append(protected_html[cursor:match.start()])
    body_start = match.end()
    body_end = matches[index + 1].start() if index + 1 < len(matches) else len(protected_html)
    body_html = protected_html[body_start:body_end]
    title_html = match.group(2)
    title_text = _strip_html_tags(title_html)
    fallback_theme = _resolve_prettify_theme(title_text, body_html)
    section_plan = _section_plan_from_operations(
      prettify_plan,
      title_text,
    )
    theme = _theme_from_plan_section(section_plan, fallback_theme) if section_plan else fallback_theme
    chunks.append(_style_heading(title_html, theme))
    prettified_body = _prettify_inline_emphasis(body_html, theme, section_plan)
    prettified_body = _layout_paired_tables(prettified_body, section_plan)
    prettified_body = _prettify_tables(prettified_body, theme)
    if body_html.strip() and theme.get("panel"):
      panel_html, rest_html = _split_panel_body(prettified_body, section_plan)
      if panel_html.strip():
        chunks.append(_build_panel_macro(panel_html, theme))
        chunks.append(rest_html)
      else:
        chunks.append(prettified_body)
    else:
      chunks.append(prettified_body)
    cursor = body_end
  chunks.append(protected_html[cursor:])
  return _restore_structured_macros(_prettify_tables("".join(chunks)), protected_macros)


def describe_prettify_scheme(markdown_text: str, prettify_plan: dict[str, Any] | None = None) -> str:
  normalized = textwrap.dedent(markdown_text).lstrip("\n")
  headings = [(level, title) for level, title in _extract_headings(normalized) if level == 2]
  if not headings:
    return "未发现二级标题，保留原始结构"

  plans: list[str] = []
  for _level, title in headings[:6]:
    fallback_theme = _resolve_prettify_theme(title, "")
    section_plan = _section_plan_from_operations(prettify_plan, title)
    theme = _theme_from_plan_section(section_plan, fallback_theme) if section_plan else fallback_theme
    style = "轻提示面板" if theme.get("panel") else "标题强调"
    layout = ""
    if section_plan and section_plan.get("layout"):
      layout = f"/{section_plan['layout']}"
    source = "Agent操作" if section_plan else "脚本兜底"
    plans.append(f"{title}:{theme['name']}({style}/{theme['accentColor']}/{source}{layout})")
  if len(headings) > 6:
    plans.append(f"其余 {len(headings) - 6} 个区块按同样规则处理")
  return "；".join(plans)


__all__ = [
  "describe_prettify_scheme",
  "prettify_storage_html",
]
