#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""budget-wrapup — Codex 额度收尾守卫。

读到 Codex 会话里的 rate_limits，当 5 小时窗口或周窗口的用量逼近限额时，
在 Stop 事件上强制 Codex 再做一轮「收尾总结」，并把交接文档落到磁盘。

零依赖，只读本地 rollout (*.jsonl)，不发任何网络请求。

入口：
    python budget_wrapup.py            # 从 stdin 读 hook 事件 JSON（Codex 调用方式）
    python budget_wrapup.py --check    # 手动看当前额度状态
    python budget_wrapup.py --selftest # 不依赖 Codex 的自测

环境变量：
    BUDGET_WRAPUP_THRESHOLD     触发阈值（百分比），默认 85
    BUDGET_WRAPUP_PRIMARY       5h 窗口单独阈值，覆盖 THRESHOLD
    BUDGET_WRAPUP_SECONDARY     周窗口单独阈值，覆盖 THRESHOLD
    BUDGET_WRAPUP_MODE          enforce（默认，强行续一轮总结）| warn（只提示）
    BUDGET_WRAPUP_MAX_BLOCKS    单会话最多强制几次，默认 3
    BUDGET_WRAPUP_STEP          再次强制需要额度再涨多少个百分点，默认 3
    BUDGET_WRAPUP_DIR           总结落盘目录，默认 <cwd>/.codex-wrapup
    BUDGET_WRAPUP_STATE         状态目录，默认 PLUGIN_DATA 或临时目录
    BUDGET_WRAPUP_LANG          zh（默认）| en
    BUDGET_WRAPUP_DEBUG         1 = 往 STATE/budget-wrapup.log 写调试日志
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import tempfile
import time

DEFAULTS = {
    "threshold": 85.0,
    "mode": "enforce",
    "max_blocks": 3,
    "step": 3.0,
}

# ---------------------------------------------------------------- 基础工具


def _env_float(name, fallback):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def _env_int(name, fallback):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def _debug(msg):
    if os.environ.get("BUDGET_WRAPUP_DEBUG", "").strip() not in ("1", "true", "yes"):
        return
    try:
        path = os.path.join(_state_dir(), "budget-wrapup.log")
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass


def _config_path():
    return os.environ.get("BUDGET_WRAPUP_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".codex", "budget-wrapup", "config.json"
    )


_CONFIG_KEYS = {
    "threshold": "BUDGET_WRAPUP_THRESHOLD",
    "primary_threshold": "BUDGET_WRAPUP_PRIMARY",
    "secondary_threshold": "BUDGET_WRAPUP_SECONDARY",
    "mode": "BUDGET_WRAPUP_MODE",
    "max_blocks": "BUDGET_WRAPUP_MAX_BLOCKS",
    "step": "BUDGET_WRAPUP_STEP",
    "lang": "BUDGET_WRAPUP_LANG",
    "dir": "BUDGET_WRAPUP_DIR",
    "block_prompt": "BUDGET_WRAPUP_BLOCK_PROMPT",
    "debug": "BUDGET_WRAPUP_DEBUG",
}


def apply_config_file():
    """config.json 只填没人显式设过的项，环境变量始终优先。"""
    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for key, env_name in _CONFIG_KEYS.items():
        if key not in data:
            continue
        if os.environ.get(env_name):
            continue
        value = data[key]
        if isinstance(value, bool):
            value = "1" if value else "0"
        os.environ[env_name] = str(value)


def _state_dir():
    explicit = os.environ.get("BUDGET_WRAPUP_STATE")
    if explicit:
        return explicit
    plugin_data = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        return plugin_data
    home_codex = os.path.join(os.path.expanduser("~"), ".codex")
    if os.path.isdir(home_codex):
        return os.path.join(home_codex, "budget-wrapup", "state")
    return os.path.join(tempfile.gettempdir(), "codex-budget-wrapup")


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        return False
    return True


# ---------------------------------------------------------------- 额度数据


def _iter_tail_lines(path, max_bytes=6_000_000):
    """从文件尾部倒序产出非空行，避免整读几十 MB 的 rollout。"""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        blob = fh.read()
    chunks = blob.split(b"\n")
    if start > 0 and chunks:
        chunks = chunks[1:]  # 第一段可能被截断，丢掉
    for raw in reversed(chunks):
        if raw.strip():
            yield raw


