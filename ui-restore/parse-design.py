#!/usr/bin/env python3
"""parse-design.py — 解析设计稿 HTML, 生成 UI 还原硬校验清单 design-manifest.json

用法:
    python3 parse-design.py <设计稿.html> [--root <项目根目录>]

输出(写入 <root>/design-manifest.json):
    breakpoints         断点白名单 = 设计稿断点 ∪ 项目既有断点。
                        断点体系以项目为准(设计稿 HTML 由产品提供, 断点不保证严谨, 仅参考);
                        硬校验钩子(ui-restore-check.py)以 breakpoints 为白名单拦截自创断点。
    designBreakpoints   设计稿全部媒体查询断点(px), 仅作布局分层参考
    projectBreakpoints  扫描项目 scss/css 得到的项目断点体系(@include media / @media px)
    queries             每条 @media 的原文 / 方向(min=宽屏起点, max=窄屏上限) / 规则数
    stats               style 块数 / 内联样式数 / CSS 变量清单
"""
import argparse
import datetime
import json
import os
import re

SKIP_DIRS = {'node_modules', '.next', '.git', 'dist', 'build', '.turbo', '.cache'}


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


def collect_project_breakpoints(root):
    """扫描项目 scss/css, 收集项目既有断点体系(@include media(Npx) 与 @media 查询头中的 px 宽度)。

    只识别 @media 查询头与项目 media mixin 调用, 不碰普通 min-width/max-width 属性声明;
    并过滤到合理视口宽度区间(300~3840), 避免元素尺寸值混入。
    """
    def add_if_viewport(bps, value):
        if 300 <= value <= 3840:
            bps.add(value)

    bps = set()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if not fn.endswith(('.scss', '.css')):
                continue
            path = os.path.join(base, fn)
            try:
                with open(path, encoding='utf-8', errors='ignore') as f:
                    text = f.read()
            except OSError:
                continue
            for m in re.finditer(r'@include\s+media\(\s*(\d+(?:\.\d+)?)px', text):
                add_if_viewport(bps, float(m.group(1)))
            for m in re.finditer(r'@media[^{;]*\{', text):
                header = m.group(0)
                for w in re.finditer(r'(?:min|max)-width\s*:\s*(\d+(?:\.\d+)?)px', header, re.IGNORECASE):
                    add_if_viewport(bps, float(w.group(1)))
    return bps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('design', help='设计稿 HTML 路径')
    ap.add_argument('--root', default=os.getcwd(), help='还原项目根目录(默认当前目录)')
    a = ap.parse_args()

    with open(a.design, encoding='utf-8', errors='ignore') as f:
        html = f.read()

    queries, design_bps = [], set()
    for q, body in media_blocks(html):
        rules = len(re.findall(r'\{[^{}]*\}', body))
        for w, kind in width_kinds(q):
            design_bps.add(w)
            queries.append({'query': q, 'width': w, 'kind': kind, 'rules': rules})

    project_bps = collect_project_breakpoints(os.path.abspath(a.root))

    manifest = {
        'version': 1,
        'design': os.path.abspath(a.design),
        'parsedAt': datetime.datetime.now().isoformat(timespec='seconds'),
        # 断点体系以项目为准: 白名单 = 项目既有断点 ∪ 设计稿断点, 钩子据此拦截自创断点
        'breakpoints': sorted(design_bps | project_bps),
        'designBreakpoints': sorted(design_bps),
        'projectBreakpoints': sorted(project_bps),
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

    print(json.dumps({k: manifest[k] for k in ('breakpoints', 'designBreakpoints', 'projectBreakpoints')},
                     ensure_ascii=False, indent=2))
    print(f'已生成 {out}', file=__import__('sys').stderr)


if __name__ == '__main__':
    main()
