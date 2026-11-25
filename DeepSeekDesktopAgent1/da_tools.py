# -*- coding: utf-8 -*-
"""da_tools.py — 工具 schema 与实现（每个有副作用的动作都带"动作后独立读回"）

验证语义（沿用并强化原版规则）：
  ok=true                执行器报告已投递，不等于目标达成
  observation_ok=true    动作后成功取得新观察，不等于效果发生
  effect_verified=true   仅当在新读取中找到与目标相符的证据
  effect_verified=false  仅当有明确相反证据
  effect_verified=null   缺少证据/无法判断（默认）
"""
from __future__ import annotations

import os
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from da_core import (Context, blocked, disposition_of, fail, identity, native_id_value, node_states,
                     same_identity, valid_ref, walk_nodes)
from da_policy import (ALLOWED_KEYS, APP_CATALOG, SIDE_EFFECT_TOOLS, risk_term, resolve_app_key,
                       sensitive_field)

MAX_SET_VALUE_CHARS = 10000


# --------------------------------------------------------------------------
# 工具 schema（发送给模型）
# --------------------------------------------------------------------------
def _fn(name: str, desc: str, props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required or []}}}


TOOLS = [
    _fn("desktop_snapshot", "读取桌面应用的无障碍控件树（返回扁平节点列表，含 ref/role/name/value/states/actions）。界面变化后必须重新快照。",
        {"app": {"type": "string", "description": "应用名称，如 notepad.exe，可留空=当前焦点窗口"},
         "window_id": {"type": "string"}, "root": {"type": "string", "description": "限定子树根控件引用"},
         "timeout_ms": {"type": "integer", "description": "默认10000；Chromium/Electron 冷启动可设 30000"}}),
    _fn("desktop_get", "只读获取最新成功快照中指定控件的属性。",
        {"ref": {"type": "string"}, "property": {"type": "string", "enum": ["value", "text", "title", "bounds", "role", "states"]}},
        ["ref"]),
    _fn("desktop_windows", "列出当前桌面窗口（JSON 数组，含 title/app_name/is_focused/pid）。用户要求枚举窗口时必须调用。", {}),
    _fn("desktop_apps", "列出正在运行的 GUI 应用。", {}),
    _fn("desktop_status", "读取 agent-desktop 状态、平台与权限。", {}),
    _fn("desktop_find", "在最新快照中搜索控件（名称/文本/value/title/description/角色/AutomationId）。只读。",
        {"query": {"type": "string"},
         "field": {"type": "string", "enum": ["all", "name", "role", "automation_id", "text", "value", "title", "description"]},
         "exact": {"type": "boolean"}, "limit": {"type": "integer"}, "app": {"type": "string"},
         "window_id": {"type": "string"}, "root": {"type": "string"}}, ["query"]),
    _fn("desktop_screenshot", "截取屏幕保存为 PNG（仅在用户明确要求截图时）。返回路径并校验文件。",
        {"screen": {"type": "integer", "description": "屏幕编号，默认0"}}),
    _fn("desktop_launch", "启动受控白名单内的应用（notepad/calculator/paint/explorer/edge/chrome/vscode）。启动后会用窗口枚举独立验证。",
        {"app": {"type": "string", "description": "白名单键名，如 notepad"}}, ["app"]),
    _fn("desktop_click", "点击最新快照中观察到的唯一控件引用。可提供 expect_text：点击后界面中应出现的文字，用于验证效果。",
        {"ref": {"type": "string"}, "expect_text": {"type": "string", "description": "点击后预期出现的文字，可留空"}}, ["ref"]),
    _fn("desktop_set_value", "安全设置指定应用中唯一文本编辑器的内容。会重新快照、核对标签与原值，写入后读回验证。",
        {"ref": {"type": "string"}, "value": {"type": "string"}, "app": {"type": "string"},
         "expected_selected_tab": {"type": "string", "description": "预期唯一选中标签的完整名称；应用无标签页时填空字符串"},
         "expected_current_value": {"type": "string", "description": "预期编辑器当前原值；空白填空字符串"}},
        ["ref", "value", "app", "expected_selected_tab", "expected_current_value"]),
    _fn("desktop_press", "发送受限键盘快捷键，如 ctrl+t、return、escape、tab、f5。",
        {"combo": {"type": "string"}}, ["combo"]),
]


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------
def _clamp_timeout(v: Any, default: int = 10000) -> int:
    try:
        return max(3000, min(int(v), 30000))
    except (TypeError, ValueError):
        return default


