#!/usr/bin/env python3
"""ptb.py — prd-to-breakdown 状态库 CLI

唯一数据源: ~/.prd-to-breakdown/prd.db (SQLite)。
技能只通过本命令读写, 不直接写 SQL、不生成任务卡文件。

用法:
    python3 ptb.py init
    python3 ptb.py jira add --jira OS-43558 --title "..." --scope frontend
    python3 ptb.py task add --jira OS-43558 --id T001 --title "..." --end frontend
    python3 ptb.py task set T001 --jira OS-43558 --status doing
    python3 ptb.py ready --jira OS-43558
    python3 ptb.py validate --jira OS-43558
    python3 ptb.py task show T001 --jira OS-43558 --md

环境变量: PTB_DB 覆盖库路径, PTB_BACKUP_DIR 覆盖备份目录。
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_DB = Path(os.environ.get('PTB_DB', Path.home() / '.prd-to-breakdown' / 'prd.db'))
DEFAULT_BACKUP_DIR = Path(os.environ.get('PTB_BACKUP_DIR', Path.home() / '.prd-to-breakdown' / 'backups'))
SCHEMA_VERSION = 3
BACKUP_KEEP = 20
BACKUP_INTERVAL_SEC = 600

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jiras(
  jira TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  scope TEXT CHECK(scope IN ('frontend','backend','both')),
  status TEXT NOT NULL DEFAULT 'breaking' CHECK(status IN ('breaking','developing','done','archived')),
  project_brief TEXT,
  design_notes TEXT,
  baseline TEXT,
  out_of_scope TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS demands(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE,
  page_id TEXT,
  title TEXT,
  path TEXT NOT NULL,
  llm_path TEXT,
  jira TEXT,
  fetched_at TEXT NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS design_drafts(
  jira TEXT NOT NULL REFERENCES jiras(jira) ON DELETE CASCADE,
  path TEXT NOT NULL,
  covers TEXT,
  note TEXT,
  PRIMARY KEY(jira, path)
);

CREATE TABLE IF NOT EXISTS requirements(
  jira TEXT NOT NULL REFERENCES jiras(jira) ON DELETE CASCADE,
  id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  acceptance TEXT,
  priority TEXT CHECK(priority IN ('must','should','optional')),
  excluded_frontend TEXT,
  PRIMARY KEY(jira, id)
);

CREATE TABLE IF NOT EXISTS tasks(
  jira TEXT NOT NULL REFERENCES jiras(jira) ON DELETE CASCADE,
  id TEXT NOT NULL,
  seq INTEGER,
  title TEXT NOT NULL,
  end TEXT CHECK(end IN ('frontend','backend','integration')),
  status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','doing','done','blocked')),
  estimate TEXT CHECK(estimate IN ('S','M','L')),
  parallel TEXT,
  milestone TEXT,
  design_ref TEXT,
  goal TEXT,
  not_responsible TEXT,
  implementation TEXT,
  boundaries TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(jira, id)
);

CREATE TABLE IF NOT EXISTS task_deps(
  jira TEXT NOT NULL,
  task_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY(jira, task_id, depends_on),
  FOREIGN KEY(jira, task_id) REFERENCES tasks(jira, id) ON DELETE CASCADE,
  FOREIGN KEY(jira, depends_on) REFERENCES tasks(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_files(
  jira TEXT NOT NULL,
  task_id TEXT NOT NULL,
  path TEXT NOT NULL,
  change TEXT CHECK(change IN ('add','modify','delete','refactor','none')),
  note TEXT,
  PRIMARY KEY(jira, task_id, path),
  FOREIGN KEY(jira, task_id) REFERENCES tasks(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_requirements(
  jira TEXT NOT NULL,
  task_id TEXT NOT NULL,
  req TEXT NOT NULL,
  PRIMARY KEY(jira, task_id, req),
  FOREIGN KEY(jira, task_id) REFERENCES tasks(jira, id) ON DELETE CASCADE,
  FOREIGN KEY(jira, req) REFERENCES requirements(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS acceptance(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jira TEXT NOT NULL,
  task_id TEXT NOT NULL,
  seq INTEGER,
  descr TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'functional' CHECK(kind IN ('functional','visual','regression','contract')),
  via TEXT,
  command TEXT,
  depends_contract INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL DEFAULT 'pending' CHECK(result IN ('pending','pass','fail','blocked')),
  FOREIGN KEY(jira, task_id) REFERENCES tasks(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS verifications(
  jira TEXT NOT NULL,
  task_id TEXT NOT NULL,
  seq INTEGER,
  cmd TEXT NOT NULL,
  FOREIGN KEY(jira, task_id) REFERENCES tasks(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS completions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jira TEXT NOT NULL,
  task_id TEXT NOT NULL,
  date TEXT NOT NULL,
  evidence TEXT,
  deviations TEXT,
  FOREIGN KEY(jira, task_id) REFERENCES tasks(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jira TEXT NOT NULL,
  task_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('start','progress','verify','block','resume','complete','note')),
  message TEXT,
  FOREIGN KEY(jira, task_id) REFERENCES tasks(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS risks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jira TEXT NOT NULL REFERENCES jiras(jira) ON DELETE CASCADE,
  descr TEXT NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jira TEXT NOT NULL REFERENCES jiras(jira) ON DELETE CASCADE,
  ref TEXT,
  question TEXT NOT NULL,
  options TEXT,
  answer TEXT,
  conclusion TEXT,
  date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts(
  jira TEXT NOT NULL REFERENCES jiras(jira) ON DELETE CASCADE,
  id TEXT NOT NULL,
  method TEXT,
  path TEXT,
  status TEXT NOT NULL DEFAULT 'fixme' CHECK(status IN ('fixme','defined','aligned')),
  note TEXT,
  PRIMARY KEY(jira, id)
);

CREATE TABLE IF NOT EXISTS contract_fields(
  jira TEXT NOT NULL,
  contract_id TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT,
  required INTEGER,
  enum_values TEXT,
  note TEXT,
  PRIMARY KEY(jira, contract_id, name),
  FOREIGN KEY(jira, contract_id) REFERENCES contracts(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS contract_deltas(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jira TEXT NOT NULL,
  contract_id TEXT,
  field TEXT,
  expected TEXT,
  actual TEXT,
  resolution TEXT,
  date TEXT NOT NULL,
  FOREIGN KEY(jira, contract_id) REFERENCES contracts(jira, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tracking_points(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jira TEXT NOT NULL REFERENCES jiras(jira) ON DELETE CASCADE,
  task_id TEXT,
  event TEXT CHECK(event IN ('exposure','click','pv')),
  api TEXT,
  element TEXT,
  timing TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS i18n_keys(
  jira TEXT NOT NULL REFERENCES jiras(jira) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT,
  note TEXT,
  PRIMARY KEY(jira, key)
);

CREATE TABLE IF NOT EXISTS corrections(
  id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  jira TEXT,
  task TEXT,
  stage TEXT CHECK(stage IN ('review','integration','prod')),
  dimension TEXT CHECK(dimension IN ('visual','requirement','contract','scope','docs')),
  user_instruction TEXT,
  expected TEXT,
  actual TEXT,
  applied_change TEXT,
  root_cause TEXT,
  blame TEXT CHECK(blame IN ('prd-to-breakdown','task-runner','ui-restore','unclear')),
  rule_gap TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','adopted','rejected'))
);

CREATE TABLE IF NOT EXISTS upgrades(
  id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  summary TEXT,
  targets TEXT,
  commit_hash TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS upgrade_corrections(
  upgrade_id TEXT NOT NULL REFERENCES upgrades(id) ON DELETE CASCADE,
  correction_id TEXT NOT NULL REFERENCES corrections(id) ON DELETE CASCADE,
  PRIMARY KEY(upgrade_id, correction_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_jira_status ON tasks(jira, status);
CREATE INDEX IF NOT EXISTS idx_acc_task ON acceptance(jira, task_id);
CREATE INDEX IF NOT EXISTS idx_corr_status ON corrections(status);
"""


# ---------------------------------------------------------------- 基础设施

def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def today():
    return datetime.date.today().isoformat()


def connect(db_path):
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.executescript(SCHEMA)
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.commit()
    return conn


def read_text(value=None, file=None):
    if file:
        if file == '-':
            return sys.stdin.read()
        return Path(file).expanduser().read_text(encoding='utf-8')
    return value


def backup(conn, db_path, backup_dir):
    """写操作前做快照；10 分钟内已有备份则跳过。"""
    db_path = Path(db_path).expanduser()
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    backup_dir = Path(backup_dir).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(backup_dir.glob('prd-*.db'))
    if existing:
        age = datetime.datetime.now().timestamp() - existing[-1].stat().st_mtime
        if age < BACKUP_INTERVAL_SEC:
            return
    conn.commit()
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    target = backup_dir / f'prd-{stamp}.db'
    if target.exists():
        target = backup_dir / f'prd-{stamp}-{os.getpid()}.db'
    conn.execute('VACUUM INTO ?', (str(target),))
    for old in sorted(backup_dir.glob('prd-*.db'))[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)


