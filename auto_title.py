#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""herdr-auto-title — herdr 插件:用本地 coding agent CLI 给 pane 生成简短任务标题。

## 脚本定义

用途
    解决 N 个 agent pane 全部显示成 `codex` / `pi - <dirname>` 而无法分辨的问题
    (上游 issue #1723 的现状)。当 agent 一轮跑完(状态变回 idle)时,读取 pane
    最近内容,交给本地 coding agent 生成一个 20~36 列宽的短标题,再通过
    `herdr pane report-metadata` 写回。

形态
    herdr 插件,订阅 `pane.agent_status_changed` 事件。正常负载下事件触发后立刻
    进入 provider,不需要轮询或常驻进程,也不需要改 herdr 核心代码。
    用 `herdr plugin link <本目录>` 安装。

适用边界
    - 只使用 herdr 的公开 CLI 与插件事件契约,对 stock herdr 有效。
    - herdr spawn 插件命令后不等待(后台线程),并自带 32 个并发上限。
      本脚本让不同 pane 并行、同一 pane 去重；若 hook 尚未启动就被宿主拒绝，
      需要 Herdr 的持久队列能力才能提供严格的 at-least-once 保证。

## 参数定义

    (无参数)              插件事件模式,由 herdr 调用,读取环境变量
    once                  手动扫一遍所有 pane(给存量 pane 补标题用)
    status                打印状态表
    reset [--pane ID]...  重置 pane 状态并重算(不指定则重置全部)
    clear                 清掉本插件设置的所有标题

    --provider NAME       codebuddy | acpx-codebuddy | acpx | custom(默认 codebuddy)
    --provider-cmd CMD    自定义 provider 命令模板
    --agent CMD           acpx provider 的底层 agent 命令
    --model ID            模型 id(默认 hy3-ioa)
    --lines N             读取 pane 行数(默认 90)
    --max-bytes N         喂给模型的最大字节数(默认 4000)
    --max-cols N          标题最大显示宽度(默认 36)
    --recheck-rounds N    第几轮复核标题(默认 5)
    --target WHICH        改哪里:tab(默认)| pane | both
                          tab  = 改 tab 名,顶部 tab 栏最显眼
                          pane = 改 pane 的 metadata title(边框 + 侧栏 {pane} token)
                          both = 两处都写
    --timeout N           单次 provider 调用超时秒数(默认 120)
    --dry-run             只打印将要做什么
    -v, --verbose         调试日志(会把 prompt 与原始返回写入 provider.log)

    环境变量: HERDR_BIN(默认 herdr), HERDR_PLUGIN_STATE_DIR(优先),
              HERDR_AUTO_TITLE_STATE_DIR(本地覆盖),
              HERDR_AUTO_TITLE_SESSION(命名会话状态隔离覆盖)。

## 输出结果定义

    stdout    once/status 的输出;事件模式无输出
    stderr    错误与告警,格式 `LEVEL: message`
    退出码    0 成功;2 参数或环境错误

    副作用文件:
        <state_dir>/state.json        pane 状态记录
        <state_dir>/provider.log      最近一次 provider 调用的 prompt 与原始返回

## 关键设计决策

    1. 事件驱动而非轮询。herdr 在 `pane.agent_status_changed` 事件里直接 spawn
       插件命令,状态变化的那一刻就开始工作,没有轮询间隔带来的延迟。

    2. 只有状态变回 idle 才继续,其余状态立即返回。事件也会因 presentation 变化
       而触发,所以轮次计数要求"上次状态不是 idle",避免重复计数。

    3. 两段触发,兼顾"多快出标题"和"标题准不准":
       - T0 初稿:状态变 working(用户刚发出第一个 prompt)就生成,约 15 秒后标题
         就出现。此时只有 prompt,质量取决于 prompt 写得是否具体。
       - T1 定稿:一轮跑完变 idle 后再生成一次,此时有 prompt + agent 回复,
         信息量充足,标题更准。定稿之后不再用初稿覆盖。
       只用其中一段也可以:--draft-only 只做初稿(省调用,出得快)。

    4. 每个 pane 默认最多 3 次模型调用:初稿 + 定稿 + 第 recheck_rounds 轮复核一次
       (把当前标题一起喂给模型判断保留还是修正,覆盖"任务漂移"又不会来回跳)。

    5. 不覆盖用户或 agent 已有的标题:手动 rename(label 非空)永久停手;agent
       自带的 OSC 标题若不是占位目录名则跳过,且每次事件重新判断。

    6. 默认不设 TTL,标题长期保留。daemon 挂掉时留着"上一轮任务"严格优于退回
       目录名;且 TTL 上限只有 24 小时,靠 TTL 仍得常驻续期。

    7. 调用 provider 前清掉 HERDR_* 环境变量并在中性目录运行:herdr 会给插件
       注入 HERDR_PANE_ID 等,不清会让子进程以为自己在 pane 内;中性目录则避免
       加载宿主仓库的 AGENTS.md 污染标题生成。

    8. 保守失败:任何一步出错只跳过当前 pane,不重试、不影响其他 pane。

    9. 默认改 tab 名而不是 pane 的 metadata title。tab 名在顶部 tab 栏一直可见,
       比只改边框更显眼。两条规则:
       - tab 已被命名且不是我们设的,视为用户手动命名,不覆盖。
       - 一个 tab 被切成多个 pane 时,标题只能有一个,取"第一个有 agent 的 pane"
         作为该 tab 的代言 pane(顺序沿用 herdr agent list 的返回顺序);
         其余 pane 让位,否则后跑完的 pane 会盖掉先跑完的。
       agent 自己上报了像样的 OSC 标题时,直接拿它当 tab 名,不调模型。
       注意 herdr 没有清除 tab 名的接口,所以 reset 是写回序号让它回到未命名态。

   10. 锁被占时停靠事件意图,不允许丢。同一 pane 的 provider 调用可长达
       1~2 分钟,期间的 working(T0)/settled(T1)事件拿不到非阻塞 pane_lock。
       直接丢弃会让"首个 prompt 的 T0"或"首次回复后的 T1"永不发生(实测:
       agent 启动 ready 的 idle 触发的 final 持锁时,用户首条 prompt 的 T0
       就这样丢了)。修复:锁忙时把意图写进 state.json 的每-pane 合并标志
       (working 停为 pending_draft,settled 停为 pending_settle),锁持有者
       在同一进程内串行消费(先 draft 后 settle)。不新增 hook 进程、不阻塞
       等锁,所以不占 herdr 的 32 个插件槽。标志生命周期由 mark/clear 独占
       管理,账目提交透明保留;消费前从磁盘重读,reset/clear 递增 generation
       清掉标志后,迟到结果不再触发新的生成。

   11. 原生会话身份是标题生命周期的边界。每个状态事件都会从 `pane get`
       读取完整的 agent_session(source/agent/kind/value);身份首次出现或变化时,
       清空上一会话的轮次、指纹和完成标记,让 `/new` / `/clear` 后的下一条
       prompt 可以重新命名。同一 pane 上旧的插件标题仍用于确认 tab 所有权,
       但不会作为新会话的“当前标题”传给 provider;人工改名保护保持不变。
       没有或格式不完整的 agent_session 不推断会话切换,兼容未安装集成的 agent。

## 调用范例

    # 安装(本地开发用 link,改动立即生效)
    herdr plugin link <本插件目录>
    herdr plugin list

    # 手动给存量 pane 补标题(先空跑看效果)
    python3 auto_title.py once --dry-run -v
    python3 auto_title.py once

    # 换 codex 来起标题(acpx 提供 provider 抽象)
    python3 auto_title.py once --provider acpx --agent 'codex --model gpt-5.4'

    # 运维
    python3 auto_title.py status
    python3 auto_title.py reset --pane w17:p1
    herdr plugin log auto-title
"""

from __future__ import annotations

import argparse
import errno
import hashlib
from contextlib import contextmanager
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

# ---------------------------------------------------------------- 常量与默认配置

DEFAULT_MODEL = "hy3-ioa"
DEFAULT_LINES = 90
DEFAULT_MAX_BYTES = 4000
DEFAULT_MAX_COLS = 36
MIN_COLS = 8
DEFAULT_RECHECK_ROUNDS = 5
FROZEN_ROUNDS = 12
DEFAULT_TIMEOUT = 120
# T0 初稿时 pane 里通常只有欢迎屏残渣 + 用户第一条 prompt,中文短 prompt
# 清洗后很短;阈值过高会把"第一个 prompt 出标题"这条核心路径整个挡死。
THIN_CONTENT_CHARS = 12
# 初稿失败(薄内容/NO_TITLE)后的冷却:prompt 在一轮内不会变多,连试必然连败
DRAFT_RETRY_COOLDOWN = 60
DEFAULT_JOBS = 4
STATE_TTL_DAYS = 7

# 锁被占时停靠的事件意图；由 mark/clear 独占管理，账目提交透明保留。
PARKED_DRAFT = "pending_draft"
PARKED_SETTLE = "pending_settle"
PARKED_KEYS = (PARKED_DRAFT, PARKED_SETTLE)

# Herdr 的公开 pane API 对原生 agent 会话使用这四个字段。只有全部为非空
# 字符串才接受该身份，避免不完整上报把旧会话误判成一次 /new。
AGENT_SESSION_FIELDS = ("source", "agent", "kind", "value")
# 会话切换后仍需识别“当前 tab 是本插件写的”，同时绝不能取消用户的人工命名。
SESSION_OWNERSHIP_KEYS = ("manual", "tab_user_named", "title", "tab_title", "tab_id")

METADATA_SOURCE = "herdr:auto-title"
NO_TITLE = "NO_TITLE"

# 清理子进程环境:herdr 会注入 HERDR_*,不清会让 provider 以为自己在 pane 内
STRIPPED_ENV_PREFIXES = ("HERDR_",)
STRIPPED_ENV_EXACT = ("CODEBUDDY_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "CLAUDECODE")

PROVIDER_PRESETS = {
    # 直接调用 codebuddy 的非交互模式。最快,依赖最少。
    "codebuddy": "codebuddy -p --tools '' --model {model}",
    # 经 acpx 走 ACP,输出语义为 final assistant text,可换任意注册 agent。
    "acpx-codebuddy": (
        "acpx --cwd '{workdir}' --format quiet --deny-all --timeout {timeout} "
        "--allowed-tools '' --agent 'codebuddy --acp --model {model}' exec"
    ),
    "acpx": (
        "acpx --cwd '{workdir}' --format quiet --deny-all --timeout {timeout} "
        "--allowed-tools '' --agent '{agent}' exec"
    ),
}


def state_session_scope() -> str | None:
    """为命名 Herdr 会话隔离 pane-id 状态，默认会话保留历史存储位置。"""
    name = os.environ.get("HERDR_AUTO_TITLE_SESSION") or os.environ.get("HERDR_SESSION")
    if name and name != "default":
        return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]

    socket_path = os.environ.get("HERDR_SOCKET_PATH", "")
    marker = "/sessions/"
    if marker in socket_path:
        name = socket_path.split(marker, 1)[1].split("/", 1)[0]
        if name:
            return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return None

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[()][0-9A-Za-z]"
    r"|\x1b[=>]"
)
RULE_RE = re.compile(r"^[\s─-╿═-▪-▫]{20,}$")
NOISE_SUBSTRINGS = (
    "bypass permissions",
    "for shortcuts",
    "shift+Tab to cycle",
    "Context left",
    "tokens ·",
    "esc to interrupt",
)


# ---------------------------------------------------------------------- 基础工具


# 事件模式标志:herdr 调用时没有 -v,决策日志只落文件不上 stderr,
# 否则会把 herdr 插件日志(滚动 50 条)刷满,反而丢了诊断线索。
EVENT_MODE = False
LOG_MAX_BYTES = 1_000_000


def _append_log(level: str, message: str) -> None:
    """所有决策都落一份带时间戳的文件日志,这是事件模式的唯一诊断入口。"""
    try:
        state_dir = CONFIG.state_dir
    except (NameError, AttributeError):
        return  # CONFIG 尚未初始化(如 Config 构造失败报错路径)
    try:
        ensure_state_dir()
        path = state_dir / "auto-title.log"
        with file_lock(state_dir / "auto-title.log.lock", blocking=True):
            if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
                path.unlink()  # 诊断日志允许只保留最近一轮。
            stamp = time.strftime("%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as handle:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                handle.write(f"{stamp} {level}: {message}\n")
    except OSError:
        pass


def log(level: str, message: str) -> None:
    if not EVENT_MODE or level in ("WARN", "ERROR"):
        print(f"{level}: {message}", file=sys.stderr, flush=True)
    _append_log(level, message)


def info(message: str) -> None:
    log("INFO", message)


def warn(message: str) -> None:
    log("WARN", message)


def error(message: str) -> None:
    log("ERROR", message)


def debug(message: str) -> None:
    if VERBOSE:
        log("DEBUG", message)
    elif EVENT_MODE:
        # 事件模式的 debug 只进文件:决策链(为什么没改名)就在这些行里
        _append_log("DEBUG", message)


class Config:
    def __init__(self, args: argparse.Namespace) -> None:
        # 插件进程继承的是 herdr server 的 PATH,里面常常没有 ~/.local/bin,
        # 所以优先用 herdr 注入的 HERDR_BIN_PATH(自身可执行文件路径)。
        self.herdr_bin = (
            os.environ.get("HERDR_BIN_PATH")
            or os.environ.get("HERDR_BIN")
            or "herdr"
        )
        self.model = args.model
        self.lines = args.lines
        self.max_bytes = args.max_bytes
        self.max_cols = args.max_cols
        self.recheck_rounds = args.recheck_rounds
        self.timeout = args.timeout
        self.dry_run = args.dry_run
        self.draft_only = args.draft_only
        self.jobs = args.jobs
        self.target = args.target
        self.agent = args.agent

        override_state_dir = os.environ.get("HERDR_AUTO_TITLE_STATE_DIR")
        injected_state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
        state_dir = override_state_dir or injected_state_dir
        if not state_dir:
            xdg = os.environ.get("XDG_STATE_HOME") or str(
                Path.home() / ".local" / "state"
            )
            state_dir = str(Path(xdg) / "herdr-auto-title")
        base_state_dir = Path(state_dir)
        scope = state_session_scope()
        self.state_dir = base_state_dir if scope is None else base_state_dir / "sessions" / scope
        self.state_path = self.state_dir / "state.json"
        self.legacy_state_path: Path | None = None
        if override_state_dir is None and injected_state_dir and scope is None:
            xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
            legacy_path = Path(xdg) / "herdr-auto-title" / "state.json"
            if legacy_path != self.state_path:
                self.legacy_state_path = legacy_path
        self.legacy_migration_marker = self.state_dir / ".legacy-state-migrated-v1"
        self.log_path = self.state_dir / "provider.log"

        if args.provider_cmd:
            self.provider_name = "custom"
            self.provider_cmd = args.provider_cmd
        else:
            self.provider_name = args.provider
            if args.provider not in PROVIDER_PRESETS:
                raise ValueError(
                    f"unknown provider {args.provider!r}; "
                    f"choose from {sorted(PROVIDER_PRESETS)} or pass --provider-cmd"
                )
            self.provider_cmd = PROVIDER_PRESETS[args.provider]

    def argv_for(self, workdir: str) -> list[str]:
        return shlex.split(
            self.provider_cmd.format(
                model=self.model,
                workdir=workdir,
                timeout=self.timeout,
                agent=self.agent,
            )
        )


VERBOSE = False
CONFIG: Config


def ensure_state_dir() -> None:
    """创建插件私有状态目录，避免标题和诊断暴露给同机其他账号。"""
    CONFIG.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(CONFIG.state_dir, 0o700)
    except OSError:
        # Windows 不支持 Unix 权限位时仍可依赖当前用户的目录 ACL。
        pass


@contextmanager
def file_lock(path: Path, *, blocking: bool):
    """跨平台文件锁；事件去重可非阻塞，状态提交必须短暂阻塞。"""
    ensure_state_dir()
    handle = path.open("a+b")
    locked = False
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

        if fcntl is not None:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle, flags)
                locked = True
            except OSError as exc:
                if blocking or exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
        elif msvcrt is not None:
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
            while not locked:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    if not blocking:
                        break
                    time.sleep(0.02)
        else:
            raise OSError("no supported cross-process file lock is available")

        yield locked
    finally:
        if locked:
            try:
                handle.seek(0)
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


@contextmanager
def state_lock():
    """保护短暂的 state.json 读改写；不得覆盖 provider 调用。"""
    with file_lock(CONFIG.state_dir / "state.json.lock", blocking=True) as locked:
        if not locked:
            raise OSError("could not lock auto-title state")
        yield


@contextmanager
def pane_lock(pane_id: str, *, blocking: bool):
    """同一 pane 的重复事件只保留一个执行者，不拖慢其他 pane 的 T0。"""
    digest = hashlib.sha256(pane_id.encode("utf-8")).hexdigest()[:24]
    with file_lock(CONFIG.state_dir / f"pane-{digest}.lock", blocking=blocking) as locked:
        yield locked


# ------------------------------------------------------------------ herdr CLI 封装


def herdr(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        [CONFIG.herdr_bin, *args], capture_output=True, text=True, timeout=timeout
    )


def herdr_json(*args: str, timeout: int = 15):
    proc = herdr(*args, timeout=timeout)
    if proc.returncode != 0:
        debug(f"herdr {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return payload.get("result")


def list_agents() -> list[dict]:
    result = herdr_json("agent", "list")
    if not isinstance(result, dict):
        return []
    agents = result.get("agents")
    return agents if isinstance(agents, list) else []


def get_pane(pane_id: str) -> dict | None:
    result = herdr_json("pane", "get", pane_id)
    if isinstance(result, dict):
        pane = result.get("pane")
        if isinstance(pane, dict):
            return pane
    return None


def agent_session_identity(pane: dict) -> dict[str, str] | None:
    """标准化 pane API 的原生会话身份；未知/不完整上报保持旧行为。"""
    raw = pane.get("agent_session")
    if not isinstance(raw, dict):
        return None
    identity: dict[str, str] = {}
    for field in AGENT_SESSION_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        identity[field] = value.strip()
    return identity


def read_pane(pane_id: str) -> str:
    proc = herdr(
        "agent",
        "read",
        pane_id,
        "--source",
        "recent",
        "--lines",
        str(CONFIG.lines),
        "--format",
        "text",
        timeout=20,
    )
    return proc.stdout if proc.returncode == 0 else ""


def report_title(pane_id: str, title: str) -> bool:
    args = ["pane", "report-metadata", pane_id, "--source", METADATA_SOURCE, "--title", title]
    # 不传 --ttl-ms 表示永不过期;见设计决策 6
    proc = herdr(*args, timeout=15)
    if proc.returncode != 0:
        debug(f"report-metadata failed for {pane_id}: {proc.stderr.strip()[:200]}")
        return False
    return True


def clear_title(pane_id: str) -> bool:
    proc = herdr(
        "pane", "report-metadata", pane_id, "--source", METADATA_SOURCE, "--clear-title", timeout=15
    )
    return proc.returncode == 0


# ------------------------------------------------------------------ tab 相关封装


def list_tabs() -> dict[str, dict] | None:
    """返回 tab_id -> tab 信息;查询失败返回 None(调用方应快速失败重试)。

    tab list 的 label 是"显示名":未命名的 tab 回落到序号字符串,
    因此 label == str(number) 即代表还没被命名过。
    """
    result = herdr_json("tab", "list")
    if not isinstance(result, dict):
        warn("tab list query failed; will retry on next event")
        return None
    tabs = result.get("tabs")
    if not isinstance(tabs, list):
        warn("tab list returned unexpected payload; will retry on next event")
        return None
    return {t["tab_id"]: t for t in tabs if isinstance(t, dict) and t.get("tab_id")}


def rename_tab(tab_id: str, label: str) -> bool:
    proc = herdr("tab", "rename", tab_id, label, timeout=15)
    if proc.returncode != 0:
        debug(f"tab rename failed for {tab_id}: {proc.stderr.strip()[:200]}")
        return False
    return True


def tab_is_unnamed(tab: dict) -> bool:
    """tab 还没被命名(label 不是有意义的名字)。

    herdr 的 label 语义(0.8.2,workspace.rs::tab_display_name):
    custom_name 优先,否则回落到 "tabs 数组位置+1"。number 则是稳定
    公开编号(递增分配,不随关闭重排)。同一 workspace 里前面有 tab
    关闭过时,未命名 tab 的 label(位置序)会小于 number(稳定编号),
    所以不能用 label == number 判断;否则这类 tab 会被当成用户手动
    命名而永久跳过(实测:未命名的 w18:tD label='7'/number=13 就这样
    丢了两轮会话的自动标题)。
    数字 label 一律视为未命名:位置回落必然是纯数字;用户手动命名的
    纯数字极罕见且视觉上与未命名无差别,自动标题接管是更优结果。
    空 label 同样视为未命名(无意义的名字)。"""
    label = str(tab.get("label") or "").strip()
    return not label or label.isdigit()


def first_agent_panes() -> dict[str, str]:
    """tab_id -> 该 tab 下第一个有 agent 的 pane_id。

    一个 tab 可能被切成多个 pane,标题只能有一个,所以取第一个有 agent 的
    pane 作为这个 tab 的"代言 pane"。其余 pane 让位,避免互相覆盖。
    顺序沿用 herdr agent list 的返回顺序(按 workspace / tab / pane 排列)。
    """
    primary: dict[str, str] = {}
    for agent in list_agents():
        pane_id = agent.get("pane_id")
        tab_id = agent.get("tab_id")
        if pane_id and tab_id:
            primary.setdefault(tab_id, pane_id)
    return primary


def should_skip_tab(pane_id: str, tab: dict, entry: dict, primary: dict[str, str]) -> bool:
    """判断能不能改这个 tab 的名字;返回 True 表示应跳过。

    两条规则:
    1. tab 已被命名且不是我们设的,视为用户手动命名,不覆盖。
    2. 一个 tab 多个 pane 时,只有第一个有 agent 的 pane 能定标题,
       其余让位,否则后跑完的 pane 会盖掉先跑完的。
    """
    if not tab_is_unnamed(tab) and tab.get("label") != entry.get("tab_title"):
        entry["tab_user_named"] = True
        debug(
            f"{pane_id}: tab {tab.get('tab_id')} already named {tab.get('label')!r}, "
            "leaving it alone"
        )
        return True

    tab_id = tab.get("tab_id")
    first = primary.get(tab_id) if tab_id else None
    if first is not None and first != pane_id:
        entry["tab_secondary"] = True
        debug(
            f"{pane_id}: tab {tab_id} is already represented by {first}, stepping aside"
        )
        return True
    return False


def apply_title(pane_id: str, tab_id: str | None, title: str, tab: dict | None, entry: dict) -> bool:
    """按 --target 把标题写到 pane 的 metadata title 和/或 tab 名上。"""
    ok = True
    if CONFIG.target in ("pane", "both"):
        if report_title(pane_id, title):
            entry["title"] = title
        else:
            ok = False
    if CONFIG.target in ("tab", "both"):
        if tab_id and tab is not None:
            if rename_tab(tab_id, title):
                entry["tab_title"] = title
                entry["tab_id"] = tab_id
            else:
                ok = False
        else:
            ok = False
    if ok:
        entry["applied"] = time.time()
    return ok


# ---------------------------------------------------------------------- 文本处理


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def clean_screen(text: str) -> str:
    """去掉终端 UI 噪音:分隔线、底部状态栏、token 计数。"""
    text = strip_ansi(text)
    kept: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            kept.append("")
            continue
        if RULE_RE.match(line):
            continue
        if any(noise in line for noise in NOISE_SUBSTRINGS):
            continue
        kept.append(line)
    out: list[str] = []
    for line in kept:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def truncate_bytes(text: str, max_bytes: int) -> str:
    """超长时保留尾部:最近的内容对"现在在干什么"最有信息量。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    text = encoded[-max_bytes:].decode("utf-8", errors="ignore")
    newline = text.find("\n")
    return text[newline + 1 :] if newline >= 0 else text


def display_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate_width(text: str, max_cols: int) -> str:
    if display_width(text) <= max_cols:
        return text
    out = ""
    for ch in text:
        if display_width(out + ch) > max_cols:
            break
        out += ch
    return out.rstrip()


def strip_emoji(text: str) -> str:
    out = []
    for ch in text:
        if unicodedata.category(ch) in ("So", "Sk") and ord(ch) > 0x2100:
            continue
        out.append(ch)
    return "".join(out)


def sanitize_title(raw: str) -> str | None:
    """把模型原始输出整理成可写入的标题;无法使用时返回 None。"""
    text = strip_ansi(raw).strip()
    for line in text.splitlines():
        if line.strip():
            text = line.strip()
            break
    else:
        return None

    for quote in ("'", '"', "“", "”", "‘", "’", "「", "」", "《", "》", "`"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
    text = re.sub(r"^(?:[-*•·]|\d+[.)、])\s*", "", text)
    text = re.sub(r"^(?:标题|title)\s*[:：]\s*", "", text, flags=re.IGNORECASE)

    text = strip_emoji(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")

    if not text or text.upper() == NO_TITLE:
        return None
    if display_width(text) < MIN_COLS:
        return None
    return truncate_width(text, CONFIG.max_cols)


def build_prompt(content: str, current_title: str | None) -> str:
    rules = "\n".join(
        [
            "- 只输出标题本身,不要引号、不要解释、不要序号、不要 emoji",
            f"- 显示宽度控制在 {MIN_COLS}~{CONFIG.max_cols} 列之间"
            f"(一个中文字或全角标点算 2 列,一个英文字母算 1 列)",
            "- 用「动词 + 对象」短语,不要以「帮/帮我/请」开头,不要出现「用户」二字",
            "- 语言与会话中用户提问的语言保持一致",
            "- 文件路径、API 名、专有名词保留原文,不要翻译",
            f"- 如果内容不足以判断任务,只输出 {NO_TITLE}",
        ]
    )
    parts = ["请为下面这个 AI coding agent 会话生成一个简短标题。", "", "要求:", rules]
    if current_title:
        parts += [
            "",
            f"这个会话当前的标题是「{current_title}」。",
            "如果它仍然准确,原样输出它;如果任务已经变化,输出新的标题。",
        ]
    parts += ["", "会话内容(已去除终端 UI 噪音):", "---", content, "---"]
    return "\n".join(parts)


def is_placeholder_title(stripped: str | None, cwd: str | None) -> bool:
    """判断 pane 自带的 OSC 标题是否只是目录名之类的占位信息。

    codebuddy 会自己把任务写进 OSC 标题,此时不该再生成;而 codex / pi / kiro
    只报目录名("lightcloud"、"π - mason-skills"),那正是需要自动标题的场景。
    """
    if not stripped or not stripped.strip():
        return True
    basename = Path(cwd).name if cwd else ""
    text = stripped.strip()
    text = re.sub(r"^[^\w\s]+\s*", "", text)  # spinner 前缀(✳ ⟳)
    text = re.sub(r"^[\w().+]+\s*[-–—:]\s*", "", text)  # "π - " / "kiro: "
    text = text.strip()
    if not text:
        return True
    if basename and (text == basename or text.endswith("/" + basename)):
        return True
    if cwd and text.endswith(cwd):
        return True
    if re.fullmatch(r"[\w.\-]+", text):  # 单个 token,通常是目录名
        return True
    return False


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ provider 调用


def extra_bin_dirs() -> list[str]:
    """插件进程拿到的 PATH 往往不含用户的 bin 目录,这里补上常见的几个。

    codebuddy 这类 CLI 通常装在 ~/.local/bin 或 nvm 的 node bin 下,
    而 herdr server 进程的 PATH 常常没有它们。
    """
    home = Path.home()
    candidates = [home / ".local" / "bin", home / "bin", Path("/usr/local/bin")]
    nvm = home / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        try:
            versions = sorted(nvm.iterdir(), reverse=True)
        except OSError:
            versions = []
        candidates += [v / "bin" for v in versions]
    return [str(d) for d in candidates if d.is_dir()]


def clean_env() -> dict:
    """供 provider 子进程使用的环境:去掉 HERDR_* 并补全 PATH。"""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(STRIPPED_ENV_PREFIXES) or key in STRIPPED_ENV_EXACT:
            del env[key]
    extra = [d for d in extra_bin_dirs() if d not in env.get("PATH", "")]
    if extra:
        existing = env.get("PATH")
        env["PATH"] = os.pathsep.join(extra + ([existing] if existing else []))
    return env


def neutral_workdir() -> str:
    """中性工作目录,避免 provider 读取宿主仓库的项目级配置。"""
    path = CONFIG.state_dir / "workdir"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def generate_title(content: str, current_title: str | None) -> str | None:
    """调用 provider 生成标题;失败时返回 None 且不重试。"""
    prompt = build_prompt(content, current_title)
    workdir = neutral_workdir()
    argv = CONFIG.argv_for(workdir)

    if CONFIG.dry_run:
        info(f"[dry-run] would call: {shlex.join(argv)} <<< {len(prompt)} bytes")
        return None

    debug(f"calling provider: {shlex.join(argv)}")
    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CONFIG.timeout,
            cwd=workdir,
            env=clean_env(),
        )
    except subprocess.TimeoutExpired:
        warn(f"provider timed out after {CONFIG.timeout}s")
        return None
    except OSError as exc:
        warn(f"provider could not be launched: {exc}")
        return None

    elapsed = time.time() - started
    if proc.returncode != 0:
        warn(f"provider exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
        return None

    record_log(prompt, proc.stdout, proc.stderr, elapsed)
    title = sanitize_title(proc.stdout)
    debug(f"provider returned in {elapsed:.1f}s: {title!r}")
    return title


def record_log(prompt: str, stdout: str, stderr: str, elapsed: float) -> None:
    if not VERBOSE:
        return
    try:
        with CONFIG.log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ({elapsed:.1f}s) ===\n"
                f"--- prompt ---\n{prompt}\n--- stdout ---\n{stdout}\n"
                f"--- stderr ---\n{(stderr or '').strip()[:1000]}\n"
            )
    except OSError:
        pass


# ---------------------------------------------------------------------- 状态存储


def read_state_file(path: Path, *, warn_on_error: bool) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if warn_on_error:
            warn(f"state file is unreadable: {path}")
        return {}
    return data if isinstance(data, dict) else {}


def load_state() -> dict:
    return read_state_file(CONFIG.state_path, warn_on_error=True)


def save_state(state: dict) -> None:
    """原子替换状态文件，避免并发读者看到半写入的 JSON。"""
    ensure_state_dir()
    fd, raw_tmp = tempfile.mkstemp(
        prefix="state-", suffix=".tmp", dir=CONFIG.state_dir
    )
    tmp = Path(raw_tmp)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CONFIG.state_path)
        try:
            os.chmod(CONFIG.state_path, 0o600)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink()


def migrate_legacy_state() -> None:
    """一次性合并旧 XDG 状态，保留源文件，避免升级后误判用户标题。"""
    legacy_path = CONFIG.legacy_state_path
    if legacy_path is None or CONFIG.legacy_migration_marker.exists():
        return
    with state_lock():
        if CONFIG.legacy_migration_marker.exists():
            return
        legacy = read_state_file(legacy_path, warn_on_error=True)
        current = load_state()
        if legacy:
            merged = dict(legacy)
            # 新目录中已存在的记录反映更新后的运行，优先保留。
            merged.update(current)
            save_state(merged)
        CONFIG.legacy_migration_marker.touch(exist_ok=True)
        try:
            os.chmod(CONFIG.legacy_migration_marker, 0o600)
        except OSError:
            pass


# ---------------------------------------------------------------------- 核心逻辑


def decide(entry: dict, status: str | None = "idle") -> str:
    """判断该做什么。

    返回空串表示跳过;否则是 draft / final / recheck 之一。
    用空串而不是 "skip",是为了让调用方可以直接用 `if action:` 判断。

    draft    用户刚发出第一个 prompt(状态变 working),先出一版初稿,
             让标题尽快出现;信息量只有 prompt,质量不如 final。
    final    一轮跑完(状态变 idle),基于 prompt + agent 回复生成定稿。
    recheck  第 recheck_rounds 轮后复核一次,覆盖"任务漂移"。
    """
    if entry.get("manual"):
        return ""
    if entry.get("inflight"):
        return ""

    if status == "working":
        # 只在第一次 prompt 时出初稿;后续轮次不再动
        if entry.get("draft_done") or entry.get("final_done"):
            return ""
        # 失败冷却:薄内容或 NO_TITLE 在同一轮里不会自愈,连试只是烧调用
        if time.time() - entry.get("draft_fail_at", 0) < DRAFT_RETRY_COOLDOWN:
            return ""
        return "draft"

    rounds = entry.get("rounds", 0)
    if rounds < 1:
        return ""
    if not entry.get("final_done") and not CONFIG.draft_only:
        return "final"
    if not CONFIG.draft_only:
        if CONFIG.recheck_rounds <= rounds <= FROZEN_ROUNDS and not entry.get("rechecked"):
            return "recheck"
    return ""


def generate_for(pane_id: str, entry: dict) -> str | None:
    """读 pane 内容并生成标题。内容没变则直接沿用已有标题。"""
    content = truncate_bytes(clean_screen(read_pane(pane_id)), CONFIG.max_bytes)
    if len(content.strip()) < THIN_CONTENT_CHARS:
        debug(f"{pane_id}: pane content too thin to title")
        return None
    digest = fingerprint(content)
    # 旧标题仍保留在 entry 中以证明 tab 归本插件所有；但它描述的是上一原生
    # 会话，传给 provider 会诱导模型“保留”而不是为 /new 后的任务重新命名。
    current_title = None if entry.get("session_rollover") else (
        entry.get("tab_title") or entry.get("title")
    )
    if entry.get("fingerprint") == digest and current_title:
        debug(f"{pane_id}: content unchanged, keeping existing title")
        return current_title
    if not CONFIG.dry_run:
        entry["fingerprint"] = digest
    return generate_title(content, current_title)


def prepare(
    pane_id: str,
    pane: dict,
    entry: dict,
    tabs: dict[str, dict],
    status: str,
    primary: dict[str, str] | None = None,
) -> tuple[str, dict | None, str | None]:
    """决定这个 pane 该怎么处理。

    返回 (action, tab, reuse):
      action  "" 跳过 / "draft" / "final" / "recheck" 需生成 / "reuse" 直接复用
      tab     tab 模式下对应的 tab 信息
      reuse   action == "reuse" 时可直接使用的标题(agent 自己上报的那个)
    """
    skip = ("", None, None)

    # pane 被用户手动 rename 过:说明用户在这条通道上有自己的意图,不碰
    if pane.get("label"):
        if not entry.get("manual"):
            info(f"{pane_id}: manual label detected, leaving it alone")
        entry["manual"] = True
        return skip

    tab_id = pane.get("tab_id")
    tab = tabs.get(tab_id) if tab_id else None
    if CONFIG.target in ("tab", "both") and tab is not None:
        if should_skip_tab(pane_id, tab, entry, primary or {}):
            return skip

    own = pane.get("terminal_title_stripped")
    if not is_placeholder_title(own, pane.get("cwd")):
        entry["own_title"] = True
        # agent 自己已经报了一个像样的标题:tab 模式直接拿来用,不必调模型;
        # pane 模式则跳过,避免在同一条通道上重复展示两个标题。
        if CONFIG.target == "tab" and tab is not None:
            # 已经是这个标题就别再写一次,否则每次扫描都会重复改写
            if tab.get("label") == own:
                return skip
            return ("reuse", tab, own)
        debug(f"{pane_id}: agent already reports a real title ({own!r})")
        return skip

    entry["own_title"] = False
    return (decide(entry, status), tab, None)


def reset_for_agent_session(entry: dict, session: dict[str, str]) -> dict:
    """为新的原生 agent 会话重建标题生命周期，保留标题归属与停靠事件。"""
    next_entry = {
        "rounds": 0,
        "calls": 0,
        "last_status": None,
        "generation": entry.get("generation", 0) + 1,
        "revision": entry.get("revision", 0) + 1,
        "seen": time.time(),
        "agent": session["agent"],
        "agent_session": session,
        "session_rollover": True,
    }
    for key in SESSION_OWNERSHIP_KEYS + PARKED_KEYS:
        if key in entry:
            next_entry[key] = entry[key]
    return next_entry


def begin_event(
    pane_id: str,
    status: str,
    data: dict,
    session: dict[str, str] | None,
) -> tuple[dict, int, int]:
    """记录事件跃变并立即落盘，provider 期间不持有全局状态锁。

    返回的快照剥离停靠标志：标志的生命周期由 mark/clear 独占管理，
    避免账目提交把并发停靠的新事件意图覆盖掉。

    rounds 只在 working→idle 跃变时递增：agent 启动 ready 的 idle
    (unknown→idle)也算 idle 跃变，若让它递增，decide 会对只有欢迎屏的
    空面板烧一次 provider 调用(实测每个新 pane 都这样浪费一次，且常
    返回 NO_TITLE)。存量 pane 的补标题职责在 once 命令，不在事件路径。
    """
    with state_lock():
        state = load_state()
        entry = state.setdefault(
            pane_id,
            {
                "rounds": 0,
                "calls": 0,
                "last_status": None,
                "generation": 0,
                "revision": 0,
            },
        )
        if session is not None and entry.get("agent_session") != session:
            # generation 让正在运行的旧 provider 结果无法提交；revision 同时
            # 失效当前事件的旧快照。停靠标记由 reset helper 原样带入，确保
            # 新会话的 T1 不会因切换而丢失。
            entry = reset_for_agent_session(entry, session)
            state[pane_id] = entry
            debug(f"{pane_id}: native agent session changed; restarting title lifecycle")
        else:
            entry.setdefault("generation", 0)
            entry["revision"] = entry.get("revision", 0) + 1
        if status == "idle" and entry.get("last_status") == "working":
            entry["rounds"] = entry.get("rounds", 0) + 1
        entry["last_status"] = status
        entry["seen"] = time.time()
        entry["agent"] = (
            session["agent"] if session is not None else data.get("agent") or entry.get("agent")
        )
        save_state(state)
        snapshot = {k: v for k, v in entry.items() if k not in PARKED_KEYS}
        return snapshot, entry["generation"], entry["revision"]


def commit_event(pane_id: str, generation: int, revision: int, entry: dict) -> bool:
    """仅提交仍属于本次事件世代的结果，reset/clear 后的迟到结果会被丢弃。

    磁盘上比本快照新的停靠标志被透明保留：它们属于锁释放后才会消费的事件。
    """
    with state_lock():
        state = load_state()
        current = state.get(pane_id)
        if (
            not isinstance(current, dict)
            or current.get("generation", 0) != generation
            or current.get("revision", 0) != revision
        ):
            return False
        parked = {k: current[k] for k in PARKED_KEYS if current.get(k)}
        merged = {**entry, **parked}
        merged["generation"] = generation
        merged["revision"] = revision
        state[pane_id] = merged
        save_state(state)
        return True


def mark_pending_event(pane_id: str, status: str) -> None:
    """锁被占用时把事件意图写进 state.json；working 停为 draft，settled 停为 final。"""
    key = PARKED_DRAFT if status == "working" else PARKED_SETTLE
    with state_lock():
        state = load_state()
        entry = state.get(pane_id)
        if not isinstance(entry, dict):
            # begin_event 可能还没跑过；先建最小骨架，字段与 begin_event 保持一致。
            entry = {
                "rounds": 0,
                "calls": 0,
                "last_status": None,
                "generation": 0,
                "revision": 0,
            }
            state[pane_id] = entry
        entry[key] = True
        save_state(state)
    debug(f"{pane_id}: lock busy, parked {status} intent as {key}")


def clear_parked(pane_id: str, key: str) -> None:
    """决策已出(生成/复用/跳过都算消费)，作废对应停靠标志。"""
    with state_lock():
        state = load_state()
        entry = state.get(pane_id)
        if isinstance(entry, dict) and entry.pop(key, None) is not None:
            save_state(state)


def next_parked_event(pane_id: str) -> str | None:
    """读取下一个待消费的停靠意图；无则返回 None。"""
    with state_lock():
        state = load_state()
        entry = state.get(pane_id)
        if not isinstance(entry, dict):
            return None
        if entry.get(PARKED_DRAFT):
            return "working"
        if entry.get(PARKED_SETTLE):
            return "idle"
        return None


def consume_pending_events(pane_id: str, data: dict, result: int) -> int:
    """仍持有 pane_lock 时串行消费停靠的事件意图。

    每轮从 state 重读标志(而非内存快照)，所以 reset/clear 递增 generation
    清掉标志后，迟到结果不会再触发新的生成。处理失败(tabs 查询失败等)
    时保留标志，下次事件重试。
    """
    while result == 0:
        next_status = next_parked_event(pane_id)
        if next_status is None:
            return result
        result = _handle_event(pane_id, next_status, data)
    return result


def handle_event() -> int:
    """插件事件入口:由 herdr 在 pane.agent_status_changed 时调用。"""
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    if not raw:
        error("HERDR_PLUGIN_EVENT_JSON is missing; run without arguments only as a plugin hook")
        return 2
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        error(f"HERDR_PLUGIN_EVENT_JSON is not valid JSON: {exc}")
        return 2

    data = event.get("data") or {}
    pane_id = data.get("pane_id")
    status = data.get("agent_status")
    if not pane_id:
        return 0

    # background pane 完成时 Herdr 会报告 done；对标题状态机它和 idle 都是
    # "本轮已完成"，必须进入 T1，不能因为 tab 未聚焦而漏掉定稿。
    if status == "done":
        status = "idle"

    # 只关心 working(用户发了 prompt)与 settled(一轮跑完)两个跃变，
    # blocked/unknown 直接返回，避免无谓地拉起进程。
    if status not in ("working", "idle"):
        debug(f"{pane_id}: status={status}, nothing to do")
        return 0

    with pane_lock(pane_id, blocking=False) as acquired:
        if not acquired:
            # 锁被同 pane 的上一个事件占着(通常是 provider 调用)。不能直接丢:
            # 首个 prompt 的 T0 或首次回复后的 T1 都可能因此永不发生。
            # 把事件意图持久化成每-pane 合并队列的标志，由锁持有者在同进程内
            # 串行消费(不新增 hook 进程，不占 herdr 的 32 个插件槽)。
            mark_pending_event(pane_id, status)
            return 0
        result = _handle_event(pane_id, status, data)
        return consume_pending_events(pane_id, data, result)


def mark_pending_event(pane_id: str, status: str) -> None:
    """锁被占用时把事件意图写进 state.json；working 停为 draft，settled 停为 final。"""
    key = "pending_draft" if status == "working" else "pending_settle"
    with state_lock():
        state = load_state()
        entry = state.get(pane_id)
        if not isinstance(entry, dict):
            # begin_event 可能还没跑过；先建最小骨架，字段与 begin_event 保持一致。
            entry = {
                "rounds": 0,
                "calls": 0,
                "last_status": None,
                "generation": 0,
                "revision": 0,
            }
            state[pane_id] = entry
        entry[key] = True
        save_state(state)
    debug(f"{pane_id}: lock busy, parked {status} intent as {key}")


def consume_pending_events(pane_id: str, data: dict, result: int) -> int:
    """仍持有 pane_lock 时串行消费停靠的事件意图。

    每轮从 state 重读标志(而非内存快照)，所以 reset/clear 递增 generation
    清掉标志后，迟到结果不会再触发新的生成。先清标志再处理：同 pane 的
    mark 只发生在锁被占时，而消费循环全程持锁，两者不会交错；处理失败
    (tabs 查询失败等)时标志已清，依赖下次真实事件重试。
    """
    while result == 0:
        next_status = next_parked_event(pane_id)
        if next_status is None:
            return result
        clear_parked(pane_id, PARKED_DRAFT if next_status == "working" else PARKED_SETTLE)
        result = _handle_event(pane_id, next_status, data)
    return result


def _handle_event(pane_id: str, status: str, data: dict) -> int:
    """处理一个 pane 的事件；不同 pane 可并行生成，同 pane 的重复事件会合并。"""
    pane = get_pane(pane_id)
    if pane is None:
        return 0
    entry, generation, revision = begin_event(
        pane_id, status, data, agent_session_identity(pane)
    )
    if CONFIG.target in ("tab", "both"):
        tabs = list_tabs()
        # 查询失败就整体退出(非零),herdr 会记为 failed;下次事件再触发时重试。
        # 带着空 tabs 继续跑会导致写入失败却照常记账,标题就永远丢了。
        if tabs is None:
            return 1
        primary = first_agent_panes()
    else:
        tabs, primary = {}, {}
    action, tab, reuse = prepare(pane_id, pane, entry, tabs, status, primary)
    debug(
        f"{pane_id}: status={status} rounds={entry.get('rounds')} "
        f"calls={entry.get('calls')} -> {action or 'skip'}"
    )
    if not action:
        commit_event(pane_id, generation, revision, entry)
        return 0


    if action == "reuse":
        title = reuse
    else:
        title = generate_for(pane_id, entry)

    tab_id = tab.get("tab_id") if tab else None
    if CONFIG.dry_run:
        where = f"tab {tab_id}" if CONFIG.target == "tab" else pane_id
        info(f"[dry-run] {where}: would set title {title!r}")
        return 0

    entry["calls"] = entry.get("calls", 0) + 1
    if not title:
        warn(f"{pane_id}: no title produced")
        if action == "draft":
            entry["draft_fail_at"] = time.time()
    elif apply_title(pane_id, tab_id, title, tab, entry):
        info(f"{tab_id or pane_id}: set title -> {title}")
        # 只有写成功才记账;失败时保持原状,下次事件重试
        entry.pop("draft_fail_at", None)
        if action == "draft":
            entry["draft_done"] = True
        elif action == "final":
            entry["final_done"] = True
        elif action == "recheck":
            entry["rechecked"] = True
        elif action == "reuse":
            entry["final_done"] = True
        # 仅成功写入后才确认新会话已接管标题；写失败时下次重试仍不把旧标题
        # 传给 provider，避免 /new 后退回上一任务的语义。
        entry.pop("session_rollover", None)
    else:
        warn(f"{pane_id}: failed to write title; will retry on next event")
    commit_event(pane_id, generation, revision, entry)
    return 0


# ---------------------------------------------------------------------- 子命令


def cmd_once(args: argparse.Namespace) -> int:
    """手动扫一遍所有 agent pane,给存量 pane 补标题。

    插件只对未来的状态变化生效,存量 pane 需要靠这个命令补一次。
    """
    with state_lock():
        state = load_state()
    initial_revisions = {
        pane_id: entry.get("revision", 0)
        for pane_id, entry in state.items()
        if isinstance(entry, dict)
    }
    scanned: set[str] = set()
    pane_filter = set(args.pane) if args.pane else None
    if CONFIG.target in ("tab", "both"):
        tabs = list_tabs()
        if tabs is None:
            error("tab list query failed; nothing written this run")
            return 1
        primary = first_agent_panes()
    else:
        tabs, primary = {}, {}

    # 阶段一(串行):挑出需要生成的 pane。这一步只调 herdr CLI,很快。
    pending: list[tuple[str, dict, str, dict | None, str | None]] = []
    for agent in list_agents():
        pane_id = agent.get("pane_id")
        if not pane_id or not agent.get("agent"):
            continue
        if pane_filter and pane_id not in pane_filter:
            continue
        entry = state.setdefault(
            pane_id, {"rounds": 0, "calls": 0, "generation": 0, "revision": 0}
        )
        entry.setdefault("generation", 0)
        entry.setdefault("revision", 0)
        initial_revisions.setdefault(pane_id, entry["revision"])
        scanned.add(pane_id)
        entry["seen"] = time.time()
        entry["agent"] = agent.get("agent")
        # 存量 pane 没有跃变历史,当前是 idle 就当作一轮已完成
        if agent.get("agent_status") == "idle":
            entry["rounds"] = max(entry.get("rounds", 0), 1)

        pane = get_pane(pane_id)
        if pane is None:
            continue
        action, tab, reuse = prepare(pane_id, pane, entry, tabs, "idle", primary)
        debug(f"{pane_id}: rounds={entry.get('rounds')} -> {action or 'skip'}")
        if action:
            pending.append((pane_id, entry, action, tab, reuse))

    if not pending:
        info("nothing to do")
        return 0

    # 阶段二(并行):调用 provider。每个约 15 秒,串行会非常慢。
    # reuse 的 pane 直接沿用 agent 自己的标题,不进这一阶段。
    def work(item):
        pane_id, entry, action, _tab, reuse = item
        if action == "reuse":
            return pane_id, reuse
        return pane_id, generate_for(pane_id, entry)

    if CONFIG.jobs > 1 and len(pending) > 1:
        with ThreadPoolExecutor(max_workers=min(CONFIG.jobs, len(pending))) as pool:
            results = list(pool.map(work, pending))
    else:
        results = [work(item) for item in pending]

    # 阶段三(串行):写回标题并记账,避免并发写状态文件
    touched = 0
    for item, (_pid, title) in zip(pending, results):
        pane_id, entry, action, tab, _reuse = item
        tab_id = tab.get("tab_id") if tab else None
        where = tab_id or pane_id
        if CONFIG.dry_run:
            info(f"[dry-run] {where}: would set title {title!r}")
            continue
        entry["calls"] = entry.get("calls", 0) + 1
        if not title:
            warn(f"{pane_id}: no title produced")
            continue
        if apply_title(pane_id, tab_id, title, tab, entry):
            info(f"{where}: set title -> {title}")
            touched += 1
            # 只有写成功才记账;失败时保持原状,下次运行重试
            if action == "draft":
                entry["draft_done"] = True
            elif action == "reuse":
                # 复用 agent 自己的标题已经是最终形态,不再走 final
                entry["final_done"] = True
            elif action == "final":
                entry["final_done"] = True
            elif action == "recheck":
                entry["rechecked"] = True
        else:
            warn(f"{pane_id}: failed to write title")

    if not CONFIG.dry_run:
        with state_lock():
            latest = load_state()
            for pane_id in scanned:
                entry = state.get(pane_id)
                current = latest.get(pane_id)
                if not isinstance(entry, dict):
                    continue
                if current is None or (
                    isinstance(current, dict)
                    and current.get("revision", 0) == initial_revisions[pane_id]
                ):
                    latest[pane_id] = entry
            save_state(latest)
    info(f"done, {touched} title(s) written")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state()
    if not state:
        print("no state recorded yet")
        return 0
    header = f"{'PANE':<10} {'AGENT':<12} {'R':>3} {'C':>3}  TITLE"
    print(header)
    print("-" * len(header))
    for pane_id in sorted(state):
        entry = state[pane_id]
        if CONFIG.target == "tab":
            title = entry.get("tab_title")
        else:
            title = entry.get("title")
        if not title:
            title = "<manual>" if entry.get("manual") else "-"
            if entry.get("tab_user_named"):
                title = "<tab 由用户命名>"
            elif entry.get("tab_secondary"):
                title = "<tab 由另一个 pane 代表>"
        print(
            f"{pane_id:<10} {entry.get('agent', '?'):<12} "
            f"{entry.get('rounds', 0):>3} {entry.get('calls', 0):>3}  {title}"
        )
    print(f"\nstate file: {CONFIG.state_path}")
    return 0


def revert_target(pane_id: str, entry: dict, tabs: dict[str, dict]) -> None:
    """撤销本插件写过的标题。

    pane 的 metadata title 有 --clear-title 可以真正清掉;
    tab 名没有清除接口(set_custom_name 会把空串存成 Some("")),
    只能写回序号,让它回到"看起来未命名"的状态。
    """
    if CONFIG.target in ("pane", "both") and entry.get("title"):
        if clear_title(pane_id):
            entry["title"] = None
    if CONFIG.target in ("tab", "both") and entry.get("tab_title"):
        tab_id = entry.get("tab_id")
        tab = tabs.get(tab_id) if tab_id else None
        if tab is None:
            warn(f"{pane_id}: tab is unavailable; leaving its title unchanged")
            return
        if tab.get("label") != entry["tab_title"]:
            entry["tab_user_named"] = True
            entry["tab_title"] = None
            info(f"{pane_id}: tab title changed by user, leaving it unchanged")
            return
        if rename_tab(tab_id, str(tab.get("number"))):
            entry["tab_title"] = None


def reset_entry(entry: dict) -> dict:
    """保留世代标记以废弃已在路上的 provider 结果。"""
    return {
        "rounds": 0,
        "calls": 0,
        "last_status": None,
        "generation": entry.get("generation", 0) + 1,
        "revision": entry.get("revision", 0) + 1,
        "seen": time.time(),
        "agent": entry.get("agent"),
    }


def cmd_reset(args: argparse.Namespace) -> int:
    with state_lock():
        state = load_state()
    targets = args.pane if args.pane else list(state)
    if not targets:
        info("nothing to reset")
        return 0
    for pane_id in targets:
        if CONFIG.dry_run:
            info(f"[dry-run] {pane_id}: would reset")
            continue
        with pane_lock(pane_id, blocking=True):
            with state_lock():
                current_state = load_state()
                entry = dict(current_state.get(pane_id) or {})
            tabs = list_tabs() if CONFIG.target in ("tab", "both") else {}
            if CONFIG.target in ("tab", "both") and tabs is None:
                warn(f"{pane_id}: tab list query failed; reset skipped")
                continue
            revert_target(pane_id, entry, tabs)
            with state_lock():
                current_state = load_state()
                current = current_state.get(pane_id)
                if isinstance(current, dict):
                    current_state[pane_id] = reset_entry(current)
                    save_state(current_state)
        info(f"{pane_id}: reset")
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    with state_lock():
        state = load_state()
    for pane_id in list(state):
        entry = state[pane_id]
        if not (entry.get("title") or entry.get("tab_title")):
            continue
        if CONFIG.dry_run:
            info(f"[dry-run] {pane_id}: would revert title")
            continue
        with pane_lock(pane_id, blocking=True):
            with state_lock():
                current_state = load_state()
                current = dict(current_state.get(pane_id) or {})
            tabs = list_tabs() if CONFIG.target in ("tab", "both") else {}
            if CONFIG.target in ("tab", "both") and tabs is None:
                warn(f"{pane_id}: tab list query failed; clear skipped")
                continue
            revert_target(pane_id, current, tabs)
            current["calls"] = 0
            current["generation"] = current.get("generation", 0) + 1
            current["revision"] = current.get("revision", 0) + 1
            with state_lock():
                current_state = load_state()
                if isinstance(current_state.get(pane_id), dict):
                    current_state[pane_id] = current
                    save_state(current_state)
        info(f"{pane_id}: reverted title")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """重放判定链,解释目标 pane 为什么(还)没有被命名。

    默认聚焦"还没出成果"的 pane(tab 模式 = tab 未命名的,pane 模式 = 无
    metadata title 的);用 --pane 指定任意 pane。只读,不改状态。
    """
    state = load_state()
    if CONFIG.target in ("tab", "both"):
        tabs = list_tabs()
        if tabs is None:
            error("tab list query failed; diagnosis needs a consistent tab snapshot")
            return 1
    else:
        tabs = {}
    primary = first_agent_panes() if CONFIG.target in ("tab", "both") else {}
    pane_filter = set(args.pane) if args.pane else None
    shown = 0

    for agent in list_agents():
        pane_id = agent.get("pane_id")
        if not pane_id or not agent.get("agent"):
            continue
        pane = get_pane(pane_id)
        if pane is None:
            continue
        entry = dict(state.get(pane_id) or {})
        done = entry.get("tab_title") if CONFIG.target == "tab" else entry.get("title")
        if not pane_filter and done:
            continue  # 已经出成果的默认不展示,聚焦问题 pane
        if pane_filter and pane_id not in pane_filter:
            continue

        action, tab, reuse = prepare(pane_id, pane, entry, tabs, "idle", primary)
        draft = decide(dict(entry), "working")
        tab_id = pane.get("tab_id")
        label = tab.get("label") if tab else "?"
        flags = [k for k in ("manual", "tab_user_named", "tab_secondary", "own_title") if entry.get(k)]
        print(f"{pane_id} -> tab {tab_id} (label={label!r})")
        print(
            f"  agent={agent.get('agent')} status={agent.get('agent_status')} "
            f"rounds={entry.get('rounds', 0)} calls={entry.get('calls', 0)} "
            f"draft_done={entry.get('draft_done')} final_done={entry.get('final_done')}"
        )
        print(f"  挡断标记: {flags or '无'}")
        verdict = action or "skip(已处理或被挡)"
        if reuse:
            verdict += f",可直接复用 {reuse!r}"
        print(f"  idle 决策: {verdict}    working 决策: {draft or 'skip'}")
        print()
        shown += 1

    if not shown:
        print("没有需要诊断的 pane(都已命名或没有 agent)")
    print(f"事件日志: {CONFIG.state_dir / 'auto-title.log'}")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    with state_lock():
        state = load_state()
        cutoff = time.time() - STATE_TTL_DAYS * 86400
        stale = [
            pane_id
            for pane_id, entry in state.items()
            if isinstance(entry, dict) and entry.get("seen", 0) < cutoff
        ]
        for pane_id in stale:
            del state[pane_id]
        save_state(state)
    info(f"removed {len(stale)} stale state entries")
    return 0


# ------------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto_title.py",
        description="herdr 插件:用本地 coding agent CLI 给 pane 生成简短任务标题。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  herdr plugin link <本插件目录>     安装(事件驱动,无需常驻)
  %(prog)s once --dry-run -v                        空跑看效果
  %(prog)s once                                     给存量 pane 补标题
  %(prog)s once --provider acpx --agent 'codex --model gpt-5.4'
  %(prog)s status                                   查看状态
  %(prog)s diagnose                                 解释哪些 pane 没被命名及原因
  %(prog)s reset --pane w17:p1                      重算某个 pane
  herdr plugin log auto-title                       查看插件运行日志
""",
    )
    parser.add_argument(
        "command", nargs="?", help="once | status | reset | clear | gc | diagnose;省略则为插件事件模式"
    )
    parser.add_argument("--pane", action="append", metavar="ID", help="限定 pane,可重复")
    parser.add_argument(
        "--provider", default="codebuddy", help=f"preset: {', '.join(PROVIDER_PRESETS)}"
    )
    parser.add_argument("--provider-cmd", help="自定义 provider 命令模板")
    parser.add_argument("--agent", default=f"codebuddy --acp --model {DEFAULT_MODEL}")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--lines", type=int, default=DEFAULT_LINES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-cols", type=int, default=DEFAULT_MAX_COLS)
    parser.add_argument("--recheck-rounds", type=int, default=DEFAULT_RECHECK_ROUNDS)
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help="只在第一次 prompt 后生成一次,不做定稿与复核(省调用,质量取决于 prompt)",
    )
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="once 模式并发数")
    parser.add_argument(
        "--target",
        choices=("tab", "pane", "both"),
        default="tab",
        help="改哪里:tab 名(默认,顶部 tab 栏最显眼) / pane 的 metadata title / 两者",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    global VERBOSE, CONFIG, EVENT_MODE

    parser = build_parser()
    args = parser.parse_args(argv)
    VERBOSE = args.verbose
    EVENT_MODE = args.command is None

    try:
        CONFIG = Config(args)
    except ValueError as exc:
        error(str(exc))
        return 2

    try:
        ensure_state_dir()
        migrate_legacy_state()
    except OSError as exc:
        error(f"could not prepare plugin state: {exc}")
        return 1

    commands = {
        "once": cmd_once,
        "status": cmd_status,
        "reset": cmd_reset,
        "clear": cmd_clear,
        "gc": cmd_gc,
        "diagnose": cmd_diagnose,
    }
    if args.command is None:
        return handle_event()
    if args.command not in commands:
        error(f"unknown command {args.command!r}; choose from {', '.join(commands)}")
        return 2
    try:
        return commands[args.command](args)
    except subprocess.TimeoutExpired as exc:
        error(f"herdr command timed out: {exc}")
        return 1
    except OSError as exc:
        error(f"{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
