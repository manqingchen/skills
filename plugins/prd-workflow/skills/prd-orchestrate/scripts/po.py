#!/usr/bin/env python3
"""po.py — prd-orchestrate 协调者 CLI

在业务仓库根目录运行，把 prd-to-breakdown 状态库里依赖已就绪的任务
按「每条任务两臂」并行派发给 Codex 与 Claude Code；两臂在各自 worktree
实现、验收并留下 PO_ARM_REPORT.json，**不提交**。协调者对两臂做
「代码 diff / 结果 / 效果」三维比对，用户选定一臂后才由协调者提交并
合入集成分支，最后生成单文件 HTML 报告。

设计约束：
- 只通过 ptb.py 读写状态库，不直接写 SQL；
- worker 不提交、不回写验收，避免两臂互相覆盖；
- 不轮询 Orca：一次唤醒只执行一次 check；
- 只有显式 `choose` 才合并；不自动 push / 建 MR / 合并回用户分支；
- 所有状态写入 run.json（原子写 + flock）。

常用：
    python3 po.py --db ~/.prd-to-breakdown/prd.db preflight --jira OS-43643
    python3 po.py --db ... plan --jira OS-43643 --root . --max-parallel 3
    python3 po.py --db ... init --jira OS-43643 --root .
    python3 po.py --db ... dispatch --run-dir <run>
    python3 po.py --db ... advance --run-dir <run>
    python3 po.py --db ... compare --run-dir <run> [--task T001]
    python3 po.py --db ... choose --run-dir <run> --task T001 --arm codex
    python3 po.py --db ... report --run-dir <run>
"""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = SKILL_DIR.parents[1]
PTB = PLUGIN_ROOT / 'scripts' / 'ptb.py'
DEFAULT_DB = Path(os.environ.get('PTB_DB', Path.home() / '.prd-to-breakdown' / 'prd.db'))
RUNS_ROOT = Path(os.environ.get('PO_RUNS_ROOT', Path.home() / '.prd-to-breakdown' / 'runs'))
REPORTS_ROOT = Path(os.environ.get('PO_REPORTS_ROOT', Path.home() / '.prd-to-breakdown' / 'reports'))

TERMINAL_MERGE = ('merged', 'conflict', 'failed')
STATUS_ICON = {'todo': '○', 'doing': '◐', 'done': '●', 'blocked': '■'}
DEFAULT_ARMS = ('codex', 'claude')
ARM_REPORT = 'PO_ARM_REPORT.json'
ARM_ARTIFACTS = '.po-artifacts'
ARM_EXCLUDES = [f':(exclude){ARM_REPORT}', f':(exclude){ARM_REPORT[:-5]}.md',
                f':(exclude){ARM_ARTIFACTS}/**']


class PoError(RuntimeError):
    pass


# ---------------------------------------------------------------- 基础工具

def now():
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


def say(msg=''):
    print(msg, flush=True)


def warn(msg):
    print('WARN: ' + msg, file=sys.stderr, flush=True)


def die(msg, code=1):
    print('错误: ' + msg, file=sys.stderr)
    sys.exit(code)


def as_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2)


def load_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PoError(f'JSON 解析失败: {e}\n{text[:400]}')


def first(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ''):
            return d[k]
    return default


def result_of(payload):
    if isinstance(payload, dict):
        if 'result' in payload:
            return payload['result']
        if 'data' in payload:
            return payload['data']
    return payload


def sanitize(name):
    return re.sub(r'[^A-Za-z0-9._-]+', '-', str(name)).strip('-') or 'task'