def write(args, fn):
    conn = connect(args.db)
    try:
        backup(conn, args.db, args.backup_dir)
        fn(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def emit(data, as_json=False):
    print(json.dumps(data, ensure_ascii=False, indent=2) if as_json else data)


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def require_jira(conn, jira):
    row = conn.execute('SELECT 1 FROM jiras WHERE jira = ?', (jira,)).fetchone()
    if not row:
        sys.exit(f'错误: 需求 {jira} 不存在, 先执行 jira add')


def touch_jira(conn, jira):
    conn.execute('UPDATE jiras SET updated_at = ? WHERE jira = ?', (now(), jira))


def next_seq(conn, table, jira, task_id=None):
    if task_id is None:
        row = conn.execute(f'SELECT COALESCE(MAX(seq), 0) + 1 FROM {table} WHERE jira = ?', (jira,)).fetchone()
    else:
        row = conn.execute(f'SELECT COALESCE(MAX(seq), 0) + 1 FROM {table} WHERE jira = ? AND task_id = ?',
                           (jira, task_id)).fetchone()
    return row[0]


# ---------------------------------------------------------------- jira

def cmd_jira_add(args):
    def run(conn):
        conn.execute(
            'INSERT INTO jiras(jira,title,scope,status,project_brief,design_notes,baseline,out_of_scope,created_at,updated_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?)',
            (args.jira, args.title, args.scope, args.status,
             read_text(args.brief, args.brief_file),
             read_text(args.design_notes, args.design_notes_file),
             args.baseline, read_text(args.out_of_scope, args.out_of_scope_file),
             now(), now()))
    write(args, run)
    print(f'已创建需求 {args.jira}')


def cmd_jira_set(args):
    fields, values = [], []
    for name in ('title', 'scope', 'status', 'baseline'):
        value = getattr(args, name)
        if value is not None:
            fields.append(f'{name} = ?')
            values.append(value)
    for name, column in (('brief', 'project_brief'), ('design_notes', 'design_notes'),
                         ('out_of_scope', 'out_of_scope')):
        value = read_text(getattr(args, name), getattr(args, f'{name}_file'))
        if value is not None:
            fields.append(f'{column} = ?')
            values.append(value)
    if not fields:
        sys.exit('错误: 没有要更新的字段')
    fields.append('updated_at = ?')
    values.append(now())
    values.append(args.jira)

    def run(conn):
        require_jira(conn, args.jira)
        conn.execute(f"UPDATE jiras SET {', '.join(fields)} WHERE jira = ?", values)
    write(args, run)
    print(f'已更新需求 {args.jira}')


def render_jira(conn, jira, as_md=False):
    j = conn.execute('SELECT * FROM jiras WHERE jira = ?', (jira,)).fetchone()
    if not j:
        sys.exit(f'错误: 需求 {jira} 不存在')
    reqs = conn.execute('SELECT * FROM requirements WHERE jira = ? ORDER BY id', (jira,)).fetchall()
    tasks = conn.execute('SELECT * FROM tasks WHERE jira = ? ORDER BY seq, id', (jira,)).fetchall()
    drafts = conn.execute('SELECT * FROM design_drafts WHERE jira = ?', (jira,)).fetchall()
    done = sum(1 for t in tasks if t['status'] == 'done')
    if not as_md:
        return {
            'jira': dict(j), 'design_drafts': rows_to_dicts(drafts),
            'requirements': rows_to_dicts(reqs), 'tasks': rows_to_dicts(tasks),
            'progress': f'{done}/{len(tasks)}',
        }
    lines = [f"# {j['jira']} {j['title']}", '',
             f"- 范围: {j['scope']} / 状态: {j['status']} / 进度: {done}/{len(tasks)}"]
    if drafts:
        lines.append('- 设计稿: ' + '; '.join(f"{d['path']}" + (f" ({d['covers']})" if d['covers'] else '') for d in drafts))
    if j['baseline']:
        lines.append(f"- 基线约束: {j['baseline']}")
    if reqs:
        lines += ['', '## 需求', '', '| ID | 需求 | 优先级 |', '|---|---|---|']
        lines += [f"| {r['id']} | {r['title']} | {r['priority'] or 'must'} |" for r in reqs]
    if tasks:
        lines += ['', '## 任务', '', '| ID | 标题 | 端 | 依赖 | 状态 |', '|---|---|---|---|---|']
        for t in tasks:
            deps = conn.execute('SELECT depends_on FROM task_deps WHERE jira=? AND task_id=?', (jira, t['id'])).fetchall()
            lines.append(f"| {t['id']} | {t['title']} | {t['end'] or ''} | "
                         f"{','.join(d['depends_on'] for d in deps) or '—'} | {t['status']} |")
    return '\n'.join(lines)


def cmd_jira_show(args):
    conn = connect(args.db)
    try:
        print(render_jira(conn, args.jira, as_md=args.md))
    finally:
        conn.close()


def cmd_jira_list(args):
    conn = connect(args.db)
    try:
        rows = conn.execute('SELECT jira,title,scope,status,updated_at FROM jiras ORDER BY updated_at DESC').fetchall()
        if args.json:
            print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print(f"{r['jira']}\t{r['status']}\t{r['scope'] or ''}\t{r['title']}\t{r['updated_at']}")
    finally:
        conn.close()


# ---------------------------------------------------------------- design / req

def cmd_design_add(args):
    def run(conn):
        require_jira(conn, args.jira)
        conn.execute('INSERT OR REPLACE INTO design_drafts(jira,path,covers,note) VALUES(?,?,?,?)',
                     (args.jira, args.path, args.covers, args.note))
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'已登记设计稿 {args.path}')


def cmd_design_list(args):
    conn = connect(args.db)
    try:
        rows = conn.execute('SELECT * FROM design_drafts WHERE jira = ?', (args.jira,)).fetchall()
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_req_add(args):
    def run(conn):
        require_jira(conn, args.jira)
        # UPSERT 而非 INSERT OR REPLACE：OR REPLACE 会先删旧行，
        # 级联丢掉 task_requirements 里 R→T 的关联（修正文案时反复踩过）
        conn.execute(
            'INSERT INTO requirements(jira,id,title,description,acceptance,priority,excluded_frontend)'
            ' VALUES(?,?,?,?,?,?,?)'
            ' ON CONFLICT(jira,id) DO UPDATE SET title=excluded.title, description=excluded.description,'
            ' acceptance=excluded.acceptance, priority=excluded.priority,'
            ' excluded_frontend=excluded.excluded_frontend',
            (args.jira, args.id, args.title,
             read_text(args.description, args.description_file),
             read_text(args.acceptance, args.acceptance_file),
             args.priority, read_text(args.excluded_frontend, args.excluded_frontend_file)))
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'已写入需求 {args.id}')


def cmd_req_link(args):
    def run(conn):
        require_jira(conn, args.jira)
        if not conn.execute('SELECT 1 FROM requirements WHERE jira=? AND id=?', (args.jira, args.id)).fetchone():
            sys.exit(f'错误: 需求 {args.id} 不存在')
        if not conn.execute('SELECT 1 FROM tasks WHERE jira=? AND id=?', (args.jira, args.task)).fetchone():
            sys.exit(f'错误: 任务 {args.task} 不存在')
        conn.execute('INSERT OR IGNORE INTO task_requirements(jira, task_id, req) VALUES(?,?,?)',
                     (args.jira, args.task, args.id))
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'已关联 {args.id} -> {args.task}')


def cmd_req_list(args):
    conn = connect(args.db)
    try:
        rows = conn.execute('SELECT * FROM requirements WHERE jira = ? ORDER BY id', (args.jira,)).fetchall()
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
    finally:
        conn.close()


# ---------------------------------------------------------------- task

def cmd_task_add(args):
    def run(conn):
        require_jira(conn, args.jira)
        if conn.execute('SELECT 1 FROM tasks WHERE jira=? AND id=?', (args.jira, args.id)).fetchone():
            sys.exit(f'错误: 任务 {args.id} 已存在；改字段用 task set / task text，不要重复插入')
        seq = args.seq or next_seq(conn, 'tasks', args.jira)
        conn.execute(
            'INSERT INTO tasks(jira,id,seq,title,end,status,estimate,parallel,milestone,design_ref,'
            'goal,not_responsible,implementation,boundaries,notes,created_at,updated_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (args.jira, args.id, seq, args.title, args.end, args.status, args.estimate,
             args.parallel, args.milestone, args.design_ref,
             read_text(args.goal, args.goal_file),
             read_text(args.not_responsible, args.not_responsible_file),
             read_text(args.implementation, args.implementation_file),
             read_text(args.boundaries, args.boundaries_file),
             read_text(args.notes, args.notes_file),
             now(), now()))
        for dep in args.dep or []:
            conn.execute('INSERT OR IGNORE INTO task_deps(jira,task_id,depends_on) VALUES(?,?,?)',
                         (args.jira, args.id, dep))
        for req in args.req or []:
            conn.execute('INSERT OR IGNORE INTO task_requirements(jira,task_id,req) VALUES(?,?,?)',
                         (args.jira, args.id, req))
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'已创建任务 {args.id}')


def cmd_task_set(args):
    fields, values = [], []
    for name in ('title', 'end', 'status', 'estimate', 'parallel', 'milestone', 'design_ref', 'seq'):
        value = getattr(args, name)
        if value is not None:
            fields.append(f'{name} = ?')
            values.append(value)
    if not fields:
        sys.exit('错误: 没有要更新的字段')
    fields.append('updated_at = ?')
    values += [now(), args.jira, args.id]

    def run(conn):
        cur = conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE jira = ? AND id = ?", values)
        if cur.rowcount == 0:
            sys.exit(f'错误: 任务 {args.id} 不存在')
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'已更新任务 {args.id}')


TEXT_FIELDS = ('goal', 'not_responsible', 'implementation', 'boundaries', 'notes')


def cmd_task_text(args):
    if args.field not in TEXT_FIELDS:
        sys.exit(f'错误: 字段只能是 {", ".join(TEXT_FIELDS)}')
    content = args.text if args.text is not None else read_text(None, args.file)
    if content is None:
        sys.exit('错误: 用 --text / --file / --stdin 提供内容')

    def run(conn):
        cur = conn.execute(f'UPDATE tasks SET {args.field} = ?, updated_at = ? WHERE jira = ? AND id = ?',
                           (content, now(), args.jira, args.id))
        if cur.rowcount == 0:
            sys.exit(f'错误: 任务 {args.id} 不存在')
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'已写入 {args.id}.{args.field}')


def render_task(conn, jira, tid, as_md=False):
    t = conn.execute('SELECT * FROM tasks WHERE jira = ? AND id = ?', (jira, tid)).fetchone()
    if not t:
        sys.exit(f'错误: 任务 {tid} 不存在')
    deps = [r['depends_on'] for r in conn.execute(
        'SELECT depends_on FROM task_deps WHERE jira=? AND task_id=?', (jira, tid))]
    files = conn.execute('SELECT * FROM task_files WHERE jira=? AND task_id=?', (jira, tid)).fetchall()
    acc = conn.execute('SELECT * FROM acceptance WHERE jira=? AND task_id=? ORDER BY seq, id', (jira, tid)).fetchall()
    ver = [r['cmd'] for r in conn.execute(
        'SELECT cmd FROM verifications WHERE jira=? AND task_id=? ORDER BY seq', (jira, tid))]
    comps = conn.execute('SELECT * FROM completions WHERE jira=? AND task_id=? ORDER BY id', (jira, tid)).fetchall()
    data = {'task': dict(t), 'depends_on': deps, 'files': rows_to_dicts(files),
            'acceptance': rows_to_dicts(acc), 'verifications': ver, 'completions': rows_to_dicts(comps)}
    if not as_md:
        return data
    lines = [f"# {t['id']} {t['title']}", '',
             f"- 端: {t['end'] or ''} / 状态: {t['status']} / 估计: {t['estimate'] or ''}",
             f"- 依赖: {', '.join(deps) or '无'} / 里程碑: {t['milestone'] or '—'}"]
    if t['design_ref']:
        lines.append(f"- 设计稿: {t['design_ref']}")
    for field, label in (('goal', '目标'), ('not_responsible', '本任务不负责'),
                         ('implementation', '实现要点'), ('boundaries', '边界与异常'), ('notes', '备注')):
        if t[field]:
            lines += ['', f'## {label}', '', t[field]]
    if files:
        lines += ['', '## 涉及文件', '', '| 文件 | 改动方式 | 说明 |', '|---|---|---|']
        lines += [f"| `{f['path']}` | {f['change'] or ''} | {f['note'] or ''} |" for f in files]
    if acc:
        lines += ['', '## 验收标准', '']
        for a in acc:
            mark = {'pass': 'x', 'fail': '!', 'blocked': '-'}.get(a['result'], ' ')
            extra = f" （{a['kind']}" + (f", via {a['via']}" if a['via'] else '') + ')'
            lines.append(f"- [{mark}] {a['descr']}{extra}")
    if ver:
        lines += ['', '## 验证命令', ''] + [f'- `{c}`' for c in ver]
    if comps:
        lines += ['', '## 完成记录', '']
        for c in comps:
            lines.append(f"- {c['date']}: {c['evidence'] or ''}" + (f" | 偏差: {c['deviations']}" if c['deviations'] else ''))
    return '\n'.join(lines)


def cmd_task_show(args):
    conn = connect(args.db)
    try:
        result = render_task(conn, args.jira, args.id, as_md=args.md)
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_task_list(args):
    conn = connect(args.db)
    try:
        sql = 'SELECT id,title,end,status,estimate FROM tasks WHERE jira = ?'
        params = [args.jira]
        if args.status:
            sql += ' AND status = ?'
            params.append(args.status)
        rows = conn.execute(sql + ' ORDER BY seq, id', params).fetchall()
        if args.json:
            print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print(f"{r['id']}\t{r['status']}\t{r['end'] or ''}\t{r['estimate'] or ''}\t{r['title']}")
    finally:
        conn.close()


def cmd_dep_add(args):
    def run(conn):
        conn.execute('INSERT OR IGNORE INTO task_deps(jira,task_id,depends_on) VALUES(?,?,?)',
                     (args.jira, args.task, args.on))
    write(args, run)
    print(f'{args.task} 依赖 {args.on}')


