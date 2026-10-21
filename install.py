#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 budget-wrapup 装到本机 Codex 的 hooks.json 里。

Codex CLI 0.163 只加载 config / 用户级 / 项目级 hooks，插件自带的 hooks/hooks.json
不会自动生效，所以这里直接把配置写进 ~/.codex/hooks.json，并把脚本复制到
~/.codex/budget-wrapup/，以后源目录挪走也不影响。

用法：
    python install.py             # 安装 / 更新
    python install.py --status    # 看装没装
    python install.py --uninstall # 卸载
    python install.py --yes       # 不问，直接做
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins", "budget-wrapup")
SCRIPT_SRC = os.path.join(PLUGIN_DIR, "hooks", "budget_wrapup.py")
CODEX_HOME = os.path.join(os.path.expanduser("~"), ".codex")
HOOKS_JSON = os.path.join(CODEX_HOME, "hooks.json")
TARGET_DIR = os.path.join(CODEX_HOME, "budget-wrapup")
TARGET_SCRIPT = os.path.join(TARGET_DIR, "budget_wrapup.py")
MARK = "budget_wrapup.py"

EVENTS = [
    ("SessionStart", None, 15),
    ("UserPromptSubmit", None, 15),
    ("Stop", None, 20),
]

DEFAULT_CONFIG = {
    "threshold": 85,
    "mode": "enforce",
    "max_blocks": 3,
    "step": 3,
    "lang": "zh",
}


def find_python():
    """挑一个能在 hook 里跑起来的解释器命令行。"""
    candidates = ["py -3", "python", "python3"]
    import shutil as _sh

    if os.name == "nt":
        if _sh.which("py"):
            return "py -3"
        if _sh.which("python"):
            return "python"
        return "python3"
    for name in ("python3", "python"):
        if _sh.which(name):
            return name
    return "python3"


def build_group(python_cmd, matcher, timeout):
    quoted = '"%s"' % TARGET_SCRIPT.replace('"', "")
    handler = {
        "type": "command",
        "command": f"{python_cmd} {quoted}",
        "timeout": timeout,
        "statusMessage": "检查 Codex 额度",
    }
    if os.name == "nt":
        handler["commandWindows"] = f"{python_cmd} {quoted}"
    group = {}
    if matcher:
        group["matcher"] = matcher
    group["hooks"] = [handler]
    return group


def is_ours(group):
    for handler in group.get("hooks", []) or []:
        blob = str(handler.get("command", "")) + str(handler.get("commandWindows", ""))
        if MARK in blob:
            return True
    return False


def load_existing():
    if not os.path.isfile(HOOKS_JSON):
        return {"hooks": {}}
    with open(HOOKS_JSON, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return {"hooks": {}}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit("~/.codex/hooks.json 不是对象，先手动检查一下。")
    data.setdefault("hooks", {})
    return data


def backup():
    if not os.path.isfile(HOOKS_JSON):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = f"{HOOKS_JSON}.bak.{stamp}"
    shutil.copy2(HOOKS_JSON, dst)
    return dst


def strip_ours(data):
    hooks = data.setdefault("hooks", {})
    removed = 0
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        keep = []
        for group in groups:
            if isinstance(group, dict) and is_ours(group):
                removed += 1
            else:
                keep.append(group)
        if keep:
            hooks[event] = keep
        else:
            hooks.pop(event, None)
    return removed


def cmd_install(assume_yes):
    if not os.path.isfile(SCRIPT_SRC):
        raise SystemExit(f"找不到 {SCRIPT_SRC}，请在项目根目录运行。")
    os.makedirs(TARGET_DIR, exist_ok=True)
    shutil.copy2(SCRIPT_SRC, TARGET_SCRIPT)

    python_cmd = find_python()
    data = load_existing()
    bak = backup()
    strip_ours(data)
    hooks = data.setdefault("hooks", {})
    for event, matcher, timeout in EVENTS:
        hooks.setdefault(event, []).append(build_group(python_cmd, matcher, timeout))
    if "description" not in data:
        data["description"] = "budget-wrapup: 额度快满时强制收尾"

    with open(HOOKS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    config_path = os.path.join(TARGET_DIR, "config.json")
    if not os.path.isfile(config_path):
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    print(f"脚本  → {TARGET_SCRIPT}")
    print(f"配置  → {HOOKS_JSON}  (解释器: {python_cmd})")
    print(f"参数  → {config_path}")
    if bak:
        print(f"备份  → {bak}")
    print()
    print("还差一步人工操作（Codex 的 hook 必须本人信任一次）：")
    print("  1. 打开 codex")
    print("  2. 输入 /hooks")
    print("  3. 对 budget-wrapup 的三条 hook 按信任")
    print()
    print("信任前 Codex 会跳过它们，但不会报错。")
    return 0


def cmd_uninstall():
    if os.path.isfile(HOOKS_JSON):
        data = load_existing()
        removed = strip_ours(data)
        backup()
        if data.get("hooks"):
            with open(HOOKS_JSON, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        else:
            os.remove(HOOKS_JSON)
        print(f"已从 hooks.json 移除 {removed} 条 budget-wrapup hook。")
    else:
        print("没有 ~/.codex/hooks.json，跳过。")
    if os.path.isdir(TARGET_DIR):
        shutil.rmtree(TARGET_DIR, ignore_errors=True)
        print(f"已删除 {TARGET_DIR}")
    return 0


def cmd_status():
    installed = False
    if os.path.isfile(HOOKS_JSON):
        try:
            data = load_existing()
            for event, groups in (data.get("hooks") or {}).items():
                for group in groups or []:
                    if isinstance(group, dict) and is_ours(group):
                        installed = True
                        print(f"  {event:<16} 已配置")
        except Exception as exc:
            print(f"hooks.json 解析失败：{exc}")
    if not installed:
        print("未安装。跑 python install.py。")
        return 1
    print(f"脚本：{TARGET_SCRIPT} {'✓' if os.path.isfile(TARGET_SCRIPT) else '✗ 缺失'}")
    print("记得在 codex 里 /hooks 里确认它们是 trusted 状态。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="安装 budget-wrapup 到 Codex hooks.json")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args()
    if args.status:
        return cmd_status()
    if args.uninstall:
        return cmd_uninstall()
    return cmd_install(args.yes or True)


if __name__ == "__main__":
    sys.exit(main())
