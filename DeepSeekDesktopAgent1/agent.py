# -*- coding: utf-8 -*-
"""DeepSeek Desktop Agent —— 在 PowerShell 里用自然语言控制 Windows 桌面

用法（PowerShell 7）：
    ai "打开记事本并输入 Hello"          # 一次性任务（见 ps\\DeskAgent.ps1）
    python agent.py                       # 交互模式
    python agent.py --dry-run "打开计算器"  # 干跑：只看模型会怎么做，不动桌面
    python agent.py --doctor              # 环境体检
    python agent.py --verify-audit        # 校验审计日志哈希链
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # PowerShell 中文不乱码
    except Exception:  # noqa: BLE001
        pass

from da_core import (LOG_DIR, ROOT, STOP_FILE, AuditLog, Context, Desktop, disposition_of, digest,
                     find_agent_desktop, verify_audit)
from da_policy import SIDE_EFFECT_TOOLS, authorize
from da_tools import TOOLS, run_tool

API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
MODEL_FALLBACKS = ["deepseek-v4-flash"]  # 不同时期官方文档的模型名不同；主名被拒时自动回退
DEFAULT_MAX_STEPS = int(os.environ.get("DESKAGENT_MAX_STEPS", "40"))

SYSTEM_PROMPT = """你是 Windows 桌面自动化 Agent，通过受控工具操作电脑。不得要求执行任意 shell、PowerShell、Python 命令。
工作流程：先观察，再规划；每次只执行必要动作；界面变化后重新观察；每个副作用动作之后核对结果。
不得擅自发送消息、删除文件、付款、提交订单、发布内容或执行其他不可逆操作；遇到这些必须先向用户说明并请求明确确认。
回答使用简体中文，简洁说明正在执行的步骤和结果。

操作状态与完成判定规则：
1. 工具返回 ok=true 只表示执行器报告动作已执行，不代表用户目标已达成。
2. observation_ok=true 只表示动作后成功取得新观察，不代表预期效果已发生。
3. effect_verified=true 仅当新读取中找到与本次操作目标相符的证据（如 verification_scope 字段所示）。
4. effect_verified=false 仅当有明确证据证明效果未发生或相反。
5. 缺少证据、无法判断或尚未检查时 effect_verified 为 null；此时必须如实告诉用户“动作已执行，但效果尚未验证”，并继续做安全的只读检查。
6. 不得因为工具调用成功或界面仍可见就推断任务完成。delivered_unverified 绝不等于成功。