def _find_key(obj, key="rate_limits", _budget=20000):
    """在任意嵌套的 JSON 里找第一个符合条件的 rate_limits 字典。"""
    stack = [obj]
    seen = 0
    while stack and seen < _budget:
        seen += 1
        cur = stack.pop()
        if isinstance(cur, dict):
            candidate = cur.get(key)
            if isinstance(candidate, dict) and (
                "primary" in candidate or "secondary" in candidate
            ):
                return candidate
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _rate_limits_from_file(path):
    try:
        for raw in _iter_tail_lines(path):
            if b"rate_limits" not in raw and b"used_percent" not in raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            found = _find_key(obj)
            if found:
                return found
    except Exception as exc:  # 文件被占用、路径不存在等
        _debug(f"read transcript failed: {exc!r}")
    return None


def _session_candidates(session_id):
    """transcript_path 为空时，按 session_id 去 ~/.codex/sessions 兜底找。"""
    if not session_id:
        return []
    root = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
    if not os.path.isdir(root):
        return []
    hits = []
    needle = session_id.replace("thr_", "")
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            if needle and needle in name:
                full = os.path.join(dirpath, name)
                hits.append((os.path.getmtime(full), full))
    hits.sort(reverse=True)
    return [p for _m, p in hits]


def load_rate_limits(event):
    """按 transcript_path → session_id 兜底 → 最新 rollout 的顺序找额度。"""
    paths = []
    tp = event.get("transcript_path")
    if isinstance(tp, str) and tp and os.path.isfile(tp):
        paths.append(tp)
    paths.extend(_session_candidates(event.get("session_id")))
    root = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
    if os.path.isdir(root):
        newest = []
        for dirpath, _d, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(".jsonl"):
                    full = os.path.join(dirpath, name)
                    newest.append((os.path.getmtime(full), full))
        newest.sort(reverse=True)
        paths.extend(p for _m, p in newest[:3])

    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        rl = _rate_limits_from_file(path)
        if rl:
            _debug(f"rate_limits <- {path}")
            return rl
    return None


def normalize_windows(rate_limits):
    """把 primary/secondary 统一成带标签的窗口列表。"""
    windows = []
    for key, fallback_label in (("primary", "5h"), ("secondary", "weekly")):
        raw = rate_limits.get(key)
        if not isinstance(raw, dict):
            continue
        used = raw.get("used_percent")
        if not isinstance(used, (int, float)):
            continue
        minutes = raw.get("window_minutes")
        if isinstance(minutes, (int, float)):
            if minutes <= 6 * 60:
                label = f"{int(minutes // 60)}h" if minutes >= 60 else f"{int(minutes)}m"
            elif minutes >= 5 * 24 * 60:
                label = "weekly"
            else:
                label = f"{int(minutes // 60)}h"
        else:
            label = fallback_label
        resets_at = raw.get("resets_at")
        windows.append(
            {
                "window": key,
                "label": label,
                "used": float(used),
                "minutes": minutes,
                "resets_at": resets_at,
            }
        )
    return windows


def _reset_hint(window):
    resets_at = window.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        return ""
    delta = int(resets_at - time.time())
    if delta <= 0:
        return "即将重置"
    if delta < 3600:
        return f"约 {max(1, delta // 60)} 分钟后重置"
    hours, minutes = divmod(delta // 60, 60)
    if hours < 48:
        return f"约 {hours} 小时 {minutes} 分后重置"
    return f"约 {hours // 24} 天后重置"


def _fmt_windows(windows, lang="zh"):
    parts = []
    for w in windows:
        hint = _reset_hint(w)
        piece = f"{w['label']} {w['used']:.0f}%"
        if hint:
            piece += f"（{hint}）"
        parts.append(piece)
    if not parts:
        return "额度未知"
    return " / ".join(parts)


# ---------------------------------------------------------------- 状态


def _state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "session"))[:120]
    return os.path.join(_state_dir(), f"state-{safe}.json")


def read_state(session_id):
    path = _state_path(session_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"blocked_count": 0, "last_percent": 0.0, "last_ts": 0.0}


def write_state(session_id, state):
    path = _state_path(session_id)
    if not _ensure_dir(os.path.dirname(path)):
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except Exception as exc:
        _debug(f"write state failed: {exc!r}")


# ---------------------------------------------------------------- 决策


def _windows_over(windows):
    """返回超过各自阈值的窗口。"""
    base = _env_float("BUDGET_WRAPUP_THRESHOLD", DEFAULTS["threshold"])
    over = []
    for w in windows:
        if w["window"] == "primary":
            limit = _env_float("BUDGET_WRAPUP_PRIMARY", base)
        elif w["window"] == "secondary":
            limit = _env_float("BUDGET_WRAPUP_SECONDARY", base)
        else:
            limit = base
        if w["used"] >= limit:
            over.append(w)
    return over


