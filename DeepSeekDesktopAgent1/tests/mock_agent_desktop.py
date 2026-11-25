# -*- coding: utf-8 -*-
"""有状态的 agent-desktop 替身（遵循官方 JSON 契约 2.6），仅用于离线回归测试。
真实桌面上的行为由 ps\\Verify-Independent.ps1 另行验证。

环境变量：
  MOCK_STATE            状态文件路径（必需）
  MOCK_SCENARIO=login   额外窗口：含密码框与"删除账号"按钮、"确认删除"对话框
  MOCK_INCOMPLETE=1     snapshot 返回 complete=false
  MOCK_SETVALUE_LIE=1   set-value 谎报 delivered_verified 但并不写入
  MOCK_LAUNCH_NOWINDOW=1 launch 成功但不产生窗口
"""
import json
import os
import struct
import sys
import zlib

STATE = os.environ["MOCK_STATE"]


def load():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    s = {"snap": 0, "snapshots": {}, "next_pid": 200, "next_win": 10, "text": "", "tabs": ["无标题"], "sel": 0,
         "windows": [{"id": "w-1", "app_name": "explorer.exe", "title": "主页 - 文件资源管理器", "pid": 100,
                      "is_focused": True, "minimized": False, "visible": True, "accessible": True}],
         "log": []}
    if os.environ.get("MOCK_SCENARIO") == "login":
        s["windows"].append({"id": "w-2", "app_name": "loginapp.exe", "title": "登录", "pid": 150, "is_focused": False,
                             "minimized": False, "visible": True, "accessible": True})
    return s