def cmd_file_add(args):
    def run(conn):
        conn.execute('INSERT OR REPLACE INTO task_files(jira,task_id,path,change,note) VALUES(?,?,?,?,?)',
                     (args.jira, args.task, args.path, args.change, args.note))
    write(args, run)
    print(f'已登记文件 {args.path}')


def cmd_acc_add(args):
    def run(conn):
        seq = next_seq(conn, 'acceptance', args.jira, args.task)
        conn.execute(
            'INSERT INTO acceptance(jira,task_id,seq,descr,kind,via,command,depends_contract,result)'
            ' VALUES(?,?,?,?,?,?,?,?,?)',
            (args.jira, args.task, seq, read_text(args.desc, args.desc_file),
             args.kind, args.via, args.command, 1 if args.contract else 0, 'pending'))
    write(args, run)
    print(f'已添加验收项 {args.task}#{args.kind}')


def cmd_acc_set(args):
    def run(conn):
        # result 必填保持不变；--desc 允许修正验收文本（PRD 口径变更时旧文本改不了会永久报错）
        sets, params = ['result = ?'], [args.result]
        if args.desc:
            sets.append('descr = ?'); params.append(args.desc)
        params.extend([args.id, args.jira, args.task])
        cur = conn.execute(
            f"UPDATE acceptance SET {', '.join(sets)} WHERE id = ? AND jira = ? AND task_id = ?", params)
        if cur.rowcount == 0:
            sys.exit(f'错误: 验收项 {args.id} 不存在')
    write(args, run)
    print(f'验收项 {args.id} -> {args.result}')


def cmd_verify_add(args):
    def run(conn):
        seq = next_seq(conn, 'verifications', args.jira, args.task)
        conn.execute('INSERT INTO verifications(jira,task_id,seq,cmd) VALUES(?,?,?,?)',
                     (args.jira, args.task, seq, args.cmd))
    write(args, run)
    print('已添加验证命令')


def cmd_complete(args):
    def run(conn):
        row = conn.execute('SELECT status FROM tasks WHERE jira=? AND id=?', (args.jira, args.task)).fetchone()
        if not row:
            sys.exit(f'错误: 任务 {args.task} 不存在')
        pending = conn.execute(
            "SELECT COUNT(*) c FROM acceptance WHERE jira=? AND task_id=? AND result != 'pass'",
            (args.jira, args.task)).fetchone()['c']
        if pending and not args.force:
            sys.exit(f'错误: {args.task} 还有 {pending} 条验收未通过，不能标记完成（确需强行完成用 --force，并在 deviations 说明）')
        conn.execute('INSERT INTO completions(jira,task_id,date,evidence,deviations) VALUES(?,?,?,?,?)',
                     (args.jira, args.task, args.date or today(),
                      read_text(args.evidence, args.evidence_file),
                      read_text(args.deviations, args.deviations_file)))
        conn.execute('UPDATE tasks SET status=?, updated_at=? WHERE jira=? AND id=?',
                     ('done', now(), args.jira, args.task))
        log_event(conn, args.jira, args.task, 'complete',
                  read_text(args.evidence, args.evidence_file))
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'任务 {args.task} 已完成')


# ---------------------------------------------------------------- 进度与对账

def log_event(conn, jira, task_id, kind, message=None):
    conn.execute('INSERT INTO task_events(jira,task_id,ts,kind,message) VALUES(?,?,?,?,?)',
                 (jira, task_id, now(), kind, message))


def cmd_start(args):
    def run(conn):
        row = conn.execute('SELECT status FROM tasks WHERE jira=? AND id=?', (args.jira, args.task)).fetchone()
        if not row:
            sys.exit(f'错误: 任务 {args.task} 不存在')
        if row['status'] == 'done':
            sys.exit('错误: 任务已完成，不重复开工；确需重做先把状态改回 todo')
        kind = 'resume' if row['status'] == 'doing' else 'start'
        conn.execute("UPDATE tasks SET status='doing', updated_at=? WHERE jira=? AND id=?",
                     (now(), args.jira, args.task))
        log_event(conn, args.jira, args.task, kind, read_text(args.msg, args.msg_file))
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'任务 {args.task} 已标记进行中')


def cmd_progress(args):
    message = read_text(args.msg, args.msg_file)
    if not message:
        sys.exit('错误: 用 --msg / --msg-file 提供进度说明')

    def run(conn):
        row = conn.execute('SELECT status FROM tasks WHERE jira=? AND id=?', (args.jira, args.task)).fetchone()
        if not row:
            sys.exit(f'错误: 任务 {args.task} 不存在')
        log_event(conn, args.jira, args.task, args.kind, message)
        if args.kind == 'block':
            conn.execute("UPDATE tasks SET status='blocked', updated_at=? WHERE jira=? AND id=?",
                         (now(), args.jira, args.task))
        elif args.kind == 'resume' and row['status'] == 'blocked':
            conn.execute("UPDATE tasks SET status='doing', updated_at=? WHERE jira=? AND id=?",
                         (now(), args.jira, args.task))
        touch_jira(conn, args.jira)
    write(args, run)
    print(f'已记录进度 {args.task}#{args.kind}')


def cmd_log(args):
    conn = connect(args.db)
    try:
        rows = conn.execute('SELECT * FROM task_events WHERE jira=? AND task_id=? ORDER BY id',
                            (args.jira, args.task)).fetchall()
        if args.json:
            print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
        elif not rows:
            print('无进度事件')
        else:
            for r in rows:
                msg = (r['message'] or '').replace('\n', ' ')
                if len(msg) > 160:
                    msg = msg[:160] + '…'
                print(f"{r['ts']}\t{r['kind']}\t{msg}")
    finally:
        conn.close()


def git_states(root, paths):
    """返回 {相对路径: git 状态码}；非 git 仓库或无改动时返回空。"""
    if not root or not paths:
        return {}
    try:
        proc = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--', *paths],
                              capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            return {}
        out = {}
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            code, path = line[:2], line[3:]
            if ' -> ' in path:
                path = path.split(' -> ')[-1]
            out[path.strip().strip('"')] = code
        return out
    except Exception:
        return {}


def reconcile_task(conn, jira, task, root):
    tid = task['id']
    status = task['status']
    comp = conn.execute('SELECT * FROM completions WHERE jira=? AND task_id=? ORDER BY id DESC LIMIT 1',
                        (jira, tid)).fetchone()
    events = conn.execute('SELECT * FROM task_events WHERE jira=? AND task_id=? ORDER BY id',
                          (jira, tid)).fetchall()
    files = conn.execute('SELECT path, change FROM task_files WHERE jira=? AND task_id=?',
                         (jira, tid)).fetchall()
    acc_rows = conn.execute('SELECT result, COUNT(*) c FROM acceptance WHERE jira=? AND task_id=? GROUP BY result',
                            (jira, tid)).fetchall()
    acc = {r['result']: r['c'] for r in acc_rows}
    pending = acc.get('pending', 0) + acc.get('fail', 0) + acc.get('blocked', 0)
    paths = [f['path'] for f in files]
    git = git_states(root, paths) if root else {}
    touched = {p: c for p, c in git.items() if c.strip()}

    if status == 'done':
        if comp:
            suspected, action = 'complete', '无需处理'
        else:
            suspected, action = 'inconsistent', '状态 done 但无完成记录：回退为 doing 后复核'
    elif status == 'blocked':
        suspected = 'blocked'
        action = '确认阻塞是否解除（看最近事件与 notes），解除后 progress --kind resume'
    elif status == 'doing':
        if touched or events:
            suspected = 'partially_implemented'
            action = '读最近事件 + 查看改动，确认已完成到哪条验收，从断点继续'
        else:
            suspected = 'started_no_change'
            action = '刚开工，按 implementation 第一步继续'
    else:  # todo
        if touched or events:
            suspected = 'residual_work'
            action = '库显示未开始但有改动/事件：上次可能中断且未回写，先核对改动再决定补做或重做'
        else:
            suspected = 'not_started'
            action = '正常开工'

    return {
        'task': tid,
        'title': task['title'],
        'claimed_status': status,
        'suspected_state': suspected,
        'recommended_action': action,
        'acceptance': acc,
        'unfinished_acceptance': pending,
        'completion_record': dict(comp) if comp else None,
        'last_events': rows_to_dicts(events[-5:]),
        'touched_files': touched,
        'missing_files': [p for p in paths if root and not (Path(root) / p).exists()],
    }


def cmd_doctor(args):
    conn = connect(args.db)
    try:
        root = args.root or os.getcwd()
        if not conn.execute('SELECT 1 FROM jiras WHERE jira=?', (args.jira,)).fetchone():
            sys.exit(f'错误: 需求 {args.jira} 不存在')
        tasks = conn.execute('SELECT * FROM tasks WHERE jira=? ORDER BY seq, id', (args.jira,)).fetchall()
        reports = [reconcile_task(conn, args.jira, t, root) for t in tasks]
        summary = {}
        for r in reports:
            summary[r['suspected_state']] = summary.get(r['suspected_state'], 0) + 1
        if args.json:
            print(json.dumps({'root': str(root), 'summary': summary, 'tasks': reports},
                             ensure_ascii=False, indent=2))
            return
        print(f'对账根目录: {root}')
        for r in reports:
            if args.only_open and r['suspected_state'] == 'complete':
                continue
            acc = ', '.join(f'{k}={v}' for k, v in sorted(r['acceptance'].items())) or '无验收项'
            print(f"\n{r['task']} [{r['claimed_status']} → {r['suspected_state']}] {r['title']}")
            print(f'  验收: {acc}' + (f" / 未完成 {r['unfinished_acceptance']}" if r['unfinished_acceptance'] else ''))
            if r['touched_files']:
                print('  有改动: ' + ', '.join(f'{p}({c.strip() or "clean"})' for p, c in r['touched_files'].items()))
            if r['missing_files']:
                print('  缺失文件: ' + ', '.join(r['missing_files']))
            if r['last_events']:
                e = r['last_events'][-1]
                print(f"  最近事件: {e['ts']} {e['kind']} {(e['message'] or '')[:80]}")
            print(f"  建议: {r['recommended_action']}")
        print('\n汇总: ' + ', '.join(f'{k}={v}' for k, v in sorted(summary.items())))
    finally:
        conn.close()


# ---------------------------------------------------------------- 决策 / 契约 / 埋点 / i18n

def cmd_decision_add(args):
    def run(conn):
        require_jira(conn, args.jira)
        conn.execute('INSERT INTO decisions(jira,ref,question,options,answer,conclusion,date) VALUES(?,?,?,?,?,?,?)',
                     (args.jira, args.ref, args.question, args.options, args.answer,
                      read_text(args.conclusion, args.conclusion_file), today()))
    write(args, run)
    print('已记录决策')


def cmd_decision_list(args):
    conn = connect(args.db)
    try:
        rows = conn.execute('SELECT * FROM decisions WHERE jira = ? ORDER BY id', (args.jira,)).fetchall()
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_contract_add(args):
    def run(conn):
        require_jira(conn, args.jira)
        conn.execute('INSERT OR REPLACE INTO contracts(jira,id,method,path,status,note) VALUES(?,?,?,?,?,?)',
                     (args.jira, args.id, args.method, args.path, args.status, args.note))
    write(args, run)
    print(f'已写入契约 {args.id}')


def cmd_contract_field(args):
    def run(conn):
        conn.execute('INSERT OR REPLACE INTO contract_fields(jira,contract_id,name,type,required,enum_values,note)'
                     ' VALUES(?,?,?,?,?,?,?)',
                     (args.jira, args.contract, args.name, args.type,
                      1 if args.required else 0, args.enum, args.note))
    write(args, run)
    print(f'已写入字段 {args.contract}.{args.name}')


