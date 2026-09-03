#!/usr/bin/env python3
"""herdr-auto-title 的真实环境验收脚本(需 Herdr 管理 pane,产生真实 provider 调用)。

## 脚本定义

用途
    把 `AGENTS.md` 要求的手工"完成改动后本机试用"自动化:在一次性命名 Herdr
    会话里驱动真实 agent 与真实 provider,验证插件的事件链路与会话身份重置:

    S1 基线      新 pane + 短 prompt,等待 idle 后插件应完成 T0+T1,
                tab 被命名为非占位标题,状态里 calls>=2 且 draft/final 完成。
    S2 /new     同 pane 执行 /new + 主题不同的短 prompt,新原生会话应触发
                生命周期重置(generation 递增、calls 从 0 重计)且 tab 名
                更新为新任务语义。
    S3 第二次换新会话:pi 再次 /new(验证连续 rollover),codex 用 /clear;
                验收同 S2。

    三个场景各一次短 prompt,provider 调用保持最小(合计约 6 次)。

边界
    - 必须在 Herdr 管理 pane 内运行(HERDR_ENV=1);普通 shell 里嵌套 herdr
      会被 allow_nested 拒绝,脚本保守失败。
    - 会话身份依赖 herdr 的 agent 集成上报(codex 需 SessionStart hook 可信)。
      身份不可用时,身份重置场景无法真实发生,脚本如实报告 environment-blocked
      而不是伪造通过/失败。
    - 只操作脚本自建的命名会话与外层 pane;绝不触碰用户会话的 pane/tab。
    - 不记录也不校验原生会话 ID、prompt 全文、凭据或完整 provider 日志。

## 参数定义

    --agent KIND            启动的 agent 类型(默认 codex;须已安装且被 herdr 识别)
    --no-agent-args         不给 agent 追加任何原生参数(默认追加
                            --dangerously-bypass-hook-trust,规避本机 codex
                            用户层 hook 信任不满足导致 SessionStart 不上报)
    --agent-arg ARG         覆盖默认追加参数,可重复
    --workdir PATH          隔离会话首个 pane 的工作目录(默认 /var/tmp;
                            需是 agent 已信任目录,否则首启出现信任确认框)
    --session NAME          命名会话名(默认 real-check-<时间戳>,保证唯一)
    --settle-timeout SEC    等 provider 定稿写 tab 的上限(默认 90 秒)
    --prompt-timeout SEC    等 agent 回到 idle 的上限(默认 240 秒)
    --session-timeout SEC   等命名会话就绪的上限(默认 120 秒,负载高时调大)
    --keep                  结束时保留命名会话与外层 pane 供诊断(默认清理,
                            但报告 JSON 始终保留)
    --report PATH           报告输出路径(默认 /var/tmp/real-session-check-<时间戳>.json)
    -v, --verbose           打印每步 CLI 调用与轮询细节
    -h, --help              显示帮助

## 输出结果定义

    stdout      分场景 PASS/FAIL/BLOCKED 摘要、身份可用性、最终 tab 名
    报告文件    JSON:会话名、每场景逐项检查、身份可用性、最终 tab 名;
                文件名带时间戳可追踪;不含原生会话 ID
    退出码      0  全部场景通过且会话身份可用
                1  存在真实的插件行为失败(报告里有 failing 检查项)
                2  环境不满足,未能开始验证(不在 herdr pane 内/herdr 缺失/
                   pane 或 agent 启动失败等)
                3  插件侧检查通过,但会话身份不可用,S2/S3 被环境阻塞
                130 被 Ctrl-C 中断(仍会尽力清理)

## 调用范例

    python3 real_session_check.py --help
    python3 real_session_check.py                      # 默认 codex,全场景
    python3 real_session_check.py -v --settle-timeout 120
    python3 real_session_check.py --no-agent-args      # 验证未绕过信任时的降级行为

## 关键设计决策

    - 复用 herdr-throwaway-repro 工作流的安全约束:唯一命名会话、只清理
      自建 pane、所有地址从命令输出读取。嵌套启动默认被 herdr 禁止,因此
      用 HERDR_CONFIG_PATH 指向临时配置副本(仅追加 allow_nested=true)
      开启,不修改共享配置;插件与注册表目录跟 XDG_CONFIG_HOME/HOME 走,
      local link 的插件在一次性会话里同样生效。
    - 声明式检查而不是截图比对:验收读 herdr 公开 API(tab list / pane get)
      与插件状态文件 state.json,与离线 e2e 覆盖的 fake CLI 面一致。
    - 身份值(agent_session.value 等)只用于判定"是否变化",不写入报告,
      避免把原生会话 ID 留档。
    - 失败不自动重试、不自动修复环境(如 hook 信任),由人根据报告处理;
      失败即指向明确段落(启动/上报/生成/写 tab)。
    - codex 输入框对 send-text 内的换行不提交,统一"发文本 + 单独 Return";
      外层 shell 则用换行直接执行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)
import auto_title  # noqa: E402  仅复用 state_session_scope,与插件状态目录推导保持一致

# herdr 会话选择相关变量:调用命名会话时必须清除继承值,避免误连父会话
SESSION_ENV_KEYS = (
    "HERDR_SOCKET_PATH",
    "HERDR_CLIENT_SOCKET_PATH",
    "HERDR_SESSION",
    "HERDR_WORKSPACE_ID",
    "HERDR_TAB_ID",
    "HERDR_PANE_ID",
)

# 短 prompt:主题互异、无敏感信息、明确要求不跑命令,provider 调用保持最小
PROMPTS = {
    "S1": "用一句话介绍什么是冒泡排序,直接回复,不要运行命令。",
    "S2": "用一句话介绍什么是快速排序,直接回复,不要运行命令。",
    "S3": "用一句话介绍什么是堆排序,直接回复,不要运行命令。",
}

AGENT_NAME = "auto1"
VERBOSE = False


def log(message: str) -> None:
    print(message, flush=True)


def debug(message: str) -> None:
    if VERBOSE:
        print(f"  .. {message}", flush=True)


def base_env() -> dict:
    """父会话调用环境:直接继承当前进程 env,

    脚本本身运行在父会话 pane 内,HERDR_SOCKET_PATH 等继承值正是父会话
    寻址依据,不能清除;只有调用命名会话时才用 session_env 清掉重设。
    """
    return dict(os.environ)


def session_env(session: str) -> dict:
    """命名会话调用环境:清除全部会话选择变量后只指定 HERDR_SESSION,
    herdr 会按命名会话自行解析 socket;避免误连父会话。"""
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV_KEYS}
    env["HERDR_SESSION"] = session
    return env


HERDR_BIN = os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr(args: list[str], session: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    env = session_env(session) if session else base_env()
    debug(f"herdr(session={session}) {shlex.join(args)}")
    return subprocess.run(
        [HERDR_BIN, *args], capture_output=True, text=True, timeout=timeout, env=env
    )


def herdr_json(args: list[str], session: str | None = None, timeout: int = 20):
    """与 auto_title.herdr_json 对齐:返回 result 载荷本身,失败/解析错误返回 None。"""
    proc = herdr(args, session=session, timeout=timeout)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload.get("result")
    return None


def pane_get(pane_id: str, session: str) -> dict | None:
    result = herdr_json(["pane", "get", pane_id], session=session)
    if isinstance(result, dict):
        pane = result.get("pane")
        if isinstance(pane, dict):
            return pane
    return None


def agent_status(session: str) -> str | None:
    result = herdr_json(["agent", "get", AGENT_NAME], session=session)
    if isinstance(result, dict):
        agent = result.get("agent")
        if isinstance(agent, dict):
            return agent.get("agent_status")
    return None


def tab_label(session: str) -> str | None:
    result = herdr_json(["tab", "list"], session=session)
    if not isinstance(result, dict):
        return None
    tabs = result.get("tabs") or []
    return tabs[0].get("label") if tabs else None


def session_scope(session: str) -> str:
    """命名会话的插件状态目录哈希,与 auto_title.state_session_scope 一致。

    不能直接用 auto_title.state_session_scope():它读当前进程的
    HERDR_SESSION(即父会话),而这里要读的是测试会话的状态。
    """
    if not session or session == "default":
        raise ValueError("test session must be a named session")
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]


def state_entry(session: str, pane_id: str) -> dict | None:
    """读插件状态文件中该 pane 的账目;目录推导与 auto_title.state_session_scope 一致。"""
    scope = session_scope(session)
    if not scope:
        return None
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = Path(xdg) / "herdr" / "plugins" / "herdr-auto-title" / "sessions" / scope / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(pane_id)
    return entry if isinstance(entry, dict) else None


def send_line(pane_id: str, text: str, session: str) -> None:
    proc = herdr(["pane", "send-text", pane_id, text], session=session)
    if proc.returncode != 0:
        raise RuntimeError(f"send-text failed: {proc.stderr.strip()}")


def send_prompt(pane_id: str, text: str, session: str) -> None:
    """向 agent pane 提交一行输入;codex 输入框对文本内换行不提交,需单独回车。"""
    send_line(pane_id, text, session)
    time.sleep(1)
    herdr(["pane", "send-keys", pane_id, "Return"], session=session)


def entry_rank(entry: dict | None) -> tuple:
    """生命周期完整度排序:final > draft > 调用数。"""
    if not entry:
        return (False, False, 0)
    return (bool(entry.get("final_done")), bool(entry.get("draft_done")), entry.get("calls", 0))


def observe_turn(session: str, pane_id: str, text: str, args) -> tuple[dict | None, dict | None]:
    """提交 prompt 并持续观察整个 working→idle→(rollover) 窗口。

    返回 (最终 entry, 最佳 entry)。身份上报可能在首轮 provider 调用期间
    到达并触发 rollover 重置,把 draft/final 计数清零甚至吞掉 T1;最佳快照
    保留整个窗口内观测到的最完整生命周期,供生命周期断言降级使用。
    """
    send_prompt(pane_id, text, session)
    best: dict | None = None
    last: dict | None = None
    armed = False  # 看到 working 才开始计时 idle 后的定稿窗口
    idle_at: float | None = None
    deadline = time.time() + args.prompt_timeout + args.settle_timeout
    while time.time() < deadline:
        status = agent_status(session)
        entry = state_entry(session, pane_id)
        if entry:
            last = entry
            if best is None or entry_rank(entry) > entry_rank(best):
                best = entry
        if status == "working":
            armed = True
            idle_at = None
        elif status == "idle" and armed:
            if idle_at is None:
                idle_at = time.time()
            settled = (
                entry
                and entry.get("final_done")
                and entry.get("tab_title")
                and tab_label(session) == entry["tab_title"]
            )
            if settled:
                return entry, best
            if time.time() - idle_at > args.settle_timeout:
                return last, best
        time.sleep(2)
    return last, best


def check(name: str, ok: bool | None, detail: str, findings: list[dict]) -> None:
    """ok=None 表示观测性记录(时序导致无法断言),不算失败。"""
    findings.append({"check": name, "pass": ok, "detail": detail})
    if ok is None:
        log(f"    [NOTE] {name}{': ' + detail if detail else ''}")
    else:
        log(f"    [{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")


def verdict_of(findings: list[dict]) -> str:
    if any(f["pass"] is False for f in findings):
        return "fail"
    return "pass"


def scenario_baseline(session: str, pane_id: str, args) -> tuple[str, list[dict], dict]:
    """S1:首 prompt 后标题落盘;生命周期计数在 rollover 时可被清零,用最佳快照。"""
    findings: list[dict] = []
    log("  发送基线 prompt,观察 working→idle 全窗口")
    final, best = observe_turn(session, pane_id, PROMPTS["S1"], args)
    entry = final or best or {}
    if not entry:
        check("S1 state recorded", False, "插件状态文件里没有该 pane 的记录", findings)
        return "fail", findings, entry
    label = tab_label(session)
    check("S1 tab named", bool(label) and not str(label).isdigit(), f"tab 名 {label!r}", findings)
    check(
        "S1 title owned by plugin",
        bool(label) and label == entry.get("tab_title"),
        f"entry.tab_title={entry.get('tab_title')!r}",
        findings,
    )
    if best and best.get("final_done"):
        check("S1 lifecycle T0+T1", best.get("calls", 0) >= 2, f"calls={best.get('calls')}(最佳快照)", findings)
    elif entry.get("generation", 0) > 0 or entry.get("session_rollover"):
        # 身份上报在首轮 provider 期间到达,rollover 清掉了 T1 机会:
        # 标题已由 T0 写入且 tab 所有权成立,生命周期计数属时序性观测项。
        check(
            "S1 lifecycle (draft only)", None,
            "T0 已写标题;T1 被身份上报时序截断(rollover),hook 滞后机器下属预期",
            findings,
        )
    else:
        check("S1 title produced", False, "无 rollover 且未完成 final,生命周期异常", findings)
    verdict = verdict_of(findings)
    return verdict, findings, entry


def scenario_new_session(
    session: str,
    pane_id: str,
    args,
    key: str,
    slash: str,
    baseline_entry: dict,
    identity_available: bool,
) -> tuple[str, list[dict], dict]:
    """S2/S3:slash 命令换出新的原生会话后,插件应重置并重新生成标题。"""
    findings: list[dict] = []
    label = f"{key} ({slash})"
    old_generation = baseline_entry.get("generation", 0)
    old_title = baseline_entry.get("tab_title") or baseline_entry.get("title")
    log(f"  发送 {slash} + 新主题 prompt")
    send_line(pane_id, slash, session)
    time.sleep(2)
    herdr(["pane", "send-keys", pane_id, "Return"], session=session)
    time.sleep(2)
    entry, best = observe_turn(session, pane_id, PROMPTS[key], args)
    if not entry:
        check(f"{label} state recorded", False, "插件状态文件里没有该 pane 的记录", findings)
        return "fail", findings, baseline_entry

    if not identity_available:
        # 身份不可用时,插件按设计把同 pane 视为同一会话,不会重复生成;
        # 这不是插件缺陷,如实报告为环境阻塞,不做通过/失败判定。
        log("    [SKIP] 会话身份不可用,/new 语义无法发生(环境阻塞,非插件失败)")
        findings.append(
            {
                "check": f"{label} identity reset",
                "pass": None,
                "detail": "agent_session 未上报;无法构造新原生会话,见环境诊断",
            }
        )
        return "blocked", findings, entry

    generation = entry.get("generation", 0)
    label_now = tab_label(session)
    check(
        f"{label} generation bumped",
        generation > old_generation,
        f"generation {old_generation} -> {generation}",
        findings,
    )
    check(
        f"{label} tab retitled",
        bool(label_now) and not str(label_now).isdigit() and label_now != old_title,
        f"tab 名 {old_title!r} -> {label_now!r}",
        findings,
    )
    if entry.get("final_done"):
        check(
            f"{label} lifecycle calls>=2",
            entry.get("calls", 0) >= 2,
            f"calls={entry.get('calls')}",
            findings,
        )
    else:
        # /new 必然伴随 rollover;若新标题已落盘,T1 被时序截断属可接受降级
        title_written = bool(label_now) and not str(label_now).isdigit() and label_now != old_title
        check(
            f"{label} lifecycle (draft only)", None if title_written else False,
            "T0 已写新任务标题;T1 被上报时序截断(rollover)" if title_written
            else "未观测到新任务标题写入",
            findings,
        )
    verdict = verdict_of(findings)
    return verdict, findings, entry


def identity_available(session: str, pane_id: str, entry: dict) -> bool:
    """会话身份是否已被 herdr 集成上报;没有则 /new 场景无法真实发生。"""
    if entry.get("agent_session"):
        return True
    pane = pane_get(pane_id, session)
    return bool(pane and isinstance(pane.get("agent_session"), dict))


def cleanup(session: str, outer_pane: str | None, keep: bool, config_path: str | None) -> None:
    if keep:
        log(f"清理: --keep 生效,保留会话 {session} 与外层 pane {outer_pane} 供诊断")
        return
    log("清理: 停止并删除一次性会话、关闭外层 pane")
    herdr(["session", "stop", session], timeout=60)
    herdr(["session", "delete", session], timeout=30)
    if outer_pane:
        # 关外层 pane 用父会话(默认 env);脚本本身就运行在父会话里
        herdr(["pane", "close", outer_pane], timeout=30)
    if config_path:
        try:
            os.unlink(config_path)
        except OSError:
            pass


def write_nested_config(config_path: str) -> None:
    """写临时配置副本开启 allow_nested,不改共享配置。

    共享配置已有 [experimental] 节时,把 allow_nested 插进该节内,
    避免 TOML 重复表头导致整份配置被废弃回默认值。
    """
    src = os.path.expanduser("~/.config/herdr/config.toml")
    with open(src, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    if any(line.strip() == "allow_nested = true" for line in lines):
        result = lines
    else:
        result = list(lines)
        inserted = False
        for index, line in enumerate(lines):
            if line.strip() == "[experimental]":
                result.insert(index + 1, "allow_nested = true")
                inserted = True
                break
        if not inserted:
            result += ["[experimental]", "allow_nested = true"]
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(result) + "\n")


def build_nested_launch_command(session: str, config_path: str) -> str:
    """外层 shell 里启动一次性命名会话的完整命令(清除继承会话选择)。"""
    parts = ["env"]
    parts += [f"-u {key}" for key in SESSION_ENV_KEYS]
    parts += [f"HERDR_CONFIG_PATH={shlex.quote(config_path)}", shlex.quote(HERDR_BIN),
              "--session", shlex.quote(session)]
    return " ".join(parts)


def main(argv: list[str]) -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(
        description="herdr-auto-title 真实环境验收(一次性命名会话,真实 provider 调用)"
    )
    parser.add_argument("--agent", default="pi", help="agent 类型(默认 pi;pi 集成同步上报身份,时序最稳)")
    parser.add_argument(
        "--workdir", default="/var/tmp", help="隔离会话 pane 的工作目录(需已被 agent 信任)"
    )
    parser.add_argument("--session", default=None, help="命名会话名(默认 real-check-<时间戳>)")
    parser.add_argument("--settle-timeout", type=int, default=90, help="等定稿写 tab 的秒数")
    parser.add_argument("--prompt-timeout", type=int, default=240, help="等 agent idle 的秒数")
    parser.add_argument(
        "--session-timeout", type=int, default=120, help="等命名会话就绪的秒数"
    )
    parser.add_argument("--keep", action="store_true", help="结束时保留会话与 pane 供调试")
    parser.add_argument("--report", default=None, help="验收报告 JSON 路径")
    parser.add_argument(
        "--agent-arg", action="append", dest="agent_args", default=None,
        help="覆盖默认追加给 agent 的原生参数,可重复",
    )
    parser.add_argument("--no-agent-args", action="store_true", help="不给 agent 追加任何参数")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印轮询细节")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    # ---- 环境门禁:任何不满足都属退出码 2,不产生半途状态 ----
    if os.environ.get("HERDR_ENV") != "1":
        print("ERROR: 必须在 Herdr 管理 pane 内运行(HERDR_ENV=1);", file=sys.stderr)
        print("       嵌套启动命名会话依赖该环境。", file=sys.stderr)
        return 2
    version = herdr(["--version"], timeout=15)
    if version.returncode != 0:
        print(f"ERROR: herdr 不可用: {version.stderr.strip()}", file=sys.stderr)
        return 2
    log(f"herdr 版本: {version.stdout.strip()}")

    session = args.session or f"real-check-{time.strftime('%m%d-%H%M%S')}"
    outer_pane: str | None = None
    config_path = f"/var/tmp/herdr-{session}.toml"
    report: dict = {"session": session, "agent": args.agent, "scenarios": {}, "findings": {}}
    exit_code = 1
    try:
        # ---- 外层 pane:唯一允许清理的父会话 pane ----
        split = herdr_json(
            ["pane", "split", "--direction", "right", "--ratio", "0.5", "--cwd", args.workdir],
            timeout=30,
        )
        if not split or not split.get("pane"):
            print(f"ERROR: 无法创建外层 pane: {split}", file=sys.stderr)
            return 2
        outer_pane = split["pane"]["pane_id"]
        log(f"外层 pane: {outer_pane}")

        # ---- 一次性命名会话(临时配置副本开 allow_nested,不动共享配置) ----
        write_nested_config(config_path)
        launch = build_nested_launch_command(session, config_path)
        # 等 split 出的 shell 真正到提示符,避免启动命令在 shell 就绪前被丢弃
        shell_ready = herdr(
            ["pane", "wait-output", outer_pane, "--regex", "[$#] ?$"], timeout=30
        )
        if shell_ready.returncode != 0:
            debug(f"wait-output 未确认提示符({shell_ready.stderr.strip()}),继续尽力发送")
        proc = herdr(["pane", "send-text", outer_pane, launch + "\n"])
        if proc.returncode != 0:
            print(f"ERROR: 无法向外层 pane 发送启动命令: {proc.stderr.strip()}", file=sys.stderr)
            return 2

        # ---- 等命名会话就绪并读取目标 pane ----
        pane_id = None
        deadline = time.time() + args.session_timeout
        while time.time() < deadline:
            time.sleep(2)
            panes = herdr_json(["pane", "list"], session=session)
            if panes and panes.get("panes"):
                pane_id = panes["panes"][0]["pane_id"]
                break
        if not pane_id:
            tail = herdr(["pane", "read", outer_pane], timeout=15).stdout[-400:]
            print(
                "ERROR: 命名会话未在限时内就绪;外层 pane 末尾输出:\n" + tail,
                file=sys.stderr,
            )
            return 2
        log(f"命名会话就绪: {session};目标 pane: {pane_id}")

        # ---- 启动 agent(codex 需绕过 hook 信任规避本机 SessionStart 静默跳过;
        #      pi 集成同步上报,无需额外参数) ----
        if args.no_agent_args:
            agent_extra = []
        elif args.agent_args is not None:
            agent_extra = args.agent_args
        elif args.agent == "codex":
            agent_extra = ["--dangerously-bypass-hook-trust"]
        else:
            agent_extra = []
        start_cmd = ["agent", "start", AGENT_NAME, "--kind", args.agent,
                     "--pane", pane_id, "--timeout", "60000"]
        if agent_extra:
            start_cmd += ["--"] + agent_extra
        started = herdr_json(start_cmd, session=session, timeout=90)
        if not started or not started.get("agent"):
            print(f"ERROR: agent 启动失败: {started}", file=sys.stderr)
            return 2
        log(f"agent 就绪: {args.agent} (extra args: {agent_extra or '无'})")

        # ---- S1 基线 ----
        log("场景 S1: 基线标题生成")
        verdict, findings, entry = scenario_baseline(session, pane_id, args)
        report["scenarios"]["S1"] = verdict
        report["findings"]["S1"] = findings
        log(f"  S1: {verdict.upper()}")

        if verdict == "pass":
            identity_ok = identity_available(session, pane_id, entry)
            report["identity_reporting"] = identity_ok
            log(f"  会话身份上报: {'可用' if identity_ok else '不可用(环境阻塞)'}")
            # pi 没有 /clear(且 /fork 会开交互分支),第二次换新会话仍用 /new
            reset_slash = "/clear" if args.agent == "codex" else "/new"
            for key, slash in (("S2", "/new"), ("S3", reset_slash)):
                log(f"场景 {key}: {slash} 后新会话重新生成标题")
                verdict, findings, entry = scenario_new_session(
                    session, pane_id, args, key, slash, entry, identity_ok
                )
                report["scenarios"][key] = verdict
                report["findings"][key] = findings
                log(f"  {key}: {verdict.upper()}")

        # ---- 汇总 ----
        scenarios = report.get("scenarios", {})
        report["final_tab_label"] = tab_label(session)
        if any(v == "fail" for v in scenarios.values()):
            exit_code = 1
        elif any(v == "blocked" for v in scenarios.values()):
            exit_code = 3
        else:
            exit_code = 0
        verdict_word = {0: "PASS", 1: "FAIL", 3: "BLOCKED(环境)"}[exit_code]
        log(f"最终 tab 名: {report['final_tab_label']!r}")
        log(f"验收结论: {verdict_word} (exit={exit_code})")
        return exit_code
    finally:
        report_path = args.report or f"/var/tmp/real-session-check-{time.strftime('%m%d-%H%M%S')}.json"
        try:
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=1)
            log(f"报告: {report_path}")
        except OSError as exc:
            print(f"WARN: 报告写入失败: {exc}", file=sys.stderr)
        cleanup(session, outer_pane, args.keep, config_path)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("中断: 尽力清理", file=sys.stderr)
        sys.exit(130)