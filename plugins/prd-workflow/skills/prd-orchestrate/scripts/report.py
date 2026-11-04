#!/usr/bin/env python3
"""report.py — prd-orchestrate 单文件 HTML 报告

数据只来自 ptb CLI、run.json、plan.json 与 git；不引入任何外部资源。
"""
import html
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PTB = HERE.parents[2] / 'scripts' / 'ptb.py'
REPORTS_ROOT = Path.home() / '.prd-to-breakdown' / 'reports'
TEMPLATE = HERE / 'report_template.html'


def esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def fmt_float(value, digits=0):
    try:
        if digits:
            return f'{float(value):.{digits}f}'
        return f'{int(round(float(value)))}'
    except Exception:
        return str(value)


def load_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def ptb(db, *args, allow_rc=(0,)):
    cmd = [sys.executable, str(PTB), '--db', str(db)] + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode not in allow_rc:
        raise RuntimeError(f'ptb {" ".join(map(str, args))} 失败: {p.stderr.strip()}')
    data = load_json(p.stdout)
    return data if data is not None else p.stdout


def ptb_json(db, *args, allow_rc=(0,)):
    return ptb(db, *args, allow_rc=allow_rc)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return default


def norm_path(p):
    p = str(p or '').replace('\\', '/').strip()
    while p.startswith('./'):
        p = p[2:]
    return p.strip('/')


def collect(jira, db, run_dir, full=False):
    run = read_json(Path(run_dir) / 'run.json', {}) or {}
    plan = read_json(Path(run_dir) / 'plan.json')
    export = ptb_json(db, 'export', '--jira', jira)
    jira_row = ((export.get('jira') or {}).get('jira') or {})
    basics = ptb_json(db, 'task', 'list', '--jira', jira, '--json')

    tasks = []
    for b in basics:
        tid = b.get('id')
        show = ptb_json(db, 'task', 'show', tid, '--jira', jira)
        events = ptb_json(db, 'log', '--jira', jira, '--task', tid, '--json')
        t = show.get('task') or {}
        entry = (run.get('tasks') or {}).get(tid) or {}
        tasks.append({
            'id': tid,
            'title': t.get('title') or b.get('title') or '',
            'status': t.get('status') or b.get('status') or 'todo',
            'end': t.get('end') or b.get('end'),
            'estimate': t.get('estimate') or b.get('estimate'),
            'seq': t.get('seq'),
            'milestone': t.get('milestone'),
            'design_ref': t.get('design_ref'),
            'goal': t.get('goal'),
            'not_responsible': t.get('not_responsible'),
            'implementation': t.get('implementation'),
            'boundaries': t.get('boundaries'),
            'notes': t.get('notes'),
            'depends_on': show.get('depends_on') or [],
            'files': show.get('files') or [],
            'acceptance': show.get('acceptance') or [],
            'verifications': show.get('verifications') or [],
            'completions': show.get('completions') or [],
            'requirements': show.get('requirements') or [],
            'events': events if isinstance(events, list) else [],
            'run': entry,
        })

    validate = {'errors': [], 'warnings': []}
    try:
        validate = ptb_json(db, 'validate', '--jira', jira, '--json', allow_rc=(0, 1))
    except Exception as e:
        validate = {'errors': [str(e)], 'warnings': []}

    doctor = None
    if full and run.get('root'):
        try:
            doctor = ptb_json(db, 'doctor', '--jira', jira, '--root', run['root'], '--json')
        except Exception as e:
            doctor = {'error': str(e)}
    elif run.get('preflight', {}).get('doctor'):
        doctor = run['preflight']['doctor']

    data = {
        'jira': jira,
        'title': jira_row.get('title') or jira,
        'jira_row': jira_row,
        'run': run,
        'plan': plan,
        'tasks': tasks,
        'requirements': export.get('requirements') or [],
        'contracts': export.get('contracts') or [],
        'contract_fields': export.get('contract_fields') or [],
        'decisions': export.get('decisions') or [],
        'risks': export.get('risks') or [],
        'design_drafts': export.get('design_drafts') or [],
        'i18n_keys': export.get('i18n_keys') or [],
        'tracking_points': export.get('tracking_points') or [],
        'validate': validate,
        'doctor': doctor,
    }
    data['stats'] = stats(data)
    data['integrity'] = integrity(data)
    return data


def task_arms(entry):
    return entry.get('arms') or {}


def arm_finished(arm):
    d = arm.get('dispatch') or {}
    return bool(d.get('outcome')) or d.get('state') in ('failed',)