def cmd_contract_delta(args):
    def run(conn):
        conn.execute('INSERT INTO contract_deltas(jira,contract_id,field,expected,actual,resolution,date)'
                     ' VALUES(?,?,?,?,?,?,?)',
                     (args.jira, args.contract, args.field, args.expected, args.actual,
                      read_text(args.resolution, args.resolution_file), today()))
    write(args, run)
    print('已记录契约差异')


def cmd_contract_list(args):
    conn = connect(args.db)
    try:
        rows = conn.execute('SELECT * FROM contracts WHERE jira = ? ORDER BY id', (args.jira,)).fetchall()
        out = []
        for r in rows:
            fields = conn.execute('SELECT * FROM contract_fields WHERE jira=? AND contract_id=? ORDER BY name',
                                  (args.jira, r['id'])).fetchall()
            item = dict(r)
            item['fields'] = rows_to_dicts(fields)
            out.append(item)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_track_add(args):
    def run(conn):
        conn.execute('INSERT INTO tracking_points(jira,task_id,event,api,element,timing,note) VALUES(?,?,?,?,?,?,?)',
                     (args.jira, args.task, args.event, args.api, args.element, args.timing, args.note))
    write(args, run)
    print('已记录埋点')


def cmd_risk_add(args):
    def run(conn):
        require_jira(conn, args.jira)
        conn.execute('INSERT INTO risks(jira,descr,note) VALUES(?,?,?)',
                     (args.jira, read_text(args.desc, args.desc_file), args.note))
    write(args, run)
    print('已记录风险')


def cmd_risk_list(args):
    conn = connect(args.db)
    try:
        rows = conn.execute('SELECT * FROM risks WHERE jira = ? ORDER BY id', (args.jira,)).fetchall()
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_i18n_add(args):
    def run(conn):
        conn.execute('INSERT OR REPLACE INTO i18n_keys(jira,key,value,note) VALUES(?,?,?,?)',
                     (args.jira, args.key, args.value, args.note))
    write(args, run)
    print(f'已写入 i18n {args.key}')


# ---------------------------------------------------------------- demand（需求原文）

def cmd_demand_add(args):
    def run(conn):
        if args.jira:
            require_jira(conn, args.jira)
        conn.execute(
            'INSERT INTO demands(url,page_id,title,path,llm_path,jira,fetched_at,note) VALUES(?,?,?,?,?,?,?,?)'
            ' ON CONFLICT(url) DO UPDATE SET page_id=excluded.page_id, title=excluded.title,'
            ' path=excluded.path, llm_path=excluded.llm_path,'
            ' jira=COALESCE(excluded.jira, demands.jira), fetched_at=excluded.fetched_at,'
            ' note=COALESCE(excluded.note, demands.note)',
            (args.url, args.page_id, args.title, args.path, args.llm_path,
             args.jira, today(), read_text(args.note, args.note_file)))
        row = conn.execute('SELECT id FROM demands WHERE url = ?', (args.url,)).fetchone()
        print(f'已登记需求原文 D{row["id"]}: {args.path}')
    write(args, run)


def cmd_demand_list(args):
    conn = connect(args.db)
    try:
        sql = 'SELECT * FROM demands WHERE 1=1'
        params = []
        if args.jira:
            sql += ' AND jira = ?'
            params.append(args.jira)
        rows = conn.execute(sql + ' ORDER BY id DESC', params).fetchall()
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_demand_attach(args):
    def run(conn):
        require_jira(conn, args.jira)
        cur = conn.execute('UPDATE demands SET jira = ? WHERE id = ?', (args.jira, args.id))
        if cur.rowcount == 0:
            sys.exit(f'错误: 需求原文 D{args.id} 不存在')
    write(args, run)
    print(f'D{args.id} 已归入 {args.jira}')


# ---------------------------------------------------------------- 执行视图

def cmd_ready(args):
    conn = connect(args.db)
    try:
        rows = conn.execute("""
            SELECT t.id, t.title, t.end, t.estimate, t.milestone, t.design_ref
            FROM tasks t
            WHERE t.jira = ? AND t.status = 'todo'
              AND NOT EXISTS (
                SELECT 1 FROM task_deps d
                JOIN tasks dt ON dt.jira = d.jira AND dt.id = d.depends_on
                WHERE d.jira = t.jira AND d.task_id = t.id AND dt.status != 'done'
              )
            ORDER BY t.seq, t.id
        """, (args.jira,)).fetchall()
        data = rows_to_dicts(rows)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif not data:
            print('无可执行任务（依赖未满足或已全部完成）')
        else:
            for r in data:
                print(f"{r['id']}\t{r['end'] or ''}\t{r['estimate'] or ''}\t{r['title']}")
    finally:
        conn.close()


# ---------------------------------------------------------------- 结构设计依据核验
#
# brief 的 `## 结构设计` 小节是 Epic 级结构约束, 依据列只认四种值:
# 仓库路径 / brief 小节 / symbol@file#Lx-Ly / 「缺口→已进第 2 步提问」。
#
# codegraph 有三个会误判的行为, 这里都做了规避:
#   1. 退出码恒为 0 (符号不存在、仓库未初始化都返回 0) → 只解析 --json 内容;
#   2. 中文查询基本失效 (词法匹配, 非语义) → 依据必须写英文标识符, 中文只出现在说明里;
#   3. 索引是静态启发式的 (字符串事件总线等断链) → 只核验「符号与文件确实存在」,
#      不拿 callers/impact 的条数当断言。
# 没装 codegraph 或版本不在已测区间时降级为「路径存在 + 文件内命中标识符」, 并打 [degraded]。

DESIGN_SECTION = '结构设计'
CODEGRAPH_TESTED_PREFIX = '1.6'
_SYMBOL_EVIDENCE = re.compile(
    r'([A-Za-z_$][\w$]*)@((?:[\w.\-\[\]]+/)*[\w.\-\[\]]+\.(?:tsx?|jsx?|vue|mjs|cjs|py|go|java|kt))'
    r'(?:#L(\d+)(?:-L?(\d+))?)?')
_PATH_EVIDENCE = re.compile(
    r'((?:[\w.\-\[\]]+/)+[\w.\-\[\]]+\.(?:tsx?|jsx?|vue|mjs|cjs|py|go|java|kt|css|scss|less|json|ya?ml|md))')
_DESIGN_ITEMS = ('模块边界', '状态归属', '数据流向', '抽象', '分层')

_codegraph_cache = {}


def _codegraph_probe(root):
    """返回 (是否可用, 版本, 不可用原因)。只认已测版本区间, 其余按降级处理。"""
    key = str(root)
    if key in _codegraph_cache:
        return _codegraph_cache[key]
    exe = shutil.which('codegraph')
    if not exe:
        result = (False, None, '未安装')
    else:
        try:
            proc = subprocess.run([exe, '--version'], cwd=str(root), capture_output=True,
                                  text=True, timeout=15)
            version = (proc.stdout or '').strip().splitlines()
            version = version[-1].strip() if version else ''
        except (OSError, subprocess.SubprocessError) as exc:
            version, proc = '', None
            result = (False, None, f'调用失败({exc})')
        else:
            if not version:
                result = (False, None, '无版本输出')
            elif not version.startswith(CODEGRAPH_TESTED_PREFIX):
                result = (False, version, f'版本 {version} 不在已测区间 {CODEGRAPH_TESTED_PREFIX}.x')
            elif not (Path(root) / '.codegraph').exists():
                result = (False, version, '仓库未建 .codegraph 索引')
            else:
                result = (True, version, '')
    _codegraph_cache[key] = result
    return result


def _codegraph_query(root, symbol):
    """查符号, 返回 [(name, filePath, startLine, endLine)]; 不可解析时返回 None。"""
    exe = shutil.which('codegraph')
    try:
        proc = subprocess.run([exe, 'query', symbol, '-j', '-l', '20'], cwd=str(root),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None
    rows = []
    for item in data if isinstance(data, list) else []:
        node = item.get('node') or item
        path = (node.get('filePath') or '').replace('\\', '/')
        if path.startswith('./'):
            path = path[2:]
        rows.append((node.get('name'), path.lstrip('/'), node.get('startLine'), node.get('endLine')))
    return rows


def _brief_section(brief, name):
    """取 brief 里标题含 name 的 Markdown 小节正文; 不存在返回 None。"""
    if not brief:
        return None
    body, inside, level = [], False, 0
    for line in brief.splitlines():
        heading = re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
        if heading:
            if inside and len(heading.group(1)) <= level:
                break
            if name in heading.group(2):
                inside, level = True, len(heading.group(1))
                continue
        if inside:
            body.append(line)
    return '\n'.join(body) if inside else None


def _check_design_evidence(conn, jira, root, errors, warnings):
    row = conn.execute('SELECT project_brief FROM jiras WHERE jira=?', (jira,)).fetchone()
    section = _brief_section(row['project_brief'] if row else None, DESIGN_SECTION)
    if section is None:
        warnings.append(f'brief 缺少 `## {DESIGN_SECTION}` 小节：Epic 级结构约束未落库，执行方只能凭单卡发挥')
        return

    missing = [item for item in _DESIGN_ITEMS if item not in section]
    if missing:
        warnings.append(f'结构设计未覆盖决策项: {"、".join(missing)}')

    symbols, paths, rule_refs = [], [], []
    # 规则依据先解析并从文本中剔除, 否则 "provider-definition@..." 会被当成 symbol "definition"
    for match in _RULE_EVIDENCE.finditer(section):
        rule_id, path = match.group(1), match.group(2)
        start = int(match.group(3)) if match.group(3) else None
        end = int(match.group(4)) if match.group(4) else None
        if (rule_id, path, start, end) not in rule_refs:
            rule_refs.append((rule_id, path, start, end))
    plain, last = [], 0
    for match in _RULE_EVIDENCE.finditer(section):
        plain.append(section[last:match.start()])
        last = match.end()
    plain.append(section[last:])
    plain = ''.join(plain)
    for match in _SYMBOL_EVIDENCE.finditer(plain):
        symbol, path = match.group(1), match.group(2)
        start = int(match.group(3)) if match.group(3) else None
        end = int(match.group(4)) if match.group(4) else None
        if (symbol, path, start, end) not in symbols:
            symbols.append((symbol, path, start, end))
    for match in _PATH_EVIDENCE.finditer(section):
        path = match.group(1)
        if not any(path == s[1] for s in symbols) and not any(path == r[1] for r in rule_refs) \
                and path not in paths:
            paths.append(path)

    if not symbols and not paths and not rule_refs:
        errors.append(f'结构设计缺少可核验依据：依据列只认仓库路径 / brief 小节 / '
                      f'symbol@file#Lx-Ly / rule:<规则名>@file#Lx-Ly / 「缺口→已进第 2 步提问」，'
                      f'纯文字描述不合格')
        return

    if not root:
        warnings.append('结构设计存在依据，但未传 --root，无法核验路径与符号')
        return

    root_path = Path(root).expanduser()
    for path in paths:
        if not (root_path / path).exists():
            errors.append(f'结构设计依据路径不存在: {path}')

    if not symbols:
        if rule_refs:
            _verify_rule_evidence(root_path, rule_refs, errors, warnings)
        return
    usable, version, reason = _codegraph_probe(root_path)
    if not usable:
        warnings.append(f'[degraded] codegraph 不可用({reason})，结构设计依据改为「文件存在 + 文本命中」核对')
        for symbol, path, _, _ in symbols:
            target = root_path / path
            if not target.exists():
                errors.append(f'结构设计依据路径不存在: {path}（symbol {symbol}）')
                continue
            try:
                text = target.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                text = ''
            if not re.search(r'(?<![\w$])' + re.escape(symbol) + r'(?![\w$])', text):
                errors.append(f'结构设计依据不可核验: {path} 中找不到符号 {symbol}')
        return

    for symbol, path, start, end in symbols:
        rows = _codegraph_query(root_path, symbol)
        if rows is None:
            warnings.append(f'[degraded] codegraph 查询 {symbol} 失败，跳过该条符号核验')
            continue
        hits = [r for r in rows if r[0] == symbol and r[1] == path]
        if not hits:
            errors.append(f'结构设计依据不可核验: codegraph 索引里没有 {symbol}@{path}'
                          f'（查询返回 {len(rows)} 条同名/相关符号）')
            continue
        if start is not None:
            begin, finish = hits[0][2] or 0, hits[0][3] or hits[0][2] or 0
            if not (begin <= start <= finish):
                errors.append(f'结构设计依据行号不符: {symbol}@{path} 实际在 L{begin}-L{finish}，依据写 L{start}')

    if rule_refs:
        _verify_rule_evidence(root_path, rule_refs, errors, warnings)

    if '缺口' in section and '提问' in section:
        asked = conn.execute('SELECT COUNT(*) c FROM decisions WHERE jira=?', (jira,)).fetchone()['c']
        if not asked:
            warnings.append('结构设计声称「缺口已进第 2 步提问」，但 decisions 里没有对应记录')


# ---------------------------------------------------------------- 项目索引（codegraph + ast-grep）
#
# 索引不属于单个需求, 落在 ~/.prd-to-breakdown/index/<repo>/, 不进业务仓库、不进 jira brief:
#   meta.json     仓库路径 / commit / 工具版本 / 规模
#   scan.json     每条规则命中的文件与行号（结构约定层, 由 ast-grep 产出）
#   modules.json  按目录聚合的模块地图（文件数 / 符号数 / 各规则命中数）
# 仓库内可用 index/<repo>/rules/*.yml 覆盖内置规则（同 id 覆盖）。
#
# 分工: codegraph 回答「谁依赖谁、改动波及谁」; ast-grep 回答「这个模块内部长什么样」。

INDEX_ROOT = Path(os.environ.get('PTB_INDEX_DIR', Path.home() / '.prd-to-breakdown' / 'index'))
RULE_DIR = Path(__file__).resolve().parent.parent / 'references' / 'ast-grep-rules'
AST_GREP_TESTED_PREFIX = '0.45'
_RULE_EVIDENCE = re.compile(
    r'rule:([\w.\-]+)@((?:[\w.\-\[\]]+/)*[\w.\-\[\]]+\.\w+)(?:#L(\d+)(?:-L?(\d+))?)?')


def _repo_slug(root):
    return str(Path(root).expanduser().resolve()).strip('/').replace('/', '_').replace(':', '_')


def _index_dir(root):
    return INDEX_ROOT / _repo_slug(root)


def _ast_grep_probe():
    exe = shutil.which('ast-grep')
    if not exe:
        return False, None, '未安装'
    try:
        proc = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, None, f'调用失败({exc})'
    out = (proc.stdout or '').strip()
    version = out.split()[-1] if out else ''
    if not version:
        return False, None, '无版本输出'
    if not version.startswith(AST_GREP_TESTED_PREFIX):
        return False, version, f'版本 {version} 不在已测区间 {AST_GREP_TESTED_PREFIX}.x'
    return True, version, ''


def _rule_catalog(root=None):
    """返回 {rule_id: path}; 仓库覆盖规则优先于内置规则。"""
    catalog = {}
    folders = [RULE_DIR]
    if root:
        folders.append(_index_dir(root) / 'rules')
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob('*.yml')) + sorted(folder.glob('*.yaml')):
            try:
                lines = path.read_text(encoding='utf-8').splitlines()
            except OSError:
                continue
            for line in lines:
                match = re.match(r'^id:\s*(\S+)\s*$', line)
                if match:
                    catalog[match.group(1)] = path
                    break
    return catalog