def take_snapshot(ctx: Context, app: str = "", window_id: str = "", root: str = "",
                  timeout_ms: Any = 10000) -> Dict[str, Any]:
    """返回 agent-desktop 原始信封，并刷新 ctx.snapshot_index（含祖先链，用于对话框风险判定）。"""
    for v in (app, window_id, root):
        if not isinstance(v, str):
            return fail("INVALID_ARGS", "app、window_id和root必须是字符串")
    t = _clamp_timeout(timeout_ms)
    args = ["--timeout-ms", str(t)]
    if app.strip():
        args += ["--app", app.strip()]
    if window_id.strip():
        args += ["--window-id", window_id.strip()]
    if root.strip():
        if not valid_ref(root.strip()):
            return fail("INVALID_ARGS", "root控件引用格式无效")
        args += ["--root", root.strip()]
    args.append("--compact")
    result = ctx.desktop.run("snapshot", *args, timeout=t / 1000 + 10)
    if not result.get("ok"):
        return result
    data = result.get("data") or {}
    tree = data.get("tree")
    if data.get("complete") is False:
        ctx.snapshot_index.clear()
        return fail("SNAPSHOT_INCOMPLETE", "快照不完整，已拒绝建立可操作控件索引；请增大 timeout_ms 或用 root 缩小范围",
                    snapshot_id=data.get("snapshot_id"), nodes_observed=data.get("nodes_observed"))
    if data.get("snapshot_id") and isinstance(tree, (dict, list)):
        index: Dict[str, Dict[str, Any]] = {}
        for node, anc in walk_nodes(tree):
            ref = node.get("ref_id")
            if isinstance(ref, str):
                index[ref] = {
                    "name": node.get("name", ""), "role": node.get("role", ""),
                    "native_id": node.get("native_id"), "role_description": node.get("role_description"),
                    "description": node.get("description"), "title": node.get("title"),
                    "states": node.get("states", []), "value": node.get("value"),
                    "available_actions": node.get("available_actions", []),
                    "parent_path": [identity(a) for a in anc],
                    "ancestors": [{"role": a.get("role"), "name": a.get("name")} for a in anc],
                    "snapshot_id": data.get("snapshot_id"),
                }
        if not root.strip():
            ctx.snapshot_index.clear()
        ctx.snapshot_index.update(index)
    return result


def _trunc(s: Any, n: int = 240) -> Any:
    if isinstance(s, str) and len(s) > n:
        return s[:n] + f"…(+{len(s) - n}字)"
    return s


def model_view(result: Dict[str, Any], max_nodes: int = 400) -> Dict[str, Any]:
    """把大树压成扁平列表给模型（省 token、不会被截成非法 JSON）。密码框的值不外泄。"""
    if not result.get("ok"):
        return result
    data = result.get("data") or {}
    nodes: List[Dict[str, Any]] = []
    for node, anc in walk_nodes(data.get("tree")):
        if node.get("ref_id") or node.get("name") or node.get("value"):
            st = node_states(node)
            item = {"ref": node.get("ref_id"), "role": node.get("role"), "name": _trunc(node.get("name")),
                    "value": None if "secure" in st else _trunc(node.get("value")),
                    "id": native_id_value(node) or None, "states": st,
                    "actions": node.get("available_actions") or [], "depth": len(anc)}
            nodes.append({k: v for k, v in item.items() if v not in (None, "", [])})
    out = {"ok": True, "snapshot_id": data.get("snapshot_id"), "app": data.get("app"), "window": data.get("window"),
           "complete": data.get("complete"), "ref_count": data.get("ref_count"), "node_count": len(nodes),
           "truncated": len(nodes) > max_nodes, "nodes": nodes[:max_nodes]}
    return {k: v for k, v in out.items() if v is not None}