def summary_target(cwd):
    base = os.environ.get("BUDGET_WRAPUP_DIR")
    if not base:
        base = os.path.join(cwd or os.getcwd(), ".codex-wrapup")
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(base, f"wrapup-{stamp}.md")


def build_block_reason(event, over, all_windows):
    lang = (os.environ.get("BUDGET_WRAPUP_LANG") or "zh").strip().lower()
    target = summary_target(event.get("cwd"))
    target_pretty = target.replace("\\", "/")
    peak = max(w["used"] for w in over)
    window_note = _fmt_windows(all_windows, lang)

    if lang.startswith("en"):
        return (
            "[budget-wrapup] Usage is at the limit: {note}. Stop opening new work now.\n\n"
            "Required wrap-up before this turn ends:\n"
            "1. Goal — what this session was asked to do.\n"
            "2. Done — files/features actually changed, with paths.\n"
            "3. State — what runs, what was tested, what was not.\n"
            "4. Remaining — exactly what is unfinished and the first next step.\n"
            "5. Uncommitted — what `git status` still shows.\n\n"
            "Write this hand-off to `{target}` (create the directory if needed). "
            "If you cannot write files, print the full summary in your reply instead. "
            "Then stop: no new tool calls, no new scope."
        ).format(note=window_note, target=target_pretty)

    return (
        f"[budget-wrapup] 额度已接近上限：{window_note}（最高 {peak:.0f}%）。"
        "从现在起不要再开新任务、不要做大范围改动。\n\n"
        "本轮结束前必须完成收尾总结，按下面五段写：\n"
        "1. 目标 —— 这个会话原本要做什么。\n"
        "2. 已完成 —— 实际改了哪些文件/功能，带具体路径。\n"
        "3. 当前状态 —— 能跑吗，测过什么，没测什么。\n"
        "4. 未完成 —— 具体卡在哪，下一步第一件事是什么。\n"
        "5. 未提交改动 —— `git status` 里还留着什么。\n\n"
        f"把总结写进 `{target_pretty}`（目录不存在就建）。"
        "如果写不了文件，就把完整总结贴在回复里。"
        "写完就停：不要再调用工具，不要再扩范围。"
    )


def build_context_note(over, all_windows, lang="zh"):
    peak = max(w["used"] for w in over)
    note = _fmt_windows(all_windows, lang)
    if lang.startswith("en"):
        return (
            f"[budget-wrapup] Codex usage is near the cap ({note}, peak {peak:.0f}%). "
            "Prefer finishing and summarising over starting new work. If the user asks for "
            "a new task, say the quota risk first and keep the scope small."
        )
    return (
        f"[budget-wrapup] Codex 额度接近上限（{note}，最高 {peak:.0f}%）。"
        "优先收尾和总结，不要开新的工程。用户要新任务时，先说明额度风险，把范围压到最小。"
    )


def can_block_now(state, peak):
    max_blocks = _env_int("BUDGET_WRAPUP_MAX_BLOCKS", DEFAULTS["max_blocks"])
    step = _env_float("BUDGET_WRAPUP_STEP", DEFAULTS["step"])
    if state.get("blocked_count", 0) >= max_blocks:
        return False, "max_blocks reached"
    if state.get("blocked_count", 0) == 0:
        return True, "first trigger"
    if peak >= float(state.get("last_percent", 0.0)) + step:
        return True, "usage grew"
    return False, "already handled"


# ---------------------------------------------------------------- 事件分发