def stats(data):
    tasks = data['tasks']
    run = data['run']
    counts = {k: 0 for k in ('todo', 'doing', 'done', 'blocked')}
    for t in tasks:
        counts[t['status']] = counts.get(t['status'], 0) + 1
    acc_total = sum(len(t['acceptance']) for t in tasks)
    acc_pass = sum(1 for t in tasks for a in t['acceptance'] if a.get('result') == 'pass')
    entries = list((run.get('tasks') or {}).values())
    arms = [a for e in entries for a in task_arms(e).values()]
    dispatched = [e for e in entries
                  if any((a.get('dispatch') or {}).get('dispatch_id')
                         for a in task_arms(e).values())]
    succeeded = [a for a in arms if (a.get('dispatch') or {}).get('outcome') == 'succeeded']
    failed = [a for a in arms if (a.get('dispatch') or {}).get('outcome') == 'failed'
              or (a.get('dispatch') or {}).get('state') == 'failed']
    merged = [e for e in entries if e.get('merge', {}).get('state') == 'merged']
    conflicts = [e for e in entries if e.get('merge', {}).get('state') == 'conflict']
    ready = [e for e in entries if (e.get('compare') or {}).get('state') == 'ready']
    pending = [e for e in ready if e.get('merge', {}).get('state') != 'merged']
    reqs = data['requirements']
    covered = set()
    for t in tasks:
        covered.update(t['requirements'])
    return {
        'total': len(tasks), **counts,
        'acceptance_total': acc_total, 'acceptance_pass': acc_pass,
        'arms_total': len(arms), 'workers_succeeded': len(succeeded),
        'workers_failed': len(failed), 'merged': len(merged), 'conflicts': len(conflicts),
        'requirements_total': len(reqs), 'requirements_covered': len(covered),
        'compare_ready': len(ready), 'pending_choices': len(pending),
    }


def integrity(data):
    issues = []
    run = data['run']
    for t in data['tasks']:
        tid = t['id']
        status = t['status']
        entry = t['run'] or {}
        merge = (entry.get('merge') or {}).get('state')
        arms = task_arms(entry)
        if status == 'done' and not t['completions']:
            issues.append(('error', tid, '状态 done 但没有完成记录'))
        if status == 'done' and t['acceptance']:
            bad = [a.get('seq') for a in t['acceptance'] if a.get('result') != 'pass']
            if bad:
                issues.append(('error', tid, f'done 但验收未全过: {bad}'))
        if merge == 'merged' and status != 'done':
            issues.append(('error', tid, f'已合并但库状态是 {status}'))
        for agent, arm in arms.items():
            d = arm.get('dispatch') or {}
            g = arm.get('git') or {}
            if d.get('state') == 'failed':
                issues.append(('error', f'{tid}/{agent}',
                               '臂启动失败: ' + str(d.get('error') or '')[:200]))
            if d.get('outcome') == 'failed':
                issues.append(('error', f'{tid}/{agent}', '臂报告失败'))
            if d.get('outcome') == 'succeeded' and not (arm.get('report') or {}).get('status'):
                issues.append(('error', f'{tid}/{agent}',
                               '臂已完成但没有 PO_ARM_REPORT.json（未比对）'))
            extra = g.get('extra_files') or []
            if extra:
                issues.append(('warn', f'{tid}/{agent}',
                               '改动超出登记文件: ' + ', '.join(extra[:8])))
        if (arms and all(arm_finished(a) for a in arms.values())
                and (entry.get('compare') or {}).get('state') not in ('ready', 'chosen')
                and merge != 'merged'):
            issues.append(('warn', tid, '两臂已结束但尚未比对；运行 compare'))
        if merge == 'conflict':
            m = entry.get('merge') or {}
            detail = ', '.join(m.get('conflicts') or []) or str(m.get('error') or '')[:200]
            issues.append(('error', tid, '合并冲突: ' + (detail or '见 run.json')))
    for e in (data.get('validate') or {}).get('errors') or []:
        issues.append(('error', 'validate', e))
    for w in (data.get('validate') or {}).get('warnings') or []:
        issues.append(('warn', 'validate', w))
    return issues


# ---------------------------------------------------------------- 渲染片段

STATUS_META = {
    'done': ('●', '已完成', 'good'),
    'doing': ('◐', '进行中', 'warning'),
    'todo': ('○', '待办', 'neutral'),
    'blocked': ('■', '阻塞', 'critical'),
}
MERGE_META = {
    'merged': ('已合并', 'good'),
    'conflict': ('冲突', 'critical'),
    'failed': ('失败', 'critical'),
    'pending': ('待选择', 'neutral'),
}
COMPARE_META = {
    'ready': ('比对就绪', 'warning'),
    'chosen': ('已采纳', 'good'),
    'pending': ('待比对', 'neutral'),
    'failed': ('比对失败', 'critical'),
}

