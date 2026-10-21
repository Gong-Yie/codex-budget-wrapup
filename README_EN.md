# budget-wrapup · a wrap-up guard for Codex usage limits

When your Codex quota is nearly gone, don't let the agent stop mid-thought. This makes it write a hand-off summary before the turn is allowed to end.

- Fires when the 5-hour or weekly window crosses a threshold (85% by default)
- Forces exactly one extra round: a five-part summary (goal / done / state / remaining / uncommitted)
- Writes it to `<cwd>/.codex-wrapup/wrapup-YYYYMMDD-HHMMSS.md`
- Won't loop

## Install

```bash
python install.py
```

That copies the hook script to `~/.codex/budget-wrapup/budget_wrapup.py`, writes three hooks into `~/.codex/hooks.json` (existing file is backed up, foreign hooks preserved), and drops a `config.json` next to the script.

Then the one manual step — Codex requires a human to trust non-managed hooks:

```
codex
/hooks          # trust the SessionStart / UserPromptSubmit / Stop entries for budget-wrapup
```

Until then Codex skips them silently.

Check it:

```bash
python install.py --status
py -3 "$HOME/.codex/budget-wrapup/budget_wrapup.py" --check
```

## Where it hooks in

| Event | Behaviour |
|---|---|
| `SessionStart` | If already over the threshold, injects the remaining-quota warning up front |
| `UserPromptSubmit` | Adds a "finish and summarise" note to the model context when over the threshold |
| `Stop` | The core. Over threshold → returns `decision: block` plus the wrap-up instruction, which Codex treats as a new user prompt and runs one more turn |

The continuation happens once: `stop_hook_active` suppresses repeats, one session is capped at `max_blocks`, and re-triggering requires usage to climb another `step` points.

## Where the numbers come from

The last `token_count` event in `~/.codex/sessions/**/rollout-*.jsonl` carries `rate_limits`:

```json
{"primary":   {"used_percent": 91.0, "window_minutes": 300,   "resets_at": 1792521841},
 "secondary": {"used_percent": 48.0, "window_minutes": 10080, "resets_at": 1793108641}}
```

It prefers the `transcript_path` from the hook input, falls back to matching `session_id` under the sessions directory, then to the newest rollouts. Read-only, no network calls.

## Config

`~/.codex/budget-wrapup/config.json` — takes effect on the next hook run, no reinstall:

| Key | Default | Meaning |
|---|---|---|
| `threshold` | `85` | Percent that triggers the guard |
| `primary_threshold` | follows `threshold` | Override for the 5h window only |
| `secondary_threshold` | follows `threshold` | Override for the weekly window only |
| `mode` | `"enforce"` | `enforce` forces the extra round; `warn` only shows a notice |
| `max_blocks` | `3` | Max forced wrap-ups per session |
| `step` | `3` | Extra usage needed before forcing again |
| `lang` | `"zh"` | Summary instruction language; `en` for English |
| `dir` | `<cwd>/.codex-wrapup` | Where summaries land |
| `block_prompt` | `false` | When `true`, blocks new prompts at ≥95% (blunt, off by default) |
| `debug` | `false` | Writes `~/.codex/budget-wrapup/state/budget-wrapup.log` |

Environment variables with the same names win over the file (`BUDGET_WRAPUP_THRESHOLD`, `BUDGET_WRAPUP_MODE`, …).

## Known limits

- **Subscription auth only.** API-key sessions carry no `rate_limits`; the script exits quietly rather than guessing.
- **The CLI does not load plugin-bundled hooks.** Measured on Codex CLI 0.163: the plugin installs and enables, but `/hooks` reports `0 installed` and `hooks/hooks.json` inside the plugin root is never loaded. So installation writes a user-level `~/.codex/hooks.json` via `install.py`. The plugin manifest is kept for versions or desktop builds that do honour bundled hooks.
- **The data lags.** `rate_limits` reflects the last model request; a long turn can overshoot the threshold.
- **Soft guard.** The `Stop` hook can add one round; it cannot stop you from running Codex again with other flags.

## Uninstall

```bash
python install.py --uninstall
```

Removes only its own three hooks and `~/.codex/budget-wrapup/`, leaving other entries in `hooks.json` untouched.

## Layout

```
codex-budget-wrapup/
├── install.py                      # install / uninstall / status
├── .agents/plugins/marketplace.json
└── plugins/budget-wrapup/
    ├── plugin.json                 # plugin manifest (desktop / future versions)
    ├── hooks/
    │   ├── hooks.json              # plugin-form config (${PLUGIN_ROOT})
    │   └── budget_wrapup.py        # all logic, zero dependencies
    └── skills/wrapup-handoff/
        └── SKILL.md                # five-part template for manual wrap-ups
```

## Test

```bash
python plugins/budget-wrapup/hooks/budget_wrapup.py --selftest
```

No Codex required; covers window parsing, threshold decisions, anti-repeat logic and the summary path.
