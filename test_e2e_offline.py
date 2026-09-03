#!/usr/bin/env python3
"""herdr-auto-title 的确定性端到端测试(离线,可重复跑)。

## 脚本定义

用途
    用真实进程边界重放 herdr 插件事件模型下的竞态场景,验证停靠队列
    (pending_draft / pending_settle)在跨进程 flock 语义下的行为。
    上一轮真实 provider E2E 验证过的四个场景在这里确定性化:

    1. settled 事件在 T0 持锁生成期间到达:停靠,锁释放后消费,T1 定稿
       真实执行且 tab 被第二次写入。
    2. working 事件在 final 持锁期间到达:停靠,final 已给出标题时 draft
       跳过,不烧第二次 provider 调用。
    3. working + settled 双停靠:按 draft -> settle 顺序消费;同轮内容
       指纹短路,定稿复用初稿标题。
    4. reset 与生成进程并发:pane_lock 排队,撤销已消费的全部结果。

形态
    unittest 测试模块。每个用例通过 subprocess 以 herdr 实际 spawn hook
    的方式启动插件事件模式(HERDR_PLUGIN_EVENT_JSON 环境变量注入事件)。

确定性手段(全部经文件 gate 强制,无 sleep 依赖)
    - fake herdr CLI:覆盖 agent read / agent list / pane get / tab list /
      tab rename / pane report-metadata,其余子命令保守失败(exit 1),
      及时暴露插件对新 CLI 面的依赖。
    - fake codebuddy provider:按调用序号返回固定标题;第一次调用 touch
      provider-1.started 并阻塞等待 release 文件,用于制造确定性的持锁窗口。
    - fake herdr 的 agent read 按调用计数返回 recent-N.txt,控制指纹变化。

参数定义
    (无命令行参数;由 unittest 发现机制调用)
    python3 -m unittest -v test_e2e_offline          # 只跑本文件
    python3 -m unittest                              # 跑全部测试(含单测)

输出结果定义
    stdout/stderr   unittest 的常规输出;断言失败给出场景与状态差异
    退出码           0 全部通过;1 存在失败/错误
    副作用           仅写入测试临时目录,结束后全部清理

关键设计决策
    - 用进程而非线程:停靠机制的核心是非阻塞 flock 的跨进程行为,
      线程无法覆盖 herdr spawn 每个事件一个进程的真实模型。
    - 不改插件代码注入 fake:provider 走 PATH(插件按 argv[0]="codebuddy"
      解析),herdr CLI 走 HERDR_BIN_PATH,与生产注入路径一致。
    - 每个用例 finally 里 touch release 并 kill 残留进程,保证失败时
      不悬挂、不污染后续用例。
    - 仅 POSIX:fake 脚本依赖 shebang 与 flock,与现有单测的平台假设一致。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

MODULE_PATH = Path(__file__).with_name("auto_title.py")
PLUGIN_DIR = MODULE_PATH.parent

FAKE_HERDR = r'''#!/usr/bin/env python3
"""fake herdr CLI:响应 auto_title.py 依赖的子命令，其余保守失败。"""
import json
import os
import sys
from pathlib import Path

# fixture 路径由测试通过环境变量注入：fake 脚本所在目录与 fixture 目录无位置耦合。
FIXTURES = Path(os.environ["AUTO_TITLE_TEST_FIXTURES"])


def load_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def save_json(name, data):
    (FIXTURES / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def emit(result):
    print(json.dumps({"id": "cli:fake", "result": result}))
    sys.exit(0)


def main(argv):
    if len(argv) < 2:
        print("fake herdr: missing subcommand", file=sys.stderr)
        return 2
    group, sub = argv[1], argv[2]

    if group == "agent" and sub == "read":
        # 按调用计数返回 recent-N.txt:测试用它控制指纹是否变化。
        counter = FIXTURES / "read.count"
        n = int(counter.read_text() or "0") + 1
        counter.write_text(str(n))
        content_file = FIXTURES / f"recent-{n}.txt"
        if not content_file.exists():
            content_file = FIXTURES / "recent-last.txt"
        sys.stdout.write(content_file.read_text(encoding="utf-8"))
        return 0

    if group == "agent" and sub == "list":
        emit({"agents": load_json("agents.json")})

    if group == "pane" and sub == "get":
        panes = load_json("panes.json")
        pane_id = argv[3]
        if pane_id not in panes:
            print(f"fake herdr: unknown pane {pane_id}", file=sys.stderr)
            return 1
        emit({"pane": panes[pane_id]})

    if group == "tab" and sub == "list":
        emit({"tabs": load_json("tabs.json")})

    if group == "tab" and sub == "rename":
        tab_id, label = argv[3], argv[4]
        tabs = load_json("tabs.json")
        for tab in tabs:
            if tab["tab_id"] == tab_id:
                tab["label"] = label
        save_json("tabs.json", tabs)
        with (FIXTURES / "calls.log").open("a", encoding="utf-8") as fh:
            fh.write(f"rename {tab_id} {label}\n")
        emit({"type": "ok"})

    if group == "pane" and sub == "report-metadata":
        with (FIXTURES / "calls.log").open("a", encoding="utf-8") as fh:
            fh.write(f"report-metadata {argv[3]}\n")
        emit({"type": "ok"})

    # 未覆盖的子命令保守失败:插件若开始依赖新 CLI 面,测试必须显式失败。
    print(f"fake herdr: unsupported subcommand {' '.join(argv[1:3])}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''

FAKE_CODEBUDDY = r'''#!/usr/bin/env python3
"""fake codebuddy:按调用序号返回 titles.json 里的标题；首次调用可被 gate 阻塞。