PROSE_FIELDS = (
    ('goal', '目标'), ('not_responsible', '本任务不负责'),
    ('implementation', '实现要点'), ('boundaries', '边界与异常'), ('notes', '备注'),
)


def badge(text, tone='neutral'):
    return f'<span class="badge tone-{tone}"><span class="badge-dot"></span>{esc(text)}</span>'


def status_badge(status):
    icon, label, tone = STATUS_META.get(status, ('?', status, 'neutral'))
    return f'<span class="badge tone-{tone}"><span class="badge-icon">{icon}</span>{esc(label)}</span>'


def merge_badge(state):
    label, tone = MERGE_META.get(state or 'pending', (state, 'neutral'))
    return f'<span class="badge tone-{tone}"><span class="badge-dot"></span>{esc(label)}</span>'


def compare_badge(state):
    label, tone = COMPARE_META.get(state or 'pending', (state, 'neutral'))
    return f'<span class="badge tone-{tone}"><span class="badge-dot"></span>{esc(label)}</span>'


def prose(text, label):
    if not text:
        return ''
    return f'<div class="prose-block"><h4>{esc(label)}</h4><pre class="prose">{esc(text)}</pre></div>'


def kv(pairs):
    rows = ''.join(f'<div class="kv"><dt>{esc(k)}</dt><dd>{v}</dd></div>' for k, v in pairs if v)
    return f'<dl class="kv-list">{rows}</dl>'


def section(sid, title, body, sub=''):
    sub_html = f'<p class="section-sub">{esc(sub)}</p>' if sub else ''
    return f'<section id="{sid}" class="section"><h2>{esc(title)}</h2>{sub_html}{body}</section>'


def render_kpis(s):
    tiles = [
        ('任务完成', f"{s['done']}/{s['total']}", f"待办 {s['todo']} · 进行中 {s['doing']} · 阻塞 {s['blocked']}", ''),
        ('验收通过', f"{s['acceptance_pass']}/{s['acceptance_total']}",
         '逐条 acceptance 结果', ''),
        ('双 Agent 臂', f"{s['workers_succeeded']}/{s['arms_total']}", f"失败 {s['workers_failed']}", ''),
        ('已采纳', f"{s['merged']}", f"冲突 {s['conflicts']} · 待选择 {s['pending_choices']}", ''),
        ('需求覆盖', f"{s['requirements_covered']}/{s['requirements_total']}", 'R1..Rn 映射到任务', ''),
        ('需人工处理', f"{s['blocked'] + s['conflicts']}", f"阻塞 {s['blocked']} · 冲突 {s['conflicts']}", ''),
    ]
    cells = []
    for label, value, sub, cls in tiles:
        value_cls = 'kpi-value mono' if cls == 'mono' else 'kpi-value'
        cells.append(f'<div class="kpi"><div class="kpi-label">{esc(label)}</div>'
                     f'<div class="{value_cls}">{esc(value)}</div>'
                     f'<div class="kpi-sub">{esc(sub)}</div></div>')
    return f'<div class="kpi-grid">{"".join(cells)}</div>'


def render_statusbar(s):
    total = s['total'] or 1
    order = [('done', '已完成'), ('doing', '进行中'), ('todo', '待办'), ('blocked', '阻塞')]
    segs = []
    for key, label in order:
        n = s[key]
        if n <= 0:
            continue
        pct = n / total * 100
        segs.append(f'<div class="seg seg-{key}" style="flex:{pct:.3f} 1 0" '
                    f'title="{esc(label)} {n}"></div>')
    legend = ''.join(
        f'<span class="legend-item"><span class="legend-dot seg-{key}"></span>'
        f'{esc(label)} <b>{s[key]}</b></span>' for key, label in order)
    return f'<div class="statusbar" role="img" aria-label="任务状态分布">{"".join(segs)}</div>' \
           f'<div class="legend">{legend}</div>'


def render_integrity(issues):
    if not issues:
        return '<p class="ok-line">● 未发现数据一致性问题。</p>'
    rows = []
    for sev, ref, msg in issues:
        tone = 'critical' if sev == 'error' else 'warning'
        icon = '■' if sev == 'error' else '▲'
        rows.append(f'<li class="issue tone-{tone}"><span class="issue-icon">{icon}</span>'
                    f'<span class="issue-ref">{esc(ref)}</span><span>{esc(msg)}</span></li>')
    return f'<ul class="issues">{"".join(rows)}</ul>'


