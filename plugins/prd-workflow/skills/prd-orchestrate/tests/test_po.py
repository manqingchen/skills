"""prd-orchestrate 协调器测试：不启动真实 worker，用临时库 / 临时 git 仓库 / fake Orca。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / 'scripts'
PLUGIN = SKILL.parents[1]
PTB = PLUGIN / 'scripts' / 'ptb.py'
FIXTURES = TESTS / 'fixtures'

sys.path.insert(0, str(SCRIPTS))
import po  # noqa: E402
import report  # noqa: E402

J = 'TEST-1'


def ptb(db, *args, check=True):
    p = subprocess.run([sys.executable, str(PTB), '--db', str(db)] + [str(a) for a in args],
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f'ptb {args} 失败 rc={p.returncode}\n{p.stdout}\n{p.stderr}')
    return p


def git(cwd, *args):
    p = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f'git {args} 失败: {p.stdout}\n{p.stderr}')
    return p.stdout.strip()


class PoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / 'prd.db'
        ptb(self.db, 'init')
        ptb(self.db, 'jira', 'add', '--jira', J, '--title', '演示需求')

    def tearDown(self):
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 数据准备

    def add_task(self, tid, *, seq=1, deps=(), files=(), accs=(), status='todo'):
        ptb(self.db, 'task', 'add', '--jira', J, '--id', tid, '--title', f'task {tid}',
            '--seq', seq, '--end', 'frontend', '--status', status)
        for d in deps:
            ptb(self.db, 'dep', 'add', '--jira', J, '--task', tid, '--on', d)
        for path, change in files:
            ptb(self.db, 'file', 'add', '--jira', J, '--task', tid, '--path', path, '--change', change)
        for desc in accs:
            ptb(self.db, 'acc', 'add', '--jira', J, '--task', tid, '--desc', desc)

    def task_json(self, tid):
        return json.loads(ptb(self.db, 'task', 'show', tid, '--jira', J).stdout)

    def finish_task(self, tid):
        """把任务置 done：验收全 pass + completion。"""
        for a in self.task_json(tid)['acceptance']:
            ptb(self.db, 'acc', 'set', '--jira', J, '--task', tid, '--id', a['id'], '--result', 'pass')
        ptb(self.db, 'complete', '--jira', J, '--task', tid, '--evidence', 'unit test')

    def make_repo(self, files=None):
        repo = self.tmp / 'repo'
        repo.mkdir()
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.email', 'test@example.com')
        git(repo, 'config', 'user.name', 'tester')
        for name, content in (files or {'base.txt': 'base\n'}).items():
            p = repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', 'base')
        return repo

    def po_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = po.main(argv)
        return rc, out.getvalue(), err.getvalue()


class PlanTest(PoTestCase):
    def test_waves_dependencies_overlap_and_unknown_files(self):
        # T1 与 T3 改同一文件 → 必须拆批；T4 无文件清单 → 单独成批
        self.add_task('T1', seq=1, files=[('src/a.ts', 'add')], accs=['a'])
        self.add_task('T2', seq=2, deps=['T1'], files=[('src/b.ts', 'add')], accs=['b'])
        self.add_task('T3', seq=3, files=[('src/a.ts', 'add')], accs=['c'])
        self.add_task('T4', seq=4, accs=['d'])

        p = po.Ptb(self.db)
        tasks = po.collect_tasks(p, J)
        plan = po.make_plan(tasks, {'jira': J}, max_parallel=3)

        wave1 = plan['waves'][0]
        self.assertEqual(wave1['wave'], 1)
        self.assertEqual([b['tasks'] for b in wave1['batches']],
                         [['T1'], ['T3'], ['T4']])
        self.assertIn('file_unknown', wave1['batches'][2]['reason'])

        self.assertIn('T2', plan['skipped'])
        self.assertTrue(any('依赖 T1' in r for r in plan['skipped']['T2']))

        preview = {w['wave']: w['tasks'] for w in plan['preview']}
        self.assertEqual(preview[1], ['T1', 'T3', 'T4'])
        self.assertEqual(preview[2], ['T2'])

    def test_paths_overlap_is_prefix_aware(self):
        self.assertTrue(po.paths_overlap('src/components', 'src/components/a.tsx'))
        self.assertTrue(po.paths_overlap('./src/a.ts', 'src/a.ts'))
        self.assertFalse(po.paths_overlap('src/a.ts', 'src/b.ts'))

    def test_done_but_unmerged_dependency_is_not_satisfied(self):
        self.add_task('T1', seq=1, files=[('a.ts', 'add')], accs=['a'])
        self.add_task('T2', seq=2, deps=['T1'], files=[('b.ts', 'add')], accs=['b'])
        self.finish_task('T1')

        p = po.Ptb(self.db)
        tasks = po.collect_tasks(p, J)
        # T1 在本 run 中完成但未合并 → T2 不能派发
        entry = po.task_entry('task T1', ['codex', 'claude'])
        entry['arms']['codex']['dispatch'] = {'dispatch_id': 'd', 'outcome': 'succeeded'}
        entry['arms']['claude']['dispatch'] = {'dispatch_id': 'e', 'outcome': 'succeeded'}
        run = {'jira': J, 'tasks': {'T1': entry}}
        plan = po.make_plan(tasks, run)
        self.assertEqual(plan['waves'], [])
        self.assertIn('未合并', '；'.join(plan['skipped'].get('T2', [])))

        # 合并后即可派发
        entry['merge']['state'] = 'merged'
        plan = po.make_plan(tasks, run)
        self.assertEqual(plan['waves'][0]['batches'][0]['tasks'], ['T2'])


class SpecTest(PoTestCase):
    def test_spec_is_dual_arm_no_commit_with_report_protocol(self):
        # 先建另一条任务并加验收，确保 T1 的 acceptance.id 不是从 1 开始
        self.add_task('T0', seq=0, files=[('z.ts', 'add')], accs=['z1', 'z2'])
        self.add_task('T1', seq=1, files=[('src/a.ts', 'add')], accs=['A1', 'A2'])
        p = po.Ptb(self.db)
        tasks = po.collect_tasks(p, J)
        run = {
            'jira': J, 'db': str(self.db), 'base_sha': '0' * 40,
            'integration': {'branch': 'prd/TEST-1/run-x', 'path': str(self.tmp / 'int')},
            'run_dir': str(self.tmp / 'run'),
        }
        export = p.jira_export(J)
        spec = po.build_spec(run, tasks, 'T1', export, 3001, 'codex')

        for a in self.task_json('T1')['acceptance']:
            self.assertIn(f"| {a['seq']} | {a['id']} |", spec)
            self.assertGreater(a['id'], 2)  # 全局自增，不是任务内 seq
        self.assertIn(f'python3 {PTB} --db {self.db}', spec)
        self.assertIn('不要 commit', spec)
        self.assertIn('不要 push', spec)
        self.assertIn('PO_ARM_REPORT.json', spec)
        self.assertIn('不要执行 `acc set` / `complete` / `block`', spec)
        self.assertIn('"arm": "codex"', spec)
        self.assertNotIn('恰好一次本地提交', spec)
        self.assertNotIn('task-create', spec)


class CompareChooseTest(PoTestCase):
    def _setup(self):
        repo = self.make_repo()
        base = git(repo, 'rev-parse', 'HEAD')
        codex = self.tmp / 'wt-codex'
        claude = self.tmp / 'wt-claude'
        integration = self.tmp / 'integration'
        git(repo, 'worktree', 'add', '-q', '-b', 'wt-codex', str(codex), base)
        git(repo, 'worktree', 'add', '-q', '-b', 'wt-claude', str(claude), base)
        git(repo, 'worktree', 'add', '-q', '-b', 'int', str(integration), base)
        return repo, base, codex, claude, integration

    def _write_arm(self, wt, agent, files, acc_results, extra_file=False):
        for name, content in files.items():
            p = wt / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
        shots = f'.po-artifacts/T1/{agent}/shot.png'
        (wt / shots).parent.mkdir(parents=True, exist_ok=True)
        (wt / shots).write_bytes(b'png')
        report = {
            'task': 'T1', 'arm': agent, 'status': 'done',
            'acceptance': acc_results, 'verifications': [],
            'effect': {'summary': f'{agent} effect', 'demo': 'open page',
                       'screenshots': [shots]},
            'files': list(files) + ([ 'extra.txt' ] if extra_file else []),
        }
        (wt / 'PO_ARM_REPORT.json').write_text(
            json.dumps(report, ensure_ascii=False), encoding='utf-8')

    def _run_for(self, base, codex, claude, integration):
        entry = po.task_entry('task T1', ['codex', 'claude'])
        for agent, wt in (('codex', codex), ('claude', claude)):
            entry['arms'][agent]['dispatch'] = {
                'dispatch_id': f'd-{agent}', 'base_sha': base,
                'worktree_path': str(wt), 'outcome': 'succeeded', 'state': 'settled'}
        return {
            'jira': J, 'db': str(self.db), 'base_sha': base,
            'run_dir': str(self.tmp / 'run'),
            'integration': {'path': str(integration), 'branch': 'int', 'head_sha': base},
            'tasks': {'T1': entry},
        }

    def test_compare_reads_uncommitted_arms_and_recommends_smaller(self):
        repo, base, codex, claude, integration = self._setup()
        self.add_task('T1', files=[('fix.txt', 'add'), ('extra.txt', 'add')], accs=['fix works'])
        aid = self.task_json('T1')['acceptance'][0]['id']
        self._write_arm(codex, 'codex', {'fix.txt': 'codex\n'},
                        [{'id': aid, 'result': 'pass', 'evidence': 'ok'}])
        self._write_arm(claude, 'claude', {'fix.txt': 'claude\n', 'extra.txt': 'more\n'},
                        [{'id': aid, 'result': 'pass', 'evidence': 'ok'}])

        p = po.Ptb(self.db)
        tasks = po.collect_tasks(p, J)
        run = self._run_for(base, codex, claude, integration)
        cmp = po.compare_task(run, tasks, 'T1', run['run_dir'], verify=False)

        self.assertIsNotNone(cmp)
        self.assertEqual(run['tasks']['T1']['compare']['state'], 'ready')
        ok, codex_info, _ = po.collect_arm(run, tasks, 'T1', 'codex')
        self.assertTrue(ok, codex_info['problems'])
        self.assertEqual(codex_info['changed_files'], ['fix.txt'])
        self.assertEqual(codex_info['effect_screenshots'], 1)
        self.assertIn('PO_ARM_REPORT.json', [x.name for x in Path(codex).iterdir()])
        self.assertFalse(Path(codex, 'fix.txt').read_text().strip() == '')

        patch = Path(cmp['patches']['codex'])
        self.assertTrue(patch.is_file())
        self.assertIn('fix.txt', patch.read_text(encoding='utf-8'))
        self.assertNotIn('PO_ARM_REPORT.json', patch.read_text(encoding='utf-8'))
        self.assertTrue(Path(cmp['markdown_path']).is_file())
        self.assertEqual(cmp['recommendation']['winner'], 'codex')
        # 比对阶段只读：集成分支没有变化，也没有提交
        self.assertEqual(git(integration, 'rev-parse', 'HEAD'), base)

    def test_choose_applies_patch_commits_and_writes_ptb(self):
        repo, base, codex, claude, integration = self._setup()
        self.add_task('T1', files=[('fix.txt', 'add')], accs=['fix works'])
        aid = self.task_json('T1')['acceptance'][0]['id']
        self._write_arm(codex, 'codex', {'fix.txt': 'codex\n'},
                        [{'id': aid, 'result': 'pass', 'evidence': 'ok'}])
        self._write_arm(claude, 'claude', {'fix.txt': 'claude\n'},
                        [{'id': aid, 'result': 'pass', 'evidence': 'ok'}])

        p = po.Ptb(self.db)
        tasks = po.collect_tasks(p, J)
        run = self._run_for(base, codex, claude, integration)
        po.compare_task(run, tasks, 'T1', run['run_dir'], verify=False)
        # 合并前集成分支保持原样
        self.assertFalse((integration / 'fix.txt').exists())

        res = po.choose_task(run, tasks, 'T1', 'codex', p, orca=None)
        self.assertEqual(res['state'], 'merged')
        self.assertEqual(run['tasks']['T1']['merge']['arm'], 'codex')
        self.assertEqual(run['tasks']['T1']['compare']['state'], 'chosen')
        self.assertEqual((integration / 'fix.txt').read_text(encoding='utf-8'), 'codex\n')
        # 报告与产物不进集成分支
        self.assertFalse((integration / 'PO_ARM_REPORT.json').exists())
        self.assertFalse((integration / '.po-artifacts').exists())
        self.assertIn('[codex]', git(integration, 'log', '-1', '--format=%s'))
        self.assertNotEqual(git(integration, 'rev-parse', 'HEAD'), base)

        t = self.task_json('T1')
        self.assertEqual(t['task']['status'], 'done')
        self.assertTrue(all(a['result'] == 'pass' for a in t['acceptance']))
        self.assertTrue(t['completions'])

    def test_choose_rejects_arm_failing_acceptance(self):
        repo, base, codex, claude, integration = self._setup()
        self.add_task('T1', files=[('fix.txt', 'add')], accs=['fix works'])
        aid = self.task_json('T1')['acceptance'][0]['id']
        self._write_arm(codex, 'codex', {'fix.txt': 'codex\n'},
                        [{'id': aid, 'result': 'fail', 'evidence': 'broken'}])
        self._write_arm(claude, 'claude', {'fix.txt': 'claude\n'},
                        [{'id': aid, 'result': 'pass', 'evidence': 'ok'}])

        p = po.Ptb(self.db)
        tasks = po.collect_tasks(p, J)
        run = self._run_for(base, codex, claude, integration)
        po.compare_task(run, tasks, 'T1', run['run_dir'], verify=False)
        # codex 验收未过，只有 claude 合格
        self.assertEqual(run['tasks']['T1']['compare']['recommendation']['winner'], 'claude')

        with self.assertRaises(po.PoError):
            po.choose_task(run, tasks, 'T1', 'codex', p, orca=None)
        self.assertEqual(git(integration, 'rev-parse', 'HEAD'), base)
        self.assertEqual(self.task_json('T1')['task']['status'], 'todo')


class CheckTaskTest(PoTestCase):
    def test_check_task_filters_peer_noise(self):
        root = self.tmp / 'project'
        root.mkdir()
        # done 但缺 completion：新旧 ptb 都视为结构性 error，且都带任务前缀
        self.add_task('T1', files=[('missing1.ts', 'modify')], accs=['a'], status='done')
        self.add_task('T2', files=[('missing2.ts', 'modify')], accs=['b'], status='done')

        p = subprocess.run([sys.executable, str(SCRIPTS / 'po.py'), '--db', str(self.db), '--json',
                            'check-task', '--jira', J, '--task', 'T1', '--root', str(root)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        data = json.loads(p.stdout)
        self.assertTrue(data['errors'])
        self.assertTrue(all(e.startswith('T1 ') for e in data['errors']), data['errors'])
        self.assertGreaterEqual(data['ignored_peer_errors'], 1)


class WakeTest(PoTestCase):
    def _run(self):
        entry = po.task_entry('task T1', ['codex', 'claude'])
        entry['arms']['codex']['dispatch'] = {'dispatch_id': 'd-1', 'state': 'started'}
        entry['arms']['claude']['dispatch'] = {'dispatch_id': 'd-2', 'state': 'started'}
        return {'jira': J, 'tasks': {'T1': entry}, 'questions': []}

    def test_worker_done_normalizes_per_arm(self):
        payload = json.loads((FIXTURES / 'check_worker_done.json').read_text(encoding='utf-8'))
        run = self._run()
        msgs, need_user = po.process_wake(run, {}, payload, None)
        self.assertEqual(msgs[0]['type'], 'worker_done')
        self.assertEqual(msgs[0]['outcome'], 'succeeded')
        self.assertFalse(need_user)
        self.assertEqual(run['tasks']['T1']['arms']['codex']['dispatch']['outcome'], 'succeeded')
        self.assertIsNone(run['tasks']['T1']['arms']['claude']['dispatch'].get('outcome'))

    def test_question_requires_user(self):
        payload = json.loads((FIXTURES / 'check_question.json').read_text(encoding='utf-8'))
        run = self._run()
        msgs, need_user = po.process_wake(run, {}, payload, None)
        self.assertEqual(msgs[0]['type'], 'question')
        self.assertTrue(need_user)
        self.assertEqual(run['status'], 'waiting')
        self.assertEqual(len(run['questions']), 1)


class ReportTest(PoTestCase):
    def test_report_escapes_text_and_has_no_external_refs(self):
        self.add_task('T1', files=[('a.ts', 'add')], accs=['x'])
        ptb(self.db, 'task', 'text', 'T1', '--jira', J, '--field', 'notes',
            '--text', '<script>alert(1)</script>')
        run_dir = self.tmp / 'run'
        run_dir.mkdir()
        (run_dir / 'run.json').write_text('{}', encoding='utf-8')

        paths = report.generate(J, str(self.db), str(run_dir), out_dir=str(self.tmp / 'out'))
        html = Path(paths['html']).read_text(encoding='utf-8')

        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)
        self.assertGreater(len(html), 5000)
        self.assertTrue(Path(paths['snapshot']).exists())
        self.assertTrue((self.tmp / 'out' / 'latest.html').exists())

    def test_report_renders_dual_arm_comparison(self):
        self.add_task('T1', files=[('a.ts', 'add')], accs=['x'])
        aid = self.task_json('T1')['acceptance'][0]['id']
        run_dir = self.tmp / 'run'
        run_dir.mkdir()
        entry = po.task_entry('task T1', ['codex', 'claude'])
        for agent in ('codex', 'claude'):
            entry['arms'][agent]['dispatch'] = {
                'dispatch_id': f'd-{agent}', 'state': 'settled', 'outcome': 'succeeded',
                'worktree_path': f'/tmp/{agent}'}
            entry['arms'][agent]['report'] = {
                'status': 'done',
                'acceptance': [{'id': aid, 'result': 'pass'}],
                'effect': {'summary': f'{agent} looks right'}}
            entry['arms'][agent]['git'] = {
                'changed_files': ['a.ts'], 'insertions': 3, 'deletions': 1}
        entry['compare'] = {
            'state': 'ready', 'patches': {}, 'markdown_path': '/tmp/report.md',
            'recommendation': {'winner': 'codex', 'verdict': 'recommend', 'gates': {}},
        }
        (run_dir / 'run.json').write_text(json.dumps({
            'jira': J, 'run_dir': str(run_dir), 'options': {'arms': ['codex', 'claude']},
            'tasks': {'T1': entry},
        }, ensure_ascii=False), encoding='utf-8')

        paths = report.generate(J, str(self.db), str(run_dir), out_dir=str(self.tmp / 'out'))
        html = Path(paths['html']).read_text(encoding='utf-8')
        self.assertIn('双 Agent 比对', html)
        self.assertIn('codex looks right', html)
        self.assertIn('claude looks right', html)
        self.assertIn('po.py choose', html)


class DispatchTest(PoTestCase):
    def _fake_orca(self):
        fake = self.tmp / 'fake-orca'
        fake.write_text(f'''#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ['ORCA_LOG'], 'a', encoding='utf-8') as fh:
    fh.write(' '.join(args) + '\\n')
if args[:2] == ['worktree', 'current']:
    print(json.dumps({{"result": {{"worktree": {{"repoId": "repo1"}}}}}}))
elif args[:2] == ['orchestration', 'run-create']:
    print(json.dumps({{"result": {{"runId": "run-1"}}}}))
elif args[:2] == ['orchestration', 'worker-start']:
    agent = args[args.index('--agent') + 1]
    print(json.dumps({{"result": {{"dispatchId": "d-" + agent, "taskId": "t-" + agent,
                                  "workspace": os.environ["FAKE_WORKSPACE"],
                                  "state": "started"}}}}))
else:
    print('{{}}')
''', encoding='utf-8')
        fake.chmod(0o755)
        return fake

    def test_init_and_dispatch_start_both_arms_without_commit(self):
        repo = self.make_repo()
        fake = self._fake_orca()
        log = self.tmp / 'orca.log'
        workspace = self.tmp / 'fake-ws'
        workspace.mkdir()
        run_dir = self.tmp / 'run'
        self.add_task('T1', files=[('src/a.ts', 'add')], accs=['x'])

        env = {'ORCA_LOG': str(log), 'FAKE_WORKSPACE': str(workspace)}
        with mock.patch.dict(os.environ, env):
            rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                         'init', '--jira', J, '--root', str(repo),
                                         '--run-dir', str(run_dir)])
            self.assertEqual(rc, 0, out + err)
            run = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(run['run_id'], 'run-1')
            self.assertEqual(run['options']['arms'], ['codex', 'claude'])
            self.assertTrue(Path(run['integration']['path']).exists())

            rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                         'dispatch', '--run-dir', str(run_dir)])
            self.assertEqual(rc, 0, out + err)

        lines = [l for l in log.read_text(encoding='utf-8').splitlines() if 'worker-start' in l]
        self.assertEqual(len(lines), 2, log.read_text(encoding='utf-8'))
        by_agent = {l.split('--agent ', 1)[1].split()[0]: l for l in lines}
        self.assertEqual(set(by_agent), {'codex', 'claude'})
        for agent, line in by_agent.items():
            self.assertIn('--run run-1', line)
            self.assertIn('--repo id:repo1', line)
            self.assertIn(f"--base-branch {run['integration']['branch']}", line)
            self.assertIn('--setup run', line)
            spec_arg = line.split('--spec-file ', 1)[1].split(' --task-title', 1)[0]
            self.assertTrue(os.path.isabs(spec_arg), spec_arg)
            self.assertTrue(spec_arg.endswith(f'T1.{agent}.md'), spec_arg)
            self.assertTrue(Path(spec_arg).exists())

        run = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
        for agent in ('codex', 'claude'):
            d = run['tasks']['T1']['arms'][agent]['dispatch']
            self.assertEqual(d['dispatch_id'], f'd-{agent}')
            self.assertEqual(d['worktree_path'], str(workspace))
        self.assertNotEqual(run['tasks']['T1']['arms']['codex']['dispatch']['port'],
                            run['tasks']['T1']['arms']['claude']['dispatch']['port'])

    def test_dispatch_dry_run_does_not_touch_run_or_specs(self):
        repo = self.make_repo()
        fake = self._fake_orca()
        run_dir = self.tmp / 'run'
        self.add_task('T1', files=[('src/a.ts', 'add')], accs=['x'])
        with mock.patch.dict(os.environ, {'ORCA_LOG': str(self.tmp / 'orca.log'),
                                          'FAKE_WORKSPACE': str(self.tmp)}):
            rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                         'init', '--jira', J, '--root', str(repo),
                                         '--run-dir', str(run_dir)])
            self.assertEqual(rc, 0, out + err)
            before = (run_dir / 'run.json').read_text(encoding='utf-8')
            rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                         'dispatch', '--run-dir', str(run_dir), '--dry-run'])
            self.assertEqual(rc, 0, out + err)

        self.assertIn('DRY-RUN', out)
        self.assertEqual((run_dir / 'run.json').read_text(encoding='utf-8'), before)
        self.assertEqual(list((run_dir / 'specs').glob('*')), [])


class FullFlowTest(PoTestCase):
    def test_init_dispatch_advance_compare_choose_finalize(self):
        repo = self.make_repo()
        base = git(repo, 'rev-parse', 'HEAD')
        codex = self.tmp / 'wt-codex'
        claude = self.tmp / 'wt-claude'
        git(repo, 'worktree', 'add', '-q', '-b', 'wt-codex', str(codex), base)
        git(repo, 'worktree', 'add', '-q', '-b', 'wt-claude', str(claude), base)
        self.add_task('T1', files=[('fix.txt', 'add'), ('extra.txt', 'add')],
                      accs=['fix works'])
        aid = self.task_json('T1')['acceptance'][0]['id']

        fake = self.tmp / 'fake-orca'
        fake.write_text(f'''#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ['ORCA_LOG'], 'a', encoding='utf-8') as fh:
    fh.write(' '.join(args) + '\\n')
if args[:2] == ['worktree', 'current']:
    print(json.dumps({{"result": {{"worktree": {{"repoId": "repo1"}}}}}}))
elif args[:2] == ['orchestration', 'run-create']:
    print(json.dumps({{"result": {{"runId": "run-1"}}}}))
elif args[:2] == ['orchestration', 'worker-start']:
    agent = args[args.index('--agent') + 1]
    print(json.dumps({{"result": {{"dispatchId": "d-" + agent,
                                  "workspace": os.environ["ARM_WS_" + agent.upper()],
                                  "state": "started"}}}}))
else:
    print('{{}}')
''', encoding='utf-8')
        fake.chmod(0o755)

        run_dir = self.tmp / 'run'
        fixture = self.tmp / 'check.json'
        fixture.write_text(json.dumps({'result': {'messages': [
            {'id': 'm1', 'type': 'worker_done', 'dispatchId': 'd-codex',
             'outcome': 'succeeded'},
            {'id': 'm2', 'type': 'worker_done', 'dispatchId': 'd-claude',
             'outcome': 'succeeded'},
        ]}}), encoding='utf-8')
        env = {'ORCA_LOG': str(self.tmp / 'orca.log'),
               'ARM_WS_CODEX': str(codex), 'ARM_WS_CLAUDE': str(claude)}
        with mock.patch.dict(os.environ, env):
            rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                         'init', '--jira', J, '--root', str(repo),
                                         '--run-dir', str(run_dir)])
            self.assertEqual(rc, 0, out + err)
            rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                         'dispatch', '--run-dir', str(run_dir)])
            self.assertEqual(rc, 0, out + err)

            # 两臂各自完成并写报告（不提交、不写库）
            for wt, agent, files in (
                    (codex, 'codex', {'fix.txt': 'codex\n'}),
                    (claude, 'claude', {'fix.txt': 'claude\n', 'extra.txt': 'more\n'})):
                for name, content in files.items():
                    (wt / name).write_text(content, encoding='utf-8')
                report_data = {
                    'task': 'T1', 'arm': agent, 'status': 'done',
                    'acceptance': [{'id': aid, 'result': 'pass', 'evidence': 'ok'}],
                    'verifications': [], 'effect': {'summary': f'{agent} effect'},
                    'files': list(files),
                }
                (wt / 'PO_ARM_REPORT.json').write_text(
                    json.dumps(report_data, ensure_ascii=False), encoding='utf-8')

            rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                         'advance', '--run-dir', str(run_dir),
                                         '--fixture', str(fixture), '--no-report'])
            self.assertEqual(rc, 0, out + err)

            run = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(run['tasks']['T1']['compare']['state'], 'ready')
            self.assertEqual(run['tasks']['T1']['compare']['recommendation']['winner'],
                             'codex')
            self.assertEqual(git(repo, 'rev-parse', run['integration']['branch']), base)

            rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                         'choose', '--run-dir', str(run_dir),
                                         '--task', 'T1', '--arm', 'codex'])
            self.assertEqual(rc, 0, out + err)
            run = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(run['tasks']['T1']['merge']['state'], 'merged')
            self.assertEqual(run['tasks']['T1']['compare']['state'], 'chosen')
            self.assertEqual(self.task_json('T1')['task']['status'], 'done')
            int_path = run['integration']['path']
            self.assertEqual((Path(int_path) / 'fix.txt').read_text(encoding='utf-8'),
                             'codex\n')
            self.assertNotEqual(git(int_path, 'rev-parse', 'HEAD'), base)

            with mock.patch.object(report, 'REPORTS_ROOT', self.tmp / 'reports'):
                rc, out, err = self.po_main(['--db', str(self.db), '--orca', str(fake),
                                             'finalize', '--run-dir', str(run_dir)])
            self.assertEqual(rc, 0, out + err)
            run = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(run['status'], 'complete')
            html = Path(run['report']['html'])
            self.assertTrue(html.is_file(), html)
            self.assertIn('双 Agent 比对', html.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