def _tool_snapshot(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return model_view(take_snapshot(ctx, a.get("app", "") or "", a.get("window_id", "") or "",
                                    a.get("root", "") or "", a.get("timeout_ms", 10000)))


def _tool_get(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    ref = a.get("ref")
    if not valid_ref(ref):
        return fail("INVALID_ARGS", "拒绝读取：控件引用格式无效")
    if ref not in ctx.snapshot_index:
        return fail("REF_NOT_IN_LATEST_SNAPSHOT", "拒绝读取：引用不属于最近一次成功快照，请重新观察界面")
    prop = a.get("property", "value")
    if prop not in {"value", "text", "title", "bounds", "role", "states"}:
        return fail("INVALID_ARGS", "不支持读取该控件属性")
    r = ctx.desktop.run("get", ref, "--property", prop, timeout=15)
    if "secure" in node_states(ctx.snapshot_index[ref]) and r.get("ok") and isinstance(r.get("data"), dict):
        r["data"] = {k: ("<secure:hidden>" if k in ("value", "text") else v) for k, v in r["data"].items()}
    return r


def _tool_windows(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    r = ctx.desktop.run("list-windows")
    if r.get("ok") and isinstance(r.get("data"), list):
        r["count"] = len(r["data"])
    return r


def _tool_apps(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.desktop.run("list-apps")


def _tool_status(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.desktop.run("status")


def _tool_find(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    query = a.get("query", "")
    if not isinstance(query, str) or not query.strip():
        return fail("INVALID_ARGS", "搜索关键词不能为空")
    field = a.get("field", "all")
    fields = {"all", "name", "role", "automation_id", "text", "value", "title", "description"}
    if field not in fields:
        return fail("INVALID_ARGS", "不支持的搜索字段")
    exact = bool(a.get("exact", False))
    try:
        limit = max(1, min(int(a.get("limit", 20)), 50))
    except (TypeError, ValueError):
        return fail("INVALID_ARGS", "limit必须是有效整数")
    snap = take_snapshot(ctx, a.get("app", "") or "", a.get("window_id", "") or "", a.get("root", "") or "", 20000)
    if not snap.get("ok"):
        return fail("SNAPSHOT_FAILED", "获取最新控件快照失败", snapshot=snap)
    data = snap.get("data") or {}
    needle = query.strip().casefold()
    getters = {
        "name": lambda n: str(n.get("name") or ""), "role": lambda n: str(n.get("role") or ""),
        "automation_id": lambda n: native_id_value(n),
        "text": lambda n: " ".join(x for x in (str(n.get("name") or ""), str(n.get("value") or "")) if x),
        "value": lambda n: str(n.get("value") or ""), "title": lambda n: str(n.get("title") or ""),
        "description": lambda n: " ".join(x for x in (str(n.get("description") or ""),
                                                      str(n.get("role_description") or "")) if x),
    }
    use = list(getters) if field == "all" else [field]
    matches = []
    for node, _ in walk_nodes(data.get("tree")):
        hit = [k for k in use if ((getters[k](node).casefold() == needle) if exact else (needle in getters[k](node).casefold()))]
        if hit:
            st = node_states(node)
            matches.append({"ref_id": node.get("ref_id"), "name": node.get("name"),
                            "value": None if "secure" in st else _trunc(node.get("value")),
                            "role": node.get("role"), "native_id": node.get("native_id"),
                            "states": st, "available_actions": node.get("available_actions", []),
                            "matched_fields": hit})
            if len(matches) >= limit:
                break
    return {"ok": True, "query": query, "field": field, "exact": exact, "count": len(matches), "limit": limit,
            "snapshot_id": data.get("snapshot_id"), "complete": data.get("complete"), "matches": matches}


# --------------------------------------------------------------------------
# 观察辅助
# --------------------------------------------------------------------------
def _observe(ctx: Context, app: str = "") -> Dict[str, Any]:
    return take_snapshot(ctx, app=app, timeout_ms=10000)


def _summarize(tree: Any) -> List[Dict[str, Any]]:
    out = []
    for node, anc in walk_nodes(tree):
        out.append({"path": [identity(x) for x in anc] + [identity(node)], "states": node_states(node),
                    "value": node.get("value"), "available_actions": node.get("available_actions", [])})
    return out


def _find_text(tree: Any, text: str) -> Optional[Dict[str, Any]]:
    needle = text.casefold()
    for node, _ in walk_nodes(tree):
        if "secure" in node_states(node):
            continue
        for k in ("name", "value", "title"):
            v = node.get(k)
            if isinstance(v, str) and needle in v.casefold():
                return {"field": k, "role": node.get("role"), "name": _trunc(node.get("name"), 80)}
    return None


def _windows_list(ctx: Context) -> Optional[List[Dict[str, Any]]]:
    r = ctx.desktop.run("list-windows", timeout=15)
    d = r.get("data") if r.get("ok") else None
    return [w for w in d if isinstance(w, dict)] if isinstance(d, list) else None


def _receipt(ctx: Context, tool: str, result: Dict[str, Any]) -> None:
    delivery, _ = disposition_of(result)
    err = result.get("error")
    ctx.receipts.append({"tool": tool, "ok": result.get("ok"), "delivery": delivery,
                         "effect_verified": result.get("effect_verified"),
                         "error": err.get("message") if isinstance(err, dict) else None,
                         "scope": result.get("verification_scope")})


# --------------------------------------------------------------------------
# desktop_launch —— 用窗口枚举独立验证
# --------------------------------------------------------------------------
def _resolve_launch(ctx: Context, key: str) -> Optional[str]:
    if ctx.launch_resolver:
        return ctx.launch_resolver(key)
    for cand in APP_CATALOG[key]["launch"]:
        if "%" in cand:
            p = os.path.expandvars(cand)
            if "%" not in p and Path(p).is_file():
                return p
        else:
            return cand
    return None


def _window_matches(key: str, w: Dict[str, Any]) -> bool:
    e = APP_CATALOG[key]
    app_name = str(w.get("app_name") or "").casefold()
    title = str(w.get("title") or "").casefold()
    if app_name in e["process"]:
        if app_name == "applicationframehost.exe":
            return any(t in title for t in e["title"])
        return True
    return any(t in title for t in e["title"]) and app_name.endswith(".exe") and key == "calculator"


def _tool_launch(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    key = resolve_app_key(a.get("app"))
    if key is None:
        return blocked("应用不在受控白名单内")
    target = _resolve_launch(ctx, key)
    if not target:
        return fail("APP_NOT_INSTALLED", f"在本机未找到 {key} 的可执行文件")
    before = _windows_list(ctx)
    before_ids = {w.get("id") for w in before} if before is not None else None
    result = ctx.desktop.run("launch", target, "--timeout", "30000", timeout=45)
    result["effect_verified"] = None
    result["launch_target"] = target
    if not result.get("ok"):
        result["observation_ok"] = False
        return result
    found = None
    is_new = False
    for attempt in range(12):
        wins = _windows_list(ctx)
        if wins:
            matching = [w for w in wins if _window_matches(key, w)]
            new = [w for w in matching if before_ids is None or w.get("id") not in before_ids]
            if new:
                found, is_new = new[0], True
                break
            if matching and found is None:
                found = matching[0]  # 附着到已运行实例：不会出现新窗口
            if found is not None and attempt >= 2:
                break
        ctx.sleep(0.8)
    result["observation_ok"] = found is not None
    if found is None:
        result["verification_error"] = "启动后 10 秒内未在窗口枚举中看到该应用的窗口（可能仍在启动，请稍后用 desktop_windows 再查）"
        return result
    result["effect_verified"] = True
    result["verification_scope"] = "independent_window_enumeration"
    result["attached_existing"] = not is_new
    result["window"] = {k: found.get(k) for k in ("id", "title", "app_name", "pid", "is_focused")}
    return result


# --------------------------------------------------------------------------
# desktop_screenshot —— 校验 PNG 文件本身
# --------------------------------------------------------------------------
def _png_size(path: Path) -> Optional[tuple]:
    try:
        with path.open("rb") as f:
            head = f.read(24)
        if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
    except OSError:
        pass
    return None


def _tool_screenshot(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    d = ctx.root / "logs" / "screens"
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("screen-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".png")
    try:
        screen = max(0, min(int(a.get("screen", 0)), 8))
    except (TypeError, ValueError):
        screen = 0
    r = ctx.desktop.run("screenshot", str(path), "--screen", screen, timeout=30)
    if not r.get("ok"):
        return r
    size = _png_size(path)
    r["saved_path"] = str(path)
    r["file_exists"] = path.is_file()
    r["file_size_bytes"] = path.stat().st_size if path.is_file() else None
    r["png_dimensions"] = list(size) if size else None
    data = r.get("data") or {}
    r["artifact_verified"] = bool(size and r["file_size_bytes"] and
                                  (data.get("width") in (None, size[0])) and (data.get("height") in (None, size[1])))
    return r


# --------------------------------------------------------------------------
# desktop_click
# --------------------------------------------------------------------------
def _tool_click(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    ref = str(a.get("ref", "")).strip()
    if not valid_ref(ref):
        return fail("INVALID_ARGS", "拒绝无效控件引用")
    target = ctx.snapshot_index.get(ref)
    if target is None:
        return fail("REF_NOT_IN_LATEST_SNAPSHOT", "拒绝点击：引用不属于最近一次成功快照，请重新观察界面")
    if "Click" not in (target.get("available_actions") or []):
        return fail("ACTION_NOT_SUPPORTED", "拒绝点击：该控件没有 Click 操作权限", control=target)
    term = risk_term(target)
    if term:
        return blocked("高风险控件禁止自动点击，请由用户手动操作", matched_term=term, control=target)
    if "disabled" in node_states(target):
        return fail("TARGET_DISABLED", "拒绝点击：控件处于禁用状态", control=target)

    expect = a.get("expect_text")
    expect = expect.strip() if isinstance(expect, str) and expect.strip() else None

    result = ctx.desktop.run("click", ref)
    result["effect_verified"] = None
    if not result.get("ok"):
        delivery, _ = disposition_of(result)
        if delivery == "not_delivered":
            return result

    verification = None
    evidence = None
    for attempt in range(3 if expect else 1):
        verification = _observe(ctx)
        if verification.get("ok") and expect:
            evidence = _find_text((verification.get("data") or {}).get("tree"), expect)
            if evidence:
                break
            ctx.sleep(0.7)
    if not verification or not verification.get("ok"):
        result.update(observation_ok=False, target_present=None, match_count=None, state_changed=None,
                      value_changed=None, actions_changed=None,
                      verification_error="点击后的界面快照失败，操作结果未经验证")
        return result

    result["observation_ok"] = True
    tree = (verification.get("data") or {}).get("tree")
    old_path = target.get("parent_path")
    matches = [n for n, anc in walk_nodes(tree)
               if same_identity(n, target) and isinstance(old_path, list) and [identity(x) for x in anc] == old_path]
    result["match_count"] = len(matches)
    if not matches:
        result.update(target_present=False, state_changed=None, value_changed=None, actions_changed=None)
    elif len(matches) > 1:
        result.update(target_present=None, state_changed=None, value_changed=None, actions_changed=None,
                      verification_error="点击后目标控件匹配存在歧义")
    else:
        o = matches[0]
        result.update(target_present=True,
                      state_changed=target.get("states", []) != o.get("states", []),
                      value_changed=target.get("value") != o.get("value"),
                      actions_changed=target.get("available_actions", []) != o.get("available_actions", []))
    if expect:
        if evidence:
            result["effect_verified"] = True
            result["verification_scope"] = "post_click_snapshot_text"
            result["evidence"] = evidence
        else:
            result["verification_error"] = f"点击后的快照中未找到预期文字“{expect}”，效果尚未验证"
    return result


# --------------------------------------------------------------------------
# desktop_set_value —— 写前核对 + 写后读回
# --------------------------------------------------------------------------
def _norm_nl(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _tool_set_value(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    ref, value = str(a.get("ref", "")).strip(), a.get("value")
    if not valid_ref(ref):
        return fail("INVALID_ARGS", "拒绝无效控件引用")
    target = ctx.snapshot_index.get(ref)
    if target is None:
        return fail("REF_NOT_IN_LATEST_SNAPSHOT", "拒绝设置：引用不属于最近一次成功快照，请重新观察界面")
    if "SetValue" not in (target.get("available_actions") or []):
        return fail("ACTION_NOT_SUPPORTED", "拒绝设置：该控件没有 SetValue 操作权限", control=target)
    if not isinstance(value, str):
        return fail("INVALID_ARGS", "输入值必须是字符串")
    if len(value) > MAX_SET_VALUE_CHARS:
        return fail("INVALID_ARGS", f"输入内容超过{MAX_SET_VALUE_CHARS}字符限制")
    app, exp_tab, exp_cur = a.get("app"), a.get("expected_selected_tab"), a.get("expected_current_value")
    if not isinstance(app, str) or not app.strip():
        return fail("INVALID_ARGS", "拒绝设置：必须明确指定目标应用")
    if not isinstance(exp_tab, str):
        return fail("INVALID_ARGS", "拒绝设置：必须提供预期选中标签名称（无标签页填空字符串）")
    if not isinstance(exp_cur, str):
        return fail("INVALID_ARGS", "拒绝设置：必须提供预期编辑器原值")
    if target.get("role") != "textfield":
        return fail("INVALID_ARGS", "拒绝设置：原引用不是文本编辑器")
    why = sensitive_field(target)
    if why:
        return blocked(f"目标是敏感/密码字段（{why}），禁止自动写入，请用户手动输入")

    pre = take_snapshot(ctx, app=app)
    if not pre.get("ok"):
        return fail("PREFLIGHT_FAILED", "拒绝设置：写入前重新快照失败", preflight=pre)
    pre_data = pre.get("data") or {}
    if pre_data.get("complete") is not True:
        return fail("PREFLIGHT_INCOMPLETE", "拒绝设置：写入前快照不完整")

    all_tabs, selected, editors = [], [], []
    for n, _ in walk_nodes(pre_data.get("tree")):
        if n.get("role") == "tab":
            all_tabs.append(n)
            if "selected" in node_states(n):
                selected.append(n)
        if n.get("role") == "textfield" and "SetValue" in (n.get("available_actions") or []):
            editors.append(n)

    if exp_tab == "":
        if all_tabs:
            return fail("TAB_MISMATCH", "拒绝设置：声明无标签页，但界面存在标签页", tab_count=len(all_tabs))
    else:
        if len(selected) != 1:
            return fail("TAB_AMBIGUOUS", "拒绝设置：无法唯一确认当前选中标签", selected_tab_count=len(selected))
        if selected[0].get("name") != exp_tab:
            return fail("TAB_MISMATCH", "拒绝设置：当前选中标签与预期不一致",
                        expected_selected_tab=exp_tab, observed_selected_tab=selected[0].get("name"))
    if len(editors) != 1:
        return fail("EDITOR_NOT_UNIQUE", "拒绝设置：可写文本编辑器不是唯一的", editor_count=len(editors))
    fresh = editors[0]
    if not same_identity(fresh, target):
        return fail("EDITOR_IDENTITY_MISMATCH", "拒绝设置：重新快照中的编辑器身份与原引用不一致")
    why = sensitive_field(fresh)
    if why:
        return blocked(f"目标是敏感/密码字段（{why}），禁止自动写入")
    fresh_ref = fresh.get("ref_id")
    if not isinstance(fresh_ref, str) or fresh_ref not in ctx.snapshot_index:
        return fail("NO_FRESH_REF", "拒绝设置：重新快照未提供有效的新控件引用")

    cur = ctx.desktop.run("get", fresh_ref, "--property", "value", timeout=15)
    cur_val = (cur.get("data") or {}).get("value") if cur.get("ok") else None
    if not isinstance(cur_val, str):
        return fail("PREREAD_FAILED", "拒绝设置：无法从当前编辑器读取真实原值", current_read=cur)
    if _norm_nl(cur_val) != _norm_nl(exp_cur):
        return fail("CURRENT_VALUE_MISMATCH", "拒绝设置：编辑器当前原值与预期不一致",
                    expected_current_value=exp_cur, observed_current_value=cur_val)

    result = ctx.desktop.run("set-value", fresh_ref, "--stdin", stdin=value, timeout=30)
    result.update(preflight_verified=True, fresh_ref=fresh_ref, verified_selected_tab=exp_tab, effect_verified=None)
    if not result.get("ok"):
        delivery, _ = disposition_of(result)
        result["verification_error"] = "执行器未确认写入成功"
        if delivery == "not_delivered":
            return result

    post = take_snapshot(ctx, app=app)
    if not post.get("ok"):
        result.update(observation_ok=False, verification_error="设置后的界面快照失败，操作结果未经验证")
        return result
    result["observation_ok"] = True
    if (post.get("data") or {}).get("complete") is not True:
        result["verification_error"] = "写入后快照不完整，无法安全读回"
        return result
    cands = [n for n, _ in walk_nodes((post.get("data") or {}).get("tree")) if same_identity(n, fresh)]
    if len(cands) != 1 or not isinstance(cands[0].get("ref_id"), str) or cands[0]["ref_id"] not in ctx.snapshot_index:
        result["verification_error"] = "新快照中目标控件不存在、不唯一或无引用，无法安全读回"
        return result
    rb = ctx.desktop.run("get", cands[0]["ref_id"], "--property", "value", timeout=15)
    rb_val = (rb.get("data") or {}).get("value") if rb.get("ok") else None
    if not isinstance(rb_val, str):
        result.update(verification_error="写入后无法从 UIA 读取编辑器真实 value", readback=rb)
        return result
    result["readback_ref"] = cands[0]["ref_id"]
    result["readback_len"] = len(rb_val)
    if _norm_nl(rb_val) == _norm_nl(value):
        result["effect_verified"] = True
        result["verification_scope"] = "fresh_uia_value_readback"
        result.pop("verification_error", None)
    else:
        result["effect_verified"] = False
        result["verification_error"] = "读回值与目标值不一致"
        result["readback_value"] = _trunc(rb_val, 500)
    return result


# --------------------------------------------------------------------------
# desktop_press —— 前后界面差异
# --------------------------------------------------------------------------
def _tool_press(ctx: Context, a: Dict[str, Any]) -> Dict[str, Any]:
    combo = str(a.get("combo", "")).strip().lower().replace(" ", "")
    if combo not in ALLOWED_KEYS:
        return fail("KEY_NOT_ALLOWED", f"快捷键不在白名单中：{combo}")

    before = _observe(ctx)
    before_sum = None
    if before.get("ok"):
        tree = (before.get("data") or {}).get("tree")
        before_sum = _summarize(tree)
        if combo == "return":
            for n, anc in walk_nodes(tree):
                if "focused" in node_states(n):
                    t = {"name": n.get("name"), "role": n.get("role"), "native_id": n.get("native_id"),
                         "description": n.get("description"), "title": n.get("title"),
                         "ancestors": [{"role": x.get("role"), "name": x.get("name")} for x in anc]}
                    term = risk_term(t)
                    if term:
                        return blocked(f"当前焦点控件属于高风险操作（{term}），禁止按回车")
    elif combo == "return":
        return fail("PRE_OBSERVATION_FAILED", "按回车前无法确认焦点控件，已拒绝")

    result = ctx.desktop.run("press", combo, timeout=15)
    result["effect_verified"] = None
    result["before_observation_ok"] = before_sum is not None
    if not result.get("ok"):
        delivery, _ = disposition_of(result)
        if delivery == "not_delivered":
            return result
    ctx.sleep(0.3)
    after = _observe(ctx)
    if not after.get("ok"):
        result.update(observation_ok=False, ui_changed=None, verification_error="按键后的界面快照失败，操作结果未经验证")
        return result
    result["observation_ok"] = True
    after_sum = _summarize((after.get("data") or {}).get("tree"))
    result["before_control_count"] = len(before_sum) if before_sum is not None else None
    result["after_control_count"] = len(after_sum)
    result["ui_changed"] = (before_sum != after_sum) if before_sum is not None else None
    if before_sum is None:
        result["verification_error"] = "按键前快照不可用，无法比较界面变化"
    return result


HANDLERS = {
    "desktop_snapshot": _tool_snapshot, "desktop_get": _tool_get, "desktop_windows": _tool_windows,
    "desktop_apps": _tool_apps, "desktop_status": _tool_status, "desktop_find": _tool_find,
    "desktop_screenshot": _tool_screenshot, "desktop_launch": _tool_launch, "desktop_click": _tool_click,
    "desktop_set_value": _tool_set_value, "desktop_press": _tool_press,
}


def run_tool(ctx: Context, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """执行工具（调用方须先通过 da_policy.authorize）。永不抛异常。"""
    handler = HANDLERS.get(name)
    if handler is None:
        return fail("UNKNOWN_TOOL", "工具不在白名单中")
    if not isinstance(args, dict):
        return fail("INVALID_ARGS", "工具参数必须是 JSON 对象")
    if ctx.dry_run and name in SIDE_EFFECT_TOOLS:
        r = {"ok": True, "dry_run": True, "tool": name, "effect_verified": None,
             "note": "干跑模式：未对桌面执行任何动作"}
        _receipt(ctx, name, r)
        return r
    try:
        r = handler(ctx, args)
    except Exception as e:  # noqa: BLE001
        r = fail("TOOL_EXCEPTION", repr(e))
    if name in SIDE_EFFECT_TOOLS:
        _receipt(ctx, name, r)
    return r
