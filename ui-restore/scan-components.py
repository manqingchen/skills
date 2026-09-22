#!/usr/bin/env python3
"""scan-components.py — 盘点项目已有 UI 组件(UI 还原「组件复用优先」节点用)

用法:
    python3 scan-components.py <项目根目录> [--json]

输出:
    uiLibraries   package.json 依赖中的 UI 组件库/样式体系(可直接 import 复用)
    components    项目自有组件清单(.vue 单文件组件 / React 风格组件导出)
    barrels       index.ts 等桶文件的再导出(组件库入口)
    shadcn        是否存在 shadcn/ui 结构(components.json 或 components/ui 目录)

用途: 生成组件映射表 —— 设计稿区块 → 复用已有组件 / 需新建 1:1 还原。
"""
import argparse
import json
import os
import re
import sys

IGNORE_DIRS = {'node_modules', '.git', 'dist', 'build', '.next', '.nuxt', '.output',
               'out', 'coverage', '.turbo', '.cache', 'vendor', 'bin', 'obj', '.svelte-kit'}
SOURCE_EXTS = {'.vue', '.tsx', '.jsx', '.ts', '.js', '.svelte'}

UI_LIBS = {
    'antd': 'Ant Design(React)', 'antd-mobile': 'Ant Design Mobile',
    '@arco-design/web-react': 'Arco Design', '@douyinfe/semi-ui': 'Semi Design',
    'element-plus': 'Element Plus', 'element-ui': 'Element UI',
    'vant': 'Vant', 'naive-ui': 'Naive UI', 'vuetify': 'Vuetify',
    'tdesign-vue-next': 'TDesign', '@nutui/nutui': 'NutUI',
    'varlet': 'Varlet', 'quasar': 'Quasar',
    '@mui/material': 'MUI', '@chakra-ui/react': 'Chakra UI',
    '@mantine/core': 'Mantine', '@headlessui/react': 'Headless UI',
    'react-bootstrap': 'React Bootstrap', 'reactstrap': 'Reactstrap',
    'primereact': 'PrimeReact', 'primevue': 'PrimeVue',
    '@heroui/react': 'HeroUI', '@ark-ui/react': 'Ark UI',
    'tailwindcss': 'Tailwind CSS(样式体系)', 'unocss': 'UnoCSS(样式体系)',
    'windicss': 'Windi CSS(样式体系)',
}
UI_LIB_PREFIXES = {'@radix-ui/': 'Radix UI', 'recharts': 'Recharts(图表)', 'echarts': 'ECharts(图表)'}

EXPORT_PATTERNS = [
    re.compile(r'export\s+default\s+function\s+([A-Z]\w*)'),
    re.compile(r'export\s+default\s+([A-Z]\w*)\s*(?:;|=|$)'),
    re.compile(r'export\s+function\s+([A-Z]\w*)'),
    re.compile(r'export\s+const\s+([A-Z]\w*)'),
    re.compile(r'export\s+\{([^}]*)\}'),
]


def pascal(stem):
    return ''.join(p[:1].upper() + p[1:] for p in re.split(r'[-_.\s]+', stem) if p)


def find_ui_libs(root):
    libs = {}
    pkg_candidates = [os.path.join(root, 'package.json')]
    try:
        for entry in os.listdir(root):
            sub = os.path.join(root, entry, 'package.json')
            if entry not in IGNORE_DIRS and not entry.startswith('.') and os.path.isfile(sub):
                pkg_candidates.append(sub)
    except Exception:
        pass
    for pkg_path in pkg_candidates:
        try:
            with open(pkg_path, encoding='utf-8', errors='ignore') as f:
                pkg = json.load(f) or {}
        except Exception:
            continue
        deps = {}
        for key in ('dependencies', 'devDependencies', 'peerDependencies'):
            deps.update(pkg.get(key) or {})
        for dep in deps:
            if dep in UI_LIBS:
                libs[dep] = UI_LIBS[dep]
            else:
                for prefix, label in UI_LIB_PREFIXES.items():
                    if dep.startswith(prefix):
                        libs[dep] = label
                        break
    return libs


def scan(root):
    components, barrels = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith('.')]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SOURCE_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            stem = os.path.splitext(fn)[0]

            if ext in ('.vue', '.svelte'):
                components.append({'name': pascal(stem), 'file': rel, 'kind': ext.lstrip('.')})
                continue
            if stem == 'index' or ext not in ('.ts', '.tsx', '.js', '.jsx'):
                if stem == 'index':
                    collect_exports(full, rel, barrels, barrel=True)
                continue
            # ts/tsx/js/jsx: 文件名 PascalCase 或导出 PascalCase 符号才算组件
            names = set()
            collect_exports(full, rel, [], names=names, barrel=False)
            if re.fullmatch(r'[A-Z]\w*', stem):
                names.add(pascal(stem))
            if names:
                kind = 'react' if ext in ('.tsx', '.jsx') else 'js'
                components.append({'name': pascal(stem), 'file': rel, 'kind': kind,
                                   'exports': sorted(names)})
    return components, barrels


def collect_exports(full, rel, barrels, names=None, barrel=False):
    found = set()
    try:
        with open(full, encoding='utf-8', errors='ignore') as f:
            src = f.read()
    except Exception:
        return
    for pat in EXPORT_PATTERNS:
        for m in pat.finditer(src):
            if m.group(1) and '{' in m.group(0):
                for piece in m.group(1).split(','):
                    piece = piece.strip().split(' as ')[-1].strip()
                    if re.fullmatch(r'[A-Z]\w*', piece):
                        found.add(piece)
            elif m.group(1):
                found.add(m.group(1))
    if barrel and found:
        barrels.append({'file': rel, 'exports': sorted(found)})
    if names is not None:
        names |= found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('--json', action='store_true', help='仅输出 JSON')
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    libs = find_ui_libs(root)
    components, barrels = scan(root)

    shadcn = (os.path.isfile(os.path.join(root, 'components.json'))
              or os.path.isdir(os.path.join(root, 'src/components/ui'))
              or os.path.isdir(os.path.join(root, 'components/ui')))

    by_dir = {}
    for c in components:
        by_dir.setdefault(os.path.dirname(c['file']) or '.', []).append(c['name'])

    result = {
        'root': root,
        'uiLibraries': libs,
        'shadcnUi': shadcn,
        'totalComponents': len(components),
        'componentsByDir': {d: sorted(set(ns)) for d, ns in sorted(by_dir.items())},
        'components': components,
        'barrels': barrels,
    }

    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('\n===== 人类可读摘要 =====', file=sys.stderr)
    print(f"项目: {root}", file=sys.stderr)
    print(f"UI 库: {', '.join(f'{k} ({v})' for k, v in libs.items()) or '未检出'}", file=sys.stderr)
    print(f"shadcn/ui 结构: {'是' if shadcn else '否'}", file=sys.stderr)
    print(f"自有组件 {len(components)} 个:", file=sys.stderr)
    for d, ns in sorted(by_dir.items()):
        print(f"  {d}/: {', '.join(sorted(set(ns)))}", file=sys.stderr)


if __name__ == '__main__':
    main()
