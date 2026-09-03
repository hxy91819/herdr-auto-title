#!/usr/bin/env python3
"""herdr-auto-title 的可观察行为测试。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch


MODULE_PATH = Path(__file__).with_name("auto_title.py")
SPEC = importlib.util.spec_from_file_location("auto_title", MODULE_PATH)
assert SPEC and SPEC.loader
auto_title = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auto_title)


def args(*, target: str = "tab", dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        model=auto_title.DEFAULT_MODEL,
        lines=auto_title.DEFAULT_LINES,
        max_bytes=auto_title.DEFAULT_MAX_BYTES,
        max_cols=auto_title.DEFAULT_MAX_COLS,
        recheck_rounds=auto_title.DEFAULT_RECHECK_ROUNDS,
        timeout=auto_title.DEFAULT_TIMEOUT,
        dry_run=dry_run,
        draft_only=False,
        jobs=auto_title.DEFAULT_JOBS,
        target=target,
        agent=f"codebuddy --acp --model {auto_title.DEFAULT_MODEL}",
        provider="codebuddy",
        provider_cmd=None,
    )


class AutoTitleBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "HERDR_AUTO_TITLE_STATE_DIR": self.temp_dir.name,
                "HERDR_PLUGIN_STATE_DIR": "",
            },
        )
        self.env.start()
        auto_title.CONFIG = auto_title.Config(args())
        auto_title.EVENT_MODE = False
        auto_title.VERBOSE = False
        auto_title.ensure_state_dir()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def configure(self, *, target: str = "tab", dry_run: bool = False) -> None:
        auto_title.CONFIG = auto_title.Config(args(target=target, dry_run=dry_run))
        auto_title.ensure_state_dir()

    def test_t0_short_prompt_reaches_provider_at_configured_threshold(self) -> None:
        self.configure(target="pane")
        content = "add title ok"
        self.assertEqual(len(content), auto_title.THIN_CONTENT_CHARS)
        with (
            patch.object(auto_title, "read_pane", return_value=content),
            patch.object(auto_title, "generate_title", return_value="修复标题生成") as generate,
        ):
            self.assertEqual(auto_title.generate_for("w1:p1", {}), "修复标题生成")
        generate.assert_called_once()

    def test_failed_t0_enters_cooldown_but_idle_final_remains_available(self) -> None:
        self.configure(target="pane")
        pane = {
            "pane_id": "w1:p1",
            "tab_id": "w1:t1",
            "label": None,
            "terminal_title_stripped": None,
            "cwd": "/tmp",
        }
        with (
            patch.object(auto_title, "get_pane", return_value=pane),
            patch.object(auto_title, "read_pane", return_value="tiny"),
        ):
            self.assertEqual(auto_title._handle_event("w1:p1", "working", {"agent": "pi"}), 0)

        entry = auto_title.load_state()["w1:p1"]
        self.assertIn("draft_fail_at", entry)
        self.assertEqual(auto_title.decide(entry, "working"), "")
        entry["rounds"] = 1
        self.assertEqual(auto_title.decide(entry, "idle"), "final")

    def test_background_done_event_runs_t1_final(self) -> None:
        self.configure(target="pane")
        auto_title.save_state(
            {
                "w1:p1": {
                    "rounds": 0,
                    "calls": 0,
                    "last_status": "working",
                    "generation": 0,
                    "revision": 0,
                }
            }
        )
        pane = {
            "pane_id": "w1:p1",
            "tab_id": "w1:t1",
            "label": None,
            "terminal_title_stripped": None,
            "cwd": "/tmp",
        }
        event = {"data": {"pane_id": "w1:p1", "agent_status": "done", "agent": "pi"}}
        with (
            patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event)}),
            patch.object(auto_title, "get_pane", return_value=pane),
            patch.object(auto_title, "read_pane", return_value="complete title generation"),
            patch.object(auto_title, "generate_title", return_value="完成标题生成"),
            patch.object(auto_title, "report_title", return_value=True) as report,
        ):
            self.assertEqual(auto_title.handle_event(), 0)

        report.assert_called_once_with("w1:p1", "完成标题生成")
        entry = auto_title.load_state()["w1:p1"]
        self.assertEqual(entry["rounds"], 1)
        self.assertTrue(entry["final_done"])

    def make_tab_pane(self, pane_id: str = "w1:p1", tab_id: str = "w1:t1") -> dict:
        return {
            "pane_id": pane_id,
            "tab_id": tab_id,
            "label": None,
            "terminal_title_stripped": None,
            "cwd": "/tmp",
        }

    def configure_tab_world(self, pane_id: str = "w1:p1", tab_id: str = "w1:t1") -> dict:
        """tab 模式下固定的 pane/tab 世界,返回 tabs 供断言。"""
        self.configure(target="tab")
        tabs = {tab_id: {"tab_id": tab_id, "label": "1", "number": "1"}}
        pane = self.make_tab_pane(pane_id, tab_id)
        enter = patch.object(auto_title, "get_pane", return_value=pane)
        enter.start()
        self.addCleanup(enter.stop)
        tabs_patch = patch.object(auto_title, "list_tabs", return_value=tabs)
        tabs_patch.start()
        self.addCleanup(tabs_patch.stop)
        primary_patch = patch.object(
            auto_title, "first_agent_panes", return_value={tab_id: pane_id}
        )
        primary_patch.start()
        self.addCleanup(primary_patch.stop)
        return tabs

    def test_settled_event_during_blocked_t0_runs_t1_after_release(self) -> None:
        """T0 持锁生成期间到达的 settled 事件不允许丢:T0 结束后必须消费并二次写 tab。"""
        tabs = self.configure_tab_world()
        gate = threading.Event()
        entered_provider = threading.Event()
        contents = iter(["第一个 prompt 内容足够生成初稿", "prompt 内容加上第一轮回复内容足够定稿"])

        def fake_generate(content: str, current_title: str | None) -> str:
            if not entered_provider.is_set():
                entered_provider.set()
                gate.wait(timeout=30)
                return "初稿标题"
            return "定稿标题"

        event_json_working = {
            "data": {"pane_id": "w1:p1", "agent_status": "working", "agent": "pi"}
        }
        event_json_done = {
            "data": {"pane_id": "w1:p1", "agent_status": "done", "agent": "pi"}
        }
        failures: list[BaseException] = []

        def t0_runner() -> None:
            try:
                with (
                    patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event_json_working)}),
                    patch.object(auto_title, "read_pane", side_effect=lambda pid: next(contents)),
                    patch.object(auto_title, "generate_title", side_effect=fake_generate),
                    patch.object(auto_title, "rename_tab", return_value=True) as rename,
                ):
                    auto_title.handle_event()
                    self.parked_rename_calls = rename.call_args_list
            except BaseException as exc:  # 传回主线程断言
                failures.append(exc)

        worker = threading.Thread(target=t0_runner)
        worker.start()
        self.assertTrue(entered_provider.wait(timeout=10), "T0 never reached the provider")

        # 第二个"进程"送入 done 事件:必须立刻返回(不阻塞、不烧 provider),
        # 只把 settled 意图持久化,等 T0 释放锁后消费。
        parked_at = time.monotonic()
        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event_json_done)}):
            self.assertEqual(auto_title.handle_event(), 0)
        self.assertLess(time.monotonic() - parked_at, 5.0)
        entry = auto_title.load_state()["w1:p1"]
        self.assertTrue(entry.get("pending_settle"))

        gate.set()
        worker.join(timeout=60)
        self.assertEqual(failures, [])
        self.assertEqual(
            self.parked_rename_calls,
            [
                (("w1:t1", "初稿标题"),),
                (("w1:t1", "定稿标题"),),
            ],
        )
        entry = auto_title.load_state()["w1:p1"]
        self.assertTrue(entry["draft_done"])
        self.assertTrue(entry["final_done"])
        self.assertNotIn("pending_settle", entry)
        self.assertEqual(entry["calls"], 2)
        self.assertEqual(tabs["w1:t1"]["label"], "1")

    def test_working_event_during_blocked_final_skips_draft_when_final_wins(self) -> None:
        """final 持锁时到达的 working 事件持久化意图;final 已给出标题则 draft 不再烧调用。"""
        self.configure_tab_world()
        gate = threading.Event()
        entered_provider = threading.Event()

        def fake_generate(content: str, current_title: str | None) -> str:
            if not entered_provider.is_set():
                entered_provider.set()
                gate.wait(timeout=30)
                return "定稿标题"
            raise AssertionError("draft must not call the provider after a successful final")

        event_json_idle = {
            "data": {"pane_id": "w1:p1", "agent_status": "idle", "agent": "pi"}
        }
        event_json_working = {
            "data": {"pane_id": "w1:p1", "agent_status": "working", "agent": "pi"}
        }
        failures: list[BaseException] = []

        def final_runner() -> None:
            try:
                with (
                    patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event_json_idle)}),
                    patch.object(auto_title, "read_pane", return_value="第一轮回复后的完整对话内容"),
                    patch.object(auto_title, "generate_title", side_effect=fake_generate),
                    patch.object(auto_title, "rename_tab", return_value=True) as rename,
                ):
                    auto_title.handle_event()
                    self.parked_rename_calls = rename.call_args_list
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=final_runner)
        worker.start()
        self.assertTrue(entered_provider.wait(timeout=10), "final never reached the provider")

        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event_json_working)}):
            self.assertEqual(auto_title.handle_event(), 0)
        entry = auto_title.load_state()["w1:p1"]
        self.assertTrue(entry.get("pending_draft"))

        gate.set()
        worker.join(timeout=60)
        self.assertEqual(failures, [])
        self.assertEqual(
            self.parked_rename_calls,
            [call("w1:t1", "定稿标题")],
        )
        entry = auto_title.load_state()["w1:p1"]
        self.assertTrue(entry["final_done"])
        self.assertNotIn("pending_draft", entry)
        self.assertEqual(entry["calls"], 1)

    def test_reset_waiting_on_pane_lock_reverts_consumed_parked_results(self) -> None:
        """reset 在 pane_lock 上排队：T0 与停靠的 T1 全部消费完后，reset 撤销一切。"""
        tabs = self.configure_tab_world()
        gate = threading.Event()
        entered_provider = threading.Event()
        rename_calls: list = []

        def fake_rename(tab_id: str, label: str) -> bool:
            rename_calls.append((tab_id, label))
            tabs[tab_id]["label"] = label
            return True

        contents = iter(["第一个 prompt 内容足够生成初稿", "加上第一轮回复的内容足够定稿"])
        generate_results = iter(["初稿标题", "定稿标题"])

        def fake_generate(content: str, current_title: str | None) -> str:
            if not entered_provider.is_set():
                entered_provider.set()
                gate.wait(timeout=30)
            return next(generate_results)

        event_json_working = {
            "data": {"pane_id": "w1:p1", "agent_status": "working", "agent": "pi"}
        }
        event_json_done = {
            "data": {"pane_id": "w1:p1", "agent_status": "done", "agent": "pi"}
        }
        failures: list[BaseException] = []

        def t0_runner() -> None:
            try:
                with (
                    patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event_json_working)}),
                    patch.object(auto_title, "read_pane", side_effect=lambda pid: next(contents)),
                    patch.object(auto_title, "generate_title", side_effect=fake_generate),
                ):
                    auto_title.handle_event()
            except BaseException as exc:
                failures.append(exc)

        # rename_tab 在主线程 patch，覆盖 T0 与 reset 两个线程；
        # 线程内各自 patch 会在先结束的线程退出时把属性恢复成进入前的值，
        # 覆盖后启动线程的 patch，导致 revert 调到真实的 herdr CLI。
        with patch.object(auto_title, "rename_tab", side_effect=fake_rename):
            worker = threading.Thread(target=t0_runner)
            worker.start()
            self.assertTrue(entered_provider.wait(timeout=10), "T0 never reached the provider")

            with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event_json_done)}):
                self.assertEqual(auto_title.handle_event(), 0)

            reset_failures: list[BaseException] = []

            def reset_runner() -> None:
                try:
                    reset_args = argparse.Namespace(pane=["w1:p1"], dry_run=False)
                    self.assertEqual(auto_title.cmd_reset(reset_args), 0)
                except BaseException as exc:
                    reset_failures.append(exc)

            # reset 与 T0 并发启动：它的 pane_lock 会排队，直到 T0 与停靠的 T1 都消费完。
            reset_worker = threading.Thread(target=reset_runner)
            reset_worker.start()
            gate.set()
            worker.join(timeout=60)
            reset_worker.join(timeout=60)
        self.assertEqual(failures, [])
        self.assertEqual(reset_failures, [])
        # T0 初稿 + 停靠 T1 定稿都写入了，reset 最后把 tab 恢复回未命名序号。
        self.assertEqual(
            rename_calls,
            [
                ("w1:t1", "初稿标题"),
                ("w1:t1", "定稿标题"),
                ("w1:t1", "1"),
            ],
        )
        entry = auto_title.load_state()["w1:p1"]
        self.assertEqual(entry["calls"], 0)
        self.assertNotIn("draft_done", entry)
        self.assertNotIn("final_done", entry)
        self.assertNotIn("pending_draft", entry)
        self.assertNotIn("pending_settle", entry)
        self.assertEqual(entry["generation"], 1)

    def test_parked_draft_and_settle_intents_merge_and_consume_in_order(self) -> None:
        """final 失败后,停靠的 working 与 done 意图按 draft -> settle 顺序消费。"""
        self.configure_tab_world()
        gate = threading.Event()
        entered_provider = threading.Event()
        generate_results = iter([None, "初稿标题"])
        draft_content = "prompt 上屏后的完整内容足够初稿"
        contents = iter(
            [
                "第一轮回复后屏幕上已有完整对话内容",
                draft_content,
                draft_content,
            ]
        )

        def fake_generate(content: str, current_title: str | None):
            if not entered_provider.is_set():
                entered_provider.set()
                gate.wait(timeout=30)
            return next(generate_results)

        failures: list[BaseException] = []

        def final_runner() -> None:
            try:
                with (
                    patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"pane_id": "w1:p1", "agent_status": "idle", "agent": "pi"}})}),
                    patch.object(auto_title, "read_pane", side_effect=lambda pid: next(contents)),
                    patch.object(auto_title, "generate_title", side_effect=fake_generate),
                    patch.object(auto_title, "rename_tab", return_value=True) as rename,
                ):
                    auto_title.handle_event()
                    self.parked_rename_calls = rename.call_args_list
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=final_runner)
        worker.start()
        self.assertTrue(entered_provider.wait(timeout=10))

        # final 被挡期间:working 停靠为 draft,done 停靠为 settle。
        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"pane_id": "w1:p1", "agent_status": "working", "agent": "pi"}})}):
            self.assertEqual(auto_title.handle_event(), 0)
        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"pane_id": "w1:p1", "agent_status": "done", "agent": "pi"}})}):
            self.assertEqual(auto_title.handle_event(), 0)
        entry = auto_title.load_state()["w1:p1"]
        self.assertTrue(entry.get("pending_draft"))
        self.assertTrue(entry.get("pending_settle"))

        gate.set()
        worker.join(timeout=60)
        self.assertEqual(failures, [])
        # final 失败(不写 tab)-> draft 写初稿 -> settle 同内容指纹复用同一标题再写一次。
        self.assertEqual(
            self.parked_rename_calls,
            [
                (("w1:t1", "初稿标题"),),
                (("w1:t1", "初稿标题"),),
            ],
        )
        entry = auto_title.load_state()["w1:p1"]
        self.assertTrue(entry["draft_done"])
        self.assertTrue(entry["final_done"])
        self.assertNotIn("pending_draft", entry)
        self.assertNotIn("pending_settle", entry)

    def test_numeric_display_label_is_unnamed_not_user_named(self) -> None:
        """位置回落产生的数字 label(≠number)不是用户命名,必须让自动标题接管。"""
        self.configure_tab_world(tab_id="w1:t1", pane_id="w1:p1")
        # 位置回落 label '7',稳定编号 13:未命名 tab 在多 tab workspace 里的真实形态
        with patch.object(
            auto_title,
            "list_tabs",
            return_value={"w1:t1": {"tab_id": "w1:t1", "label": "7", "number": "13"}},
        ):
            entry = {}
            skipped = auto_title.should_skip_tab(
                "w1:p1",
                {"tab_id": "w1:t1", "label": "7", "number": "13"},
                entry,
                {"w1:t1": "w1:p1"},
            )
            self.assertFalse(skipped)
            self.assertFalse(entry.get("tab_user_named"))
            self.assertTrue(auto_title.tab_is_unnamed({"label": "7", "number": "13"}))
            self.assertTrue(auto_title.tab_is_unnamed({"label": "", "number": "1"}))
            self.assertFalse(
                auto_title.tab_is_unnamed({"label": "插桩工具验收", "number": "3"})
            )

    def test_clear_does_not_overwrite_a_tab_renamed_by_user(self) -> None:
        self.configure(target="tab")
        entry = {"tab_id": "w1:t1", "tab_title": "插件原标题"}
        tabs = {"w1:t1": {"tab_id": "w1:t1", "label": "用户标题", "number": 1}}
        with patch.object(auto_title, "rename_tab") as rename:
            auto_title.revert_target("w1:p1", entry, tabs)
        rename.assert_not_called()
        self.assertTrue(entry["tab_user_named"])
        self.assertIsNone(entry["tab_title"])

    def test_default_tab_target_passes_existing_tab_title_to_provider(self) -> None:
        self.configure(target="tab")
        with (
            patch.object(auto_title, "read_pane", return_value="write release validation plan"),
            patch.object(auto_title, "generate_title", return_value="验证发布流程") as generate,
        ):
            auto_title.generate_for("w1:p1", {"tab_title": "已有标题"})
        self.assertEqual(generate.call_args.args[1], "已有标题")

    def test_state_and_diagnostic_files_are_private(self) -> None:
        auto_title.save_state({"w1:p1": {"calls": 0}})
        auto_title._append_log("INFO", "test")
        for path, mode in (
            (auto_title.CONFIG.state_dir, 0o700),
            (auto_title.CONFIG.state_path, 0o600),
            (auto_title.CONFIG.state_dir / "auto-title.log", 0o600),
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)

    def test_different_panes_can_generate_concurrently(self) -> None:
        entered: list[tuple[str, float]] = []
        gate = threading.Event()

        def hold(pane_id: str) -> None:
            gate.wait()
            with auto_title.pane_lock(pane_id, blocking=True) as locked:
                self.assertTrue(locked)
                entered.append((pane_id, time.monotonic()))
                time.sleep(0.15)

        workers = [threading.Thread(target=hold, args=(pane_id,)) for pane_id in ("w1:p1", "w1:p2")]
        for worker in workers:
            worker.start()
        gate.set()
        for worker in workers:
            worker.join()

        self.assertEqual(len(entered), 2)
        self.assertLess(abs(entered[0][1] - entered[1][1]), 0.1)

    def test_plugin_state_directory_from_herdr_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as managed_dir:
            with patch.dict(
                os.environ,
                {"HERDR_AUTO_TITLE_STATE_DIR": "", "HERDR_PLUGIN_STATE_DIR": managed_dir},
            ):
                config = auto_title.Config(args())
        self.assertEqual(config.state_dir, Path(managed_dir))

    def test_named_session_uses_its_own_state_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as managed_dir:
            with patch.dict(
                os.environ,
                {
                    "HERDR_AUTO_TITLE_STATE_DIR": "",
                    "HERDR_PLUGIN_STATE_DIR": managed_dir,
                    "HERDR_SESSION": "review-session",
                },
            ):
                config = auto_title.Config(args())
        self.assertEqual(config.state_dir.parent.parent, Path(managed_dir))
        self.assertEqual(config.state_dir.name, "a92180bcf87404d2")

    def test_managed_state_migrates_legacy_records_once_without_deleting_source(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            managed_dir = Path(root) / "managed"
            xdg_dir = Path(root) / "xdg"
            legacy_path = xdg_dir / "herdr-auto-title" / "state.json"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(json.dumps({"w1:p9": {"tab_title": "旧标题"}}))
            with patch.dict(
                os.environ,
                {"HERDR_PLUGIN_STATE_DIR": str(managed_dir), "XDG_STATE_HOME": str(xdg_dir)},
                clear=True,
            ):
                auto_title.CONFIG = auto_title.Config(args())
                auto_title.ensure_state_dir()
                auto_title.migrate_legacy_state()

            self.assertTrue(legacy_path.exists())
            self.assertEqual(auto_title.load_state()["w1:p9"]["tab_title"], "旧标题")
            self.assertTrue(auto_title.CONFIG.legacy_migration_marker.exists())


if __name__ == "__main__":
    unittest.main()
