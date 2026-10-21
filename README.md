# budget-wrapup · Codex 额度收尾守卫

用量快到限额时，别让 Codex 把话说到一半就断。它会在这一轮结束前拦一下，逼 Codex 先写一份能交接的总结，再收工。

- 5 小时窗口或周窗口用到阈值（默认 85%）→ 触发
- 触发时 Codex 会被强制多跑一轮：写五段总结（目标 / 已完成 / 当前状态 / 未完成 / 未提交改动）
- 总结落到 `<cwd>/.codex-wrapup/wrapup-YYYYMMDD-HHMMSS.md`
- 同一会话不会无限循环

## 安装

```bash
python install.py
```

会做三件事：

1. 把 hook 脚本复制到 `~/.codex/budget-wrapup/budget_wrapup.py`
2. 往 `~/.codex/hooks.json` 写三条 hook（已有文件会先备份，别人的 hook 保留）
3. 生成 `~/.codex/budget-wrapup/config.json`

然后必须做一步人工操作——Codex 对非管理来源的 hook 一律要求本人信任：

```
codex
/hooks          # 把 budget-wrapup 的 SessionStart / UserPromptSubmit / Stop 三条设为 trusted
```

没信任之前 Codex 会静默跳过它们，不报错。

验证装好了：

```bash
python install.py --status
py -3 "$HOME/.codex/budget-wrapup/budget_wrapup.py" --check
```

`--check` 直接打当前窗口用量和阈值，不依赖 Codex 在跑。

## 它拦在哪

| 事件 | 行为 |
|---|---|
| `SessionStart` | 已经超阈值时，开局就注入一句「剩多少、先收尾」 |
| `UserPromptSubmit` | 超阈值时给模型塞上下文，界面上提示额度，不拦你说话 |
| `Stop` | 核心。超阈值 → 返回 `decision: block` + 收尾指令，Codex 当成新的用户消息再跑一轮 |

`Stop` 的续轮只给一次机会：`stop_hook_active` 为真时不再叠，同一会话最多 `max_blocks` 次，而且上一次触发后额度得再涨 `step` 个点才会再提醒。

## 额度从哪读

`~/.codex/sessions/**/rollout-*.jsonl` 里最后一条 `token_count` 事件携带的 `rate_limits`：

```json
{"primary":   {"used_percent": 91.0, "window_minutes": 300,   "resets_at": 1792521841},
 "secondary": {"used_percent": 48.0, "window_minutes": 10080, "resets_at": 1793108641}}
```

优先用 hook 输入里的 `transcript_path`；拿不到就按 `session_id` 在 sessions 目录里找，再不行取最新的几个 rollout。全程只读本地文件，不发网络请求。

## 配置

`~/.codex/budget-wrapup/config.json`，改完不用重装，下一次 hook 就生效：

| 键 | 默认 | 作用 |
|---|---|---|
| `threshold` | `85` | 触发阈值（百分比） |
| `primary_threshold` | 跟随 `threshold` | 只给 5h 窗口单独设 |
| `secondary_threshold` | 跟随 `threshold` | 只给周窗口单独设 |
| `mode` | `"enforce"` | `enforce` 强制续一轮；`warn` 只提示不拦 |
| `max_blocks` | `3` | 单会话最多强制几次 |
| `step` | `3` | 再次强制需要额度再涨几个点 |
| `lang` | `"zh"` | 总结指令语言，`en` 切英文 |
| `dir` | `<cwd>/.codex-wrapup` | 总结落盘目录 |
| `block_prompt` | `false` | 设为 `true` 时额度 ≥95% 直接拦下你的新消息（很粗暴，默认关） |
| `debug` | `false` | 写 `~/.codex/budget-wrapup/state/budget-wrapup.log` |

环境变量同名优先于配置文件（`BUDGET_WRAPUP_THRESHOLD`、`BUDGET_WRAPUP_MODE` …）。

## 局限

- **只对订阅登录有效。** API key 登录的会话没有 `rate_limits`，脚本会安静退出，不会误判。
- **CLI 的插件机制不吃插件自带的 hooks。** 在 Codex CLI 0.163 上实测：插件能装、能启用，但 `/hooks` 面板里显示 `0 installed`，插件目录里的 `hooks/hooks.json` 不会被加载。所以安装走 `install.py` 写用户级 `~/.codex/hooks.json`。仓库里保留插件清单（`plugins/budget-wrapup/`），给以后支持插件 hooks 的版本或桌面端用。
- **数据有滞后。** `rate_limits` 是上一次模型请求返回的值，快到极限时单轮可能就跨过阈值。
- **是软约束。** `Stop` hook 只能让 Codex 多跑一轮，拦不住你用别的参数硬跑下去。

## 卸载

```bash
python install.py --uninstall
```

只删自己的三条 hook 和 `~/.codex/budget-wrapup/`，`hooks.json` 里别人的配置原样留着。

## 目录结构

```
codex-budget-wrapup/
├── install.py                      # 装/卸/查
├── .agents/plugins/marketplace.json
└── plugins/budget-wrapup/
    ├── plugin.json                 # 插件清单（桌面端 / 以后版本）
    ├── hooks/
    │   ├── hooks.json              # 插件形式的配置（${PLUGIN_ROOT}）
    │   └── budget_wrapup.py        # 全部逻辑，零依赖
    └── skills/wrapup-handoff/
        └── SKILL.md                # 手动触发收尾时的五段模板
```

## 自测

```bash
python plugins/budget-wrapup/hooks/budget_wrapup.py --selftest
```

不需要 Codex 在跑，覆盖窗口解析、阈值判定、防重复逻辑和总结路径生成。