def _scan_rule(root, rule_path, language, globs=None):
    """扫描单条规则, 返回 [(file, start, end)]（行号 1 起）; 失败返回 None。

    ast-grep 一条规则只绑定一种语言, 这里按需生成 Ts/Tsx 变体再扫描。
    """
    exe = shutil.which('ast-grep')
    if not exe:
        return None
    try:
        text = Path(rule_path).read_text(encoding='utf-8')
    except OSError:
        return None
    variant = re.sub(r'^language:\s*\w+', f'language: {language}', text, count=1, flags=re.M)
    tmp_dir = Path(tempfile.mkdtemp(prefix='ptb-rule-'))
    tmp = tmp_dir / f'{Path(rule_path).stem}.{language.lower()}.yml'
    tmp.write_text(variant, encoding='utf-8')
    cmd = [exe, 'scan', '--rule', str(tmp), '--json']
    if globs:
        cmd += ['--globs', globs]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=180)
        data = json.loads(proc.stdout or '[]')
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    hits = []
    for item in data if isinstance(data, list) else []:
        rng = item.get('range') or {}
        start = (rng.get('start') or {}).get('line')
        end = (rng.get('end') or {}).get('line')
        if start is None:
            continue
        path = (item.get('file') or '').replace('\\', '/')
        if path.startswith('./'):
            path = path[2:]
        hits.append((path, start + 1, (end + 1) if end is not None else start + 1))
    return hits


def _module_of(path, depth=2):
    parts = [p for p in path.split('/') if p]
    if not parts:
        return '.'
    if len(parts) > depth:
        return '/'.join(parts[:depth])
    return '/'.join(parts[:-1]) or '.'


def _tool_version(name):
    exe = shutil.which(name)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.split()[-1] if out else None


def _index_files(root):
    exe = shutil.which('codegraph')
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, 'files', '-j'], cwd=str(root),
                              capture_output=True, text=True, timeout=180)
        data = json.loads(proc.stdout or '[]')
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _scan_all(root):
    scan = {}
    for rule_id, rule_path in sorted(_rule_catalog(root).items()):
        hits = []
        for language in ('Ts', 'Tsx'):
            got = _scan_rule(root, rule_path, language)
            if got is None:
                sys.exit(f'错误: 规则 {rule_id} 扫描失败（{rule_path}）')
            hits.extend(got)
        hits.sort(key=lambda h: (h[0], h[1]))
        scan[rule_id] = [{'file': f, 'line': s, 'endLine': e} for f, s, e in hits]
    return scan


def _modules_of(files, scan):
    modules = {}
    for item in files:
        module = _module_of(item.get('path') or '')
        entry = modules.setdefault(module, {'files': 0, 'symbols': 0, 'rules': {}})
        entry['files'] += 1
        entry['symbols'] += item.get('nodeCount') or 0
    for rule_id, hits in scan.items():
        for hit in hits:
            module = _module_of(hit['file'])
            entry = modules.setdefault(module, {'files': 0, 'symbols': 0, 'rules': {}})
            entry['rules'][rule_id] = entry['rules'].get(rule_id, 0) + 1
    return dict(sorted(modules.items()))