def save(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def out(command, ok=True, data=None, error=None, code=0):
    env = {"version": "2.6", "ok": ok, "command": command}
    if ok:
        env["data"] = data if data is not None else {}
    else:
        env["error"] = error
    sys.stdout.write(json.dumps(env, ensure_ascii=False) + "\n")
    sys.exit(0 if ok else 1)


def err(command, code, msg, delivery="not_delivered", retry="safe"):
    out(command, False, error={"code": code, "message": msg, "disposition": {"delivery": delivery, "retry": retry}})


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        return args[i + 1] if i + 1 < len(args) else default
    return default


def focused_window(s):
    for w in s["windows"]:
        if w["is_focused"]:
            return w
    return s["windows"][-1]


def build_tree(s, w, sid):
    """返回 (tree, refs)；refs: ref->节点副本（用于 get/click/set-value 的定位）"""
    n = {"i": 0}
    refs = {}

    def mk(role, name, actions=None, states=None, value=None, native=None, children=None):
        n["i"] += 1
        node = {"role": role, "name": name}
        if actions is not None:
            node["ref_id"] = "@%s:e%d" % (sid, n["i"])
            node["available_actions"] = actions
            refs[node["ref_id"]] = node
        if states:
            node["states"] = states
        if value is not None:
            node["value"] = value
        if native:
            node["native_id"] = {"kind": "automation_id", "value": native}
        if children:
            node["children"] = children
        return node

    app = w["app_name"]
    if app == "notepad.exe":
        tabs = [mk("tab", t, ["Click"], ["selected"] if i == s["sel"] else []) for i, t in enumerate(s["tabs"])]
        ed = mk("textfield", "文本编辑器", ["SetValue", "SetFocus"], ["focused"], s["text"], "RichEditD2DPT")
        ed["_live"] = "text"
        bar = mk("group", "工具栏", None, None, None, None,
                 [mk("button", "新建标签页", ["Click"]), mk("button", "设置", ["Click"])])
        root = mk("window", w["title"], None, None, None, None, [mk("tablist", "标签", None, None, None, None, tabs), bar, ed])
    elif app == "loginapp.exe":
        pw = mk("textfield", "密码", ["SetValue"], ["secure"], "hunter2", "pwd")
        pw["_live"] = "secret"
        dlg = mk("dialog", "确认删除", None, None, None, None, [mk("button", "确定", ["Click"])])
        root = mk("window", "登录", None, None, None, None,
                  [mk("textfield", "用户名", ["SetValue"], [], "", "user"), pw,
                   mk("button", "删除账号", ["Click"]), mk("button", "登录", ["Click"]), dlg])
    else:
        root = mk("window", w["title"], None, None, None, None, [mk("button", "关于", ["Click"])])
    return root, refs


def strip(node):
    node.pop("_live", None)
    for c in node.get("children", []) or []:
        strip(c)
    return node


def main():
    a = sys.argv[1:]
    while a and a[0] in ("--session", "--headed"):
        a = a[2:] if a[0] == "--session" else a[1:]
    cmd, rest = a[0], a[1:]
    s = load()
    s["log"].append(cmd)

    if cmd == "version":
        out(cmd, data={"version": "mock-1.0", "os": "windows"})
    if cmd == "status":
        out(cmd, data={"platform": "windows", "accessibility": "granted"})
    if cmd == "permissions":
        out(cmd, data={"accessibility": "granted", "automation": "not_required"})
    if cmd == "list-windows":
        app = opt(rest, "--app")
        ws = [w for w in s["windows"] if not app or w["app_name"].lower() == app.lower()]
        save(s)
        out(cmd, data=ws)
    if cmd == "list-apps":
        out(cmd, data={"apps": [{"name": w["app_name"], "pid": w["pid"]} for w in s["windows"]]})
    if cmd == "launch":
        name = os.path.basename(rest[0]).lower()
        exe = name if name.endswith(".exe") else name + ".exe"
        if exe not in ("notepad.exe", "calc.exe", "mspaint.exe", "explorer.exe"):
            err(cmd, "APP_NOT_FOUND", "unknown app " + rest[0])
        if os.environ.get("MOCK_LAUNCH_NOWINDOW") != "1":
            for w in s["windows"]:
                w["is_focused"] = False
            s["next_win"] += 1
            s["next_pid"] += 1
            titles = {"notepad.exe": "无标题 - 记事本", "calc.exe": "计算器", "mspaint.exe": "无标题 - 画图",
                      "explorer.exe": "文件资源管理器"}
            s["windows"].append({"id": "w-%d" % s["next_win"], "app_name": exe, "title": titles[exe],
                                 "pid": s["next_pid"], "is_focused": True, "minimized": False, "visible": True,
                                 "accessible": True})
        save(s)
        out(cmd, data={"app": exe, "pid": s["next_pid"]})
    if cmd == "snapshot":
        app = opt(rest, "--app")
        cands = [w for w in s["windows"] if (not app or w["app_name"].lower() == app.lower())]
        if not cands:
            err(cmd, "WINDOW_NOT_FOUND", "no window")
        w = cands[-1] if app else focused_window(s)
        s["snap"] += 1
        sid = "s%d" % s["snap"]
        tree, refs = build_tree(s, w, sid)
        s["snapshots"][sid] = {"app": w["app_name"], "refs": {k: {"name": v["name"], "role": v["role"]} for k, v in refs.items()}}
        save(s)
        complete = os.environ.get("MOCK_INCOMPLETE") != "1"
        data = {"app": w["app_name"], "snapshot_id": sid, "ref_count": len(refs), "complete": complete,
                "window": {"id": w["id"], "title": w["title"]}, "tree": strip(tree)}
        if not complete:
            data.update(truncated=True, nodes_observed=5)
        out(cmd, data=data)
    if cmd in ("get", "click", "set-value", "clear"):
        ref = rest[0]
        if not ref.startswith("@") or ":" not in ref:
            err(cmd, "INVALID_ARGS", "bad ref")
        sid = ref[1:].split(":")[0]
        snap = s["snapshots"].get(sid)
        if not snap:
            err(cmd, "SNAPSHOT_NOT_FOUND", "no snapshot " + sid)
        meta = snap["refs"].get(ref)
        if not meta:
            err(cmd, "STALE_REF", "stale")
        if cmd == "get":
            prop = opt(rest, "--property", "text")
            if prop == "value":
                if meta["name"] == "文本编辑器":
                    v = s["text"]
                elif meta["name"] == "密码":
                    v = "hunter2"
                else:
                    v = ""
                out(cmd, data={"property": "value", "ref": ref, "value": v})
            out(cmd, data={"property": prop, "ref": ref, "value": meta["name"]})
        if cmd == "click":
            if meta["name"] == "新建标签页":
                s["tabs"].append("无标题 %d" % (len(s["tabs"]) + 1))
                s["sel"] = len(s["tabs"]) - 1
            elif meta["role"] == "tab":
                s["sel"] = s["tabs"].index(meta["name"])
            elif meta["name"] == "设置":
                s["windows"].append({"id": "w-99", "app_name": "settings.exe", "title": "设置", "pid": 999,
                                     "is_focused": False, "minimized": False, "visible": True, "accessible": True})
            save(s)
            out(cmd, data={"action": "click", "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}})
        if cmd == "set-value":
            if "--stdin" in rest:
                val = sys.stdin.read()
            else:
                val = rest[1]
            if os.environ.get("MOCK_SETVALUE_LIE") != "1":
                s["text"] = val
            save(s)
            out(cmd, data={"action": "set-value", "disposition": {"delivery": "delivered_verified", "retry": "unsafe"},
                           "post_state": {"role": "textfield", "value": val}})
    if cmd == "press":
        combo = rest[0]
        if combo in ("alt+f4", "win+l"):
            err(cmd, "POLICY_DENIED", "blocked combo")
        if combo == "ctrl+t":
            s["tabs"].append("无标题 %d" % (len(s["tabs"]) + 1))
            s["sel"] = len(s["tabs"]) - 1
        save(s)
        out(cmd, data={"action": "press", "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}})
    if cmd == "screenshot":
        path = rest[0]
        w, h = 16, 9
        raw = b"".join(b"\x00" + b"\x20\x40\x80" * w for _ in range(h))

        def chunk(t, d):
            c = struct.pack(">I", len(d)) + t + d
            return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
        with open(path, "wb") as f:
            f.write(png)
        out(cmd, data={"format": "png", "width": w, "height": h, "path": path, "scale_factor": 1.0})
    err(cmd, "INVALID_ARGS", "mock does not implement " + cmd)


main()
