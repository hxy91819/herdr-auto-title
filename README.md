# herdr-auto-title

A [herdr](https://github.com/herdrdev/herdr) plugin that gives every agent tab a short, human-readable task title — automatically, the moment you send your first prompt.

Without it, N agent panes all show up as `codex`, `pi - <dirname>`, or bare tab numbers in the tab bar. With it, tabs read like `修复标题生成竞态` or `Review the release checklist`.

## How it works

The plugin is fully event-driven (no polling, no daemon) and uses only herdr's public plugin contract:

- **T0 draft** — on the first `working` status change (you just sent the first prompt), it reads the pane and generates a draft title immediately (provider latency only).
- **T1 final** — when the turn settles (`idle` / background `done`), it regenerates from prompt + reply, which carries much more context.
- **Recheck** — optionally re-validates the title after N rounds to catch task drift.

Robustness details:

- A per-pane merged parking queue persists events that arrive while a pane is already generating, so a settled event can never be silently dropped by the non-blocking pane lock (fixed with observable cross-process tests).
- User-renamed tabs are never touched; agent-reported OSC titles are reused directly; in multi-pane tabs only the first agent pane speaks for the tab.
- Generation/revision guards make `reset`/`clear` immune to late provider results.
- When an installed Herdr agent integration reports a new native `agent_session` (for example after Codex `/new` or `/clear`), the next prompt starts a fresh title lifecycle. The previous plugin title stays visible until that prompt is titled, while a user-renamed tab remains untouched.
- Titles follow the language of the conversation (Chinese prompts get Chinese titles).

## Requirements

- herdr >= 0.8.2
- Python 3 (standard library only, no third-party dependencies)
- A local coding-agent CLI as the title provider. The default preset targets the CodeBuddy CLI (`codebuddy -p --tools '' --model hy3-ioa`); you can switch to any local agent via `--provider` / `--provider-cmd`, including `acpx` presets.

## Install

```bash
git clone https://github.com/hxy91819/herdr-auto-title.git
herdr plugin link /path/to/herdr-auto-title
herdr plugin list        # should show herdr-auto-title enabled [local:...]
```

Edits take effect immediately — herdr re-reads the linked manifest before each event hook.

## Usage

No configuration needed for the happy path: open a new tab, start an agent, send a prompt — the tab gets a draft title within seconds and a refined one when the turn finishes.

Operate on existing panes:

```bash
python3 auto_title.py once --dry-run -v   # preview what would be generated
python3 auto_title.py once                # backfill titles for existing panes
python3 auto_title.py status              # per-pane state table
python3 auto_title.py diagnose            # explain why a pane is not titled
python3 auto_title.py reset --pane w17:p1 # re-run for one pane
python3 auto_title.py clear               # revert all titles written by the plugin
```

Useful flags (see `--help` for the full list):

| Flag | Default | Meaning |
|---|---|---|
| `--provider` | `codebuddy` | `codebuddy` / `acpx-codebuddy` / `acpx` / `custom` |
| `--provider-cmd` | — | custom provider command template, e.g. `codex exec --model gpt-5.4` |
| `--model` | `hy3-ioa` | model id passed to the provider |
| `--target` | `tab` | write to `tab` (tab bar) / `pane` (pane metadata) / `both` |
| `--timeout` | `120` | per-call provider timeout in seconds |
| `--recheck-rounds` | `5` | which round triggers a title recheck |
| `--draft-only` | off | draft only, skip final/recheck (fewer provider calls) |

State lives under the directory herdr injects via `HERDR_PLUGIN_STATE_DIR` (per named session), with a diagnostic log at `auto-title.log` inside it.

## Tests

```bash
python3 -m unittest            # unit + deterministic end-to-end suites, ~4s
python3 -m unittest -v test_e2e_offline   # the cross-process race-scenario suite only
```

The end-to-end suite replays the real event model (one process per event, herdr-injected environment) against a fake `herdr` CLI and a fake provider, with file-gate timing — fully deterministic, no real provider or session needed.

### Real-environment acceptance

For changes that touch session-identity handling or provider integration, run the real-session acceptance after the offline suites pass:

```bash
# from inside a Herdr-managed pane (HERDR_ENV=1)
python3 real_session_check.py                    # default: pi agent, ~6 provider calls
python3 real_session_check.py -v --keep          # keep the throwaway session for debugging
python3 real_session_check.py --agent codex      # codex needs --dangerously-bypass-hook-trust
```

The script creates its own uniquely named throwaway Herdr session (via a temporary `HERDR_CONFIG_PATH` copy that enables `experimental.allow_nested` — shared config is never touched), starts a real coding agent in it, and verifies:

- **S1 baseline** — first prompt produces a T0+T1 titled tab owned by the plugin (`calls>=2`, `draft_done`/`final_done`).
- **S2 `/new`** — a fresh native session (`agent_session` identity change) resets the lifecycle (`generation` bumps, calls restart) and retitles the tab for the new task.
- **S3 second rollover** — `/new` again (pi) or `/clear` (codex) retitles once more.

It writes a JSON report to `/var/tmp/real-session-check-<timestamp>.json` and always cleans up the named session and outer pane. Exit codes: `0` pass, `1` plugin-behavior failure, `2` environment error, `3` pass but session-identity reporting unavailable (identity scenarios blocked).

Notes from real runs: the Codex integration's user-level `SessionStart` hook can be silently skipped by Codex when its hook trust hash is stale, leaving every pane without `agent_session` (identity scenarios then report environment-blocked). Codex also creates threads lazily, so its session report can land ~one prompt late; the pi integration reports synchronously on `session_start` and is the recommended test agent.

## License

MIT — see [LICENSE](LICENSE).