def _git_head(root):
    if not shutil.which('git'):
        return ''
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=str(root),
                              capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def _git_dirty(root):
    """返回 True=有未提交改动 / False=干净 / None=不是 git 仓库。

    索引自身产生的目录（.codegraph、备份）不算「改动」，否则会把索引产物当成源码变更、每次重扫。
    """
    if not shutil.which('git'):
        return None
    try:
        proc = subprocess.run(['git', 'status', '--porcelain'], cwd=str(root),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    ignored = ('.codegraph', '.next', 'node_modules')
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if path.startswith(ignored):
            continue
        return True
    return False


def _read_meta(root):
    path = _index_dir(root) / 'meta.json'
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _write_index(root, files, scan, cg_ok, cg_version, cg_reason, ag_version, ag_reason=''):
    out = _index_dir(root)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'rules').mkdir(exist_ok=True)
    meta = {
        'root': str(Path(root).resolve()),
        'slug': _repo_slug(root),
        'commit': _git_head(root),
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'tools': {
            'codegraph': {'available': cg_ok, 'version': cg_version, 'reason': cg_reason},
            'ast_grep': {'available': bool(ag_version), 'version': ag_version, 'reason': ag_reason},
        },
        'files': len(files),
        'symbols': sum((i.get('nodeCount') or 0) for i in files),
        'rules': {rule_id: len(hits) for rule_id, hits in scan.items()},
    }
    modules = _modules_of(files, scan)
    for name, payload in (('meta.json', meta), ('scan.json', scan), ('modules.json', modules)):
        (out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return meta, modules


def _report_meta(meta, modules):
    print(f"  仓库 {meta['root']} @ {meta['commit'] or 'n/a'}")
    print(f"  codegraph: {'可用 ' + (meta['tools']['codegraph']['version'] or '') if meta['tools']['codegraph']['available'] else '不可用'}")
    print(f"  ast-grep : {meta['tools']['ast_grep']['version'] or '不可用'}")
    print(f"  文件 {meta['files']} / 符号 {meta['symbols']} / 模块 {len(modules)}")
    for rule_id, count in sorted(meta['rules'].items(), key=lambda kv: -kv[1]):
        print(f"    {rule_id:<20} {count}")


def cmd_index_build(args):
    root = Path(args.root).expanduser()
    if not root.exists():
        sys.exit(f'错误: 项目根不存在: {root}')
    ag_ok, ag_version, ag_reason = _ast_grep_probe()
    if not ag_ok:
        sys.exit(f'错误: ast-grep 不可用({ag_reason})，无法建立结构索引')
    cg_ok, cg_version, cg_reason = _codegraph_probe(root)
    scan = _scan_all(root)
    meta, modules = _write_index(root, _index_files(root), scan, cg_ok, cg_version, cg_reason,
                                 ag_version, ag_reason)
    if args.json:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return
    print(f'索引已写入 {_index_dir(root)}')
    _report_meta(meta, modules)


def _git_worktree_paths(root):
    """同仓所有 worktree 的绝对路径, 主 worktree 在首位; 非 git 仓库返回 []。"""
    if not shutil.which('git'):
        return []
    try:
        proc = subprocess.run(['git', 'worktree', 'list', '--porcelain'], cwd=str(root),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    paths = []
    for line in proc.stdout.splitlines():
        if line.startswith('worktree '):
            paths.append(Path(line[len('worktree '):].strip()).resolve())
    return paths


def _find_seed_index(root):
    """找一个已有 codegraph 索引的同仓 worktree 作为播种源（优先主 worktree）。"""
    root = Path(root).resolve()
    candidates = [p for p in _git_worktree_paths(root)
                  if p != root and (p / '.codegraph' / 'codegraph.db').exists()]
    return candidates[0] if candidates else None


def _copy_db(src, dst):
    """用 SQLite backup API 复制, 源库被 daemon 持有 WAL 时也能拿到一致快照。"""
    src_conn = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _seed_index(src_root, dst_root):
    """把源 worktree 的索引播种到目标 worktree（只搬库, 不搬 daemon 的 pid/sock）。"""
    src_dir, dst_dir = src_root / '.codegraph', dst_root / '.codegraph'
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in ('codegraph.db', 'lessons.db'):
        if (src_dir / name).exists():
            _copy_db(src_dir / name, dst_dir / name)
    gitignore = dst_dir / '.gitignore'
    if not gitignore.exists():
        gitignore.write_text('# CodeGraph data files\n*.db\n*.db-wal\n*.db-shm\ncache/\n*.log\n.dirty\n',
                             encoding='utf-8')


def cmd_index_ensure(args):
    """索引保鲜：缺失则播种或 codegraph init，已存在则增量 sync；ast-grep 按变更决定是否重扫。"""
    root = Path(args.root).expanduser()
    if not root.exists():
        sys.exit(f'错误: 项目根不存在: {root}')

    if args.background:
        log = _index_dir(root) / 'ensure.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(Path(__file__).resolve()), 'index', 'ensure', '--root', str(root)]
        if not args.seed:
            cmd.append('--no-seed')
        with open(log, 'ab') as fh:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        print(f'已在后台建立索引（pid {proc.pid}），日志 {log}')
        return

    actions = []
    meta = _read_meta(root)

    # ---- codegraph: 没有就播种/init，有就增量 sync
    cg_exe = shutil.which('codegraph')
    cg_version = _tool_version('codegraph')
    db = root / '.codegraph' / 'codegraph.db'
    need_init = bool(args.rebuild or not db.exists())
    if not need_init and meta:
        previous = ((meta.get('tools') or {}).get('codegraph') or {}).get('version')
        if previous and cg_version and previous != cg_version:
            need_init = True
            actions.append(f'codegraph 从 {previous} 升到 {cg_version}，旧库 schema 需重建')
    seeded = False
    if need_init and args.seed and not args.rebuild:
        source = _find_seed_index(root)
        if source:
            try:
                _seed_index(source, root)
                seeded = True
                actions.append(f'从 {source} 播种索引（同仓 worktree，相对路径可直接复用）')
                need_init = False
            except (sqlite3.Error, OSError) as exc:
                actions.append(f'播种失败({exc})，退回全量 init')
    if not cg_exe:
        cg_action = 'unavailable'
    elif need_init:
        if (root / '.codegraph').exists():
            backup = root / f".codegraph.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
            (root / '.codegraph').rename(backup)
            actions.append(f'旧索引已备份为 {backup.name}')
        try:
            proc = subprocess.run([cg_exe, 'init', '-y'], cwd=str(root), capture_output=True,
                                  text=True, timeout=1800)
            cg_action = 'init' if proc.returncode == 0 else 'init-failed'
        except (OSError, subprocess.SubprocessError):
            cg_action = 'init-failed'
        actions.append(f'codegraph {cg_action}（全量）')
    else:
        try:
            proc = subprocess.run([cg_exe, 'sync'], cwd=str(root), capture_output=True,
                                  text=True, timeout=1800)
            cg_action = 'sync' if proc.returncode == 0 else 'sync-failed'
        except (OSError, subprocess.SubprocessError):
            cg_action = 'sync-failed'
        actions.append(f'codegraph {cg_action}（增量{"，播种后对账" if seeded else ""}）')

    # ---- ast-grep: 无索引可 sync，按 HEAD / 工作区 / 工具版本决定是否重扫
    ag_version = _tool_version('ast-grep')
    out = _index_dir(root)
    need_scan = bool(args.force or meta is None or not (out / 'scan.json').exists())
    if not need_scan:
        if (meta.get('tools', {}).get('ast_grep') or {}).get('version') != ag_version:
            need_scan = True
            actions.append('ast-grep 版本变化，重扫')
        elif meta.get('commit') != _git_head(root):
            need_scan = True
            actions.append(f"仓库 HEAD 变化（{meta.get('commit') or 'n/a'} -> {_git_head(root) or 'n/a'}），重扫")
        elif _git_dirty(root):
            need_scan = True
            actions.append('工作区有未提交改动，重扫')

    if ag_version is None:
        ag_action, ag_meta = 'unavailable', (meta['tools']['ast_grep'] if meta else {})
    elif need_scan:
        scan = _scan_all(root)
        cg_ok = bool(db.exists())
        meta, _ = _write_index(root, _index_files(root), scan, cg_ok, cg_version,
                               '' if cg_ok else '仓库未建索引', ag_version)
        ag_action = 'scan'
        actions.append('ast-grep 重扫并写入索引')
    else:
        ag_action = 'skip'
        # codegraph 版本/同步结果仍需登记，否则下次会重复判定
        if meta is not None:
            meta.setdefault('tools', {})['codegraph'] = {
                'available': cg_action not in ('unavailable', 'init-failed', 'sync-failed'),
                'version': cg_version, 'reason': ''}
            meta['generated_at'] = datetime.datetime.now().isoformat(timespec='seconds')
            (out / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                           encoding='utf-8')
        actions.append('ast-grep 跳过（无变更）')

    report = {
        'root': str(root.resolve()), 'codegraph': cg_action, 'ast_grep': ag_action,
        'actions': actions,
        'generated_at': (meta or {}).get('generated_at'),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f'索引保鲜 {report["root"]}')
    for line in actions:
        print(f'  {line}')
    if meta:
        print(f"  上次结构扫描 {meta.get('generated_at')} / {meta.get('files')} 文件 / {meta.get('symbols')} 符号")


def cmd_index_show(args):
    out = _index_dir(args.root)
    meta_path = out / 'meta.json'
    if not meta_path.exists():
        sys.exit(f'错误: 未找到索引 {out}，先跑 ptb index build --root <项目根>')
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    modules = json.loads((out / 'modules.json').read_text(encoding='utf-8'))
    scan = json.loads((out / 'scan.json').read_text(encoding='utf-8'))
    if args.module:
        picked = {k: v for k, v in modules.items() if args.module in k}
        if not picked:
            sys.exit(f'错误: 没有模块匹配 {args.module}')
        if args.json:
            print(json.dumps({'module': picked, 'hits': {rid: [h for h in hits if _module_of(h['file']) in picked]
                                                        for rid, hits in scan.items()}},
                             ensure_ascii=False, indent=2))
            return
        for name, info in picked.items():
            print(f"{name}\t文件 {info['files']}\t符号 {info['symbols']}")
            for rule_id, count in sorted(info['rules'].items(), key=lambda kv: -kv[1]):
                print(f"  {rule_id:<20} {count}")
                for hit in [h for h in scan.get(rule_id, []) if _module_of(h['file']) == name][:5]:
                    print(f"    {hit['file']}:{hit['line']}")
        return
    if args.json:
        print(json.dumps({'meta': meta, 'modules': modules, 'scan': scan}, ensure_ascii=False, indent=2))
        return
    print(f"索引 {meta['root']} @ {meta['commit'] or 'n/a'}（生成于 {meta['generated_at']}）")
    print(f"文件 {meta['files']} / 符号 {meta['symbols']} / 模块 {len(modules)}")
    print('规则命中：')
    for rule_id, count in sorted(meta['rules'].items(), key=lambda kv: -kv[1]):
        print(f"  {rule_id:<20} {count}")
    print('模块（按符号数）：')
    for name, info in sorted(modules.items(), key=lambda kv: -kv[1]['symbols'])[:args.limit]:
        rules = ' '.join(f'{k}={v}' for k, v in sorted(info['rules'].items()))
        print(f"  {name:<32} 文件 {info['files']:<5} 符号 {info['symbols']:<6} {rules}")


def _verify_rule_evidence(root_path, rule_refs, errors, warnings):
    ok, _, reason = _ast_grep_probe()
    catalog = _rule_catalog(root_path)
    for rule_id, path, start, end in rule_refs:
        target = root_path / path
        if not target.exists():
            errors.append(f'结构设计依据路径不存在: {path}（规则 {rule_id}）')
            continue
        if rule_id not in catalog:
            errors.append(f'结构设计依据引用了不存在的规则: {rule_id}'
                          f'（可用: {", ".join(sorted(catalog))}）')
            continue
        if not ok:
            warnings.append(f'[degraded] ast-grep 不可用({reason})，规则依据 {rule_id}@{path} 只核对文件存在')
            continue
        hits = []
        failed = False
        for language in ('Ts', 'Tsx'):
            got = _scan_rule(root_path, catalog[rule_id], language, globs=path)
            if got is None:
                failed = True
                break
            hits.extend(got)
        if failed:
            warnings.append(f'[degraded] ast-grep 扫描规则 {rule_id} 失败，跳过该条核验')
            continue
        if not hits:
            errors.append(f'结构设计依据不可核验: 规则 {rule_id} 在 {path} 无命中')
            continue
        if start is not None:
            in_range = [h for h in hits if h[0] == path and h[1] <= start <= h[2]]
            if not in_range:
                where = '、'.join(f'L{h[1]}-L{h[2]}' for h in hits[:3])
                errors.append(f'结构设计依据行号不符: 规则 {rule_id}@{path} 实际命中 {where}，依据写 L{start}')


def validate_jira(conn, jira, root=None):
    errors, warnings = [], []
    if not conn.execute('SELECT 1 FROM jiras WHERE jira=?', (jira,)).fetchone():
        return [f'需求 {jira} 不存在'], []
    task_ids = {r['id'] for r in conn.execute('SELECT id FROM tasks WHERE jira=?', (jira,))}
    # 依赖存在性
    for d in conn.execute('SELECT * FROM task_deps WHERE jira=?', (jira,)):
        if d['depends_on'] not in task_ids:
            errors.append(f"{d['task_id']} 依赖的任务 {d['depends_on']} 不存在")
        if d['depends_on'] == d['task_id']:
            errors.append(f"{d['task_id']} 依赖自身")
    # 环检测
    graph = {t: [] for t in task_ids}
    for d in conn.execute('SELECT * FROM task_deps WHERE jira=?', (jira,)):
        if d['task_id'] in graph and d['depends_on'] in graph:
            graph[d['task_id']].append(d['depends_on'])
    state = {}

    def dfs(node, path):
        state[node] = 1
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                errors.append('依赖成环: ' + ' -> '.join(path + [nxt]))
                return
            if state.get(nxt) is None:
                dfs(nxt, path + [nxt])
        state[node] = 2

    for node in list(graph):
        if state.get(node) is None:
            dfs(node, [node])
    # 需求覆盖
    reqs = {r['id'] for r in conn.execute('SELECT id FROM requirements WHERE jira=?', (jira,))}
    covered = {r['req'] for r in conn.execute('SELECT req FROM task_requirements WHERE jira=?', (jira,))}
    for r in sorted(reqs - covered):
        warnings.append(f'需求 {r} 没有被任何任务覆盖')
    for r in sorted(covered - reqs):
        errors.append(f'任务引用了不存在的需求 {r}')
    # 验收
    for t in sorted(task_ids):
        acc = conn.execute('SELECT * FROM acceptance WHERE jira=? AND task_id=?', (jira, t)).fetchall()
        if not acc:
            warnings.append(f'{t} 没有验收标准')
        for a in acc:
            if a['kind'] == 'visual' and a['via'] != 'ui-restore':
                warnings.append(f"{t} 的视觉验收未标注 via=ui-restore")
        status = conn.execute('SELECT status FROM tasks WHERE jira=? AND id=?', (jira, t)).fetchone()['status']
        if status == 'done':
            comp = conn.execute('SELECT 1 FROM completions WHERE jira=? AND task_id=?', (jira, t)).fetchone()
            if not comp:
                errors.append(f'{t} 标记完成但没有完成记录')
            failed = conn.execute("SELECT COUNT(*) c FROM acceptance WHERE jira=? AND task_id=? AND result != 'pass'",
                                  (jira, t)).fetchone()['c']
            if failed:
                errors.append(f'{t} 标记完成但仍有 {failed} 条验收未通过（结果非 pass）')
        if status == 'blocked':
            notes = conn.execute('SELECT notes FROM tasks WHERE jira=? AND id=?', (jira, t)).fetchone()['notes']
            if not notes:
                warnings.append(f'{t} 阻塞但没有写原因(notes)')
    # 文件路径存在性
    if root:
        root_path = Path(root).expanduser()
        # 同一 Jira 内被其它任务登记为 add 的路径集合：这些文件按计划顺序会先被创建
        planned = {r['path'] for r in conn.execute(
            "SELECT path FROM task_files WHERE jira=? AND change='add'", (jira,))}
        for f in conn.execute('SELECT * FROM task_files WHERE jira=?', (jira,)):
            target = root_path / f['path']
            if f['change'] == 'add' and target.exists():
                errors.append(f"{f['task_id']} 标记新增但文件已存在: {f['path']}")
            if f['change'] in ('modify', 'delete') and not target.exists():
                if f['path'] in planned:
                    continue  # 由本 Jira 另一张卡创建，属合法的前置引用，不算过期
                errors.append(f"{f['task_id']} 标记{f['change']}但文件不存在: {f['path']}")
    # 设计锚点路径
    for d in conn.execute('SELECT * FROM design_drafts WHERE jira=?', (jira,)):
        if not Path(d['path']).expanduser().exists():
            warnings.append(f"设计稿路径不存在: {d['path']}")
    # 结构设计依据（Epic 级）
    _check_design_evidence(conn, jira, root, errors, warnings)
    return errors, warnings


def cmd_validate(args):
    conn = connect(args.db)
    try:
        errors, warnings = validate_jira(conn, args.jira, root=args.root)
        if args.json:
            print(json.dumps({'errors': errors, 'warnings': warnings}, ensure_ascii=False, indent=2))
        else:
            for e in errors:
                print('错误  ' + e)
            for w in warnings:
                print('警告  ' + w)
            print(f'\n{len(errors)} 个错误, {len(warnings)} 个警告')
        sys.exit(1 if errors else 0)
    finally:
        conn.close()


# ---------------------------------------------------------------- 纠正 / 升级

def cmd_correction_add(args):
    def run(conn):
        prefix = 'F-' + datetime.date.today().strftime('%Y%m%d')
        row = conn.execute("SELECT id FROM corrections WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
                           (prefix + '%',)).fetchone()
        n = 1 if not row else int(row['id'].rsplit('-', 1)[1]) + 1
        cid = f'{prefix}-{n:02d}'
        conn.execute(
            'INSERT INTO corrections(id,date,jira,task,stage,dimension,user_instruction,expected,actual,'
            'applied_change,root_cause,blame,rule_gap,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (cid, today(), args.jira, args.task, args.stage, args.dimension,
             read_text(args.instruction, args.instruction_file),
             read_text(args.expected, args.expected_file),
             read_text(args.actual, args.actual_file),
             read_text(args.change, args.change_file),
             read_text(args.root_cause, args.root_cause_file),
             args.blame, read_text(args.rule_gap, args.rule_gap_file), 'open'))
        print(f'已记录纠正 {cid}')
    write(args, run)


def cmd_correction_list(args):
    conn = connect(args.db)
    try:
        sql = 'SELECT * FROM corrections WHERE 1=1'
        params = []
        if args.status:
            sql += ' AND status = ?'
            params.append(args.status)
        if args.jira:
            sql += ' AND jira = ?'
            params.append(args.jira)
        rows = conn.execute(sql + ' ORDER BY date DESC, id DESC', params).fetchall()
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_correction_set(args):
    def run(conn):
        # status 必填保持不变；blame / rule_gap 允许事后补填（复盘时经常晚于捕获获得归因）
        sets, params = ['status = ?'], [args.status]
        if args.blame:
            sets.append('blame = ?'); params.append(args.blame)
        if args.rule_gap:
            sets.append('rule_gap = ?'); params.append(args.rule_gap)
        params.append(args.id)
        cur = conn.execute(f"UPDATE corrections SET {', '.join(sets)} WHERE id = ?", params)
        if cur.rowcount == 0:
            sys.exit(f'错误: 纠正记录 {args.id} 不存在')
    write(args, run)
    print(f'纠正 {args.id} -> {args.status}')


def cmd_correction_cluster(args):
    conn = connect(args.db)
    try:
        sql = "SELECT dimension, blame, rule_gap, COUNT(*) c, MIN(date) first, MAX(date) last," \
              " GROUP_CONCAT(id) ids FROM corrections WHERE status='open'"
        params = []
        if args.since:
            sql += ' AND date >= ?'
            params.append(args.since)
        sql += ' GROUP BY dimension, blame, rule_gap ORDER BY c DESC'
        rows = conn.execute(sql, params).fetchall()
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_upgrade_add(args):
    def run(conn):
        prefix = 'U-' + datetime.date.today().strftime('%Y%m%d')
        row = conn.execute("SELECT id FROM upgrades WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
                           (prefix + '%',)).fetchone()
        n = 1 if not row else int(row['id'].rsplit('-', 1)[1]) + 1
        uid = f'{prefix}-{n:02d}'
        conn.execute('INSERT INTO upgrades(id,date,summary,targets,commit_hash,note) VALUES(?,?,?,?,?,?)',
                     (uid, today(), read_text(args.summary, args.summary_file),
                      args.targets, args.commit, args.note))
        for cid in (args.corrections or '').split(','):
            cid = cid.strip()
            if cid:
                if not conn.execute('SELECT 1 FROM corrections WHERE id=?', (cid,)).fetchone():
                    sys.exit(f'错误: 纠正记录 {cid} 不存在')
                conn.execute('INSERT OR IGNORE INTO upgrade_corrections(upgrade_id,correction_id) VALUES(?,?)',
                             (uid, cid))
                conn.execute("UPDATE corrections SET status='adopted' WHERE id=?", (cid,))
        print(f'已记录升级 {uid}')
    write(args, run)


def cmd_upgrade_list(args):
    conn = connect(args.db)
    try:
        rows = conn.execute('SELECT * FROM upgrades ORDER BY date DESC, id DESC').fetchall()
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
    finally:
        conn.close()


# ---------------------------------------------------------------- 其他

def cmd_export(args):
    conn = connect(args.db)
    try:
        data = {'jira': render_jira(conn, args.jira)}
        for table in ('design_drafts', 'requirements', 'tasks', 'contracts', 'contract_fields',
                      'decisions', 'tracking_points', 'i18n_keys', 'risks', 'contract_deltas'):
            rows = conn.execute(f'SELECT * FROM {table} WHERE jira = ?', (args.jira,)).fetchall()
            data[table] = rows_to_dicts(rows)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding='utf-8')
            print(f'已导出到 {args.out}')
        else:
            print(text)
    finally:
        conn.close()


