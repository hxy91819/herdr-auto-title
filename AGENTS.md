# AGENTS.md

本仓库是 herdr-auto-title 插件(已开源)。行为契约、参数与测试说明见 `README.md` 与 `auto_title.py` 模块头注释,此处只放工作流规则。

## 完成改动后的本机试用(硬性流程)

**完成判据** — 满足全部三条才算"完成一个改动",才进入试用:

1. 改动是一个逻辑闭环的功能或修复,不是松散的中间态文件堆叠;
2. `python3 -m py_compile auto_title.py` 通过;
3. `python3 -m unittest` 全绿(含 e2e 离线套件)。

基础验证没跑之前,任何状态都只是"进行中";多个独立改动各自走完本流程,不攒批合并验证。

**试用步骤** — 完成判据满足后立即执行:

1. 确认本机安装形态:`herdr plugin list` 显示 `herdr-auto-title ... enabled [local:...]`。local link 下代码改动即时生效(herdr 每次事件前刷新 manifest),无需重新安装或重启会话;"安装"这一步就是确认 link 状态。
2. 在隔离的命名测试会话里驱动真实事件链路:新 tab + 真实 agent + 短 prompt 触发 T0,等待完成触发 T1;核对 `python3 auto_title.py status`、tab 名、state 里的 `calls`/`draft_done`/`final_done`。要求在 herdr 管理的 pane 内工作(`HERDR_ENV=1`);隔离会话的建法与清理见 herdr 仓库的 `herdr-throwaway-repro` 工作流,核心约束:唯一命名的会话、外层 pane 用完即关、不触碰用户会话的任何 pane/tab。
3. 把试用证据写进改动报告:触发了哪些路径、真实 provider 调用次数、最终 tab 名。

**边界**:

- 纯文档、注释或测试文件的改动不影响插件运行时行为:可免端到端试用,但第 2 条判据的基础验证仍然必跑。
- 试用产生的 provider 调用保持最小:短 prompt、必要次数,不记录凭据或完整日志。
- 试用失败时,修复属于新的改动,重新走完本流程;不允许带着失败的试用结果提交推送。