def run_cmd(cmd, cwd=None, timeout=600, input_text=None, env=None):
    try:
        return subprocess.run(
            [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
            input=input_text, env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise PoError(f'命令超时（{timeout}s）: {shlex.join([str(c) for c in cmd])}\n{e}')


def git(cwd, *args, timeout=180):
    return run_cmd(['git', '-C', str(cwd)] + [str(a) for a in args], timeout=timeout)


def git_out(cwd, *args):
    p = git(cwd, *args)
    if p.returncode != 0:
        raise PoError(f'git {" ".join(map(str, args))} 失败: {p.stderr.strip()}')
    return p.stdout.strip()


def path_exists(cwd, rel):
    return (Path(cwd) / rel).exists()


def norm_path(p):
    p = str(p).replace('\\', '/').strip()
    while p.startswith('./'):
        p = p[2:]
    return p.strip('/')


def paths_overlap(a, b):
    """规范化路径重叠：相等，或一方是另一方的目录前缀。"""
    a, b = norm_path(a), norm_path(b)
    if not a or not b:
        return False
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


def files_overlap(files_a, files_b):
    for x in files_a:
        for y in files_b:
            if paths_overlap(x, y):
                return True
    return False


def resolve_orca(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get('ORCA_CLI_COMMAND')
    if env:
        return env
    if shutil.which('orca-dev'):
        return 'orca-dev'
    if sys.platform.startswith('linux') and not os.environ.get('ORCA_TERMINAL'):
        if shutil.which('orca-ide'):
            return 'orca-ide'
    return 'orca'


def run_state_path(jira):
    return RUNS_ROOT / jira / 'current.json'


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------- ptb / orca 包装

class Ptb:
    def __init__(self, db):
        self.db = Path(db).expanduser()

    def run(self, *args, timeout=180, input_text=None):
        cmd = [sys.executable, str(PTB), '--db', str(self.db)] + [str(a) for a in args]
        return run_cmd(cmd, timeout=timeout, input_text=input_text)

    def json(self, *args, allow_rc=(0,), timeout=180):
        p = self.run(*args, timeout=timeout)
        if p.returncode not in allow_rc:
            raise PoError(f'ptb {" ".join(map(str, args))} 失败（rc={p.returncode}）:\n{p.stderr.strip()}\n{p.stdout.strip()}')
        return load_json(p.stdout)

    def text(self, *args):
        p = self.run(*args)
        if p.returncode != 0:
            raise PoError(f'ptb {" ".join(map(str, args))} 失败:\n{p.stderr.strip()}')
        return p.stdout.strip()

    # -- 常用读取
    def task_list(self, jira):
        return self.json('task', 'list', '--jira', jira, '--json')

    def task_show(self, jira, tid):
        return self.json('task', 'show', tid, '--jira', jira)

    def jira_export(self, jira):
        return self.json('export', '--jira', jira)

    def log(self, jira, tid):
        return self.json('log', '--jira', jira, '--task', tid, '--json')

    def validate(self, jira, root=None):
        args = ['validate', '--jira', jira, '--json']
        if root:
            args += ['--root', str(root)]
        # validate 有错误时 rc=1，但 JSON 仍然完整
        return self.json(*args, allow_rc=(0, 1))

    def doctor(self, jira, root):
        return self.json('doctor', '--jira', jira, '--root', str(root), '--json')

    # -- 常用写入
    def set_status(self, jira, tid, status):
        return self.text('task', 'set', tid, '--jira', jira, '--status', status)

    def progress(self, jira, tid, kind, msg):
        return self.text('progress', '--jira', jira, '--task', tid, '--kind', kind, '--msg', msg)

    def block(self, jira, tid, msg):
        self.progress(jira, tid, 'block', msg)

    def append_notes(self, jira, tid, line):
        cur = self.task_show(jira, tid)['task'].get('notes') or ''
        new = (cur.rstrip() + '\n' + line).strip()
        p = self.run('task', 'text', tid, '--jira', jira, '--field', 'notes', '--file', '-',
                     input_text=new)
        if p.returncode != 0:
            raise PoError(f'更新任务备注失败: {p.stderr.strip()}')

    def backup(self):
        return self.text('backup')


class Orca:
    def __init__(self, cmd=None):
        self.cmd = resolve_orca(cmd)

    def run(self, *args, timeout=600):
        return run_cmd([self.cmd] + [str(a) for a in args], timeout=timeout)

    def json(self, *args, timeout=600):
        p = self.run(*args, timeout=timeout)
        if p.returncode != 0:
            raise PoError(f'{self.cmd} {" ".join(map(str, args))} 失败（rc={p.returncode}）:\n'
                          f'{p.stderr.strip()}\n{p.stdout.strip()}')
        return load_json(p.stdout)

    def result(self, *args, **kwargs):
        return result_of(self.json(*args, **kwargs))

    def current_repo(self):
        try:
            r = result_of(self.json('worktree', 'current', '--json'))
            return first(r.get('worktree', r), 'repoId', 'repo_id')
        except Exception as e:
            warn(f'无法获取 Orca repoId: {e}')
            return None

    def run_create(self, objective):
        r = self.result('orchestration', 'run-create', '--objective', objective, '--json')
        rid = first(r, 'runId', 'run_id', 'id')
        if not rid:
            raise PoError(f'run-create 未返回 runId: {as_json(r)}')
        return rid

    def check(self, run_id):
        return self.json('orchestration', 'check', '--run', run_id, '--json')

    def reply(self, message_id, body):
        p = self.run('orchestration', 'reply', '--id', message_id, '--body', body)
        if p.returncode != 0:
            raise PoError(f'reply 失败: {p.stderr.strip() or p.stdout.strip()}')
        return p.stdout

    def worker_start(self, spec, title, repo_id, base_branch, name, agent, setup, run_id, stdout_path, stderr_path):
        cmd = [self.cmd, 'orchestration', 'worker-start',
               '--spec-file', str(spec), '--task-title', title,
               '--worktree', 'new-top-level', '--repo', f'id:{repo_id}',
               '--base-branch', base_branch, '--name', name,
               '--agent', agent, '--run', run_id, '--json']
        if setup:
            cmd += ['--setup', setup]
        with open(stdout_path, 'w', encoding='utf-8') as out, open(stderr_path, 'w', encoding='utf-8') as err:
            proc = subprocess.Popen([str(c) for c in cmd], stdout=out, stderr=err)
            return proc, shlex.join(cmd)

    def worktree_path(self, workspace):
        if not workspace:
            return None
        ws = str(workspace)
        if '::' in ws:
            return ws.split('::', 1)[1]
        if os.path.isabs(ws) and Path(ws).exists():
            return ws
        try:
            r = result_of(self.json('worktree', 'show', '--worktree', f'id:{ws}', '--json'))
            wt = r.get('worktree', r)
            return first(wt, 'path')
        except Exception as e:
            warn(f'无法解析 worktree 路径 {ws}: {e}')
            return None

    def run_use(self, run_id):
        try:
            return self.result('orchestration', 'run-use', '--id', run_id, '--json')
        except Exception as e:
            warn(f'run-use 失败（可忽略）: {e}')
            return None

    def worker_list(self, run_id):
        try:
            r = result_of(self.json('orchestration', 'worker-list', '--run', run_id, '--json'))
            if isinstance(r, list):
                return r
            for k in ('workers', 'items', 'list'):
                if isinstance(r.get(k), list):
                    return r[k]
            return []
        except Exception as e:
            warn(f'worker-list 失败: {e}')
            return []

    def worker_show(self, dispatch_id):
        try:
            return self.result('orchestration', 'worker-show', '--dispatch', dispatch_id, '--json')
        except Exception as e:
            warn(f'worker-show 失败: {e}')
            return {}

    def release(self, dispatch_id):
        try:
            p = self.run('orchestration', 'worker-release', '--dispatch', dispatch_id)
            if p.returncode != 0:
                warn(f'worker-release 失败: {p.stderr.strip()}')
                return {}
            return p.stdout
        except Exception as e:
            warn(f'worker-release 失败（可忽略）: {e}')
            return {}


# ---------------------------------------------------------------- run.json 存储

class RunStore:
    def __init__(self, run_dir):
        self.dir = Path(run_dir).expanduser().resolve()
        self.path = self.dir / 'run.json'
        self.lock_path = self.dir / '.lock'

    def exists(self):
        return self.path.exists()

    def load(self):
        if not self.path.exists():
            raise PoError(f'run.json 不存在: {self.path}')
        return load_json(self.path.read_text(encoding='utf-8'))

    def save(self, data):
        data['updated_at'] = now()
        write_json_atomic(self.path, data)

    @contextlib.contextmanager
    def update(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, 'a+') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                data = self.load()
                yield data
                data['updated_at'] = now()
                write_json_atomic(self.path, data)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def new_run(jira, title, root, db, repo_id, run_id, base_ref, base_sha, integration,
            max_parallel, arms, setup, mode='git'):
    return {
        'schema': 2,
        'jira': jira,
        'title': title,
        'root': str(root),
        'db': str(db),
        'plugin_root': str(PLUGIN_ROOT),
        'orca_cmd': resolve_orca(),
        'repo_id': repo_id,
        'run_id': run_id,
        'objective': f'prd-orchestrate: {jira} {title}',
        'status': 'initialized',
        'created_at': now(),
        'updated_at': now(),
        'base_ref': base_ref,
        'base_sha': base_sha,
        'integration': integration,
        'options': {'max_parallel': max_parallel, 'arms': list(arms), 'setup': setup, 'mode': mode},
        'batches': [],
        'tasks': {},
        'wakes': [],
        'questions': [],
        'preflight': {},
        'report': {},
    }


def arm_entry(agent):
    return {
        'agent': agent,
        'dispatched_at': None,
        'dispatch': {},
        'report': {},
        'git': {},
        'verification': [],
        'notes': [],
    }


def task_entry(title, arms=None):
    return {
        'title': title,
        'dispatched_at': None,
        'arms': {a: arm_entry(a) for a in (arms or ())},
        'compare': {'state': 'pending', 'ready_at': None, 'patches': {},
                    'report_path': None, 'recommendation': {}},
        'merge': {'state': 'pending', 'arm': None, 'merge_sha': None, 'merged_at': None,
                  'conflicts': [], 'error': None},
        'notes': [],
    }


def task_arms(entry):
    return entry.get('arms') or {}


def is_arm_running(arm):
    d = arm.get('dispatch') or {}
    if d.get('state') == 'failed':
        return False
    return bool(d.get('dispatch_id')) and not d.get('outcome')


def is_running(entry):
    return any(is_arm_running(a) for a in task_arms(entry).values())


def arm_finished(arm):
    d = arm.get('dispatch') or {}
    return bool(d.get('outcome')) or d.get('state') == 'failed'


def task_finished(entry):
    arms = task_arms(entry)
    return bool(arms) and all(arm_finished(a) for a in arms.values())


def running_tasks(run):
    return [tid for tid, e in (run.get('tasks') or {}).items() if is_running(e)]


def pending_choices(run):
    out = []
    for tid, e in (run.get('tasks') or {}).items():
        if (e.get('compare') or {}).get('state') == 'ready' \
                and (e.get('merge') or {}).get('state') != 'merged':
            out.append(tid)
    return out


def update_task(run, tid, fn):
    entry = run.setdefault('tasks', {}).setdefault(tid, task_entry(''))
    return fn(entry)


# ---------------------------------------------------------------- 任务采集 / 计划

def collect_tasks(ptb, jira):
    basic = ptb.task_list(jira)
    tasks = {}
    for b in basic:
        tid = b['id']
        info = ptb.task_show(jira, tid)
        t = info['task']
        tasks[tid] = {
            'id': tid,
            'title': t.get('title') or b.get('title') or '',
            'seq': t.get('seq') if t.get('seq') is not None else 9999,
            'status': t.get('status') or b.get('status') or 'todo',
            'end': t.get('end') or b.get('end'),
            'estimate': t.get('estimate') or b.get('estimate'),
            'milestone': t.get('milestone'),
            'design_ref': t.get('design_ref'),
            'parallel': t.get('parallel'),
            'depends_on': info.get('depends_on') or [],
            'files': [norm_path(f['path']) for f in (info.get('files') or []) if f.get('path')],
            'acceptance': info.get('acceptance') or [],
            'verifications': info.get('verifications') or [],
            'completions': info.get('completions') or [],
            'requirements': info.get('requirements') or [],
        }
    return tasks


def dependency_satisfied(dep, tasks, run):
    """依赖：done + 有完成记录；本 run 内完成的任务还要求已合并。"""
    if dep not in tasks:
        return False, f'依赖 {dep} 不存在'
    t = tasks[dep]
    if t['status'] != 'done':
        return False, f'依赖 {dep} 状态 {t["status"]}'
    if not t['completions']:
        return False, f'依赖 {dep} 缺完成记录'
    if dep in (run.get('tasks') or {}):
        state = run['tasks'][dep].get('merge', {}).get('state')
        if state != 'merged':
            return False, f'依赖 {dep} 已 done 但未合并（merge={state}）'
    return True, ''


def task_eligible(tid, tasks, run, include_dispatched=False):
    t = tasks[tid]
    reasons = []
    if t['status'] != 'todo':
        reasons.append(f'状态为 {t["status"]}')
    for dep in t['depends_on']:
        ok, why = dependency_satisfied(dep, tasks, run)
        if not ok:
            reasons.append(why)
    entry = (run.get('tasks') or {}).get(tid)
    if entry:
        arms = task_arms(entry)
        if entry.get('merge', {}).get('state') in TERMINAL_MERGE:
            reasons.append('已结算')
        elif arms:
            failed = [a for a in arms.values()
                      if (a.get('dispatch') or {}).get('state') == 'failed']
            if len(failed) == len(arms):
                reasons.append('两臂启动均失败（不自动重试，可 --retry）')
            elif not include_dispatched:
                reasons.append('已派发（等待两臂完成 / 比对 / 选择）')
    return (not reasons), reasons


def dep_level(tid, tasks, visiting=None):
    visiting = visiting or set()
    if tid in visiting:
        raise PoError(f'依赖成环: {tid}')
    visiting.add(tid)
    pending_deps = [d for d in tasks[tid]['depends_on']
                    if d in tasks and tasks[d]['status'] != 'done']
    if not pending_deps:
        return 1
    return 1 + max(dep_level(d, tasks, set(visiting)) for d in pending_deps)


def make_plan(tasks, run, max_parallel=3, conservative=False):
    eligible = {}
    skipped = {}
    for tid in tasks:
        ok, reasons = task_eligible(tid, tasks, run)
        if ok:
            eligible[tid] = True
        elif tasks[tid]['status'] == 'todo':
            skipped[tid] = reasons

    waves = {}
    for tid in eligible:
        lvl = dep_level(tid, tasks)
        waves.setdefault(lvl, []).append(tid)

    out_waves = []
    for lvl in sorted(waves):
        tids = sorted(waves[lvl], key=lambda x: (tasks[x]['seq'], x))
        batches = []
        for tid in tids:
            files = tasks[tid]['files']
            unknown = not files
            if conservative:
                batches.append({'batch': len(batches) + 1, 'tasks': [tid],
                                'reason': 'conservative: 串行'})
                continue
            if unknown:
                batches.append({'batch': len(batches) + 1, 'tasks': [tid],
                                'reason': 'file_unknown: 文件清单为空，单独成批'})
                continue
            placed = False
            for b in batches:
                if any(tasks[x]['files'] is None for x in b['tasks']):
                    continue
                if len(b['tasks']) >= max_parallel:
                    continue
                if any(not tasks[x]['files'] for x in b['tasks']):
                    continue
                if any(files_overlap(files, tasks[x]['files']) for x in b['tasks']):
                    continue
                b['tasks'].append(tid)
                placed = True
                break
            if not placed:
                batches.append({'batch': len(batches) + 1, 'tasks': [tid],
                                'reason': '依赖已满足'})
        out_waves.append({'wave': lvl, 'batches': batches})

    # 全量预览：所有 todo 的拓扑层级（含尚未就绪），便于用户看整体节奏
    preview_levels = {}
    for tid, t in tasks.items():
        if t['status'] == 'todo':
            preview_levels.setdefault(dep_level(tid, tasks), []).append(tid)
    preview = [{'wave': lvl,
                'tasks': sorted(preview_levels[lvl], key=lambda x: (tasks[x]['seq'], x))}
               for lvl in sorted(preview_levels)]

    stats = {
        'total': len(tasks),
        'todo': sum(1 for t in tasks.values() if t['status'] == 'todo'),
        'done': sum(1 for t in tasks.values() if t['status'] == 'done'),
        'doing': sum(1 for t in tasks.values() if t['status'] == 'doing'),
        'blocked': sum(1 for t in tasks.values() if t['status'] == 'blocked'),
        'eligible': sum(len(b['tasks']) for w in out_waves for b in w['batches']),
        'skipped': len(skipped),
        'file_unknown': sum(1 for t in tasks.values() if not t['files']),
    }
    plan = {
        'jira': run.get('jira'),
        'generated_at': now(),
        'max_parallel': max_parallel,
        'conservative': conservative,
        'waves': out_waves,
        'preview': preview,
        'skipped': skipped,
        'stats': stats,
        'tasks': {
            tid: {
                'title': t['title'], 'status': t['status'], 'seq': t['seq'],
                'depends_on': t['depends_on'], 'files': t['files'],
                'file_unknown': not t['files'], 'requirements': t['requirements'],
                'acceptance_total': len(t['acceptance']),
            } for tid, t in tasks.items()
        },
    }
    return plan


def flatten_batches(plan):
    for w in plan['waves']:
        for b in w['batches']:
            yield w['wave'], b


def first_batch(plan):
    for _, b in flatten_batches(plan):
        if b['tasks']:
            return b
    return None


def print_plan(plan):
    s = plan['stats']
    say(f"计划 {plan['jira']}  候选 {s['eligible']}/{s['todo']}  todo"
        f"（done {s['done']} / doing {s['doing']} / blocked {s['blocked']}）")
    for w in plan['waves']:
        say(f"\nWave {w['wave']}")
        for b in w['batches']:
            say(f"  Batch {b['batch']}: {', '.join(b['tasks'])}  — {b['reason']}")
            for tid in b['tasks']:
                t = plan['tasks'][tid]
                warn_mark = ' [file_unknown]' if t['file_unknown'] else ''
                say(f"    {tid} {t['title']}{warn_mark}")
    if plan['skipped']:
        say('\n未纳入（等待依赖 / 状态不满足）：')
        for tid in sorted(plan['skipped']):
            say(f"  {tid}: {'; '.join(plan['skipped'][tid])}")
    if plan.get('preview'):
        say('\n全量拓扑预览（含尚未就绪）：')
        for w in plan['preview']:
            say(f"  Wave {w['wave']}: {', '.join(w['tasks'])}")


# ---------------------------------------------------------------- worker spec

def md_escape_cell(s):
    return str(s or '').replace('|', '\\|').replace('\n', '<br>')


def render_export_context(export, task):
    jira = (export.get('jira') or {}).get('jira') or {}
    out = []
    if jira:
        out += ['## 项目上下文', '']
        out += [f"- 需求：{jira.get('jira')} {jira.get('title')}（{jira.get('scope')}，{jira.get('status')}）"]
        if jira.get('baseline'):
            out += ['', '### 基线约束', '', jira['baseline']]
        if jira.get('project_brief'):
            out += ['', '### 项目 Brief（含结构设计，若存在以它为准）', '', jira['project_brief']]
        if jira.get('design_notes'):
            out += ['', '### 设计稿说明', '', jira['design_notes']]
        if jira.get('out_of_scope'):
            out += ['', '### 范围外', '', jira['out_of_scope']]

    reqs = {r['id']: r for r in export.get('requirements') or []}
    if task['requirements']:
        out += ['', '## 覆盖需求', '']
        for rid in task['requirements']:
            r = reqs.get(rid, {})
            out.append(f"- {rid} {r.get('title', '')}（{r.get('priority') or 'must'}）")
            if r.get('acceptance'):
                out.append(f"  - 验收：{r['acceptance']}")

    drafts = export.get('design_drafts') or []
    if drafts:
        out += ['', '## 设计稿', '']
        out += [f"- `{d.get('path')}`" + (f"（覆盖 {d.get('covers')}）" if d.get('covers') else '')
                for d in drafts]

    contracts = export.get('contracts') or []
    fields = export.get('contract_fields') or []
    if contracts:
        out += ['', '## 接口契约', '',
                '| ID | 方法 | 路径 | 状态 | 字段 |', '|---|---|---|---|---|']
        for c in contracts:
            fs = [f"{f.get('name')}:{f.get('type') or '?'}" for f in fields
                  if f.get('contract_id') == c.get('id')]
            out.append(f"| {c.get('id')} | {c.get('method') or ''} | {c.get('path') or ''} | "
                       f"{c.get('status')} | {md_escape_cell(', '.join(fs))} |")

    decisions = export.get('decisions') or []
    if decisions:
        out += ['', '## 决策与待确认', '']
        for d in decisions:
            mark = '已定' if d.get('conclusion') else '待确认'
            out.append(f"- [{mark}] {d.get('question')}"
                       + (f" → {d.get('conclusion')}" if d.get('conclusion') else
                          f"（选项：{d.get('options') or '—'}）"))

    risks = export.get('risks') or []
    if risks:
        out += ['', '## 风险与约束', '']
        out += [f"- {r.get('descr') or r.get('description') or ''}"
                + (f"（{r.get('note')}）" if r.get('note') else '') for r in risks]

    i18n = export.get('i18n_keys') or []
    if i18n:
        out += ['', '## 多语言 Key', '']
        out += [f"- `{k.get('key')}` = {k.get('value') or ''}" for k in i18n]

    tracks = export.get('tracking_points') or []
    if tracks:
        out += ['', '## 埋点', '']
        out += [f"- {t.get('event')} {t.get('element') or ''} {t.get('api') or ''}" for t in tracks]
    return '\n'.join(out)


ARM_REPORT_EXAMPLE = """{
  "task": "__TASK__",
  "arm": "__ARM__",
  "status": "done",
  "acceptance": [
    {"id": 12, "seq": 1, "result": "pass", "evidence": "截图 .po-artifacts/__TASK__/__ARM__/a.png；接口返回 200"}
  ],
  "verifications": [
    {"cmd": "pnpm lint", "rc": 0, "summary": "0 errors"}
  ],
  "effect": {
    "summary": "一句话说明实际运行效果",
    "demo": "打开哪个页面、做什么操作、看到什么",
    "screenshots": [".po-artifacts/__TASK__/__ARM__/a.png"]
  },
  "files": ["src/a.ts"],
  "blockers": "",
  "risks": "",
  "notes": ""
}"""


def build_spec(run, tasks, tid, export, port, agent):
    t = tasks[tid]
    card = Ptb(run['db']).text('task', 'show', tid, '--jira', run['jira'], '--md')
    ptb_cmd = f'python3 {shlex.quote(str(PTB))} --db {shlex.quote(str(run["db"]))}'
    jira = run['jira']
    branch = run['integration']['branch']
    int_path = run['integration']['path']
    run_dir = run.get('run_dir') or '<RUN_DIR>'
    report_path = f'{run_dir}/compare/{tid}/{agent}.patch'
    artifacts = f'{ARM_ARTIFACTS}/{tid}/{agent}/'
    report_example = (ARM_REPORT_EXAMPLE
                      .replace('__TASK__', tid).replace('__ARM__', agent))

    acc_rows = []
    for a in t['acceptance']:
        acc_rows.append(f"| {a.get('seq')} | {a.get('id')} | {a.get('kind') or 'functional'} | "
                        f"{md_escape_cell(a.get('descr'))} | {a.get('result') or 'pending'} |")

    ver = '\n'.join(f'- `{c}`' for c in t['verifications']) or '- （任务卡未登记验证命令；按仓库约定执行 lint/tsc/build）'
    file_list = '\n'.join(f'- `{p}`' for p in t['files']) or '- （任务卡未登记文件清单；只改与任务直接相关的文件，并先 `file add` 登记）'
    context = render_export_context(export, t)

    return f"""# {jira} {tid}：{t['title']}

你是本次 PRD 并行执行中的 **{agent} 臂** worker。只做 {tid} 这一条任务，在独立 worktree 中完成实现与验收，把改动留在工作区，并写出比对报告。

同一任务还有另一臂在另一个 worktree 中独立完成。你们互不通信、互不查看、互不修改对方工作区；不要提另一臂，也不要比对。

## 运行上下文

| 项 | 值 |
|---|---|
| 需求 | {jira} |
| 任务 | {tid} {t['title']} |
| 你的臂 | `{agent}` |
| 集成分支 | `{branch}` |
| 集成分支工作树（只读参考） | `{int_path}` |
| 你的工作区 | 当前 worktree 根目录（独立分支，基于集成分支） |
| 状态库 | `{run['db']}` |
| ptb 命令 | `{ptb_cmd}` |
| 独占端口（如需 dev server） | {port} |
| 协调者比对产物（不要写入） | `{report_path}` |

> 当前 worktree 基于 `{branch}` 的派发时 HEAD 拉出。不要切分支、不要 commit、不要 push、不要 merge/rebase、不要操作其他 worktree。

## 工作区铁律

1. 只做 {tid}；不要执行 `ready`、不要领其他任务、不要进入批量模式。
2. 只改下面「涉及文件」列出的路径；确需额外文件，先登记再改：
   `{ptb_cmd} file add --jira {jira} --task {tid} --path <路径> --change <add|modify|delete> --note "原因"`
3. **不要 commit、不要 push、不要 merge/rebase、不要切分支**：改动必须留在工作区，协调者要对比两臂。若工具强制提交，只允许一次本地 commit，并在报告中说明。
4. **不要执行 `acc set` / `complete` / `block` 写库**：两臂共享同一条任务记录，写回会互相覆盖。验收结果、验证结果与效果写入 `{ARM_REPORT}`，由协调者在选定臂后统一回写。
5. 可以用 `start` / `progress` 留痕，消息统一加前缀 `[{agent}]`；遇到阻塞也只记录到报告，不调用 `block`。
6. 不把全局 `validate` / `doctor` / `ready` / `task list` / `index ensure` 当作 Gate：其他任务在其他 worktree，全局校验必然出现缺文件或 residual 噪声。

## 任务卡（ptb task show --md 原文）

{card}

## 涉及文件

{file_list}

## 验收表（只记录，不写库；使用真实 acceptance.id）

| seq | acceptance.id | kind | 验收内容 | 当前库状态 |
|---|---|---|---|---|
{chr(10).join(acc_rows) if acc_rows else '| — | — | — | 任务卡未登记验收项 | — |'}

逐条在 `{ARM_REPORT}` 的 `acceptance[]` 里记录：`id` 用上表的真实 acceptance.id（不是 seq），`result` 只能 `pass | fail | blocked`，`evidence` 写可复核的证据（命令输出、`文件:行`、截图路径）。

## 验证命令

{ver}

## 执行协议

1. **Gate**：读真实代码，核对文件现状、契约状态、设计锚点、基线约束；发现记录与仓库冲突时，用 `file add` / `task text` 更新记录，不要就地偏离。
2. **开工留痕**：
   `{ptb_cmd} start --jira {jira} --task {tid} --msg "[{agent}] Gate 通过，开始实现"`
3. **实现**：遵循仓库 `AGENTS.md` 与既有风格；复用优先；只改登记路径。
4. **验证**：先跑上面的验证命令并记录每条 `cmd` 的退出码与关键输出，再逐条验收并把结果写进报告。
5. **效果留痕**：视觉/交互任务把截图存到 `{artifacts}`（相对 worktree，不提交），在 `effect.screenshots` 列出；`effect.demo` 写清复现步骤，`effect.summary` 一句话说明实际效果。
6. **写比对报告（必须）**：在 worktree 根目录创建 `{ARM_REPORT}`，格式：
```json
{report_example}
```
   - `status`：`done | blocked | failed`；半成品不允许写 `done`。
   - `acceptance` 必须覆盖上表全部验收项；缺失项协调者会按 `blocked` 处理。
   - `files` 填实际改动文件（相对路径），不要包含 `{ARM_REPORT}` 与 `{ARM_ARTIFACTS}/`。
7. **不要提交、不要回写 ptb**。确认 `git status --porcelain` 里的改动都在登记范围内。
8. **并行环境**：
   - 需要 dev server 时使用独占端口 {port}，验收结束必须结束进程；
   - 依赖共享后端/数据库/环境的验收若无法隔离，在报告中记录 `blocked`，禁止与其他 worker 互相污染；
   - 写库遇到 `database is locked`：等待 1–2 秒重试，最多 5 次。
9. **自查命令**（只看本任务，忽略 peer 噪声）：
   `python3 {shlex.quote(str(Path(__file__).resolve().parent / 'po.py'))} check-task --jira {jira} --task {tid} --root .`

## 完成时向协调者报告

```
outcome: succeeded | failed
task: {tid}
arm: {agent}
status: done | blocked | failed
files: <changed files>
verify: <command=rc; ...>
acceptance: <pass>/<total>
effect: <一句话效果 + 截图路径>
blockers: <none | ...>
```

{context}

## 项目 Brief 的结构设计优先级

如果 `project_brief` 含 `## 结构设计` 小节，它比单卡 `implementation` 更上位：状态归谁、抽象落在哪一层，以结构设计为准；发现冲突时更新记录并说明。
"""


# ---------------------------------------------------------------- 双 agent 比对 / 选择

def task_registered_files(tasks, tid):
    return list(tasks[tid]['files'])


def _is_artifact(path):
    p = norm_path(path)
    return p in (ARM_REPORT, ARM_REPORT[:-5] + '.md') or p.startswith(ARM_ARTIFACTS + '/')


def _porcelain_paths(text):
    out = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        p = line[3:].strip()
        if ' -> ' in p:
            p = p.split(' -> ', 1)[1]
        p = p.strip().strip('"')
        if p and not _is_artifact(p):
            out.append(p)
    return out


def normalize_arm_report(raw):
    if not isinstance(raw, dict):
        return {}
    acc = []
    for a in raw.get('acceptance') or []:
        if not isinstance(a, dict):
            continue
        try:
            aid = int(a.get('id'))
        except (TypeError, ValueError):
            continue
        acc.append({'id': aid, 'seq': a.get('seq'),
                    'result': a.get('result') or 'blocked',
                    'evidence': a.get('evidence') or ''})
    vers = []
    for v in raw.get('verifications') or []:
        if isinstance(v, dict):
            vers.append({'cmd': v.get('cmd') or '', 'rc': v.get('rc'),
                         'summary': v.get('summary') or ''})
    eff = raw.get('effect') if isinstance(raw.get('effect'), dict) else {}
    shots = eff.get('screenshots') if isinstance(eff.get('screenshots'), list) else []
    return {
        'status': raw.get('status') or 'unknown',
        'acceptance': acc,
        'verifications': vers,
        'effect': {'summary': eff.get('summary') or '',
                   'demo': eff.get('demo') or '',
                   'screenshots': [str(s) for s in shots]},
        'files': [str(f) for f in (raw.get('files') or [])],
        'blockers': raw.get('blockers') or '',
        'risks': raw.get('risks') or '',
        'notes': raw.get('notes') or '',
    }


def collect_arm(run, tasks, tid, agent):
    """读取某一臂的 worktree：改动、diff、报告。不提交、不写库。"""
    arm = run['tasks'][tid]['arms'][agent]
    d = arm.get('dispatch') or {}
    path = d.get('worktree_path')
    problems = []
    info = {'agent': agent, 'worktree_path': path,
            'base_sha': d.get('base_sha') or run.get('base_sha'),
            'outcome': d.get('outcome')}
    if not path or not Path(path).exists():
        info['problems'] = [f'worktree 不存在: {path}']
        return False, info, info['problems']

    head = git_out(path, 'rev-parse', 'HEAD')
    info['head_sha'] = head
    info['committed'] = head != info['base_sha']
    git(path, 'add', '-A', '-N')  # 让未跟踪文件进入 diff，只改 index 意图，不提交
    status = git(path, 'status', '--porcelain').stdout
    info['dirty'] = bool(status.strip())
    diff_files = [f for f in git_out(path, 'diff', '--name-only', info['base_sha'],
                                     '--', '.', *ARM_EXCLUDES).splitlines() if f]
    changed = sorted(set(diff_files) | set(_porcelain_paths(status)))
    info['changed_files'] = changed
    info['stat'] = git_out(path, 'diff', '--stat', info['base_sha'],
                           '--', '.', *ARM_EXCLUDES).strip()
    insertions = deletions = 0
    for line in git_out(path, 'diff', '--numstat', info['base_sha'],
                        '--', '.', *ARM_EXCLUDES).splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            for idx, key in ((0, 'insertions'), (1, 'deletions')):
                try:
                    if key == 'insertions':
                        insertions += int(parts[idx])
                    else:
                        deletions += int(parts[idx])
                except ValueError:
                    pass
    info['insertions'], info['deletions'] = insertions, deletions
    registered = set(norm_path(x) for x in task_registered_files(tasks, tid))
    info['extra_files'] = [f for f in changed if norm_path(f) not in registered]
    if not changed:
        problems.append('没有产生任何代码改动')

    report_file = Path(path) / ARM_REPORT
    raw = None
    if report_file.is_file():
        try:
            raw = load_json(report_file.read_text(encoding='utf-8'))
        except Exception as e:
            problems.append(f'{ARM_REPORT} 解析失败: {e}')
    else:
        problems.append(f'缺少 {ARM_REPORT}')
    if isinstance(raw, dict):
        if raw.get('task') not in (None, tid):
            problems.append(f'报告 task={raw.get("task")} 与 {tid} 不一致')
        if raw.get('arm') not in (None, agent):
            problems.append(f'报告 arm={raw.get("arm")} 与 {agent} 不一致')
    info['report'] = normalize_arm_report(raw)
    if info['report']:
        shots = info['report']['effect'].get('screenshots') or []
        info['effect_screenshots'] = sum(
            1 for s in shots if (Path(s) if os.path.isabs(s) else Path(path) / s).exists())
        info['effect_total'] = len(shots)
    if info['committed']:
        info.setdefault('notes', []).append('该臂产生了 commit（流程要求留工作区；按工作区 diff 比对）')
    info['problems'] = problems
    return (not problems), info, problems


def write_arm_patch(run_dir, tid, agent, info):
    out_dir = Path(run_dir) / 'compare' / tid
    out_dir.mkdir(parents=True, exist_ok=True)
    patch = out_dir / f'{agent}.patch'
    p = git(info['worktree_path'], 'diff', '--binary', info['base_sha'],
            '--', '.', *ARM_EXCLUDES)
    patch.write_text(p.stdout, encoding='utf-8')
    return str(patch)


def run_arm_verifications(run_dir, tid, agent, info, cmds):
    out_dir = Path(run_dir) / 'compare' / tid
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, cmd in enumerate(cmds, 1):
        log_path = out_dir / f'{agent}-verify-{i}.log'
        try:
            p = run_cmd(['bash', '-lc', cmd], cwd=info['worktree_path'], timeout=600)
            text = (p.stdout or '') + (p.stderr or '')
            rc = p.returncode
        except PoError as e:
            text, rc = str(e), -1
        log_path.write_text(text, encoding='utf-8')
        results.append({'cmd': cmd, 'rc': rc, 'log': str(log_path),
                        'summary': text.strip()[-800:]})
    return results


def arm_gate_problems(tasks, tid, info):
    """该臂能否被采纳：报告状态、验收覆盖与结果、验证命令。"""
    problems = list(info.get('problems') or [])
    report = info.get('report') or {}
    if report:
        if report.get('status') != 'done':
            problems.append(f'报告状态为 {report.get("status")}，不是 done')
        expected = {int(a['id']) for a in tasks[tid]['acceptance']}
        got = {a['id'] for a in report.get('acceptance') or []}
        missing = sorted(expected - got)
        if missing:
            problems.append(f'报告缺少验收项 id={missing}')
        failed = [a['id'] for a in report.get('acceptance') or []
                  if a.get('result') != 'pass']
        if failed:
            problems.append(f'验收未通过 id={failed}')
    vers = info.get('verification') or report.get('verifications') or []
    if vers and any(v.get('rc') not in (0, None) for v in vers):
        problems.append('验证命令未全部通过')
    if not info.get('changed_files'):
        problems.append('没有代码改动')
    return problems


def recommend_arm(tasks, tid, infos):
    gates = {a: arm_gate_problems(tasks, tid, i) for a, i in infos.items()}
    qualified = [a for a, p in gates.items() if not p]
    result = {'winner': None, 'verdict': 'none', 'reasons': [], 'gates': gates}
    if len(qualified) == 2:
        def size(a):
            i = infos[a]
            return (len(i.get('changed_files') or []),
                    (i.get('insertions') or 0) + (i.get('deletions') or 0))
        a, b = qualified
        sa, sb = size(a), size(b)
        if sa == sb:
            result.update(winner='tie', verdict='tie',
                          reasons=['两臂均达标，改动规模相同（文件数与增删行数一致）'])
        else:
            winner = a if sa < sb else b
            result.update(winner=winner, verdict='recommend',
                          reasons=[f'两臂均通过验收与验证；{winner} 改动更小'
                                   f'（{sa[0]} 文件 / {sa[1]} 行 vs {sb[0]} 文件 / {sb[1]} 行）'])
    elif len(qualified) == 1:
        result.update(winner=qualified[0], verdict='recommend',
                      reasons=[f'{qualified[0]} 通过验收与验证，另一臂未达标'])
    else:
        result.update(verdict='none',
                      reasons=['两臂均未通过验收/验证，不能直接采纳；先修问题或重派'])
    return result


def compare_task(run, tasks, tid, run_dir, verify=True):
    """两臂都结束后生成代码 diff / 结果 / 效果三维比对；不合并。"""
    entry = run['tasks'][tid]
    arms = task_arms(entry)
    if not arms or not all(arm_finished(a) for a in arms.values()):
        return None
    if entry.get('merge', {}).get('state') == 'merged':
        return entry.get('compare')

    infos = {}
    for agent in arms:
        ok, info, _ = collect_arm(run, tasks, tid, agent)
        if info.get('worktree_path') and Path(info['worktree_path']).exists():
            info['patch'] = write_arm_patch(run_dir, tid, agent, info)
            if verify:
                info['verification'] = run_arm_verifications(
                    run_dir, tid, agent, info, tasks[tid]['verifications'] or [])
        else:
            info['patch'] = None
        infos[agent] = info
        arm = arms[agent]
        arm['report'] = info.get('report') or {}
        arm['git'] = {k: info.get(k) for k in
                      ('changed_files', 'extra_files', 'stat', 'insertions', 'deletions',
                       'head_sha', 'base_sha', 'committed', 'problems', 'effect_screenshots',
                       'effect_total')}
        arm['verification'] = info.get('verification') or []
        arm['collected_at'] = now()

    rec = recommend_arm(tasks, tid, infos)
    comparison = {
        'task': tid,
        'title': entry.get('title') or tasks[tid]['title'],
        'generated_at': now(),
        'verify_rerun': bool(verify),
        'arms': infos,
        'recommendation': rec,
    }
    out_dir = Path(run_dir) / 'compare' / tid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'compare.json').write_text(as_json(comparison), encoding='utf-8')
    (out_dir / 'report.md').write_text(render_compare_md(tasks, tid, infos, rec), encoding='utf-8')
    entry['compare'] = {
        'state': 'ready', 'ready_at': now(),
        'patches': {a: infos[a]['patch'] for a in infos},
        'report_path': str(out_dir / 'compare.json'),
        'markdown_path': str(out_dir / 'report.md'),
        'recommendation': rec,
    }
    entry.setdefault('notes', []).append(
        f'{now()} 双 agent 比对就绪（{", ".join(infos)}）'
        + (f'，建议 {rec["winner"]}' if rec.get('winner') else ''))
    return entry['compare']


def render_compare_md(tasks, tid, infos, rec):
    agents = list(infos)

    def acc_text(i):
        acc = ((i.get('report') or {}).get('acceptance') or [])
        passed = sum(1 for a in acc if a.get('result') == 'pass')
        return f"{passed}/{len(tasks[tid]['acceptance'])}"

    def ver_text(i):
        vers = i.get('verification') or (i.get('report') or {}).get('verifications') or []
        return '; '.join(f"{v.get('cmd')}=rc{v.get('rc')}" for v in vers) or '—'

    def effect_text(i):
        return ((i.get('report') or {}).get('effect') or {}).get('summary') or '—'

    def shot_text(i):
        if not i.get('effect_total'):
            return '—'
        return f"{i.get('effect_screenshots', 0)}/{i.get('effect_total', 0)} 存在"

    head = ['# ' + tid + ' ' + (tasks[tid].get('title') or '') + ' · 双 agent 比对', '']
    head += ['| 维度 | ' + ' | '.join(agents) + ' |', '|---|' + '---|' * len(agents)]

    def row(label, fn):
        return '| ' + label + ' | ' + ' | '.join(fn(infos[a]) for a in agents) + ' |'

    lines = list(head)
    lines.append(row('状态', lambda i: str((i.get('report') or {}).get('status') or '—')
                     + (' / ' + str(i.get('outcome')) if i.get('outcome') else '')))
    lines.append(row('代码 diff', lambda i: f"{len(i.get('changed_files') or [])} 文件，"
                     f"+{i.get('insertions') or 0}/-{i.get('deletions') or 0}"))
    lines.append(row('改动文件', lambda i: ', '.join((i.get('changed_files') or [])[:8]) or '—'))
    lines.append(row('验收', acc_text))
    lines.append(row('验证', ver_text))
    lines.append(row('效果', effect_text))
    lines.append(row('截图', shot_text))
    lines.append(row('patch', lambda i: i.get('patch') or '—'))
    lines += ['', '## 机器建议', '',
              f"- 结论：**{rec.get('winner') or '无'}**（{rec.get('verdict')}）"]
    lines += [f'- {r}' for r in rec.get('reasons') or []]
    for a in agents:
        problems = rec['gates'].get(a) or []
        if problems:
            lines.append(f'- {a} 未达标：' + '；'.join(problems))
    lines += ['', '## 下一步', '',
              f'```bash\npo.py choose --run-dir <RUN_DIR> --task {tid} --arm <{"/".join(agents)}>\n```',
              '选择前不会提交、合并或回写 ptb。']
    return '\n'.join(lines) + '\n'


def block_task(ptb, run, tid, reason, merge_state=None):
    jira = run['jira']
    entry = run.setdefault('tasks', {}).setdefault(tid, task_entry(''))
    try:
        ptb.block(jira, tid, reason)
    except Exception as e:
        warn(f'置 blocked 失败 {tid}: {e}')
    try:
        ptb.append_notes(jira, tid, f"[prd-orchestrate] {now()} {reason}")
    except Exception as e:
        warn(f'追加备注失败 {tid}: {e}')
    if merge_state:
        entry.setdefault('merge', {})['state'] = merge_state
    entry.setdefault('notes', []).append(f'{now()} {reason}')


def release_arms(run, tid, orca):
    if orca is None:
        return
    for agent, arm in task_arms(run['tasks'][tid]).items():
        did = (arm.get('dispatch') or {}).get('dispatch_id')
        if did:
            try:
                orca.release(did)
                say(f'{tid}/{agent}: 已释放 worker 终端 {did}')
            except Exception as e:  # 释放失败不影响已完成的合并
                warn(f'{tid}/{agent}: 释放终端 {did} 失败: {e}')


def choose_task(run, tasks, tid, agent, ptb, orca=None, dry_run=False, force=False):
    """用户选定一臂：校验 → 应用 patch → 集成分支提交 → 回写 ptb。"""
    entry = run['tasks'][tid]
    merge = entry.setdefault('merge', {'state': 'pending'})
    if merge.get('state') == 'merged':
        raise PoError(f'{tid} 已合并（arm={merge.get("arm")}）')
    arms = task_arms(entry)
    if agent not in arms:
        raise PoError(f'{tid} 没有臂 {agent}；可选：{", ".join(arms)}')
    cmp = entry.get('compare') or {}
    if cmp.get('state') != 'ready':
        raise PoError(f'{tid} 尚未完成比对；先运行 compare/advance')

    ok, info, _ = collect_arm(run, tasks, tid, agent)
    gates = arm_gate_problems(tasks, tid, info)
    if gates and not force:
        raise PoError(f'{agent} 臂未达标，不能合并：' + '；'.join(gates)
                      + '（确需强行采纳加 --force）')

    report = info.get('report') or {}
    by_id = {a['id']: a for a in report.get('acceptance') or []}
    results = []
    for a in tasks[tid]['acceptance']:
        rec = by_id.get(int(a['id']))
        results.append({'id': int(a['id']), 'result': (rec or {}).get('result') or 'blocked',
                        'evidence': (rec or {}).get('evidence') or ''})
    not_pass = [r for r in results if r['result'] != 'pass']
    if not_pass and not force:
        raise PoError(f'{agent} 臂有 {len(not_pass)} 条验收未通过，不能合并'
                      f'（id={[r["id"] for r in not_pass]}）；确需强行采纳加 --force')

    patch = (cmp.get('patches') or {}).get(agent) or info.get('patch') \
        or write_arm_patch(run.get('run_dir') or '.', tid, agent, info)
    if not Path(patch).is_file() or Path(patch).stat().st_size == 0:
        raise PoError(f'{agent} 臂 patch 为空: {patch}')

    int_path = run['integration']['path']
    message = f"feat({run['jira']}): {tid} {entry.get('title') or tasks[tid]['title']} [{agent}]"
    if dry_run:
        say(f'DRY-RUN choose {tid} arm={agent}')
        say(f'  patch: {patch}')
        say(f'  command: git -C {int_path} apply --3way {patch}')
        say(f'  commit: {message}')
        return {'state': 'dry-run', 'arm': agent, 'patch': patch}

    if git(int_path, 'status', '--porcelain').stdout.strip():
        raise PoError(f'集成工作区不干净，拒绝应用 patch: {int_path}')
    head_before = git_out(int_path, 'rev-parse', 'HEAD')
    applied = git(int_path, 'apply', '--3way', '--whitespace=nowarn', patch)
    if applied.returncode != 0:
        git(int_path, 'reset', '--hard', head_before)
        git(int_path, 'clean', '-fd')
        err = (applied.stderr or applied.stdout or '').strip()[:2000]
        merge.update({'state': 'conflict', 'arm': agent, 'merge_sha': None,
                      'merged_at': now(), 'conflicts': [], 'error': err})
        block_task(ptb, run, tid, f'{agent} 臂 patch 应用冲突，需人工处理：{err[:300]}',
                   merge_state='conflict')
        raise PoError(f'{tid} 应用 {agent} 补丁冲突，已回滚集成分支；详见 run.json')
    add = git(int_path, 'add', '-A')
    if add.returncode != 0:
        raise PoError(f'git add 失败: {add.stderr.strip()}')
    commit = git(int_path, 'commit', '-m', message)
    if commit.returncode != 0:
        raise PoError(f'git commit 失败: {(commit.stderr or commit.stdout).strip()}')
    sha = git_out(int_path, 'rev-parse', 'HEAD')

    deviations = report.get('notes') or report.get('risks') or ''
    if not_pass:
        deviations = ((deviations + '；') if deviations else '') + \
            '强行采纳：验收未全过 ' + ','.join(str(r['id']) for r in not_pass)
    evidence_bits = [f'arm={agent}', f'commit={sha[:12]}',
                     f"acc={sum(1 for r in results if r['result'] == 'pass')}/{len(results)}",
                     'files=' + ','.join((info.get('changed_files') or [])[:6])]
    for v in (info.get('verification') or report.get('verifications') or [])[:6]:
        if v.get('cmd'):
            evidence_bits.append(f"{v['cmd']}={v.get('rc')}")
    for r in results:
        ptb.run('acc', 'set', '--jira', run['jira'], '--task', tid,
                '--id', r['id'], '--result', r['result'])
    complete_args = ['complete', '--jira', run['jira'], '--task', tid,
                     '--evidence', '; '.join(evidence_bits),
                     '--deviations', deviations or '无']
    if not_pass:
        complete_args.append('--force')
    done = ptb.run(*complete_args)
    if done.returncode != 0:
        merge.update({'state': 'failed', 'arm': agent, 'merge_sha': sha, 'merged_at': now(),
                      'error': (done.stderr or done.stdout).strip()[:2000]})
        raise PoError(f'{tid} 已合入 {sha[:12]}，但 ptb complete 失败：'
                      f'{(done.stderr or done.stdout).strip()[:600]}')

    merge.update({'state': 'merged', 'arm': agent, 'merge_sha': sha, 'merged_at': now(),
                  'conflicts': [], 'error': None})
    entry['compare']['state'] = 'chosen'
    run['integration']['head_sha'] = sha
    entry.setdefault('notes', []).append(f'{now()} 采纳 {agent} 臂 {sha[:12]}')
    release_arms(run, tid, orca)
    say(f'{tid}: 采纳 {agent} 臂，集成分支 {sha[:12]}')
    return merge


# ---------------------------------------------------------------- 唤醒消息

def find_messages(payload):
    node = result_of(payload)
    if isinstance(node, list):
        return node
    if not isinstance(node, dict):
        return []
    for key in ('check', 'inbox'):
        if isinstance(node.get(key), dict):
            nested = find_messages(node[key])
            if nested:
                return nested
    for key in ('messages', 'items', 'events', 'notifications', 'entries', 'wakes'):
        if isinstance(node.get(key), list):
            return node[key]
    if any(k in node for k in ('type', 'kind', 'event', 'worker_done', 'dispatchId', 'dispatch_id')):
        return [node]
    return []


def msg_type(m):
    t = first(m, 'type', 'kind', 'event', 'messageType', 'message_type', default='') or ''
    t = str(t).lower().replace('-', '_')
    if 'worker_done' in m and not t:
        t = 'worker_done'
    if 'question' in t:
        return 'question'
    if 'escalation' in t:
        return 'escalation'
    if 'done' in t:
        return 'worker_done'
    return t


def msg_dispatch_id(m):
    return first(m, 'dispatchId', 'dispatch_id', 'dispatch', 'workerId', 'worker_id')


def msg_outcome(m):
    return first(m, 'outcome', 'result', 'status')


def normalize_messages(payload):
    out = []
    for m in find_messages(payload):
        if not isinstance(m, dict):
            continue
        out.append({
            'type': msg_type(m),
            'dispatch_id': msg_dispatch_id(m),
            'message_id': first(m, 'id', 'messageId', 'message_id'),
            'outcome': (str(msg_outcome(m)).lower() if msg_outcome(m) is not None else None),
            'question': first(m, 'question', 'body', 'text', 'message'),
            'raw': m,
        })
    return out


def find_task_by_dispatch(run, dispatch_id):
    """返回 (task_id, agent)；兼容旧 run 的单 dispatch 结构。"""
    if not dispatch_id:
        return None, None
    for tid, e in (run.get('tasks') or {}).items():
        for agent, arm in task_arms(e).items():
            if (arm.get('dispatch') or {}).get('dispatch_id') == dispatch_id:
                return tid, agent
        if (e.get('dispatch') or {}).get('dispatch_id') == dispatch_id:
            return tid, None
    return None, None


def apply_message(run, tasks, msg, ptb):
    """处理一条唤醒消息；返回是否需要用户介入。"""
    tid, agent = find_task_by_dispatch(run, msg.get('dispatch_id'))
    mtype = msg['type']
    if mtype == 'question':
        run.setdefault('questions', []).append({
            'at': now(), 'task': tid, 'arm': agent, 'message_id': msg.get('message_id'),
            'question': msg.get('question'), 'answered': False,
        })
        run['status'] = 'waiting'
        return True
    if tid and mtype == 'worker_done':
        entry = run['tasks'][tid]
        if agent and agent in task_arms(entry):
            d = entry['arms'][agent].setdefault('dispatch', {})
        else:
            d = entry.setdefault('dispatch', {})
        d['outcome'] = msg.get('outcome') or 'unknown'
        d['done_at'] = now()
        d['state'] = 'settled'
        if msg.get('outcome') == 'failed':
            entry.setdefault('notes', []).append(
                f'{now()} {agent or "worker"} 臂报告失败（dispatch {msg.get("dispatch_id")}）')
    elif mtype == 'escalation':
        run.setdefault('questions', []).append({
            'at': now(), 'task': tid, 'arm': agent, 'message_id': msg.get('message_id'),
            'question': 'worker escalation: ' + str(msg.get('raw'))[:800], 'answered': False,
        })
        run['status'] = 'waiting'
        return True
    return False


def process_wake(run, tasks, payload, ptb):
    msgs = normalize_messages(payload)
    run.setdefault('wakes', []).append({'at': now(), 'messages': [
        {k: v for k, v in m.items() if k != 'raw'} for m in msgs]})
    need_user = False
    for m in msgs:
        need_user = apply_message(run, tasks, m, ptb) or need_user
    return msgs, need_user


# ---------------------------------------------------------------- 命令实现

def cmd_preflight(args):
    root = Path(args.root).resolve()
    p = git(root, 'rev-parse', '--show-toplevel')
    if p.returncode != 0:
        die(f'{root} 不是 git 仓库')
    git_root = Path(p.stdout.strip()).resolve()
    checks = {'root': str(git_root), 'ok': True, 'problems': [], 'warnings': []}

    dirty = git(git_root, 'status', '--porcelain', '--untracked-files=normal').stdout.strip()
    checks['dirty'] = bool(dirty)
    if dirty:
        checks['ok'] = False
        checks['problems'].append('工作区不干净：' + dirty.replace('\n', '; ')[:500])

    for name, ref in (('merge', 'MERGE_HEAD'), ('rebase', 'REBASE_HEAD'), ('cherry-pick', 'CHERRY_PICK_HEAD')):
        if git(git_root, 'rev-parse', '-q', '--verify', ref).returncode == 0:
            checks['ok'] = False
            checks['problems'].append(f'存在进行中的 {name} 操作')
    git_dir_raw = git(git_root, 'rev-parse', '--git-dir').stdout.strip()
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = (git_root / git_dir).resolve()
    if (git_dir / 'rebase-merge').exists() or (git_dir / 'rebase-apply').exists():
        checks['ok'] = False
        checks['problems'].append('存在进行中的 rebase')
    if (git_dir / 'BISECT_LOG').exists():
        checks['ok'] = False
        checks['problems'].append('存在进行中的 bisect')

    branch = git(git_root, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    checks['branch'] = branch
    base_ref = args.base_ref or branch
    checks['base_ref'] = base_ref
    base_sha = git(git_root, 'rev-parse', base_ref).stdout.strip()
    checks['base_sha'] = base_sha
    origin_ref = f'origin/{branch}'
    if git(git_root, 'rev-parse', '--verify', origin_ref).returncode == 0:
        origin_sha = git(git_root, 'rev-parse', origin_ref).stdout.strip()
        if origin_sha != base_sha:
            checks['warnings'].append(f'{branch} 与 {origin_ref} 不一致，确认基线是否最新')

    ptb = Ptb(args.db)
    try:
        val = ptb.validate(args.jira)
    except PoError as e:
        checks['ok'] = False
        checks['problems'].append(f'validate 失败: {e}')
        val = {'errors': [], 'warnings': []}
    checks['validate'] = {'errors': val.get('errors') or [], 'warnings': val.get('warnings') or []}
    if checks['validate']['errors']:
        checks['ok'] = False
        checks['problems'].append(f'{len(checks["validate"]["errors"])} 个 validate 错误')

    try:
        doc = ptb.doctor(args.jira, git_root)
        checks['doctor'] = {'summary': doc.get('summary') or {},
                            'tasks': [{'task': t.get('task'), 'claimed_status': t.get('claimed_status'),
                                       'suspected_state': t.get('suspected_state')}
                                      for t in (doc.get('tasks') or [])
                                      if t.get('suspected_state') not in ('complete', 'not_started')]}
    except PoError as e:
        checks['warnings'].append(f'doctor 失败: {e}')

    orca = Orca(args.orca)
    checks['repo_id'] = orca.current_repo()
    if not checks['repo_id']:
        checks['warnings'].append('未解析到 Orca repoId；init 前需要在 Orca 终端里运行')

    cur = run_state_path(args.jira)
    if cur.exists():
        try:
            prev = load_json(cur.read_text(encoding='utf-8'))
            if prev.get('status') not in ('complete', 'failed'):
                checks['warnings'].append(f'存在未完成 run: {prev.get("run_dir")}（可用 resume 恢复）')
            checks['previous_run'] = prev
        except Exception as e:
            checks['warnings'].append(f'current.json 无法读取: {e}')

    if args.json:
        say(as_json(checks))
    else:
        say(f"仓库: {checks['root']}")
        say(f"分支: {branch} -> {base_ref} ({base_sha[:12]})")
        say(f"repoId: {checks.get('repo_id') or '—'}")
        say(f"validate: {len(checks['validate']['errors'])} 错误 / {len(checks['validate']['warnings'])} 警告")
        if checks.get('doctor'):
            say(f"doctor: {as_json(checks['doctor']['summary'])}")
        for w in checks['warnings']:
            warn(w)
        for p in checks['problems']:
            say('阻断: ' + p)
        say('预检通过' if checks['ok'] else '预检未通过')
    return 0 if checks['ok'] else 2


def load_context(args):
    ptb = Ptb(args.db)
    run = RunStore(args.run_dir).load()
    tasks = collect_tasks(ptb, run['jira'])
    return ptb, run, tasks


def cmd_plan(args):
    if args.run_dir:
        _, run, tasks = load_context(args)
        jira = run['jira']
        max_parallel = args.max_parallel or run['options'].get('max_parallel', 3)
    else:
        run = {'jira': args.jira, 'tasks': {}, 'jira_title': ''}
        ptb = Ptb(args.db)
        tasks = collect_tasks(ptb, args.jira)
        jira = args.jira
        max_parallel = args.max_parallel or 3
    plan = make_plan(tasks, run, max_parallel, args.conservative)
    if args.out:
        write_json_atomic(args.out, plan)
    if args.json:
        say(as_json(plan))
    else:
        print_plan(plan)
    return 0


def parse_arms(value):
    parts = value if isinstance(value, (list, tuple)) else str(value or '').split(',')
    arms = [str(a).strip() for a in parts if str(a).strip()]
    arms = [a for a in arms if re.fullmatch(r'[A-Za-z0-9._-]+', a)]
    if not arms:
        raise PoError('--arms 至少需要一个 agent（默认 codex,claude）')
    if len(set(arms)) != len(arms):
        raise PoError(f'--arms 不能重复: {", ".join(arms)}')
    return arms


def cmd_init(args):
    root = Path(args.root or os.getcwd()).resolve()
    p = git(root, 'rev-parse', '--show-toplevel')
    if p.returncode != 0:
        die(f'{root} 不是 git 仓库')
    root = Path(p.stdout.strip()).resolve()
    ptb = Ptb(args.db)
    export = ptb.jira_export(args.jira)
    jira_row = (export.get('jira') or {}).get('jira') or {}
    title = jira_row.get('title') or args.jira

    branch = git(root, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    base_ref = args.base_ref or branch
    base_sha = git(root, 'rev-parse', base_ref).stdout.strip()
    run_dir = Path(args.run_dir).resolve() if args.run_dir else (RUNS_ROOT / args.jira / stamp())
    int_branch = args.integration_branch or f'prd/{args.jira}/run-{stamp()}'
    int_path = run_dir / 'integration'
    orca = Orca(args.orca)
    repo_id = args.repo_id or orca.current_repo()

    commands = []
    if args.mode == 'git':
        commands.append(f'git -C {shlex.quote(str(root))} worktree add -b {shlex.quote(int_branch)} '
                        f'{shlex.quote(str(int_path))} {base_sha}')
    else:
        commands.append(f'{orca.cmd} worktree create --name {shlex.quote(args.jira + "-integration")} '
                        f'--repo id:{repo_id} --base-branch {shlex.quote(base_ref)} '
                        f'--branch {shlex.quote(int_branch)} --no-parent --setup skip --json')
    commands.append(f'{orca.cmd} orchestration run-create --objective '
                    f'{shlex.quote("prd-orchestrate: " + args.jira + " " + title)} --json')

    if args.dry_run:
        say('DRY-RUN init:')
        for c in commands:
            say('  ' + c)
        say(f'  run_dir={run_dir}')
        return 0

    if not repo_id:
        die('未解析到 repoId；请在 Orca 终端中运行，或用 --repo-id 指定')
    run_dir.mkdir(parents=True, exist_ok=True)
    for sub in ('specs', 'mail', 'logs'):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    try:
        say('备份状态库: ' + ptb.backup())
    except Exception as e:
        warn(f'备份失败: {e}')

    run_id = orca.run_create(f'prd-orchestrate: {args.jira} {title}')
    integration = {'mode': args.mode, 'branch': int_branch, 'path': str(int_path), 'head_sha': base_sha}
    if args.mode == 'git':
        if int_path.exists() and any(int_path.iterdir()):
            die(f'集成目录非空: {int_path}')
        p = git(root, 'worktree', 'add', '-b', int_branch, str(int_path), base_sha)
        if p.returncode != 0:
            die(f'创建集成工作树失败: {p.stderr.strip()}')
    else:
        r = orca.result('worktree', 'create', '--name', f'{args.jira}-integration',
                        '--repo', f'id:{repo_id}', '--base-branch', base_ref,
                        '--branch', int_branch, '--no-parent', '--setup', 'skip', '--json')
        wt = r.get('worktree', r)
        integration['path'] = first(wt, 'path') or str(int_path)
        integration['branch'] = first(wt, 'branch') or int_branch
        integration['head_sha'] = git_out(integration['path'], 'rev-parse', 'HEAD')

    run = new_run(args.jira, title, root, args.db, repo_id, run_id, base_ref, base_sha,
                  integration, args.max_parallel, parse_arms(args.arms), args.setup, args.mode)
    run['orca_cmd'] = orca.cmd
    run['run_dir'] = str(run_dir)
    store = RunStore(run_dir)
    store.save(run)
    write_json_atomic(run_state_path(args.jira), {
        'jira': args.jira, 'run_dir': str(run_dir), 'run_id': run_id,
        'status': run['status'], 'updated_at': now(),
    })
    say(f'run 目录: {run_dir}')
    say(f'Orca run: {run_id}')
    say(f'集成分支: {integration["branch"]} ({integration["path"]})')
    return 0


def cmd_spec(args):
    ptb, run, tasks = load_context(args)
    run.setdefault('run_dir', str(Path(args.run_dir).resolve()))
    if args.task not in tasks:
        die(f'任务 {args.task} 不存在')
    arms = run['options'].get('arms') or list(DEFAULT_ARMS)
    arm = args.arm or arms[0]
    if arm not in arms:
        die(f'臂 {arm} 不在本次 run 的 arms 中：{", ".join(arms)}')
    export = ptb.jira_export(run['jira'])
    entry = run.get('tasks', {}).get(args.task) or {}
    port = args.port or (entry.get('arms', {}).get(arm, {}).get('dispatch', {}).get('port')) \
        or (3000 + (list(tasks).index(args.task) + 1))
    spec = build_spec(run, tasks, args.task, export, port, arm)
    out = Path(args.out) if args.out else (Path(args.run_dir) / 'specs' / f'{args.task}.{arm}.md')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(spec, encoding='utf-8')
    say(str(out))
    return 0


def build_dispatch_command(run, tid, arm, spec_path, name, orca):
    return [
        orca.cmd, 'orchestration', 'worker-start',
        '--spec-file', str(spec_path),
        '--task-title', f"{run['jira']} {tid} {arm} "
                        f"{(run.get('tasks', {}).get(tid, {}) or {}).get('title', '')}".strip(),
        '--worktree', 'new-top-level', '--repo', f"id:{run['repo_id']}",
        '--base-branch', run['integration']['branch'], '--name', name,
        '--agent', arm,
        '--run', run['run_id'], '--json',
    ] + (['--setup', run['options']['setup']] if run['options'].get('setup') else [])


def do_dispatch(args, run, tasks, plan):
    store = RunStore(args.run_dir)
    batch = first_batch(plan)
    if not batch:
        say('没有可派发的任务')
        return 0
    if running_tasks(run):
        say('仍有 worker 在运行，等唤醒后再派发')
        return 0
    if pending_choices(run):
        say('存在待选择的双 agent 比对，先 choose 后再派发')
        return 0
    orca = Orca(args.orca or run.get('orca_cmd'))
    jira = run['jira']
    arms = run['options'].get('arms') or list(DEFAULT_ARMS)
    # 生成每任务每臂 spec；非 dry-run 先写入 run（记录 port）再启动
    specs = {}

    def prepare(r, write_specs):
        r.setdefault('run_dir', str(Path(args.run_dir).resolve()))
        r.setdefault('batches', []).append({
            'index': len(r.get('batches') or []) + 1,
            'created_at': now(),
            'tasks': list(batch['tasks']),
            'arms': list(arms),
            'reason': batch.get('reason'),
        })
        r['status'] = 'running'
        for i, tid in enumerate(batch['tasks']):
            entry = r['tasks'].setdefault(tid, task_entry(tasks[tid]['title'], arms))
            entry['title'] = tasks[tid]['title']
            entry['dispatched_at'] = now()
            entry['arms'] = {a: arm_entry(a) for a in arms}
            entry['compare'] = {'state': 'pending', 'ready_at': None, 'patches': {},
                                'report_path': None, 'recommendation': {}}
            base_sha = r['integration'].get('head_sha') or r['base_sha']
            export = Ptb(r['db']).jira_export(jira) if write_specs else None
            for j, arm in enumerate(arms):
                port = 3000 + (i * len(arms)) + j + 1
                entry['arms'][arm]['dispatched_at'] = now()
                entry['arms'][arm]['dispatch'] = {
                    'state': 'starting', 'dispatch_id': None, 'orca_task_id': None,
                    'workspace': None, 'worktree_path': None,
                    'port': port, 'base_sha': base_sha,
                }
                spec_path = Path(args.run_dir) / 'specs' / f'{tid}.{arm}.md'
                if write_specs:
                    spec_path.write_text(build_spec(r, tasks, tid, export, port, arm),
                                         encoding='utf-8')
                specs[(tid, arm)] = spec_path
        return json.loads(json.dumps(r))

    if args.dry_run:
        run = prepare(json.loads(json.dumps(run)), write_specs=False)
    else:
        with store.update() as r:
            run = prepare(r, write_specs=True)

    run_stamp = datetime.datetime.now().strftime('%H%M%S')
    procs = []
    for tid in batch['tasks']:
        for arm in arms:
            name = sanitize(f'{jira}-{tid}-{arm}-{run_stamp}')
            spec_path = specs[(tid, arm)]
            cmd = build_dispatch_command(run, tid, arm, spec_path, name, orca)
            if args.dry_run:
                say('DRY-RUN ' + shlex.join([str(c) for c in cmd]))
                continue
            out_path = Path(args.run_dir) / 'mail' / f'start-{tid}-{arm}.json'
            err_path = Path(args.run_dir) / 'mail' / f'start-{tid}-{arm}.err'
            proc, _ = orca.worker_start(spec_path, f"{jira} {tid} {arm} {tasks[tid]['title']}",
                                        run['repo_id'], run['integration']['branch'], name,
                                        arm, run['options'].get('setup'), run['run_id'],
                                        out_path, err_path)
            procs.append((tid, arm, proc, out_path, err_path))

    if args.dry_run:
        return 0

    receipts = {}
    for tid, arm, proc, out_path, err_path in procs:
        rc = proc.wait()
        receipts[(tid, arm)] = {'rc': rc, 'out': out_path, 'err': err_path}
    with store.update() as r:
        for (tid, arm), rec in receipts.items():
            entry = r['tasks'][tid]
            d = entry['arms'][arm].setdefault('dispatch', {})
            text = Path(rec['out']).read_text(encoding='utf-8', errors='replace') if Path(rec['out']).exists() else ''
            err = Path(rec['err']).read_text(encoding='utf-8', errors='replace') if Path(rec['err']).exists() else ''
            if rec['rc'] != 0:
                d.update({'state': 'failed', 'error': (err or text)[:2000]})
                entry.setdefault('notes', []).append(f'{now()} {arm} 臂启动失败 rc={rec["rc"]}')
                warn(f'{tid}/{arm} 启动失败: {(err or text)[:400]}')
                continue
            try:
                payload = load_json(text)
                res = result_of(payload)
                d['dispatch_id'] = first(res, 'dispatchId', 'dispatch_id', 'id')
                d['orca_task_id'] = first(res, 'taskId', 'orcaTaskId', 'task_id')
                d['workspace'] = first(res, 'workspace', 'worktree', 'worktreeId')
                d['state'] = first(res, 'state', default='started')
                d['outcome'] = first(res, 'outcome')
                d['started_at'] = now()
                if not d['dispatch_id']:
                    d['state'] = 'failed'
                    d['error'] = 'receipt 缺少 dispatchId'
                    warn(f'{tid}/{arm} receipt 缺少 dispatchId: {text[:300]}')
                d['worktree_path'] = orca.worktree_path(d['workspace'])
                entry['arms'][arm]['dispatch'] = d
            except Exception as e:
                d.update({'state': 'failed', 'error': f'{e}; {text[:1000]}'})
                warn(f'{tid}/{arm} receipt 解析失败: {e}')
        r['status'] = 'running'
    say(f"已派发 batch {batch['batch']}: {', '.join(batch['tasks'])}"
        f"（{len(arms)} 臂：{', '.join(arms)}）")
    return 0


def cmd_dispatch(args):
    ptb, run, tasks = load_context(args)
    # 显式重试：丢弃该任务的双臂记录，回到 todo 重新派发（已合并的任务拒绝）
    for tid in (args.retry or []):
        entry = (run.get('tasks') or {}).get(tid)
        if not entry:
            warn(f'任务 {tid} 不在本 run 中，忽略 --retry')
            continue
        if entry.get('merge', {}).get('state') == 'merged':
            warn(f'任务 {tid} 已合并，不能重试')
            continue
        if is_running(entry):
            warn(f'任务 {tid} 仍有臂在运行，不能重试')
            continue
        entry['arms'] = {}
        entry['compare'] = {'state': 'pending', 'ready_at': None, 'patches': {},
                            'report_path': None, 'recommendation': {}}
        entry['merge'] = {'state': 'pending', 'arm': None, 'merge_sha': None,
                          'merged_at': None, 'conflicts': [], 'error': None}
        entry.setdefault('notes', []).append(f'{now()} 用户批准重试，清空双臂记录')
        try:
            ptb.set_status(run['jira'], tid, 'todo')
        except Exception as e:
            warn(f'重置 {tid} 为 todo 失败: {e}')
        RunStore(args.run_dir).save(run)
    plan = make_plan(tasks, run, args.max or run['options'].get('max_parallel', 3), args.conservative)
    return do_dispatch(args, run, tasks, plan)


def cmd_check(args):
    ptb, run, tasks = load_context(args)
    if args.fixture:
        payload = load_json(Path(args.fixture).read_text(encoding='utf-8'))
    else:
        payload = Orca(args.orca or run.get('orca_cmd')).check(run['run_id'])
        mail_dir = Path(args.run_dir) / 'mail'
        mail_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(mail_dir / f'check-{stamp()}.json', payload)
    msgs, need_user = process_wake(run, tasks, payload, ptb)
    RunStore(args.run_dir).save(run)
    if args.json:
        say(as_json({'messages': msgs, 'need_user': need_user}))
    else:
        say(f'收到 {len(msgs)} 条唤醒消息' + ('（需要用户介入）' if need_user else ''))
        for m in msgs:
            say(f"  {m['type'] or 'message'} dispatch={m['dispatch_id'] or '-'} outcome={m['outcome'] or '-'}")
    return 0


def cmd_advance(args):
    ptb, run, tasks = load_context(args)
    if args.fixture:
        payload = load_json(Path(args.fixture).read_text(encoding='utf-8'))
    else:
        payload = Orca(args.orca or run.get('orca_cmd')).check(run['run_id'])
        mail_dir = Path(args.run_dir) / 'mail'
        mail_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(mail_dir / f'check-{stamp()}.json', payload)
    msgs, need_user = process_wake(run, tasks, payload, ptb)
    say(f'唤醒消息 {len(msgs)} 条' + ('（需要用户介入）' if need_user else ''))
    # worker 可能刚写完报告，比对前重新采集
    tasks = collect_tasks(ptb, run['jira'])

    # 两臂都结束且未合并的任务：生成三维比对（不提交、不合并）
    compared = []
    for tid, entry in list(run.get('tasks', {}).items()):
        arms = task_arms(entry)
        if not arms or not all(arm_finished(a) for a in arms.values()):
            continue
        if entry.get('merge', {}).get('state') == 'merged':
            continue
        if (entry.get('compare') or {}).get('state') not in ('pending', 'failed'):
            continue
        if not any((a.get('dispatch') or {}).get('outcome') == 'succeeded' for a in arms.values()):
            continue
        if args.dry_run:
            say(f'DRY-RUN compare {tid}')
            continue
        try:
            if compare_task(run, tasks, tid, args.run_dir, verify=not args.no_verify):
                compared.append(tid)
        except Exception as e:
            warn(f'{tid} 比对失败: {e}')
    if compared:
        say('比对就绪: ' + ', '.join(compared) + '（等待 choose）')
    RunStore(args.run_dir).save(run)

    # 没有运行中的臂、没有待选择比对时，继续派发下一批
    dispatched = False
    if (not need_user and not running_tasks(run) and not pending_choices(run)
            and not args.no_dispatch):
        plan = make_plan(tasks, run, run['options'].get('max_parallel', 3), args.conservative)
        batch = first_batch(plan)
        if batch:
            if args.dry_run:
                say('DRY-RUN dispatch: ' + ', '.join(batch['tasks']))
            else:
                do_dispatch(args, run, tasks, plan)
                run = RunStore(args.run_dir).load()
            dispatched = True

    if not args.dry_run and not args.no_report:
        try:
            import report as report_mod
            paths = report_mod.generate(run['jira'], run['db'], args.run_dir, full=args.full_report)
            run.setdefault('report', {}).update(paths)
            RunStore(args.run_dir).save(run)
            say('报告: ' + paths.get('html', ''))
        except Exception as e:
            warn(f'报告生成失败: {e}')

    health = run_health(run, tasks)
    if health['stalled'] and not dispatched:
        say('停滞: ' + '；'.join(health['reasons']))
    return 0


def cmd_compare(args):
    ptb, run, tasks = load_context(args)
    tasks = collect_tasks(ptb, run['jira'])
    targets = args.task or [
        tid for tid, e in run.get('tasks', {}).items()
        if task_arms(e) and all(arm_finished(a) for a in task_arms(e).values())
        and e.get('merge', {}).get('state') != 'merged']
    if not targets:
        say('没有两臂都已结束且未合并的任务')
        return 0
    failed = []
    for tid in targets:
        if tid not in run.get('tasks', {}):
            warn(f'任务 {tid} 不在 run 中')
            failed.append(tid)
            continue
        if args.dry_run:
            say(f'DRY-RUN compare {tid}')
            continue
        try:
            cmp = compare_task(run, tasks, tid, args.run_dir, verify=not args.no_verify)
        except Exception as e:
            warn(f'{tid} 比对失败: {e}')
            failed.append(tid)
            continue
        if not cmp:
            warn(f'{tid} 尚未两臂完成，跳过')
            failed.append(tid)
            continue
        rec = cmp.get('recommendation') or {}
        say(f"{tid}: {cmp.get('markdown_path')}"
            + (f"（建议 {rec['winner']}）" if rec.get('winner') else ''))
    RunStore(args.run_dir).save(run)
    say(f'比对完成: {len(targets) - len(failed)} 成功, {len(failed)} 失败'
        + (f'（{", ".join(failed)}）' if failed else ''))
    return 0 if not failed else 1


def cmd_settle(args):
    """兼容旧命令名：settle 现在只做比对，绝不合并。"""
    return cmd_compare(args)


def cmd_choose(args):
    ptb, run, tasks = load_context(args)
    tasks = collect_tasks(ptb, run['jira'])
    if args.task not in tasks:
        die(f'任务 {args.task} 不存在')
    orca = None
    if not args.dry_run:
        orca = Orca(args.orca or run.get('orca_cmd'))
    try:
        res = choose_task(run, tasks, args.task, args.arm, ptb,
                          orca=orca, dry_run=args.dry_run, force=args.force)
    except PoError as e:
        RunStore(args.run_dir).save(run)
        die(str(e))
    RunStore(args.run_dir).save(run)
    if args.dry_run:
        return 0
    say(f"{args.task}: 已采纳 {args.arm} 臂，集成分支 {str(res.get('merge_sha'))[:12]}")
    return 0


def cmd_answer(args):
    ptb, run, tasks = load_context(args)
    Orca(args.orca or run.get('orca_cmd')).reply(args.message_id, args.body)
    found = False
    for q in run.get('questions', []):
        if str(q.get('message_id')) == str(args.message_id):
            q.update({'answered': True, 'answer': args.body, 'answered_at': now()})
            found = True
    run['status'] = 'running'
    RunStore(args.run_dir).save(run)
    say(('已回答 ' if found else '已发送回复（未在队列中找到对应问题）') + str(args.message_id))
    return 0


def run_health(run, tasks):
    running = running_tasks(run)
    total = len(tasks)
    done = sum(1 for t in tasks.values() if t['status'] == 'done')
    merged = sum(1 for e in (run.get('tasks') or {}).values()
                 if e.get('merge', {}).get('state') == 'merged')
    pending = pending_choices(run)
    ready = [tid for tid, e in (run.get('tasks') or {}).items()
             if (e.get('compare') or {}).get('state') == 'ready']
    conflicts = [tid for tid, e in (run.get('tasks') or {}).items()
                 if e.get('merge', {}).get('state') == 'conflict']
    failed = [f'{tid}/{agent}'
              for tid, e in (run.get('tasks') or {}).items()
              for agent, a in task_arms(e).items()
              if (a.get('dispatch') or {}).get('state') == 'failed'
              or (a.get('dispatch') or {}).get('outcome') == 'failed']
    blocked = [tid for tid, e in (run.get('tasks') or {}).items()
               if e.get('merge', {}).get('state') in ('conflict', 'failed')] + \
              [tid for tid, t in tasks.items() if t['status'] == 'blocked']
    pending_questions = [q for q in run.get('questions', []) if not q.get('answered')]
    reasons = []
    if conflicts:
        reasons.append('合并冲突: ' + ', '.join(sorted(set(conflicts))))
    if failed:
        reasons.append('臂失败: ' + ', '.join(sorted(set(failed))))
    if pending:
        reasons.append('待选择: ' + ', '.join(sorted(set(pending))))
    if pending_questions:
        reasons.append(f'{len(pending_questions)} 个待回答问题')
    if done < total and not pending:
        reasons.append(f'仍有 {total - done} 条未 done')
    stalled = (not running) and done < total and not pending_questions and not pending
    return {'running': running, 'done': done, 'total': total, 'merged': merged,
            'compare_ready': sorted(set(ready)), 'pending_choices': sorted(set(pending)),
            'conflicts': sorted(set(conflicts)), 'failed': sorted(set(failed)),
            'blocked': sorted(set(blocked)), 'pending_questions': pending_questions,
            'stalled': stalled, 'reasons': reasons}


def cmd_status(args):
    ptb, run, tasks = load_context(args)
    plan = make_plan(tasks, run, run['options'].get('max_parallel', 3), False)
    health = run_health(run, tasks)
    entries = run.get('tasks') or {}
    data = {'run': {
        'dir': args.run_dir, 'jira': run['jira'], 'run_id': run['run_id'],
        'status': run['status'], 'branch': run['integration']['branch'],
        'base_sha': run['base_sha'], 'head_sha': run['integration'].get('head_sha'),
        'arms': run['options'].get('arms') or list(DEFAULT_ARMS),
    }, 'health': health, 'stats': plan['stats'],
        'tasks': {tid: {
            'status': tasks[tid]['status'],
            'merge': (entries.get(tid, {}) or {}).get('merge', {}).get('state'),
            'compare': (entries.get(tid, {}) or {}).get('compare', {}).get('state'),
            'recommendation': ((entries.get(tid, {}) or {}).get('compare', {}) or {})
                              .get('recommendation', {}).get('winner'),
            'arms': {agent: {'dispatch': (a.get('dispatch') or {}).get('state'),
                             'outcome': (a.get('dispatch') or {}).get('outcome'),
                             'report': (a.get('report') or {}).get('status')}
                     for agent, a in task_arms(entries.get(tid, {})).items()},
        } for tid in tasks}}
    if args.json:
        say(as_json(data))
        return 0
    say(f"{run['jira']} run {run['run_id']}  [{run['status']}]"
        f"  臂: {', '.join(data['run']['arms'])}")
    say(f"集成分支 {run['integration']['branch']}  {run['base_sha'][:10]} -> "
        f"{(run['integration'].get('head_sha') or '')[:10]}")
    say(f"任务: merged {health['merged']}/{health['total']}, "
        f"running {len(health['running'])}, 待选择 {len(health['pending_choices'])}, "
        f"conflict {len(health['conflicts'])}, failed {len(health['failed'])}")
    if health['pending_choices']:
        say('待选择 (choose --task X --arm codex|claude):')
        for tid in health['pending_choices']:
            e = entries.get(tid) or {}
            rec = ((e.get('compare') or {}).get('recommendation') or {})
            total_acc = len(tasks[tid]['acceptance'])
            bits = []
            for agent, a in task_arms(e).items():
                g = a.get('git') or {}
                rep = a.get('report') or {}
                passed = sum(1 for x in rep.get('acceptance') or [] if x.get('result') == 'pass')
                bits.append(f"{agent}: acc {passed}/{total_acc} "
                            f"+{g.get('insertions') or 0}/-{g.get('deletions') or 0} "
                            f"({len(g.get('changed_files') or [])} 文件)")
            say(f"  {tid} {' | '.join(bits)}"
                + (f" → 建议 {rec.get('winner')}" if rec.get('winner') else ' → 无合格臂'))
    if health['pending_questions']:
        say('待回答问题:')
        for q in health['pending_questions']:
            say(f"  [{q.get('message_id')}] {q.get('task') or '-'}: {q.get('question')}")
    batch = first_batch(plan)
    if batch:
        say('下一批: ' + ', '.join(batch['tasks']) + f"（{batch['reason']}）")
    if health['stalled']:
        say('停滞: ' + '；'.join(health['reasons']))
    return 0


def cmd_report(args):
    import report as report_mod
    run = RunStore(args.run_dir).load()
    paths = report_mod.generate(run['jira'], run['db'], args.run_dir,
                                out_dir=args.out_dir, full=args.full)
    with RunStore(args.run_dir).update() as r:
        r.setdefault('report', {}).update(paths)
    say(paths['html'])
    return 0


def cmd_finalize(args):
    ptb, run, tasks = load_context(args)
    int_path = run['integration']['path']
    base = run['base_sha']
    val = ptb.validate(run['jira'], root=int_path)
    log = git_out(int_path, 'log', '--oneline', f'{base}..HEAD')
    stat = git_out(int_path, 'diff', '--stat', f'{base}..HEAD')
    run_dir = Path(args.run_dir)
    (run_dir / 'logs').mkdir(parents=True, exist_ok=True)
    (run_dir / 'logs' / 'integration.log').write_text(log + '\n\n' + stat + '\n', encoding='utf-8')
    patch = run_dir / 'logs' / 'integration.patch'
    p = git(int_path, 'diff', '--binary', f'{base}..HEAD')
    patch.write_text(p.stdout, encoding='utf-8')
    if args.release:
        orca = Orca(args.orca or run.get('orca_cmd'))
        for tid in run.get('tasks', {}):
            release_arms(run, tid, orca)
    health = run_health(run, tasks)
    dispatched = [e for e in (run.get('tasks') or {}).values()
                  if task_arms(e) and any((a.get('dispatch') or {}).get('dispatch_id')
                                          for a in task_arms(e).values())]
    all_merged = bool(dispatched) and all(e.get('merge', {}).get('state') == 'merged'
                                          for e in dispatched)
    if health['pending_choices']:
        run['status'] = 'waiting_choice'
    elif health['done'] == health['total'] and all_merged:
        run['status'] = 'complete'
    else:
        run['status'] = 'blocked'
    RunStore(args.run_dir).save(run)
    import report as report_mod
    paths = report_mod.generate(run['jira'], run['db'], args.run_dir, full=True)
    with RunStore(args.run_dir).update() as r:
        r.setdefault('report', {}).update(paths)
    say(f"集成分支: {run['integration']['branch']}")
    say(f"validate --root: {len(val.get('errors') or [])} 错误 / {len(val.get('warnings') or [])} 警告（仅信号）")
    say(f"patch: {patch}")
    say(f"报告: {paths['html']}")
    if health['pending_choices']:
        say('仍待选择（choose 后才合入）: ' + ', '.join(health['pending_choices']))
    say('未 push、未合并回用户分支；确认后再执行合并。')
    return 0


def cmd_resume(args):
    ptb, run, tasks = load_context(args)
    orca = Orca(args.orca or run.get('orca_cmd'))
    orca.run_use(run['run_id'])
    workers = orca.worker_list(run['run_id'])
    by_dispatch = {}
    for w in workers:
        did = first(w, 'dispatchId', 'dispatch_id', 'id')
        if did:
            by_dispatch[did] = w
    changed = 0
    for tid, entry in run.get('tasks', {}).items():
        for agent, arm in task_arms(entry).items():
            d = arm.get('dispatch') or {}
            did = d.get('dispatch_id')
            if did and did in by_dispatch:
                w = by_dispatch[did]
                outcome = first(w, 'outcome', 'status')
                if outcome and not d.get('outcome'):
                    d['outcome'] = str(outcome).lower()
                    d['done_at'] = d.get('done_at') or now()
                    changed += 1
        # 用 git 祖先关系重建 merged 状态
        state = entry.get('merge', {}).get('state')
        head = entry.get('merge', {}).get('merge_sha')
        if head and state != 'merged' and Path(run['integration']['path']).exists():
            if git(run['integration']['path'], 'merge-base', '--is-ancestor', head, 'HEAD').returncode == 0:
                entry.setdefault('merge', {})['state'] = 'merged'
                changed += 1
    RunStore(args.run_dir).save(run)
    say(f'对账完成，更新 {changed} 项；不会重新派发已完成 worker')
    return 0


def cmd_check_task(args):
    ptb = Ptb(args.db)
    t = ptb.task_show(args.jira, args.task)
    val = ptb.validate(args.jira, root=args.root)
    tid = args.task
    errors = [e for e in (val.get('errors') or []) if str(e).startswith(tid + ' ') or f' {tid} ' in str(e) or str(e).startswith(tid + ':')]
    warnings = [w for w in (val.get('warnings') or []) if str(w).startswith(tid + ' ') or f' {tid} ' in str(w) or str(w).startswith(tid + ':')]
    other = len(val.get('errors') or []) - len(errors)
    data = {
        'task': tid,
        'status': t['task'].get('status'),
        'acceptance': {'total': len(t.get('acceptance') or []),
                       'pass': sum(1 for a in (t.get('acceptance') or []) if a.get('result') == 'pass')},
        'completion': bool(t.get('completions')),
        'errors': errors,
        'warnings': warnings,
        'ignored_peer_errors': other,
    }
    if args.json:
        say(as_json(data))
    else:
        say(f"{tid} 状态={data['status']} 验收={data['acceptance']['pass']}/{data['acceptance']['total']} "
            f"completion={'有' if data['completion'] else '无'}")
        for e in errors:
            say('错误  ' + e)
        for w in warnings:
            say('警告  ' + w)
        if other:
            say(f'（已忽略 {other} 条其他任务/并行 worktree 噪声）')
    return 0 if not errors else 1


# ---------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(prog='po', description='prd-orchestrate 协调者 CLI')
    ap.add_argument('--db', default=str(DEFAULT_DB), help=f'ptb 库路径（默认 {DEFAULT_DB}）')
    ap.add_argument('--orca', help='Orca 可执行文件（默认按 orca-cli 规则解析）')
    ap.add_argument('--json', action='store_true', help='以 JSON 输出')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('preflight', help='只读预检')
    p.add_argument('--jira', required=True)
    p.add_argument('--root', default='.')
    p.add_argument('--base-ref')
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser('plan', help='生成波次/批次计划')
    p.add_argument('--jira')
    p.add_argument('--run-dir')
    p.add_argument('--max-parallel', type=int, default=3)
    p.add_argument('--conservative', action='store_true')
    p.add_argument('--out')
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser('init', help='初始化 run 与集成分支')
    p.add_argument('--jira', required=True)
    p.add_argument('--root', default='.')
    p.add_argument('--base-ref')
    p.add_argument('--run-dir')
    p.add_argument('--integration-branch')
    p.add_argument('--integration-mode', '--mode', dest='mode', default='git', choices=['git', 'orca'])
    p.add_argument('--repo-id')
    p.add_argument('--max-parallel', type=int, default=3)
    p.add_argument('--arms', default='codex,claude',
                   help='双 agent 臂，逗号分隔（默认 codex,claude）')
    p.add_argument('--setup', default='run', choices=['run', 'skip'])
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_init)

    p = sub.add_parser('spec', help='生成 worker spec（每任务每臂一份）')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--arm', help='codex / claude（默认第一臂）')
    p.add_argument('--out')
    p.add_argument('--port', type=int)
    p.set_defaults(func=cmd_spec)

    p = sub.add_parser('dispatch', help='派发下一批')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--max', type=int)
    p.add_argument('--conservative', action='store_true')
    p.add_argument('--retry', action='append')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser('check', help='执行一次 Run 级 check')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--fixture')
    p.set_defaults(func=cmd_check)

    p = sub.add_parser('advance', help='check + 双 agent 比对 + dispatch + report（不合并）')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--fixture')
    p.add_argument('--no-dispatch', action='store_true')
    p.add_argument('--no-report', action='store_true')
    p.add_argument('--no-verify', action='store_true', help='比对时不重跑验证命令')
    p.add_argument('--full-report', action='store_true')
    p.add_argument('--conservative', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser('compare', help='对两臂都已结束的任务生成代码 diff/结果/效果比对')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--task', action='append')
    p.add_argument('--no-verify', action='store_true', help='不重跑验证命令')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser('choose', help='人工选定一臂：应用 patch + 提交集成分支 + 回写 ptb')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--arm', required=True)
    p.add_argument('--force', action='store_true', help='验收未全过或门禁未达标时强行采纳')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_choose)

    p = sub.add_parser('settle', help='兼容旧名：只做比对，绝不合并')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--task', action='append')
    p.add_argument('--no-verify', action='store_true', help='不重跑验证命令')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser('answer', help='回答 worker 问题')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--message-id', required=True)
    p.add_argument('--body', required=True)
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser('status', help='当前 run 状态')
    p.add_argument('--run-dir', required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('report', help='生成 HTML 报告')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--out-dir')
    p.add_argument('--full', action='store_true')
    p.set_defaults(func=cmd_report)

    p = sub.add_parser('finalize', help='收尾：校验集成分支、导出 patch、最终报告')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--release', action='store_true', help='释放 worker 终端（默认保留）')
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser('resume', help='中断后对账')
    p.add_argument('--run-dir', required=True)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser('check-task', help='worker 自查（过滤 peer 噪声）')
    p.add_argument('--jira', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--root', default='.')
    p.set_defaults(func=cmd_check_task)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except PoError as e:
        die(str(e))
    except KeyboardInterrupt:
        die('已中断', 130)


if __name__ == '__main__':
    sys.exit(main())