标题为 null 时输出 NO_TITLE，模拟真实 provider 的"内容不足以判断任务"。
"""
import json
import os
import sys
import time
from pathlib import Path

# clean_env() 只剥 HERDR_*，测试注入的 AUTO_TITLE_TEST_* 会传进来。
ROOT = Path(os.environ["AUTO_TITLE_TEST_FIXTURES"])
GATE = Path(os.environ["AUTO_TITLE_TEST_GATE_DIR"])

# 读掉 stdin 的 prompt，避免对端管道破裂；内容本身不参与结果。
sys.stdin.read()

count_file = ROOT / "provider.count"
n = int(count_file.read_text() or "0") + 1
count_file.write_text(str(n))

titles = json.loads((ROOT / "titles.json").read_text(encoding="utf-8"))
title = titles[n - 1] if n <= len(titles) else titles[-1]

if n == 1:
    (GATE / "provider-1.started").touch()
    deadline = time.monotonic() + 90
    while not (GATE / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.02)

print("NO_TITLE" if title is None else title)
'''


class E2EOfflineTests(unittest.TestCase):
    """跨进程事件模型下的停靠队列端到端行为。"""

    def setUp(self) -> None:
        self.spawned: list[subprocess.Popen] = []
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.plugin_state = root / "plugin-state"
        self.fixtures = root / "fixtures"
        self.gate = root / "gate"
        self.bin_dir = root / "bin"
        for directory in (self.plugin_state, self.fixtures, self.gate, self.bin_dir):
            directory.mkdir(parents=True)
        for name, body in (("herdr", FAKE_HERDR), ("codebuddy", FAKE_CODEBUDDY)):
            path = self.bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.write_fixture(
            "panes.json",
            {
                "w1:p1": {
                    "pane_id": "w1:p1",
                    "tab_id": "w1:t1",
                    "label": None,
                    "terminal_title_stripped": None,
                    "cwd": "/tmp",
                }
            },
        )
        self.write_fixture(
            "agents.json",
            [
                {
                    "pane_id": "w1:p1",
                    "tab_id": "w1:t1",
                    "agent": "pi",
                    "agent_status": "idle",
                }
            ],
        )
        self.write_fixture(
            "tabs.json",
            # 未命名 tab 在多 tab workspace 里的真实形态：label 是位置回落('7')，
            # number 是稳定公开编号(13)，两者错开。
            [{"tab_id": "w1:t1", "label": "7", "number": "13"}],
        )
        (self.fixtures / "read.count").write_text("0")
        (self.fixtures / "provider.count").write_text("0")
        (self.fixtures / "calls.log").write_text("")

    def tearDown(self) -> None:
        # 任何失败路径都要释放 provider gate 并回收子进程,避免用例间污染。
        (self.gate / "release").touch()
        for proc in getattr(self, "spawned", []):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        self.temp_dir.cleanup()

    # ------------------------------------------------------------ 辅助

    def write_fixture(self, name: str, data) -> None:
        (self.fixtures / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def write_recent(self, index: int, content: str) -> None:
        (self.fixtures / f"recent-{index}.txt").write_text(content, encoding="utf-8")

    def write_recent_last(self, content: str) -> None:
        (self.fixtures / "recent-last.txt").write_text(content, encoding="utf-8")

    def set_titles(self, titles: list) -> None:
        self.write_fixture("titles.json", titles)

    def env_for_event(self, status: str) -> dict:
        env = dict(os.environ)
        env.update(
            {
                # 模拟 herdr spawn hook 的注入方式(见 src/app/api/plugins/panes.rs)
                "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                    {"data": {"pane_id": "w1:p1", "agent_status": status, "agent": "pi"}}
                ),
                "HERDR_PLUGIN_STATE_DIR": str(self.plugin_state),
                "HERDR_BIN_PATH": str(self.bin_dir / "herdr"),
                "AUTO_TITLE_TEST_FIXTURES": str(self.fixtures),
                "AUTO_TITLE_TEST_GATE_DIR": str(self.gate),
                "PATH": f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}",
            }
        )
        for key in ("HERDR_SESSION", "HERDR_SOCKET_PATH", "HERDR_AUTO_TITLE_SESSION"):
            env.pop(key, None)
        return env

    def run_event(self, status: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "auto_title.py"],
            cwd=PLUGIN_DIR,
            env=self.env_for_event(status),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def spawn_event(self, status: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "auto_title.py"],
            cwd=PLUGIN_DIR,
            env=self.env_for_event(status),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.spawned.append(proc)
        return proc

    def await_provider(self, timeout: float = 15.0) -> None:
        """等到第一次 provider 调用真实进入 gate,确定持锁窗口已打开。"""
        marker = self.gate / "provider-1.started"
        deadline = time.monotonic() + timeout
        while not marker.exists():
            if time.monotonic() > deadline:
                self.fail("provider never reached the gate")
            time.sleep(0.02)

    def release_provider(self) -> None:
        (self.gate / "release").touch()

    def state(self) -> dict:
        path = self.plugin_state / "state.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def entry(self) -> dict:
        return self.state()["w1:p1"]

    def rename_calls(self) -> list:
        return [
            line.split(" ", 1)[1].rstrip("\n")
            for line in (self.fixtures / "calls.log").read_text(encoding="utf-8").splitlines()
            if line.startswith("rename ")
        ]

    def provider_calls(self) -> int:
        return int((self.fixtures / "provider.count").read_text())

    # ------------------------------------------------------------ 场景

    def test_settled_event_during_blocked_t0_parks_then_consumes(self) -> None:
        """场景 1:T0 持锁期间的 done 事件停靠,释放后 T1 消费并二次写 tab。"""
        self.write_recent(1, "第一个 prompt 上屏后的会话内容足够生成初稿标题")
        self.write_recent_last("prompt 内容加上第一轮回复,内容变化足以生成定稿标题")
        self.set_titles(["初稿标题", "定稿标题"])

        t0 = self.spawn_event("working")
        self.await_provider()

        # 第二个事件进程:必须毫秒级退出,只停靠意图,不烧 provider。
        started = time.monotonic()
        done_proc = self.run_event("done")
        elapsed = time.monotonic() - started
        self.assertEqual(done_proc.returncode, 0, done_proc.stderr)
        self.assertLess(elapsed, 10.0, "parked event process must not wait on the lock")
        self.assertTrue(self.entry().get("pending_settle"))
        self.assertEqual(self.provider_calls(), 1)

        self.release_provider()
        t0.communicate(timeout=60)
        self.assertEqual(t0.returncode, 0)

        self.assertEqual(self.rename_calls(), ["w1:t1 初稿标题", "w1:t1 定稿标题"])
        entry = self.entry()
        self.assertTrue(entry["draft_done"])
        self.assertTrue(entry["final_done"])
        self.assertNotIn("pending_settle", entry)
        self.assertEqual(entry["calls"], 2)
        self.assertEqual(self.provider_calls(), 2)

    def test_working_event_parked_and_skipped_when_final_wins(self) -> None:
        """场景 2:final 持锁期间 working 停靠;final 成功后 draft 跳过不烧调用。"""
        self.write_recent(1, "第一轮回复后屏幕上已有完整对话内容可以定稿")
        self.write_recent_last("不会被读取的内容")
        self.set_titles(["定稿标题"])

        final_proc = self.spawn_event("idle")
        self.await_provider()

        started = time.monotonic()
        working_proc = self.run_event("working")
        self.assertEqual(working_proc.returncode, 0, working_proc.stderr)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertTrue(self.entry().get("pending_draft"))

        self.release_provider()
        final_proc.communicate(timeout=60)
        self.assertEqual(final_proc.returncode, 0)

        self.assertEqual(self.rename_calls(), ["w1:t1 定稿标题"])
        entry = self.entry()
        self.assertTrue(entry["final_done"])
        self.assertNotIn("pending_draft", entry)
        self.assertEqual(entry["calls"], 1)
        self.assertEqual(self.provider_calls(), 1)

    def test_parked_draft_and_settle_consume_in_order(self) -> None:
        """场景 3:final 失败后,working 与 done 停靠按 draft -> settle 消费。"""
        self.write_recent(1, "第一轮回复后屏幕上已有完整对话内容可以定稿")
        self.write_recent_last("prompt 上屏后的会话内容足够生成初稿标题")
        self.set_titles([None, "初稿标题"])

        final_proc = self.spawn_event("idle")
        self.await_provider()

        working_proc = self.run_event("working")
        self.assertEqual(working_proc.returncode, 0, working_proc.stderr)
        done_proc = self.run_event("done")
        self.assertEqual(done_proc.returncode, 0, done_proc.stderr)
        entry = self.entry()
        self.assertTrue(entry.get("pending_draft"))
        self.assertTrue(entry.get("pending_settle"))

        self.release_provider()
        final_proc.communicate(timeout=60)
        self.assertEqual(final_proc.returncode, 0)

        # final 失败(不写 tab) -> draft 写初稿 -> settle 同内容指纹复用同一标题。
        self.assertEqual(self.rename_calls(), ["w1:t1 初稿标题", "w1:t1 初稿标题"])
        entry = self.entry()
        self.assertTrue(entry["draft_done"])
        self.assertTrue(entry["final_done"])
        self.assertNotIn("pending_draft", entry)
        self.assertNotIn("pending_settle", entry)
        # settle 复用初稿标题不触发 provider:总调用 = final + draft = 2。
        self.assertEqual(entry["calls"], 3)
        self.assertEqual(self.provider_calls(), 2)

    def test_reset_queues_on_pane_lock_and_reverts_consumed_results(self) -> None:
        """场景 4:reset 在 pane_lock 排队,T0 与停靠的 T1 全部消费完后撤销。"""
        self.write_recent(1, "第一个 prompt 上屏后的会话内容足够生成初稿标题")
        self.write_recent_last("prompt 内容加上第一轮回复,内容变化足以生成定稿标题")
        self.set_titles(["初稿标题", "定稿标题"])

        t0 = self.spawn_event("working")
        self.await_provider()

        # T0 持锁期间到达的 done 事件停靠，reset 之后由锁持有者消费。
        done_proc = self.run_event("done")
        self.assertEqual(done_proc.returncode, 0, done_proc.stderr)
        self.assertTrue(self.entry().get("pending_settle"))

        # 与生成进程并发的 reset:pane_lock(blocking) 必须等到消费全部完成。
        # 用 Popen 异步启动：同步 run 会阻塞在 reset 上，而 reset 等 T0 释放锁、
        # T0 等 release，三方死锁。reset 拿 pane_lock 后排队，顺序由 flock 保证。
        reset_env = self.env_for_event("idle")
        reset_env.pop("HERDR_PLUGIN_EVENT_JSON", None)
        reset = subprocess.Popen(
            [sys.executable, "auto_title.py", "reset", "--pane", "w1:p1"],
            cwd=PLUGIN_DIR,
            env=reset_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.spawned.append(reset)
        time.sleep(0.5)  # 给 reset 进程到达 pane_lock 排队的时间；顺序正确性不依赖它

        self.release_provider()
        t0.communicate(timeout=60)
        self.assertEqual(t0.returncode, 0)
        reset_out, reset_err = reset.communicate(timeout=60)
        self.assertEqual(reset.returncode, 0, reset_err)

        self.assertEqual(
            self.rename_calls(),
            ["w1:t1 初稿标题", "w1:t1 定稿标题", "w1:t1 13"],
        )
        entry = self.entry()
        self.assertEqual(entry["calls"], 0)
        self.assertNotIn("draft_done", entry)
        self.assertNotIn("final_done", entry)
        self.assertNotIn("pending_draft", entry)
        self.assertNotIn("pending_settle", entry)
        self.assertEqual(entry["generation"], 1)


if __name__ == "__main__":
    unittest.main()