def render_context(data):
    j = data['jira_row']
    blocks = []
    if j.get('project_brief'):
        blocks.append(prose(j['project_brief'], '项目 Brief（含结构设计）'))
    if j.get('baseline'):
        blocks.append(prose(j['baseline'], '基线约束'))
    if j.get('out_of_scope'):
        blocks.append(prose(j['out_of_scope'], '范围外'))
    if j.get('design_notes'):
        blocks.append(prose(j['design_notes'], '设计稿说明'))
    if data['design_drafts']:
        items = ''.join(f'<li><code>{esc(d.get("path"))}</code>'
                        + (f' — 覆盖 {esc(d.get("covers"))}' if d.get('covers') else '')
                        + (f'<br><span class="muted">{esc(d.get("note"))}</span>' if d.get('note') else '')
                        + '</li>' for d in data['design_drafts'])
        blocks.append(f'<div class="prose-block"><h4>设计稿</h4><ul>{items}</ul></div>')
    if not blocks:
        return '<p class="muted">该需求未登记项目上下文。</p>'
    return ''.join(blocks)


def render_batches(data):
    run = data['run']
    plan = data['plan']
    batches = run.get('batches') or []
    rows = []
    if batches:
        for b in batches:
            tasks = ' '.join(f'<a class="task-link" href="#task-{esc(t)}">{esc(t)}</a>'
                             for t in (b.get('tasks') or []))
            rows.append(f'<tr><td>{esc(b.get("index"))}</td><td>{tasks}</td>'
                        f'<td>{esc(b.get("reason") or "")}</td>'
                        f'<td class="mono">{esc((b.get("created_at") or "").replace("T", " "))}</td></tr>')
    plan_html = ''
    if plan and plan.get('preview'):
        chips = ''.join(
            f'<div class="wave"><span class="wave-label">Wave {esc(w.get("wave"))}</span>'
            + ' '.join(f'<span class="chip">{esc(t)}</span>' for t in w.get('tasks') or [])
            + '</div>' for w in plan['preview'])
        plan_html = f'<h3>全量拓扑预览</h3><div class="waves">{chips}</div>'
    if not rows and not plan_html:
        return '<p class="muted">还没有派发批次。</p>'
    table = ''
    if rows:
        table = ('<h3>已派发批次</h3><div class="table-wrap"><table><thead><tr>'
                 '<th>Batch</th><th>任务</th><th>拆分原因</th><th>派发时间</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table></div>')
    return plan_html + table