def handle(event):
    name = event.get("hook_event_name") or ""
    session_id = event.get("session_id") or "unknown"
    rate_limits = load_rate_limits(event)
    if not rate_limits:
        _debug(f"{name}: no rate_limits found, skipping")
        return None

    windows = normalize_windows(rate_limits)
    if not windows:
        return None
    over = _windows_over(windows)
    reached = bool(rate_limits.get("rate_limit_reached_type"))
    mode = (os.environ.get("BUDGET_WRAPUP_MODE") or DEFAULTS["mode"]).strip().lower()
    lang = (os.environ.get("BUDGET_WRAPUP_LANG") or "zh").strip().lower()

    _debug(f"{name}: {_fmt_windows(windows)} over={[w['window'] for w in over]} reached={reached}")

    if name == "Stop":
        if not over and not reached:
            return None
        if event.get("stop_hook_active"):
            return None  # 上一轮就是被我们续的，不再叠
        if mode != "enforce":
            return {
                "systemMessage": f"额度接近上限：{_fmt_windows(windows)}。记得先收尾再收工。"
            }
        peak = max(w["used"] for w in over) if over else 100.0
        state = read_state(session_id)
        ok, why = can_block_now(state, peak)
        _debug(f"Stop: can_block={ok} ({why}) peak={peak}")
        if not ok:
            return {
                "systemMessage": f"额度接近上限：{_fmt_windows(windows)}。"
            }
        state["blocked_count"] = int(state.get("blocked_count", 0)) + 1
        state["last_percent"] = peak
        state["last_ts"] = time.time()
        write_state(session_id, state)
        return {
            "decision": "block",
            "reason": build_block_reason(event, over, windows),
            "systemMessage": f"[budget-wrapup] 已触发收尾：{_fmt_windows(windows)}",
        }

    if name == "UserPromptSubmit":
        if not over and not reached:
            return None
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": build_context_note(over, windows, lang),
            },
            "systemMessage": f"Codex 额度接近上限：{_fmt_windows(windows)}",
        }
        if os.environ.get("BUDGET_WRAPUP_BLOCK_PROMPT", "").strip() in ("1", "true", "yes"):
            peak = max(w["used"] for w in over) if over else 100.0
            if peak >= 95:
                return {
                    "decision": "block",
                    "reason": (
                        f"[budget-wrapup] 额度只剩不到 5%（{_fmt_windows(windows)}）。"
                        "先收尾：写交接总结，别再开新任务。想强行继续就临时清空 BUDGET_WRAPUP_BLOCK_PROMPT。"
                    ),
                }
        return payload

    if name == "SessionStart":
        if not over and not reached:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": build_context_note(over, windows, lang),
            }
        }

    return None


def main(argv):
    apply_config_file()
    if "--check" in argv:
        event = {"hook_event_name": "Check", "cwd": os.getcwd()}
        rl = load_rate_limits(event)
        if not rl:
            print("找不到额度信息（可能是 API key 登录，或者还没有任何会话）。")
            return 0
        windows = normalize_windows(rl)
        over = _windows_over(windows)
        base = _env_float("BUDGET_WRAPUP_THRESHOLD", DEFAULTS["threshold"])
        print(f"阈值 {base:.0f}% | {_fmt_windows(windows)}")
        print("状态：" + ("已超阈值 → 收尾守卫会介入" if over else "正常"))
        for w in windows:
            print(
                f"  {w['window']:<9} {w['label']:<7} {w['used']:5.1f}%  "
                f"窗口 {w.get('minutes')} 分钟  {_reset_hint(w)}"
            )
        return 0

    if "--selftest" in argv:
        return selftest()

    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        _debug(f"bad stdin: {exc!r}")
        return 0
    out = handle(event)
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- 自测


def selftest():
    sample = {
        "primary": {"used_percent": 91.0, "window_minutes": 300, "resets_at": time.time() + 1200},
        "secondary": {"used_percent": 48.0, "window_minutes": 10080, "resets_at": time.time() + 86400},
        "rate_limit_reached_type": None,
    }
    windows = normalize_windows(sample)
    assert len(windows) == 2, windows
    assert windows[0]["label"] == "5h", windows
    assert windows[1]["label"] == "weekly", windows
    assert [w["window"] for w in _windows_over(windows)] == ["primary"], windows

    os.environ["BUDGET_WRAPUP_THRESHOLD"] = "90"
    assert len(_windows_over(windows)) == 1
    os.environ["BUDGET_WRAPUP_THRESHOLD"] = "95"
    assert _windows_over(windows) == []
    os.environ.pop("BUDGET_WRAPUP_THRESHOLD")

    nested = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": sample}}
    assert _find_key(nested) is sample

    state = {"blocked_count": 0}
    assert can_block_now(state, 91.0)[0] is True
    state = {"blocked_count": 1, "last_percent": 91.0}
    assert can_block_now(state, 92.0)[0] is False
    assert can_block_now(state, 95.0)[0] is True
    state = {"blocked_count": 3, "last_percent": 80.0}
    assert can_block_now(state, 99.0)[0] is False

    reason = build_block_reason({"cwd": "/tmp/demo"}, windows[:1], windows)
    assert ".codex-wrapup" in reason
    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