def cmd_backup(args):
    conn = connect(args.db)
    try:
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        target = Path(args.backup_dir).expanduser() / f'prd-{stamp}-manual.db'
        target.parent.mkdir(parents=True, exist_ok=True)
        conn.execute('VACUUM INTO ?', (str(target),))
        print(f'已备份到 {target}')
    finally:
        conn.close()


def cmd_init(args):
    conn = connect(args.db)
    conn.close()
    print(f'库已就绪: {args.db}')


# ---------------------------------------------------------------- 参数

def build_parser():
    ap = argparse.ArgumentParser(prog='ptb', description='prd-to-breakdown 状态库 CLI')
    ap.add_argument('--db', default=str(DEFAULT_DB), help=f'库路径（默认 {DEFAULT_DB}）')
    ap.add_argument('--backup-dir', default=str(DEFAULT_BACKUP_DIR), help='备份目录')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('init', help='初始化库').set_defaults(func=cmd_init)
    sub.add_parser('backup', help='手动备份').set_defaults(func=cmd_backup)

    # jira
    jira = sub.add_parser('jira', help='需求')
    jsub = jira.add_subparsers(dest='sub', required=True)
    p = jsub.add_parser('add')
    p.add_argument('--jira', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--scope', default='frontend', choices=['frontend', 'backend', 'both'])
    p.add_argument('--status', default='breaking', choices=['breaking', 'developing', 'done', 'archived'])
    p.add_argument('--brief'); p.add_argument('--brief-file', dest='brief_file')
    p.add_argument('--design-notes', dest='design_notes'); p.add_argument('--design-notes-file', dest='design_notes_file')
    p.add_argument('--baseline')
    p.add_argument('--out-of-scope', dest='out_of_scope'); p.add_argument('--out-of-scope-file', dest='out_of_scope_file')
    p.set_defaults(func=cmd_jira_add)

    p = jsub.add_parser('set')
    p.add_argument('--jira', required=True)
    for name in ('title', 'scope', 'status', 'baseline'):
        p.add_argument(f'--{name}')
    p.add_argument('--brief'); p.add_argument('--brief-file', dest='brief_file')
    p.add_argument('--design-notes', dest='design_notes'); p.add_argument('--design-notes-file', dest='design_notes_file')
    p.add_argument('--out-of-scope', dest='out_of_scope'); p.add_argument('--out-of-scope-file', dest='out_of_scope_file')
    p.set_defaults(func=cmd_jira_set)

    p = jsub.add_parser('show')
    p.add_argument('--jira', required=True)
    p.add_argument('--md', action='store_true')
    p.set_defaults(func=cmd_jira_show)

    p = jsub.add_parser('list')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_jira_list)

    # design
    demand = sub.add_parser('demand', help='需求原文（Demand collection）')
    dsub0 = demand.add_subparsers(dest='sub', required=True)
    q = dsub0.add_parser('add')
    q.add_argument('--url', required=True); q.add_argument('--path', required=True)
    q.add_argument('--page-id', dest='page_id'); q.add_argument('--title')
    q.add_argument('--llm-path', dest='llm_path'); q.add_argument('--jira')
    q.add_argument('--note'); q.add_argument('--note-file', dest='note_file')
    q.set_defaults(func=cmd_demand_add)
    q = dsub0.add_parser('list')
    q.add_argument('--jira')
    q.set_defaults(func=cmd_demand_list)
    q = dsub0.add_parser('attach')
    q.add_argument('--id', type=int, required=True); q.add_argument('--jira', required=True)
    q.set_defaults(func=cmd_demand_attach)

    # design
    design = sub.add_parser('design', help='设计稿')
    dsub = design.add_subparsers(dest='sub', required=True)
    p = dsub.add_parser('add')
    p.add_argument('--jira', required=True); p.add_argument('--path', required=True)
    p.add_argument('--covers'); p.add_argument('--note')
    p.set_defaults(func=cmd_design_add)
    p = dsub.add_parser('list')
    p.add_argument('--jira', required=True)
    p.set_defaults(func=cmd_design_list)

    # req
    req = sub.add_parser('req', help='需求条目')
    rsub = req.add_subparsers(dest='sub', required=True)
    p = rsub.add_parser('link')
    p.add_argument('--jira', required=True); p.add_argument('--id', required=True)
    p.add_argument('--task', required=True)
    p.set_defaults(func=cmd_req_link)
    p = rsub.add_parser('add')
    p.add_argument('--jira', required=True); p.add_argument('--id', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--description'); p.add_argument('--description-file', dest='description_file')
    p.add_argument('--acceptance'); p.add_argument('--acceptance-file', dest='acceptance_file')
    p.add_argument('--priority', default='must', choices=['must', 'should', 'optional'])
    p.add_argument('--excluded-frontend', dest='excluded_frontend')
    p.add_argument('--excluded-frontend-file', dest='excluded_frontend_file')
    p.set_defaults(func=cmd_req_add)
    p = rsub.add_parser('list')
    p.add_argument('--jira', required=True)
    p.set_defaults(func=cmd_req_list)

    # task
    task = sub.add_parser('task', help='任务')
    tsub = task.add_subparsers(dest='sub', required=True)
    p = tsub.add_parser('add')
    p.add_argument('--jira', required=True); p.add_argument('--id', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--end', choices=['frontend', 'backend', 'integration'])
    p.add_argument('--status', default='todo', choices=['todo', 'doing', 'done', 'blocked'])
    p.add_argument('--estimate', choices=['S', 'M', 'L'])
    p.add_argument('--seq', type=int)
    p.add_argument('--parallel'); p.add_argument('--milestone')
    p.add_argument('--design-ref', dest='design_ref')
    p.add_argument('--dep', action='append'); p.add_argument('--req', action='append')
    for name in TEXT_FIELDS:
        flag = name.replace('_', '-')
        p.add_argument(f'--{flag}'); p.add_argument(f'--{flag}-file', dest=f'{name}_file')
    p.set_defaults(func=cmd_task_add)

    p = tsub.add_parser('set')
    p.add_argument('id'); p.add_argument('--jira', required=True)
    for name in ('title', 'end', 'status', 'estimate', 'parallel', 'milestone', 'design_ref'):
        p.add_argument(f'--{name}')
    p.add_argument('--seq', type=int)
    p.set_defaults(func=cmd_task_set)

    p = tsub.add_parser('text')
    p.add_argument('id'); p.add_argument('--jira', required=True)
    p.add_argument('--field', required=True)
    p.add_argument('--text'); p.add_argument('--file', default='-')
    p.set_defaults(func=cmd_task_text)

    p = tsub.add_parser('show')
    p.add_argument('id'); p.add_argument('--jira', required=True); p.add_argument('--md', action='store_true')
    p.set_defaults(func=cmd_task_show)

    p = tsub.add_parser('list')
    p.add_argument('--jira', required=True); p.add_argument('--status'); p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_task_list)

    # dep / file / acc / verify / complete
    p = sub.add_parser('dep', help='依赖')
    dsub2 = p.add_subparsers(dest='sub', required=True)
    q = dsub2.add_parser('add')
    q.add_argument('--jira', required=True); q.add_argument('--task', required=True); q.add_argument('--on', required=True)
    q.set_defaults(func=cmd_dep_add)

    p = sub.add_parser('file', help='任务涉及文件')
    fsub = p.add_subparsers(dest='sub', required=True)
    q = fsub.add_parser('add')
    q.add_argument('--jira', required=True); q.add_argument('--task', required=True)
    q.add_argument('--path', required=True)
    q.add_argument('--change', default='modify', choices=['add', 'modify', 'delete', 'refactor', 'none'])
    q.add_argument('--note')
    q.set_defaults(func=cmd_file_add)

    p = sub.add_parser('acc', help='验收项')
    asub = p.add_subparsers(dest='sub', required=True)
    q = asub.add_parser('add')
    q.add_argument('--jira', required=True); q.add_argument('--task', required=True)
    q.add_argument('--desc'); q.add_argument('--desc-file', dest='desc_file')
    q.add_argument('--kind', default='functional', choices=['functional', 'visual', 'regression', 'contract'])
    q.add_argument('--via'); q.add_argument('--command'); q.add_argument('--contract', action='store_true')
    q.set_defaults(func=cmd_acc_add)
    q = asub.add_parser('set')
    q.add_argument('--jira', required=True); q.add_argument('--task', required=True)
    q.add_argument('--id', type=int, required=True)
    q.add_argument('--result', required=True, choices=['pending', 'pass', 'fail', 'blocked'])
    q.add_argument('--desc', help='修正验收文本（PRD 口径变更时同步旧条目）')
    q.set_defaults(func=cmd_acc_set)

    p = sub.add_parser('verify', help='验证命令')
    vsub = p.add_subparsers(dest='sub', required=True)
    q = vsub.add_parser('add')
    q.add_argument('--jira', required=True); q.add_argument('--task', required=True); q.add_argument('--cmd', required=True)
    q.set_defaults(func=cmd_verify_add)

    p = sub.add_parser('complete', help='标记任务完成')
    p.add_argument('--jira', required=True); p.add_argument('--task', required=True)
    p.add_argument('--evidence'); p.add_argument('--evidence-file', dest='evidence_file')
    p.add_argument('--deviations'); p.add_argument('--deviations-file', dest='deviations_file')
    p.add_argument('--date')
    p.add_argument('--force', action='store_true', help='验收未全通过时强行标记完成')
    p.set_defaults(func=cmd_complete)

    p = sub.add_parser('start', help='标记开工（置 doing 并记录事件）')
    p.add_argument('--jira', required=True); p.add_argument('--task', required=True)
    p.add_argument('--msg'); p.add_argument('--msg-file', dest='msg_file')
    p.set_defaults(func=cmd_start)

    p = sub.add_parser('progress', help='追加进度事件')
    p.add_argument('--jira', required=True); p.add_argument('--task', required=True)
    p.add_argument('--kind', default='progress',
                   choices=['progress', 'verify', 'block', 'resume', 'note'])
    p.add_argument('--msg'); p.add_argument('--msg-file', dest='msg_file')
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser('log', help='查看任务进度事件')
    p.add_argument('--jira', required=True); p.add_argument('--task', required=True)
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_log)

    p = sub.add_parser('doctor', help='库内状态与项目实际进度对账')
    p.add_argument('--jira', required=True)
    p.add_argument('--root', help='项目根（默认当前目录）')
    p.add_argument('--json', action='store_true')
    p.add_argument('--only-open', dest='only_open', action='store_true', help='只显示未完成的任务')
    p.set_defaults(func=cmd_doctor)

    # decision
    p = sub.add_parser('decision', help='待确认/决策记录')
    dsub3 = p.add_subparsers(dest='sub', required=True)
    q = dsub3.add_parser('add')
    q.add_argument('--jira', required=True); q.add_argument('--ref')
    q.add_argument('--question', required=True); q.add_argument('--options')
    q.add_argument('--answer'); q.add_argument('--conclusion'); q.add_argument('--conclusion-file', dest='conclusion_file')
    q.set_defaults(func=cmd_decision_add)
    q = dsub3.add_parser('list')
    q.add_argument('--jira', required=True)
    q.set_defaults(func=cmd_decision_list)

    # contract
    p = sub.add_parser('contract', help='接口契约')
    csub = p.add_subparsers(dest='sub', required=True)
    q = csub.add_parser('add')
    q.add_argument('--jira', required=True); q.add_argument('--id', required=True)
    q.add_argument('--method'); q.add_argument('--path')
    q.add_argument('--status', default='fixme', choices=['fixme', 'defined', 'aligned'])
    q.add_argument('--note')
    q.set_defaults(func=cmd_contract_add)
    q = csub.add_parser('field')
    q.add_argument('--jira', required=True); q.add_argument('--contract', required=True)
    q.add_argument('--name', required=True); q.add_argument('--type')
    q.add_argument('--required', action='store_true'); q.add_argument('--enum', dest='enum'); q.add_argument('--note')
    q.set_defaults(func=cmd_contract_field)
    q = csub.add_parser('delta')
    q.add_argument('--jira', required=True); q.add_argument('--contract'); q.add_argument('--field')
    q.add_argument('--expected'); q.add_argument('--actual')
    q.add_argument('--resolution'); q.add_argument('--resolution-file', dest='resolution_file')
    q.set_defaults(func=cmd_contract_delta)
    q = csub.add_parser('list')
    q.add_argument('--jira', required=True)
    q.set_defaults(func=cmd_contract_list)

    # track / i18n
    p = sub.add_parser('track', help='埋点')
    tsub2 = p.add_subparsers(dest='sub', required=True)
    q = tsub2.add_parser('add')
    q.add_argument('--jira', required=True); q.add_argument('--task')
    q.add_argument('--event', required=True, choices=['exposure', 'click', 'pv'])
    q.add_argument('--api'); q.add_argument('--element'); q.add_argument('--timing'); q.add_argument('--note')
    q.set_defaults(func=cmd_track_add)

    p = sub.add_parser('i18n', help='多语言 key')
    isub = p.add_subparsers(dest='sub', required=True)
    q = isub.add_parser('add')
    q.add_argument('--jira', required=True); q.add_argument('--key', required=True)
    q.add_argument('--value'); q.add_argument('--note')
    q.set_defaults(func=cmd_i18n_add)

    p = sub.add_parser('risk', help='风险与约束')
    rsub2 = p.add_subparsers(dest='sub', required=True)
    q = rsub2.add_parser('add')
    q.add_argument('--jira', required=True)
    q.add_argument('--desc'); q.add_argument('--desc-file', dest='desc_file')
    q.add_argument('--note')
    q.set_defaults(func=cmd_risk_add)
    q = rsub2.add_parser('list')
    q.add_argument('--jira', required=True)
    q.set_defaults(func=cmd_risk_list)

    # 执行与校验
    p = sub.add_parser('ready', help='依赖已就绪的待办任务')
    p.add_argument('--jira', required=True); p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_ready)

    p = sub.add_parser('validate', help='一致性校验')
    p.add_argument('--jira', required=True); p.add_argument('--root', help='项目根，用于文件路径存在性校验')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser('index', help='项目索引（codegraph + ast-grep）')
    ixsub = p.add_subparsers(dest='sub', required=True)
    q = ixsub.add_parser('build', help='重建索引')
    q.add_argument('--root', required=True); q.add_argument('--json', action='store_true')
    q.set_defaults(func=cmd_index_build)
    q = ixsub.add_parser('show', help='查看索引 / 单个模块')
    q.add_argument('--root', required=True); q.add_argument('--module')
    q.add_argument('--limit', type=int, default=15); q.add_argument('--json', action='store_true')
    q.set_defaults(func=cmd_index_show)
    q = ixsub.add_parser('ensure', help='索引保鲜：未建则播种或 init，已建则增量 sync')
    q.add_argument('--root', required=True)
    q.add_argument('--force', action='store_true', help='强制重扫 ast-grep 规则')
    q.add_argument('--rebuild', action='store_true', help='强制全量重建 codegraph 索引')
    q.add_argument('--no-seed', dest='seed', action='store_false',
                   help='不从同仓其它 worktree 播种，直接全量 init')
    q.add_argument('--background', action='store_true', help='后台执行（建 worktree 时用）')
    q.add_argument('--json', action='store_true')
    q.set_defaults(func=cmd_index_ensure, seed=True)

    # correction / upgrade
    p = sub.add_parser('correction', help='纠正记录')
    cosub = p.add_subparsers(dest='sub', required=True)
    q = cosub.add_parser('add')
    q.add_argument('--jira'); q.add_argument('--task')
    q.add_argument('--stage', default='review', choices=['review', 'integration', 'prod'])
    q.add_argument('--dimension', required=True, choices=['visual', 'requirement', 'contract', 'scope', 'docs'])
    for name in ('instruction', 'expected', 'actual', 'change', 'root_cause', 'rule_gap'):
        flag = name.replace('_', '-')
        q.add_argument(f'--{flag}'); q.add_argument(f'--{flag}-file', dest=f'{name}_file')
    q.add_argument('--blame', default='unclear', choices=['prd-to-breakdown', 'task-runner', 'ui-restore', 'unclear'])
    q.set_defaults(func=cmd_correction_add)
    q = cosub.add_parser('list')
    q.add_argument('--status', default='open'); q.add_argument('--jira')
    q.set_defaults(func=cmd_correction_list)
    q = cosub.add_parser('set')
    q.add_argument('--id', required=True)
    q.add_argument('--status', required=True, choices=['open', 'adopted', 'rejected'])
    q.add_argument('--blame', choices=['prd-to-breakdown', 'task-runner', 'ui-restore', 'unclear'],
                   help='补填/修正归因')
    q.add_argument('--rule-gap', help='补填一句话可执行的规则缺口')
    q.set_defaults(func=cmd_correction_set)
    q = cosub.add_parser('cluster')
    q.add_argument('--since'); q.set_defaults(func=cmd_correction_cluster)

    p = sub.add_parser('upgrade', help='技能升级记录')
    usub = p.add_subparsers(dest='sub', required=True)
    q = usub.add_parser('add')
    q.add_argument('--summary'); q.add_argument('--summary-file', dest='summary_file')
    q.add_argument('--targets', required=True); q.add_argument('--commit'); q.add_argument('--note')
    q.add_argument('--corrections')
    q.set_defaults(func=cmd_upgrade_add)
    q = usub.add_parser('list')
    q.set_defaults(func=cmd_upgrade_list)

    p = sub.add_parser('export', help='导出某需求为 JSON')
    p.add_argument('--jira', required=True); p.add_argument('--out')
    p.set_defaults(func=cmd_export)

    return ap


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