def render_acceptance(task):
    acc = task['acceptance']
    if not acc:
        return '<p class="muted">未登记验收项。</p>'
    rows = []
    for a in acc:
        result = a.get('result') or 'pending'
        tone = {'pass': 'good', 'fail': 'critical', 'blocked': 'warning'}.get(result, 'neutral')
        rows.append(
            f'<tr><td class="mono">{esc(a.get("seq"))}</td><td class="mono">{esc(a.get("id"))}</td>'
            f'<td>{esc(a.get("kind") or "")}</td><td>{esc(a.get("descr"))}</td>'
            f'<td>{badge(result, tone)}</td><td class="mono muted">{esc(a.get("via") or "")}</td></tr>')
    return ('<div class="table-wrap"><table class="acc-table"><thead><tr><th>seq</th>'
            '<th>acceptance.id</th><th>kind</th><th>验收内容</th><th>结果</th><th>via</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render_files(task):
    if not task['files']:
        return '<p class="muted">未登记文件清单（file_unknown）。</p>'
    items = []
    for f in task['files']:
        change = f.get('change') or ''
        note = f.get('note') or ''
        items.append(f'<li><code>{esc(f.get("path"))}</code> <span class="muted">{esc(change)}</span>'
                     + (f' — {esc(note)}' if note else '') + '</li>')
    return f'<ul class="file-list">{"".join(items)}</ul>'


def render_events(events):
    if not events:
        return '<p class="muted">无进度事件。</p>'
    rows = []
    for e in events:
        rows.append(f'<li><span class="mono muted">{esc(e.get("ts"))}</span> '
                    f'<span class="chip chip-small">{esc(e.get("kind"))}</span> '
                    f'{esc(e.get("message"))}</li>')
    return f'<ul class="timeline">{"".join(rows)}</ul>'


def render_worker(entry):
    arms = task_arms(entry)
    if not arms:
        return '<p class="muted">本 run 未派发该任务。</p>'
    blocks = []
    for agent, arm in arms.items():
        d = arm.get('dispatch') or {}
        g = arm.get('git') or {}
        rep = arm.get('report') or {}
        ver = arm.get('verification') or rep.get('verifications') or []
        acc = rep.get('acceptance') or []
        acc_pass = sum(1 for a in acc if a.get('result') == 'pass')
        effect = (rep.get('effect') or {}).get('summary') or ''
        shots = (f"{g.get('effect_screenshots') or 0}/{g.get('effect_total') or 0}"
                 if g.get('effect_total') else '')
        pairs = [
            ('dispatch', f'<code>{esc(d.get("dispatch_id"))}</code>' if d.get('dispatch_id') else ''),
            ('state', esc(d.get('state'))),
            ('outcome', esc(d.get('outcome'))),
            ('worktree', f'<code class="path">{esc(d.get("worktree_path"))}</code>' if d.get('worktree_path') else ''),
            ('port', esc(d.get('port'))),
            ('报告状态', esc(rep.get('status'))),
            ('代码 diff', f"+{esc(g.get('insertions') or 0)}/-{esc(g.get('deletions') or 0)}"
                         f" · {len(g.get('changed_files') or [])} 文件"),
            ('验收', f"{acc_pass}/{len(acc)}" if acc else ''),
            ('验证', '; '.join(f"{v.get('cmd')}=rc{v.get('rc')}" for v in ver) if ver else ''),
            ('效果', esc(effect)),
            ('截图', esc(shots)),
            ('耗时', esc(elapsed(d))),
        ]
        extra = g.get('extra_files') or []
        extra_html = (f'<p class="issue tone-warning"><span class="issue-icon">▲</span>'
                      f'超出登记文件：{esc(", ".join(extra))}</p>' if extra else '')
        blocks.append(f'<div class="prose-block"><h4>{esc(agent)} 臂</h4>'
                      f'{kv(pairs)}{extra_html}</div>')
    cmp = entry.get('compare') or {}
    m = entry.get('merge') or {}
    rec = cmp.get('recommendation') or {}
    head = f'<p>比对：{compare_badge(cmp.get("state"))}'
    if rec.get('winner'):
        head += f' · 机器建议 <b>{esc(rec.get("winner"))}</b>（{esc(rec.get("verdict"))}）'
    head += f' · 采纳：{merge_badge(m.get("state"))}'
    if m.get('arm'):
        head += f' <code>{esc(m.get("arm"))}</code>'
    if m.get('merge_sha'):
        head += f' <code>{esc(str(m.get("merge_sha"))[:12])}</code>'
    head += '</p>'
    if cmp.get('state') == 'ready' and m.get('state') != 'merged':
        head += (f'<p class="muted">比对报告：<code>{esc(cmp.get("markdown_path"))}</code>'
                 '（选择后才会提交并合入集成分支）</p>')
    return head + ''.join(blocks)


def elapsed(d):
    try:
        from datetime import datetime
        a = datetime.fromisoformat(str(d.get('started_at') or d.get('dispatched_at') or ''))
        b = datetime.fromisoformat(str(d.get('done_at') or ''))
        secs = int((b - a).total_seconds())
        if secs < 0:
            return ''
        return f'{secs // 60} 分 {secs % 60} 秒'
    except Exception:
        return ''


def render_task(task):
    tid = task['id']
    entry = task['run'] or {}
    merge = (entry.get('merge') or {}).get('state')
    acc = task['acceptance']
    acc_pass = sum(1 for a in acc if a.get('result') == 'pass')
    meta = [f'端 {task["end"]}' if task.get('end') else '',
            f'估时 {task["estimate"]}' if task.get('estimate') else '',
            f'验收 {acc_pass}/{len(acc)}' if acc else '无验收项',
            f'依赖 {", ".join(task["depends_on"])}' if task['depends_on'] else '无依赖']
    cmp = (entry.get('compare') or {}).get('state')
    if cmp:
        meta.append(f'比对 {cmp}')
    if merge:
        meta.append(f'merge {merge}')
    head = (f'<summary><span class="task-id">{esc(tid)}</span>'
            f'<span class="task-title">{esc(task["title"])}</span>'
            f'{status_badge(task["status"])}'
            f'{merge_badge(merge) if merge else ""}'
            f'<span class="task-meta">{esc(" · ".join(meta))}</span></summary>')
    prose_html = ''.join(prose(task[k], label) for k, label in PROSE_FIELDS)
    deps = ', '.join(task['depends_on']) or '无'
    reqs = ', '.join(task['requirements']) or '—'
    ver = ''.join(f'<li><code>{esc(c)}</code></li>' for c in task['verifications']) or '<li class="muted">未登记</li>'
    completions = ''.join(
        f'<li><span class="mono muted">{esc(c.get("date"))}</span> {esc(c.get("evidence"))}'
        + (f'<br><span class="muted">偏差：{esc(c.get("deviations"))}</span>' if c.get('deviations') else '')
        + '</li>' for c in task['completions']) or '<li class="muted">无完成记录</li>'
    body = f"""
    {prose_html}
    {kv([('依赖', esc(deps)), ('覆盖需求', esc(reqs)),
         ('里程碑', esc(task.get('milestone'))), ('设计锚点', esc(task.get('design_ref')))])}
    <div class="grid-2">
      <div><h4>涉及文件</h4>{render_files(task)}</div>
      <div><h4>验证命令</h4><ul class="file-list">{ver}</ul></div>
    </div>
    <h4>验收标准</h4>{render_acceptance(task)}
    <h4>完成记录</h4><ul class="timeline">{completions}</ul>
    <h4>双 Agent 与比对</h4>{render_worker(entry)}
    <h4>进度事件</h4>{render_events(task['events'])}
    """
    return f'<details class="task" id="task-{esc(tid)}"{" open" if task["status"] == "blocked" else ""}>{head}<div class="task-body">{body}</div></details>'


def render_tasks(data):
    if not data['tasks']:
        return '<p class="muted">该需求没有任务。</p>'
    cards = ''.join(render_task(t) for t in data['tasks'])
    return f'<div class="task-list">{cards}</div>'


def render_requirements(data):
    reqs = data['requirements']
    if not reqs:
        return '<p class="muted">该需求未登记需求条目。</p>'
    coverage = {}
    for t in data['tasks']:
        for r in t['requirements']:
            coverage.setdefault(r, []).append(t['id'])
    rows = []
    for r in reqs:
        rid = r.get('id')
        cov = coverage.get(rid, [])
        chips = ' '.join(f'<a class="task-link" href="#task-{esc(x)}">{esc(x)}</a>' for x in cov) or '<span class="muted">未覆盖</span>'
        rows.append(f'<tr><td class="mono">{esc(rid)}</td><td>{esc(r.get("title"))}</td>'
                    f'<td>{esc(r.get("priority") or "must")}</td>'
                    f'<td>{esc(r.get("acceptance"))}</td><td>{chips}</td></tr>')
    return ('<div class="table-wrap"><table><thead><tr><th>ID</th><th>需求</th><th>优先级</th>'
            f'<th>验收</th><th>覆盖任务</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render_contracts(data):
    contracts = data['contracts']
    if not contracts:
        return '<p class="muted">未登记接口契约。</p>'
    fields = data['contract_fields']
    rows = []
    for c in contracts:
        fs = [f'{f.get("name")}:{f.get("type") or "?"}'
              + ('*' if f.get('required') else '')
              + (f'[{f.get("enum_values")}]' if f.get('enum_values') else '')
              for f in fields if f.get('contract_id') == c.get('id')]
        rows.append(f'<tr><td class="mono">{esc(c.get("id"))}</td><td class="mono">{esc(c.get("method"))}</td>'
                    f'<td class="mono">{esc(c.get("path"))}</td><td>{badge(c.get("status") or "", "neutral")}</td>'
                    f'<td>{esc(", ".join(fs))}</td></tr>')
    return ('<div class="table-wrap"><table><thead><tr><th>ID</th><th>方法</th><th>路径</th>'
            f'<th>状态</th><th>字段</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render_decisions_risks(data):
    parts = []
    if data['decisions']:
        items = ''.join(
            f'<li><span class="chip chip-small">{"已定" if d.get("conclusion") else "待确认"}</span> '
            f'{esc(d.get("question"))}'
            + (f'<br><span class="muted">→ {esc(d.get("conclusion"))}</span>' if d.get('conclusion') else
               f'<br><span class="muted">选项：{esc(d.get("options"))}</span>' if d.get('options') else '')
            + '</li>' for d in data['decisions'])
        parts.append(f'<h3>决策与待确认</h3><ul class="timeline">{items}</ul>')
    if data['risks']:
        items = ''.join(f'<li>{esc(r.get("descr"))}'
                        + (f'<br><span class="muted">{esc(r.get("note"))}</span>' if r.get('note') else '')
                        + '</li>' for r in data['risks'])
        parts.append(f'<h3>风险与约束</h3><ul class="timeline">{items}</ul>')
    if data['i18n_keys']:
        items = ''.join(f'<li><code>{esc(k.get("key"))}</code> = {esc(k.get("value"))}</li>'
                        for k in data['i18n_keys'])
        parts.append(f'<h3>多语言 Key</h3><ul class="timeline">{items}</ul>')
    if data['tracking_points']:
        items = ''.join(f'<li><span class="chip chip-small">{esc(t.get("event"))}</span> '
                        f'{esc(t.get("element"))} {esc(t.get("api"))} {esc(t.get("note"))}</li>'
                        for t in data['tracking_points'])
        parts.append(f'<h3>埋点</h3><ul class="timeline">{items}</ul>')
    return ''.join(parts) or '<p class="muted">未登记决策、风险或埋点。</p>'


def render_validate(data):
    v = data.get('validate') or {}
    errs, warns = v.get('errors') or [], v.get('warnings') or []
    head = (f'<p><b>{len(errs)}</b> 个错误，<b>{len(warns)}</b> 个警告'
            '（validate 无 --root，只校验记录一致性）。</p>')
    items = ''.join(f'<li class="issue tone-critical"><span class="issue-icon">■</span>{esc(e)}</li>' for e in errs)
    items += ''.join(f'<li class="issue tone-warning"><span class="issue-icon">▲</span>{esc(w)}</li>' for w in warns)
    return head + (f'<ul class="issues">{items}</ul>' if items else '')


def render_comparison(data):
    tasks = [t for t in data['tasks'] if task_arms(t['run'] or {})]
    if not tasks:
        return '<p class="muted">还没有双 agent 派发记录。</p>'
    run_dir = (data['run'] or {}).get('run_dir') or '<RUN_DIR>'
    blocks = []
    for t in tasks:
        entry = t['run'] or {}
        arms = task_arms(entry)
        cmp = entry.get('compare') or {}
        rec = cmp.get('recommendation') or {}
        merge = entry.get('merge') or {}
        rows = []
        for agent, arm in arms.items():
            g = arm.get('git') or {}
            rep = arm.get('report') or {}
            acc = rep.get('acceptance') or []
            acc_pass = sum(1 for a in acc if a.get('result') == 'pass')
            ver = arm.get('verification') or rep.get('verifications') or []
            ver_text = '; '.join(f"{v.get('cmd')}=rc{v.get('rc')}" for v in ver) or '—'
            effect = (rep.get('effect') or {}).get('summary') or '—'
            shots = (f"{g.get('effect_screenshots') or 0}/{g.get('effect_total') or 0} 存在"
                     if g.get('effect_total') else '—')
            patch = (cmp.get('patches') or {}).get(agent) or '—'
            rows.append(
                f'<tr><td><b>{esc(agent)}</b></td>'
                f'<td>{esc(rep.get("status") or "—")}</td>'
                f'<td class="mono">{len(g.get("changed_files") or [])} 文件 · '
                f'+{esc(g.get("insertions") or 0)}/-{esc(g.get("deletions") or 0)}</td>'
                f'<td class="mono">{acc_pass}/{len(t["acceptance"])}</td>'
                f'<td class="mono">{esc(ver_text)}</td>'
                f'<td>{esc(effect)}</td>'
                f'<td class="mono">{esc(shots)}</td>'
                f'<td class="mono path">{esc(patch)}</td></tr>')
        head = (f'<h3>{esc(t["id"])} {esc(t["title"])} {compare_badge(cmp.get("state"))} '
                f'{merge_badge(merge.get("state"))}'
                + (f' <span class="muted">建议 {esc(rec.get("winner"))}</span>'
                   if rec.get('winner') else ''))
        head += '</h3>'
        table = ('<div class="table-wrap"><table><thead><tr><th>臂</th><th>报告状态</th>'
                 '<th>代码 diff</th><th>验收</th><th>验证</th><th>效果</th><th>截图</th>'
                 f'<th>patch</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
        cmd = ''
        if cmp.get('state') == 'ready' and merge.get('state') != 'merged':
            cmd = (f'<p class="muted">选择：<code>po.py choose --run-dir {esc(run_dir)} '
                   f'--task {esc(t["id"])} --arm {"|".join(esc(a) for a in arms)}</code>'
                   '（选择前不提交、不合并）</p>')
        blocks.append(head + table + cmd)
    return ''.join(blocks)


def render_next(data):
    run = data['run']
    branch = (run.get('integration') or {}).get('branch') or '—'
    root = run.get('root') or '<业务仓库>'
    run_dir = run.get('run_dir') or '<RUN_DIR>'
    lines = [
        f'<p>集成分支：<code>{esc(branch)}</code>（只包含已 choose 的任务；未 push、未合并回用户分支）。</p>',
        f'<p>确认后合并：<code>git -C {esc(root)} merge --no-ff {esc(branch)}</code></p>',
    ]
    notes = []
    for t in data['tasks']:
        entry = t['run'] or {}
        m = entry.get('merge') or {}
        cmp = entry.get('compare') or {}
        if cmp.get('state') == 'ready' and m.get('state') != 'merged':
            notes.append(f'<li>{esc(t["id"])} 双 agent 比对已就绪，等待人工选择：'
                         f'<code>po.py choose --run-dir {esc(run_dir)} --task {esc(t["id"])} '
                         f'--arm {"|".join(esc(a) for a in task_arms(entry))}</code></li>')
        if m.get('state') == 'conflict':
            notes.append(f'<li>{esc(t["id"])} 采纳时 patch 应用冲突，已回滚集成分支；'
                         '需人工处理后再 choose。</li>')
        for agent, arm in task_arms(entry).items():
            d = arm.get('dispatch') or {}
            if d.get('state') == 'failed':
                notes.append(f'<li>{esc(t["id"])}/{esc(agent)} 臂启动失败：'
                             f'{esc(d.get("error") or "")}；确认后用 '
                             f'<code>po.py dispatch --retry {esc(t["id"])}</code> 重跑两臂。</li>')
        if t['status'] == 'blocked' and m.get('state') != 'conflict':
            notes.append(f'<li>{esc(t["id"])} 处于 blocked：查看下方进度事件与备注，解除后 resume。</li>')
    if notes:
        lines.append('<h3>需要处理</h3><ul class="timeline">' + ''.join(notes) + '</ul>')
    return ''.join(lines)


def render_html(data):
    run = data['run']
    meta = [f"需求 {data['jira']}", f"范围 {data['jira_row'].get('scope') or '—'}",
            f"run {run.get('run_id') or '—'}",
            f"arms {','.join(run.get('options', {}).get('arms') or ['codex', 'claude'])}",
            f"并发 {run.get('options', {}).get('max_parallel') or '—'}",
            f"base {str(run.get('base_sha') or '')[:10]}",
            f"集成 {str((run.get('integration') or {}).get('head_sha') or '')[:10]}"]
    chips = ''.join(f'<span class="meta-chip">{esc(m)}</span>' for m in meta)
    body = f"""
    <header class="hero">
      <div class="hero-top">
        <div>
          <p class="eyebrow">prd-orchestrate · 并行执行报告</p>
          <h1>{esc(data['jira'])} {esc(data['title'])}</h1>
          <div class="meta-row">{chips}</div>
        </div>
        <div class="hero-actions no-print">
          <button type="button" id="theme-toggle" class="btn">切换主题</button>
          <button type="button" onclick="window.print()" class="btn">打印</button>
        </div>
      </div>
      <p class="hero-note">生成于 <span class="mono">{esc(data.get('generated_at') or '')}</span>。
      状态色始终配合文字/图标，不只靠颜色表达。</p>
    </header>
    <main>
      {render_kpis(data['stats'])}
      {section('status', '任务状态', render_statusbar(data['stats']))}
      {section('compare', '双 Agent 比对', render_comparison(data))}
      {section('integrity', '数据一致性', render_integrity(data['integrity']))}
      {section('context', '项目上下文', render_context(data))}
      {section('batches', '波次与批次', render_batches(data))}
      {section('tasks', '任务看板', render_tasks(data))}
      {section('requirements', '需求覆盖', render_requirements(data))}
      {section('contracts', '接口契约', render_contracts(data))}
      {section('notes', '决策 · 风险 · 埋点', render_decisions_risks(data))}
      {section('validate', '库校验', render_validate(data))}
      {section('next', '合并与下一步', render_next(data))}
    </main>
    <footer class="footer">prd-orchestrate · 单文件离线报告 · 无外部资源</footer>
    """
    template = TEMPLATE.read_text(encoding='utf-8')
    return (template
            .replace('__TITLE__', esc(f"{data['jira']} {data['title']} · 并行执行报告"))
            .replace('__BODY__', body))


def generate(jira, db, run_dir, out_dir=None, full=False):
    data = collect(jira, db, run_dir, full=full)
    from datetime import datetime
    data['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    html_text = render_html(data)
    out = Path(out_dir) if out_dir else (REPORTS_ROOT / jira)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    html_path = out / f'{stamp}.html'
    html_path.write_text(html_text, encoding='utf-8')
    latest = out / 'latest.html'
    latest.write_text(html_text, encoding='utf-8')
    snap = out / f'{stamp}.json'
    snap.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    latest_snap = out / 'latest.json'
    latest_snap.write_text(snap.read_text(encoding='utf-8'), encoding='utf-8')
    return {'html': str(latest), 'snapshot': str(latest_snap), 'generated_at': data['generated_at']}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='生成 prd-orchestrate HTML 报告')
    ap.add_argument('--jira', required=True)
    ap.add_argument('--db', default=str(Path.home() / '.prd-to-breakdown' / 'prd.db'))
    ap.add_argument('--run-dir', required=True)
    ap.add_argument('--out-dir')
    ap.add_argument('--full', action='store_true')
    a = ap.parse_args()
    print(generate(a.jira, a.db, a.run_dir, out_dir=a.out_dir, full=a.full)['html'])