工具选择与证据规则：
1. 列出/枚举/统计窗口必须调用 desktop_windows；不得用快照或截图代替。
2. 读取某窗口控件/内容用 desktop_snapshot；需要视觉信息且用户明确要求时才截图。
3. 应用未运行时用 desktop_launch 启动（仅白名单应用）；启动结果以窗口枚举验证。
4. 写文本前先 snapshot 找到唯一编辑器，再调用 desktop_set_value；写入后以读回结果为准。
5. 点击时尽量提供 expect_text（点击后应出现的文字）以便验证。
6. 只有工具实际返回的数据才能作为事实依据；没有返回的信息明确说明“未取得”，不得编造。工具失败时报告真实错误。
7. 被策略阻止(blocked=true)的动作不要换种方式绕过，向用户说明原因即可。"""


# --------------------------------------------------------------------------
# 模型调用
# --------------------------------------------------------------------------
_active_model = {"name": MODEL}


def call_model(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    import requests  # 延迟导入，便于离线单元测试

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY（先运行 ai-key 保存密钥）")
    candidates = [_active_model["name"]] + [m for m in MODEL_FALLBACKS if m != _active_model["name"]]
    last_err: Optional[Exception] = None
    for model in candidates:
        for attempt in range(3):
            try:
                resp = requests.post(
                    API_URL, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                          "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": 2048},
                    timeout=90)
            except requests.RequestException as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = requests.HTTPError(f"{resp.status_code} {resp.text[:200]}", response=resp)
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code in (400, 404) and "model" in resp.text.lower() and model != candidates[-1]:
                last_err = requests.HTTPError(f"{resp.status_code} {resp.text[:200]}", response=resp)
                break  # 换下一个模型名
            resp.raise_for_status()
            _active_model["name"] = model
            return resp.json()["choices"][0]["message"]
    raise last_err or RuntimeError("模型调用失败")


# --------------------------------------------------------------------------
# 历史裁剪（保持 tool_call 与 tool 消息成对）
# --------------------------------------------------------------------------
def trim_history(history: List[Dict[str, Any]], keep: int = 60) -> List[Dict[str, Any]]:
    if len(history) <= keep + 1:
        return history
    system, rest = history[0], history[1:]
    rest = rest[-keep:]
    while rest and rest[0].get("role") != "user":
        rest = rest[1:]
    for m in rest[:-12]:
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 1500:
            m["content"] = m["content"][:1200] + "…（旧工具结果已压缩）"
    return [system] + rest


def _pack(result: Any, limit: int = 14000) -> str:
    s = json.dumps(result, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return s
    return json.dumps({"ok": result.get("ok") if isinstance(result, dict) else None, "truncated": True,
                       "preview": s[: limit - 400]}, ensure_ascii=False)


# --------------------------------------------------------------------------
# 任务循环
# --------------------------------------------------------------------------
def run_task(ctx: Context, history: List[Dict[str, Any]], user_text: str,
             model_fn: Callable[[List[Dict[str, Any]]], Dict[str, Any]] = call_model,
             max_steps: int = DEFAULT_MAX_STEPS) -> str:
    ctx.begin(user_text)
    ctx.audit.write("task.begin", {"request": digest(user_text), "dry_run": ctx.dry_run})
    history.append({"role": "user", "content": user_text})
    failures: Dict[str, int] = {}
    try:
        for step in range(max_steps):
            if STOP_FILE.exists():
                ctx.audit.write("task.stop", {"reason": "STOP file"})
                return "已检测到急停文件 STOP，任务已中止。（ai-resume 可解除）"
            history[:] = trim_history(history)
            message = model_fn(history)
            history.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                return _finalize(ctx, message.get("content") or "任务结束。")
            tripped = False
            for call in calls:
                name = call["function"]["name"]
                args: Dict[str, Any] = {}
                if tripped:
                    result = {"ok": False, "blocked": True,
                              "error": {"code": "CIRCUIT_OPEN", "message": "相同工具调用连续失败达到 3 次，已停止本批次后续调用。"}}
                elif STOP_FILE.exists():
                    result = {"ok": False, "blocked": True,
                              "error": {"code": "STOPPED", "message": "急停文件 STOP 存在，已停止所有动作。"}}
                else:
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("参数必须是 JSON 对象")
                        allowed, reason = authorize(name, args, ctx.request, ctx.snapshot_index)
                        if not allowed:
                            result = {"ok": False, "blocked": True,
                                      "error": {"code": "BLOCKED_BY_POLICY", "message": reason,
                                                "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
                            ctx.receipts.append({"tool": name, "ok": False, "delivery": "not_delivered",
                                                 "effect_verified": None, "error": "策略阻止：" + reason, "scope": None}
                                                ) if name in SIDE_EFFECT_TOOLS else None
                        else:
                            result = run_tool(ctx, name, args)
                    except Exception as e:  # noqa: BLE001
                        result = {"ok": False, "error": {"code": "EXCEPTION", "message": str(e)}}
                    sig = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
                    if isinstance(result, dict) and result.get("ok") is True:
                        failures.pop(sig, None)
                    else:
                        failures[sig] = failures.get(sig, 0) + 1
                        tripped = failures[sig] >= 3
                ctx.audit.write("tool.result", {
                    "tool": name, "args": digest(args), "ok": result.get("ok"),
                    "blocked": bool(result.get("blocked")), "delivery": disposition_of(result)[0],
                    "effect_verified": result.get("effect_verified"),
                    "verification_scope": result.get("verification_scope"),
                    "observation_ok": result.get("observation_ok"),
                    "verification_error": result.get("verification_error")})
                history.append({"role": "tool", "tool_call_id": call["id"], "content": _pack(result)})
            if tripped:
                return _finalize(ctx, "检测到相同工具调用连续失败 3 次，已触发熔断。请检查工具错误后重试。")
        return _finalize(ctx, f"已达到最大步数 {max_steps}，任务中止（可用 --max-steps 调大）。")
    finally:
        ctx.audit.write("task.end", {"receipts": len(ctx.receipts)})
        ctx.end()


def _finalize(ctx: Context, text: str) -> str:
    """附上"执行回执"，并在有未验证动作时自动加警告——不依赖模型是否诚实。"""
    done = [r for r in ctx.receipts if r["tool"] in SIDE_EFFECT_TOOLS]
    if not done:
        return text
    lines = ["", "── 执行回执（由程序生成，非模型自述）──"]
    unverified = 0
    for r in done:
        ev = r["effect_verified"]
        mark = {True: "✔ 已验证", False: "✘ 验证失败"}.get(ev, "？ 未验证")
        if ev is not True:
            unverified += 1
        extra = f" [{r['scope']}]" if r.get("scope") else ""
        err = f" — {r['error']}" if r.get("error") else ""
        lines.append(f"  {r['tool']:<20} ok={r['ok']} delivery={r['delivery'] or '-'} {mark}{extra}{err}")
    if unverified:
        lines.append(f"⚠ 有 {unverified} 个动作没有获得验证证据：动作可能已执行，但效果尚未确认，请人工核对。")
    return text + "\n" + "\n".join(lines)


# --------------------------------------------------------------------------
# 环境体检
# --------------------------------------------------------------------------
def doctor() -> int:
    ok_all = True

    def line(ok: bool, name: str, detail: str = "") -> None:
        nonlocal ok_all
        ok_all &= ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    print(f"Python {sys.version.split()[0]}  ROOT={ROOT}")
    line(sys.version_info >= (3, 10), "Python >= 3.10")
    line(bool(os.environ.get("DEEPSEEK_API_KEY")), "DEEPSEEK_API_KEY 已设置", "(运行 ai-key 保存)")
    exe = find_agent_desktop()
    line(exe is not None, "找到 agent-desktop", str(exe or "→ npm install -g agent-desktop"))
    if exe is None:
        return 1
    d = Desktop([str(exe)], session=os.environ.get("AGENT_DESKTOP_SESSION", ""))
    v = d.run("version", timeout=20)
    line(bool(v.get("ok")), "agent-desktop version", json.dumps(v.get("data"), ensure_ascii=False)[:120])
    line(str(v.get("version", "")).startswith("2."), "JSON 契约版本 2.x", str(v.get("version")))
    s = d.run("status", timeout=20)
    line(bool(s.get("ok")), "agent-desktop status", json.dumps(s.get("data"), ensure_ascii=False)[:160])
    p = d.run("permissions", timeout=20)
    line(bool(p.get("ok")), "UIA 权限", json.dumps(p.get("data"), ensure_ascii=False)[:160])
    w = d.run("list-windows", timeout=20)
    n = len(w["data"]) if w.get("ok") and isinstance(w.get("data"), list) else -1
    line(n > 0, "枚举窗口", f"共 {n} 个")
    try:
        import requests  # noqa: F401
        line(True, "requests 已安装")
    except ImportError:
        line(False, "requests 已安装", "→ pip install requests")
    print("\n结论：" + ("环境正常。" if ok_all else "存在未通过项，请按提示修复。"))
    return 0 if ok_all else 1


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build_context(dry_run: bool) -> Optional[Context]:
    exe = find_agent_desktop()
    if exe is None:
        print("错误：找不到 agent-desktop。请运行：npm install -g agent-desktop，或设置 AGENT_DESKTOP_EXE。")
        return None
    audit = AuditLog(LOG_DIR / "audit.jsonl")
    desktop = Desktop([str(exe)], session=os.environ.get("AGENT_DESKTOP_SESSION", ""), audit=audit)
    return Context(desktop, audit, ROOT, dry_run=dry_run)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="DeepSeek Desktop Agent")
    ap.add_argument("task", nargs="*", help="一次性任务；省略则进入交互模式")
    ap.add_argument("--dry-run", action="store_true", help="干跑：副作用工具不真正执行")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--verify-audit", action="store_true")
    ns = ap.parse_args(argv)

    if ns.doctor:
        return doctor()
    if ns.verify_audit:
        ok, n, msg = verify_audit(LOG_DIR / "audit.jsonl")
        print(("PASS " if ok else "FAIL ") + f"审计日志 {n} 条：{msg}")
        return 0 if ok else 1
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误：当前会话没有 DEEPSEEK_API_KEY。请先运行：ai-key")
        return 2
    ctx = build_context(ns.dry_run)
    if ctx is None:
        return 2
    history: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def one(text: str) -> None:
        import requests
        try:
            print("\nAgent >", run_task(ctx, history, text, max_steps=ns.max_steps))
        except requests.HTTPError as e:
            print("DeepSeek API 错误：", e, (e.response.text[:500] if e.response is not None else ""))
        except Exception as e:  # noqa: BLE001
            print("执行错误：", repr(e))

    if ns.task:
        one(" ".join(ns.task))
        return 0

    print("DeepSeek Desktop Agent  |  /exit 退出  /reset 清空上下文  /dry 切换干跑  急停：另开窗口运行 ai-stop")
    print("桌面执行器：", ctx.desktop.prefix[0], "| 干跑" if ctx.dry_run else "")
    while True:
        try:
            text = input("\n你 > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            return 0
        if not text:
            continue
        if text.lower() in ("/exit", "exit", "quit"):
            return 0
        if text.lower() == "/reset":
            history[:] = [{"role": "system", "content": SYSTEM_PROMPT}]
            print("已清空对话上下文（不会撤销已发送给系统的动作）。")
            continue
        if text.lower() == "/dry":
            ctx.dry_run = not ctx.dry_run
            print("干跑模式：", "开" if ctx.dry_run else "关")
            continue
        one(text)


if __name__ == "__main__":
    sys.exit(main())
