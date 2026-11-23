#!/usr/bin/env python3
"""selfcheck.py — prd-to-breakdown §8 自检清单的机检部分

补 ptb validate 未覆盖、但清单里可机判的条目（只读查询 prd.db，不写库、不替代 validate）：

  错误（阻止交付）:
    - jiras.scope 为空（第 0.5 步的拆解范围未记录）
    - 某条 requirement 的 excluded_frontend（「前端不处理」边界）为空
    - 有 kind=visual 验收的任务没有 design_ref
    - design_drafts.covers 引用了不存在的任务
    - 埋点 API 与事件类型不匹配（exposure→TrackBeseen / click→sendTrack）或缺 element/timing
  警告（台账）:
    - 同一文件路径登记在多张卡上（应只登记在创建它的任务）

用法: python3 selfcheck.py --jira OS-xxxx [--db ~/.prd-to-breakdown/prd.db]
环境变量 PTB_DB 可覆盖库路径。有错误退出码 1。
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description='prd-to-breakdown 自检清单机检')
    ap.add_argument('--jira', required=True)
    ap.add_argument('--db', default=os.environ.get('PTB_DB',
                str(Path.home() / '.prd-to-breakdown' / 'prd.db')))
    args = ap.parse_args()

    conn = sqlite3.connect(f'file:{Path(args.db).expanduser()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    errors, warnings = [], []

    if not conn.execute('SELECT 1 FROM jiras WHERE jira=?', (args.jira,)).fetchone():
        print(f'错误  需求 {args.jira} 不存在')
        sys.exit(1)

    # 第 0.5 步：范围已写入 jiras.scope
    scope = conn.execute('SELECT scope FROM jiras WHERE jira=?', (args.jira,)).fetchone()['scope']
    if not scope:
        errors.append('jiras.scope 为空 —— 第 0.5 步的拆解范围（frontend/backend/both）未记录')

    # 每条需求都写了「前端不处理」边界
    for r in conn.execute(
            "SELECT id FROM requirements WHERE jira=? "
            "AND (excluded_frontend IS NULL OR TRIM(excluded_frontend) = '')", (args.jira,)):
        errors.append(f"需求 {r['id']} 未写「前端不处理」边界（excluded_frontend 为空）")

    # 含 UI 改动的任务登记了 design_ref
    task_ids = {r['id'] for r in conn.execute('SELECT id FROM tasks WHERE jira=?', (args.jira,))}
    for r in conn.execute(
            "SELECT DISTINCT task_id FROM acceptance WHERE jira=? AND kind='visual'", (args.jira,)):
        tid = r['task_id']
        if tid not in task_ids:
            errors.append(f'visual 验收挂在了不存在的任务 {tid} 上')
            continue
        t = conn.execute('SELECT design_ref FROM tasks WHERE jira=? AND id=?',
                         (args.jira, tid)).fetchone()
        if not t['design_ref']:
            errors.append(f'{tid} 有 visual 验收但没有 design_ref（设计稿路径 + 区块）')

    # design_drafts.covers 写清覆盖哪些任务，且任务存在（执行方按「一稿一轮」组织还原）
    for d in conn.execute('SELECT path, covers FROM design_drafts WHERE jira=?', (args.jira,)):
        for tid in [x.strip() for x in (d['covers'] or '').split(',') if x.strip()]:
            if tid not in task_ids:
                errors.append(f"设计稿 {d['path']} covers 引用了不存在的任务 {tid}")

    # 埋点：类型→API 匹配、位置与时机写清（tracking_points 只定义一次，任务只引用）
    for t in conn.execute('SELECT * FROM tracking_points WHERE jira=?', (args.jira,)):
        api = t['api'] or ''
        if t['event'] == 'exposure' and api != 'TrackBeseen':
            errors.append(f"埋点(id={t['id']}) exposure 事件必须用 TrackBeseen，当前: {api or '空'}")
        if t['event'] == 'click' and api != 'sendTrack':
            errors.append(f"埋点(id={t['id']}) click 事件必须用 sendTrack，当前: {api or '空'}")
        if not t['element'] or not t['timing']:
            errors.append(f"埋点(id={t['id']}) 未写清 element/timing（挂在哪个组件、什么时机触发）")

    # kind=contract 的验收必须带 --contract 标注（contract-align 的回扫点，漏标会扫不到）
    for a in conn.execute(
            "SELECT id, task_id, descr FROM acceptance "
            "WHERE jira=? AND kind='contract' AND depends_contract=0", (args.jira,)):
        errors.append(f"验收(id={a['id']}, {a['task_id']}) kind=contract 但未加 --contract 标注"
                      f"（depends_contract=0），contract-align 回扫会漏掉")

    # 同一文件的行只出现在创建它的任务上（台账）
    cnt, owners = Counter(), {}
    for f in conn.execute('SELECT task_id, path FROM task_files WHERE jira=?', (args.jira,)):
        cnt[f['path']] += 1
        owners.setdefault(f['path'], []).append(f['task_id'])
    for p, c in sorted(cnt.items()):
        if c > 1:
            warnings.append(
                f"文件 {p} 登记在 {c} 张卡上（{','.join(owners[p])}）——"
                f"应只登记在创建它的任务，后续任务在 implementation 里写「扩展 T00x 创建的 <路径>」")

    conn.close()
    for e in errors:
        print('错误  ' + e)
    for w in warnings:
        print('警告  ' + w)
    print(f'\n{len(errors)} 个错误, {len(warnings)} 个警告（机检部分；'
          f'配合 ptb validate --jira … --root … 使用，需人工判断的条目见 SKILL §8）')
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
