"""selfcheck.py 埋点 moduleId 机检测试：临时库构造任务与埋点，不碰真实库。"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / 'scripts'
PLUGIN = SKILL.parents[1]
PTB = PLUGIN / 'scripts' / 'ptb.py'

J = 'TEST-TRACK'


def ptb(db, *args):
    p = subprocess.run([sys.executable, str(PTB), '--db', str(db)] + [str(a) for a in args],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f'ptb {args} 失败 rc={p.returncode}\n{p.stdout}\n{p.stderr}')
    return p


def selfcheck(db, jira=J):
    return subprocess.run([sys.executable, str(SCRIPTS / 'selfcheck.py'),
                           '--jira', jira, '--db', str(db)],
                          capture_output=True, text=True)


class TrackModuleIdCheck(unittest.TestCase):
    """埋点必须落在 moduleId 键上：实现要点（缺省 goal）按词边界含 moduleId。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / 'prd.db'
        ptb(self.db, 'init')
        ptb(self.db, 'jira', 'add', '--jira', J, '--title', '埋点机检测试')

    def tearDown(self):
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 数据准备

    def add_task(self, tid, impl=None, goal=None):
        args = ['task', 'add', '--jira', J, '--id', tid, '--title', f'task {tid}',
                '--end', 'frontend']
        if impl is not None:
            args += ['--implementation', impl]
        if goal is not None:
            args += ['--goal', goal]
        ptb(self.db, *args)

    def add_track(self, tid, event='click', api='sendTrack'):
        args = ['track', 'add', '--jira', J, '--event', event, '--api', api,
                '--element', '.car-item', '--timing', '点击时']
        if tid is not None:
            args += ['--task', tid]
        ptb(self.db, *args)

    # ---------------------------------------------------------------- 用例

    def test_module_id_written_passes(self):
        """实现要点写了 moduleId（各种常见写法）→ 不报 moduleId 错误。"""
        for n, impl in enumerate(("sendTrack({ moduleId: 'car-list', tracking_type: 'click' })",
                                  '点击回调里从 { moduleId } 组装上报参数',
                                  "模块 moduleId 取常量 TRACK_CAR_LIST 的值",
                                  '在moduleId参数上传模块值，不要写成 mti_module'), start=1):
            tid = f'T00{n}'
            with self.subTest(impl=impl):
                self.add_task(tid, impl=impl)
                self.add_track(tid)
                p = self.check_only_module_errors()
                self.assertEqual(p.returncode, 0, p.stdout)

    def test_module_id_missing_fails(self):
        """实现要点没写 moduleId → 报错且文案固定。"""
        self.add_task('T001', impl='在列表 item 的点击回调里加点击埋点')
        self.add_track('T001')
        p = self.check_only_module_errors()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('埋点(id=1) 关联任务 T001 的实现要点没写 moduleId', p.stdout)
        self.assertIn('mti_module 在代码里对应 sendTrack/TrackBeseen 的 moduleId 参数', p.stdout)

    def test_constant_name_module_id_not_enough(self):
        """只有常量名里含 MODULE_ID 不算：区分大小写、按词边界。"""
        for n, impl in enumerate(("模块 key 用常量 MODULE_ID（值为 'car-list'）上报",
                                  '上报参数用 mti_module 与 module_id 两个键组装',
                                  '上报参数挂在 moduleIds 数组里',
                                  '把 xmoduleId 或 moduleIdValue 当成模块字段'), start=1):
            tid = f'T00{n}'
            with self.subTest(impl=impl):
                self.add_task(tid, impl=impl)
                self.add_track(tid)
                p = self.check_only_module_errors()
                self.assertNotEqual(p.returncode, 0, p.stdout)
                self.assertIn(f'关联任务 {tid} 的实现要点没写 moduleId', p.stdout)

    def test_implementation_takes_priority_over_goal(self):
        """implementation 非空时只看 implementation，goal 写了也不救。"""
        self.add_task('T001', impl='用常量 MODULE_ID 的值上报',
                      goal='moduleId 写在 sendTrack 参数上')
        self.add_track('T001')
        p = self.check_only_module_errors()
        self.assertNotEqual(p.returncode, 0)

    def test_falls_back_to_goal_when_implementation_empty(self):
        """implementation 为空时取 goal 判断。"""
        self.add_task('T001', goal='sendTrack 的 moduleId 传 shop-list')
        self.add_track('T001')
        p = self.check_only_module_errors()
        self.assertEqual(p.returncode, 0, p.stdout)

        self.add_task('T002', goal='目标见 PRD 第 3 节')
        self.add_track('T002')
        p = self.check_only_module_errors()
        self.assertIn('埋点(id=2) 关联任务 T002 的实现要点没写 moduleId', p.stdout)

    def test_empty_task_id_skipped(self):
        """task_id 为空的行跳过，不报关联任务/moduleId 错误。"""
        self.add_track(None)
        p = self.check_only_module_errors()
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_missing_task_reported(self):
        """task_id 指向不存在的任务 → 报「关联的任务不存在」。"""
        self.add_track('T404')
        p = self.check_only_module_errors()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('埋点(id=1) 关联的任务 T404 不存在', p.stdout)

    # ---------------------------------------------------------------- 工具

    def check_only_module_errors(self):
        """跑 selfcheck，断言除 moduleId 相关外没有其它错误（fixture 干净）。"""
        p = selfcheck(self.db)
        noise = [ln for ln in p.stdout.splitlines()
                 if ln.startswith('错误') and 'moduleId' not in ln and '不存在' not in ln]
        self.assertEqual(noise, [], f'fixture 应只触发 moduleId 相关错误: {p.stdout}')
        return p


class ExistingChecksUnchanged(unittest.TestCase):
    """既有三条埋点检查不回归：API 与事件类型不匹配仍然报错。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / 'prd.db'
        ptb(self.db, 'init')
        ptb(self.db, 'jira', 'add', '--jira', J, '--title', '既有检查回归测试')
        ptb(self.db, 'task', 'add', '--jira', J, '--id', 'T001', '--title', 'task T001',
            '--end', 'frontend', '--implementation', 'sendTrack({ moduleId: "x" })')

    def tearDown(self):
        self._tmp.cleanup()

    def test_exposure_with_sendtrack_still_fails(self):
        ptb(self.db, 'track', 'add', '--jira', J, '--task', 'T001', '--event', 'exposure',
            '--api', 'sendTrack', '--element', '.car-item', '--timing', '进入可视区域')
        p = selfcheck(self.db)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('exposure 事件必须用 TrackBeseen', p.stdout)

    def test_missing_element_timing_still_fails(self):
        ptb(self.db, 'track', 'add', '--jira', J, '--task', 'T001', '--event', 'click',
            '--api', 'sendTrack')
        p = selfcheck(self.db)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('未写清 element/timing', p.stdout)


if __name__ == '__main__':
    unittest.main()
