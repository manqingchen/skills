import base64
import html
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from markdownify import markdownify as md


@dataclass(slots=True)
class MacroAsset:
    filename: str
    content: str


def _extract_parameter(block: str, name: str) -> str:
    match = re.search(
        rf'<ac:parameter[^>]*ac:name="{re.escape(name)}"[^>]*>(.*?)</ac:parameter>',
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return html.unescape(_strip_tags(match.group(1)).strip())


def _extract_url_parameter(block: str, name: str) -> str:
    match = re.search(
        rf'<ac:parameter[^>]*ac:name="{re.escape(name)}"[^>]*>(.*?)</ac:parameter>',
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""

    body = match.group(1)
    ri_url_match = re.search(r'ri:value="([^"]+)"', body, flags=re.IGNORECASE)
    if ri_url_match:
        return html.unescape(ri_url_match.group(1).strip())

    href_match = re.search(r'href="([^"]+)"', body, flags=re.IGNORECASE)
    if href_match:
        return html.unescape(href_match.group(1).strip())

    return html.unescape(_strip_tags(body).strip())


def _extract_attachment_filename_parameter(block: str, name: str) -> str:
    match = re.search(
        rf'<ac:parameter[^>]*ac:name="{re.escape(name)}"[^>]*>(.*?)</ac:parameter>',
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""

    attachment_match = re.search(r'ri:filename="([^"]+)"', match.group(1), flags=re.IGNORECASE)
    if attachment_match:
        return html.unescape(attachment_match.group(1).strip())
    return ""


def _render_notice_block(title: str, body: str) -> str:
    return f"> **{title}**\n>\n> {body}".rstrip()


def _extract_plain_text_body(block: str) -> str:
    cdata_match = re.search(
        r"<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>",
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if cdata_match:
        return cdata_match.group(1).strip()

    body_match = re.search(
        r"<ac:plain-text-body>(.*?)</ac:plain-text-body>",
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not body_match:
        return ""
    return html.unescape(_strip_tags(body_match.group(1)).strip())


def _extract_rich_text_body(block: str) -> str:
    opening = re.search(r"<ac:rich-text-body\b[^>]*>", block, flags=re.IGNORECASE)
    if not opening:
        return ""
    token_pattern = re.compile(r"<ac:rich-text-body\b[^>]*>|</ac:rich-text-body>", flags=re.IGNORECASE)
    depth = 0
    body_start = opening.end()
    for token in token_pattern.finditer(block, opening.start()):
        if token.group(0).lower().startswith("<ac:rich-text-body"):
            depth += 1
            continue
        depth -= 1
        if depth == 0:
            return block[body_start:token.start()].strip()
    return ""


def _strip_tags(content: str) -> str:
    return re.sub(r"<[^>]+>", "", content)


def _build_svg_placeholder(name: str, summary: str) -> str:
    safe_name = html.escape(name)
    safe_summary = html.escape(summary or "请前往 Confluence 查看原始内容")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="720" height="160" viewBox="0 0 720 160">
  <rect width="720" height="160" rx="16" fill="#F5F7FA" />
  <rect x="24" y="24" width="672" height="112" rx="12" fill="#FFFFFF" stroke="#D0D7DE" />
  <text x="48" y="66" font-size="24" font-family="Arial, sans-serif" fill="#1F2328">复杂宏：{safe_name}</text>
  <text x="48" y="102" font-size="18" font-family="Arial, sans-serif" fill="#57606A">{safe_summary}</text>
  <text x="48" y="128" font-size="16" font-family="Arial, sans-serif" fill="#57606A">请在 Confluence 原页面查看完整渲染</text>
</svg>
"""


def extract_complex_macro_preview_titles(view_html: str) -> dict[str, list[str]]:
    previews: dict[str, list[str]] = {}
    soup = BeautifulSoup(f"<body>{view_html}</body>", "lxml")

    for node in soup.select("[data-macro-name]"):
        macro_name = str(node.get("data-macro-name") or "").strip().lower()
        if not macro_name:
            continue

        preview_title = ""

        image = node.find("img")
        if image and image.get("src"):
            path = unquote(urlsplit(str(image.get("src"))).path)
            preview_title = path.split("/")[-1]

        if not preview_title and macro_name == "drawio":
            data_node = node.find(attrs={"data-diagramdata": True})
            encoded = str(data_node.get("data-diagramdata") or "") if data_node else ""
            if encoded:
                try:
                    payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
                    preview_title = str(payload.get("previewName") or "").strip()
                except Exception:
                    preview_title = ""

        if preview_title:
            previews.setdefault(macro_name, []).append(preview_title)

    return previews


def extract_view_macro_links(
    view_html: str,
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    page_links: dict[str, str] = {}
    macro_links: dict[str, dict[str, str]] = {}
    soup = BeautifulSoup(f"<body>{view_html}</body>", "lxml")

    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "").strip()
        text = anchor.get_text(" ", strip=True)
        if not href or not text:
            continue
        if "/pages/viewpage.action?pageId=" in href:
            page_links[text] = href

    for node in soup.select('[data-macro-name="jira"][data-jira-key]'):
        key = str(node.get("data-jira-key") or "").strip()
        anchor = node.find("a", href=True)
        href = str(anchor.get("href") or "").strip() if anchor else ""
        if key and href:
            macro_links.setdefault("jira", {})[key] = href

    return page_links, macro_links


def extract_storage_macro_preview_titles(storage_html: str) -> dict[str, list[str]]:
    previews: dict[str, list[str]] = {}

    for match in re.finditer(
        r'<ac:structured-macro\b[^>]*ac:name="mermaid-cloud"[^>]*>(.*?)</ac:structured-macro>',
        storage_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        block = match.group(0)
        filename = _extract_parameter(block, "filename")
        if not filename:
            continue
        previews.setdefault("mermaid-cloud", []).extend(
            [
                f"{filename}.svg",
                f"{filename}.png",
            ]
        )

    return previews


def transform_macros(
    storage_html: str,
    macro_asset_prefix: str,
    attachment_path_map: dict[str, str] | None = None,
    complex_macro_preview_paths: dict[str, list[str]] | None = None,
    macro_link_map: dict[str, dict[str, str]] | None = None,
) -> tuple[str, dict[str, str], list[MacroAsset]]:
    markdown_blocks: dict[str, str] = {}
    macro_assets: list[MacroAsset] = []
    block_index = 0
    complex_index = 0
    preview_queue = {
        name: list(paths) for name, paths in (complex_macro_preview_paths or {}).items()
    }
    macro_links = macro_link_map or {}
    attachment_paths = attachment_path_map or {}

    def replace_known_tokens(markdown: str) -> str:
        rendered = markdown
        for token, block in markdown_blocks.items():
            rendered = rendered.replace(token, block)
        return rendered

    def render_rich_text_body(block: str) -> str:
        body = _extract_rich_text_body(block)
        if not body:
            return ""
        transformed_body = transform_macro_blocks(body)
        body_markdown = md(
            transformed_body,
            heading_style="ATX",
            table_infer_header=True,
            keep_inline_images_in=["td", "th"],
        ).strip()
        return replace_known_tokens(body_markdown)

    def render_macro(name: str, block: str) -> str:
        nonlocal block_index, complex_index

        name = name.strip().lower()

        if name in {"code", "noformat"}:
            block_index += 1
            language = _extract_parameter(block, "language") or ""
            title = _extract_parameter(block, "title")
            code = _extract_plain_text_body(block)
            info = language
            if title:
                info = f'{info} title="{title}"' if info else f'title="{title}"'
            fence = f"```{info}\n{code}\n```".strip()
            token = f"CWIKIMARKDOWNBLOCK{block_index}TOKEN"
            markdown_blocks[token] = fence
            return f"<p>{token}</p>"

        if name in {"panel", "info", "note", "warning", "tip"}:
            block_index += 1
            title = _extract_parameter(block, "title")
            body = render_rich_text_body(block)
            prefix = title or name.upper()
            token = f"CWIKIMARKDOWNBLOCK{block_index}TOKEN"
            quoted_body = "\n> ".join(body.splitlines()) if body else ""
            markdown_blocks[token] = f"> **{prefix}**\n>\n> {quoted_body}".rstrip()
            return f"<p>{token}</p>"

        if name == "status":
            text = (
                _extract_parameter(block, "title")
                or _extract_parameter(block, "colour")
                or "状态"
            )
            return f"<strong>[{html.escape(text)}]</strong>"

        if name == "jira":
            key = _extract_parameter(block, "key") or "JIRA"
            href = (macro_links.get("jira") or {}).get(key)
            if href:
                return f'<a href="{html.escape(href)}">{html.escape(key)}</a>'
            return html.escape(key)

        if name == "expand":
            block_index += 1
            title = _extract_parameter(block, "title") or "展开内容"
            body = render_rich_text_body(block)
            token = f"CWIKIMARKDOWNBLOCK{block_index}TOKEN"
            markdown_blocks[token] = (
                f"<details><summary>{html.escape(title)}</summary>\n\n{body}\n\n</details>"
            )
            return f"<p>{token}</p>"

        if name == "section":
            block_index += 1
            body = render_rich_text_body(block)
            token = f"CWIKIMARKDOWNBLOCK{block_index}TOKEN"
            markdown_blocks[token] = body
            return f"<p>{token}</p>"

        if name == "column":
            block_index += 1
            body = render_rich_text_body(block)
            token = f"CWIKIMARKDOWNBLOCK{block_index}TOKEN"
            markdown_blocks[token] = body
            return f"<p>{token}</p>"

        if name == "toc":
            return ""

        if name == "markdown":
            block_index += 1
            body = _extract_plain_text_body(block)
            token = f"CWIKIMARKDOWNBLOCK{block_index}TOKEN"
            markdown_blocks[token] = body
            return f"<p>{token}</p>"

        if name == "widget":
            widget_url = _extract_url_parameter(block, "url")
            if widget_url:
                escaped_url = html.escape(widget_url)
                return f'<a href="{escaped_url}">{escaped_url}</a>'

        if name == "view-file":
            filename = _extract_attachment_filename_parameter(block, "name") or _extract_parameter(block, "name")
            local_path = attachment_paths.get(filename)
            label = "查看 HTML 附件" if filename.lower().endswith((".html", ".htm")) else "查看附件"
            if local_path:
                return f' <a href="{html.escape(local_path)}">{html.escape(label)}</a>'
            return f" {html.escape(label)}"

        preview_paths = preview_queue.get(name) or []
        if preview_paths:
            preview_path = preview_paths.pop(0)
            return f'<img src="{preview_path}" alt="复杂宏-{name or "macro"}" />'

        complex_index += 1
        filename = f"{name or 'macro'}-{complex_index}.svg"
        summary = _extract_parameter(block, "key") or _extract_parameter(block, "title")
        if name == "mermaid-cloud" and not summary:
            summary = "未获取到 Mermaid 源码，请在 Confluence 原页面查看或手动补充。"
        macro_assets.append(
            MacroAsset(
                filename=filename,
                content=_build_svg_placeholder(name or "macro", summary),
            )
        )
        return f'<img src="{macro_asset_prefix}/{filename}" alt="复杂宏-{name or "macro"}" />'

    def transform_macro_blocks(source: str) -> str:
        token_pattern = re.compile(
            r"<ac:structured-macro\b[^>]*(?:/>|>)|</ac:structured-macro>",
            flags=re.IGNORECASE,
        )
        output: list[str] = []
        cursor = 0
        macro_start: int | None = None
        macro_name = ""
        depth = 0

        for match in token_pattern.finditer(source):
            token = match.group(0)
            lowered = token.lower()
            if lowered.startswith("<ac:structured-macro"):
                if depth == 0:
                    output.append(source[cursor:match.start()])
                    macro_start = match.start()
                    name_match = re.search(r'ac:name="([^"]+)"', token, flags=re.IGNORECASE)
                    macro_name = name_match.group(1) if name_match else ""
                if lowered.endswith("/>"):
                    if depth == 0:
                        output.append(render_macro(macro_name, token))
                        cursor = match.end()
                        macro_start = None
                        macro_name = ""
                    continue
                depth += 1
                continue

            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and macro_start is not None:
                block = source[macro_start:match.end()]
                output.append(render_macro(macro_name, block))
                cursor = match.end()
                macro_start = None
                macro_name = ""

        if depth != 0 or macro_start is not None:
            return source

        output.append(source[cursor:])
        return "".join(output)

    transformed = transform_macro_blocks(storage_html)
    return transformed, markdown_blocks, macro_assets
