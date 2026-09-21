#!/usr/bin/env python3
"""parse-design.py — 解析设计稿 HTML, 生成 UI 还原硬校验清单 design-manifest.json

用法:
    python3 parse-design.py <设计稿.html> [--root <项目根目录>]

输出(写入 <root>/design-manifest.json):
    breakpoints  设计稿全部媒体查询断点(px)——还原代码的断点只允许从这里取
    queries      每条 @media 的原文 / 方向(min=宽屏起点, max=窄屏上限) / 规则数
    stats        style 块数 / 内联样式数 / CSS 变量清单

硬校验钩子(ui-restore-check.py)以 breakpoints 为白名单拦截自创断点。
"""
import argparse
import datetime
import json
import os
import re


def media_blocks(html):
    """yield (查询原文, 块内样式文本), 花括号配平, 支持嵌套"""
    for m in re.finditer(r'@media[^{;]*\{', html):
        start = m.end() - 1
        depth, i = 1, start + 1
        while i < len(html) and depth:
            if html[i] == '{':
                depth += 1
            elif html[i] == '}':
                depth -= 1
            i += 1
        yield m.group(0).rstrip('{').strip(), html[start + 1:i - 1]


def width_kinds(q):
    """从单条媒体查询中提取 (宽度px, 方向)。兼容传统语法与 Level 4 范围语法"""
    out = []
    for m in re.finditer(r'(min-width|max-width)\s*:\s*(\d+(?:\.\d+)?)px', q, re.IGNORECASE):
        out.append((float(m.group(2)), 'min' if m.group(1).lower().startswith('min') else 'max'))
    for m in re.finditer(r'width\s*(>=|<=|>|<)\s*(\d+(?:\.\d+)?)px', q, re.IGNORECASE):
        out.append((float(m.group(2)), 'min' if m.group(1) in ('>=', '>') else 'max'))
    for m in re.finditer(r'(\d+(?:\.\d+)?)px\s*(<=|>=)\s*width', q, re.IGNORECASE):
        out.append((float(m.group(1)), 'min' if m.group(2) == '>=' else 'max'))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('design', help='设计稿 HTML 路径')
    ap.add_argument('--root', default=os.getcwd(), help='还原项目根目录(默认当前目录)')
    a = ap.parse_args()

    with open(a.design, encoding='utf-8', errors='ignore') as f:
        html = f.read()

    queries, bps = [], set()
    for q, body in media_blocks(html):
        rules = len(re.findall(r'\{[^{}]*\}', body))
        for w, kind in width_kinds(q):
            bps.add(w)
            queries.append({'query': q, 'width': w, 'kind': kind, 'rules': rules})

    manifest = {
        'version': 1,
        'design': os.path.abspath(a.design),
        'parsedAt': datetime.datetime.now().isoformat(timespec='seconds'),
        'breakpoints': sorted(bps),
        'queries': queries,
        'stats': {
            'styleBlocks': len(re.findall(r'<style\b', html, re.IGNORECASE)),
            'inlineStyles': len(re.findall(r'style="', html)),
            'cssVariables': sorted(set(re.findall(r'--[A-Za-z0-9-]+(?=\s*:)', html))),
        },
    }

    out = os.path.join(os.path.abspath(a.root), 'design-manifest.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f'已生成 {out}', file=__import__('sys').stderr)


if __name__ == '__main__':
    main()
