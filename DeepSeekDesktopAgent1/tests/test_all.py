# -*- coding: utf-8 -*-
"""离线回归测试：python tests\\test_all.py
覆盖：授权策略（含原版 notepad/"no" 误判回归）、各工具的前后验证、谎报成功检测、密码框保护、
      审计哈希链篡改检测、超时/坏输出处理、完整任务循环（脚本化模型）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
_TMP_HOME = tempfile.mkdtemp(prefix="deskagent_home_")
os.environ["DESKAGENT_HOME"] = _TMP_HOME

import agent  # noqa: E402
import da_core  # noqa: E402
from da_core import AuditLog, Context, Desktop, verify_audit  # noqa: E402
from da_policy import authorize, forbids, has, risk_term  # noqa: E402
from da_tools import run_tool  # noqa: E402

MOCK = str(HERE / "mock_agent_desktop.py")


def make_ctx(tmp, extra_env=None, dry_run=False):
    env = dict(os.environ)
    env["MOCK_STATE"] = str(Path(tmp) / "state.json")
    env.update(extra_env or {})
    audit = AuditLog(Path(tmp) / "audit.jsonl")
    ctx = Context(Desktop([sys.executable, MOCK], audit=audit, env=env), audit, Path(tmp), dry_run=dry_run)
    ctx.sleep = lambda s: None
    ctx.launch_resolver = lambda key: {"notepad": "notepad.exe", "calculator": "calc.exe"}.get(key, key + ".exe")
    return ctx


def state_of(ctx):
    return json.loads(Path(ctx.desktop.env["MOCK_STATE"]).read_text(encoding="utf-8"))


def ref_of(snapshot_result, name):
    for n in snapshot_result["nodes"]:
        if n.get("name") == name and n.get("ref"):
            return n["ref"]
    raise AssertionError(f"快照中没有 {name}: {snapshot_result}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deskagent_t_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        try:
            (Path(_TMP_HOME) / "STOP").unlink()
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ 策略
class PolicyTests(Base):
    def idx(self, name, role="button", actions=("Click",), ancestors=()):
        return {"@s1:e1": {"name": name, "role": role, "available_actions": list(actions), "states": [],
                           "native_id": None, "ancestors": list(ancestors)}}

    def test_regression_notepad_does_not_trigger_negation(self):
        # 原版 bug：英文否定词 "no" 以子串匹配，"notepad" 里的 "no" 使点击被判为"用户禁止"
        self.assertFalse(forbids("open notepad and click new tab", "click"))
        ok, why = authorize("desktop_click", {"ref": "@s1:e1"}, "open notepad and click new tab", self.idx("New tab"))
        self.assertTrue(ok, why)

    def test_regression_negation_does_not_cross_clauses(self):
        # 原版 bug：「不要发送消息，点击记事本」中的"不要"误伤"点击"
        self.assertFalse(forbids("不要发送消息，点击记事本", "点击"))
        ok, why = authorize("desktop_click", {"ref": "@s1:e1"}, "不要发送消息，点击记事本", self.idx("记事本"))
        self.assertTrue(ok, why)

    def test_negation_still_blocks(self):
        for req in ("不要点击新建标签页", "请勿点击新建标签页", "do not click new tab", "never click the new tab"):
            ok, _ = authorize("desktop_click", {"ref": "@s1:e1"}, req, self.idx("新建标签页"))
            self.assertFalse(ok, req)

    def test_high_risk_click_blocked_even_if_user_asks(self):
        ok, why = authorize("desktop_click", {"ref": "@s1:e1"}, "点击删除账号", self.idx("删除账号"))
        self.assertFalse(ok)
        self.assertIn("高风险", why)

    def test_dialog_ancestor_risk(self):
        anc = [{"role": "dialog", "name": "确认删除"}]
        self.assertEqual(risk_term({"name": "确定", "role": "button", "ancestors": anc}), "删除")
        ok, _ = authorize("desktop_click", {"ref": "@s1:e1"}, "点击确定", self.idx("确定", ancestors=anc))
        self.assertFalse(ok)

    def test_click_requires_ref_in_snapshot(self):
        ok, _ = authorize("desktop_click", {"ref": "@s9:e9"}, "点击新建标签页", {})
        self.assertFalse(ok)

    def test_launch_policy(self):
        yes = [("打开记事本", "notepad"), ("请打开 notepad", "notepad"), ("启动计算器", "calculator"),
               ("open the calculator", "calculator")]
        for req, app in yes:
            self.assertTrue(authorize("desktop_launch", {"app": app}, req, {})[0], req)
        no = [("不要打开记事本", "notepad"), ("打开计算器", "notepad"), ("打开记事本", "cmd"),
              ("打开记事本", r"C:\Windows\System32\cmd.exe"), ("打开 powershell", "powershell"),
              ("帮我看看窗口", "notepad")]
        for req, app in no:
            self.assertFalse(authorize("desktop_launch", {"app": app}, req, {})[0], (req, app))

    def test_set_value_policy(self):
        a = {"app": "notepad.exe", "value": "hi"}
        self.assertTrue(authorize("desktop_set_value", a, "打开记事本并输入 hi", {})[0])
        self.assertFalse(authorize("desktop_set_value", a, "不要输入任何内容，打开记事本", {})[0])
        self.assertFalse(authorize("desktop_set_value", a, "只读模式：输入 hi 到记事本", {})[0])
        self.assertFalse(authorize("desktop_set_value", a, "打开计算器并输入 hi", {})[0])
        self.assertFalse(authorize("desktop_set_value", a, "查看记事本", {})[0])

    def test_press_and_screenshot_policy(self):
        self.assertTrue(authorize("desktop_press", {"combo": "return"}, "在记事本里按回车", {})[0])
        self.assertFalse(authorize("desktop_press", {"combo": "return"}, "看看窗口", {})[0])
        self.assertTrue(authorize("desktop_press", {"combo": "ctrl+t"}, "新建标签页", {})[0])
        self.assertFalse(authorize("desktop_press", {"combo": "ctrl+t"}, "新建标签页，不要使用快捷键", {})[0])
        self.assertTrue(authorize("desktop_screenshot", {}, "请截图", {})[0])
        self.assertFalse(authorize("desktop_screenshot", {}, "不要截图，列出窗口", {})[0])
        self.assertFalse(authorize("desktop_screenshot", {}, "列出窗口", {})[0])

    def test_readonly_and_unknown(self):
        self.assertTrue(authorize("desktop_windows", {}, "列出窗口", {})[0])
        self.assertFalse(authorize("desktop_shell", {}, "任意", {})[0])
        self.assertFalse(authorize("desktop_windows", {}, None, {})[0])
        self.assertTrue(has("Open Notepad", "notepad"))


# ------------------------------------------------------------------ 工具
class ToolTests(Base):
    def launch_notepad(self, ctx):
        r = run_tool(ctx, "desktop_launch", {"app": "notepad"})
        self.assertTrue(r["ok"], r)
        return r

    def snap(self, ctx, app="notepad.exe"):
        r = run_tool(ctx, "desktop_snapshot", {"app": app})
        self.assertTrue(r["ok"], r)
        return r

    def test_launch_verified_by_independent_enumeration(self):
        ctx = make_ctx(self.tmp)
        r = self.launch_notepad(ctx)
        self.assertIs(r["effect_verified"], True)
        self.assertEqual(r["verification_scope"], "independent_window_enumeration")
        self.assertFalse(r["attached_existing"])
        self.assertEqual(r["window"]["app_name"], "notepad.exe")
        self.assertEqual(sum(w["app_name"] == "notepad.exe" for w in state_of(ctx)["windows"]), 1)

    def test_launch_without_window_is_not_claimed_verified(self):
        ctx = make_ctx(self.tmp, {"MOCK_LAUNCH_NOWINDOW": "1"})
        r = run_tool(ctx, "desktop_launch", {"app": "notepad"})
        self.assertTrue(r["ok"])
        self.assertIsNone(r["effect_verified"])
        self.assertIn("未在窗口枚举", r["verification_error"])

    def test_launch_rejects_non_whitelisted(self):
        ctx = make_ctx(self.tmp)
        r = run_tool(ctx, "desktop_launch", {"app": "cmd.exe"})
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("blocked"))

    def test_set_value_end_to_end_with_readback(self):
        ctx = make_ctx(self.tmp)
        self.launch_notepad(ctx)
        s = self.snap(ctx)
        text = "-- 你好, DeskAgent\n第二行 \"引号\" & | > <"
        r = run_tool(ctx, "desktop_set_value", {"ref": ref_of(s, "文本编辑器"), "value": text, "app": "notepad.exe",
                                                 "expected_selected_tab": "无标题", "expected_current_value": ""})
        self.assertTrue(r["ok"], r)
        self.assertIs(r["effect_verified"], True)
        self.assertEqual(r["verification_scope"], "fresh_uia_value_readback")
        self.assertEqual(state_of(ctx)["text"], text)  # 第三方（状态文件）也读到同样内容

    def test_set_value_detects_lying_executor(self):
        ctx = make_ctx(self.tmp, {"MOCK_SETVALUE_LIE": "1"})
        self.launch_notepad(ctx)
        s = self.snap(ctx)
        r = run_tool(ctx, "desktop_set_value", {"ref": ref_of(s, "文本编辑器"), "value": "abc", "app": "notepad.exe",
                                                 "expected_selected_tab": "无标题", "expected_current_value": ""})
        self.assertTrue(r["ok"])  # 执行器自称成功
        self.assertIs(r["effect_verified"], False)  # 但读回证明没写进去
        self.assertIn("不一致", r["verification_error"])

    def test_set_value_wrong_tab_and_wrong_original(self):
        ctx = make_ctx(self.tmp)
        self.launch_notepad(ctx)
        s = self.snap(ctx)
        base = {"ref": ref_of(s, "文本编辑器"), "value": "x", "app": "notepad.exe"}
        r = run_tool(ctx, "desktop_set_value", {**base, "expected_selected_tab": "别的标签", "expected_current_value": ""})
        self.assertEqual(r["error"]["code"], "TAB_MISMATCH")
        s = self.snap(ctx)
        r = run_tool(ctx, "desktop_set_value", {**base, "ref": ref_of(s, "文本编辑器"),
                                                 "expected_selected_tab": "无标题", "expected_current_value": "旧内容"})
        self.assertEqual(r["error"]["code"], "CURRENT_VALUE_MISMATCH")
        self.assertEqual(state_of(ctx)["text"], "")  # 没有任何写入

    def test_set_value_refuses_after_tab_changed(self):
        ctx = make_ctx(self.tmp)
        self.launch_notepad(ctx)
        s = self.snap(ctx)
        c = run_tool(ctx, "desktop_click", {"ref": ref_of(s, "新建标签页")})
        self.assertTrue(c["ok"])
        s = self.snap(ctx)
        r = run_tool(ctx, "desktop_set_value", {"ref": ref_of(s, "文本编辑器"), "value": "x", "app": "notepad.exe",
                                                 "expected_selected_tab": "无标题", "expected_current_value": ""})
        self.assertEqual(r["error"]["code"], "TAB_MISMATCH")

    def test_set_value_stale_ref_refused(self):
        ctx = make_ctx(self.tmp)
        self.launch_notepad(ctx)
        r = run_tool(ctx, "desktop_set_value", {"ref": "@s77:e3", "value": "x", "app": "notepad.exe",
                                                 "expected_selected_tab": "无标题", "expected_current_value": ""})
        self.assertEqual(r["error"]["code"], "REF_NOT_IN_LATEST_SNAPSHOT")

    def test_password_field_protected(self):
        ctx = make_ctx(self.tmp, {"MOCK_SCENARIO": "login"})
        s = run_tool(ctx, "desktop_snapshot", {"app": "loginapp.exe"})
        self.assertNotIn("hunter2", json.dumps(s, ensure_ascii=False))  # 值不外泄给模型
        r = run_tool(ctx, "desktop_set_value", {"ref": ref_of(s, "密码"), "value": "x", "app": "loginapp.exe",
                                                 "expected_selected_tab": "", "expected_current_value": "hunter2"})
        self.assertTrue(r.get("blocked"), r)
        g = run_tool(ctx, "desktop_get", {"ref": ref_of(s, "密码"), "property": "value"})
        self.assertNotIn("hunter2", json.dumps(g, ensure_ascii=False))

    def test_high_risk_clicks_blocked_at_tool_layer(self):
        ctx = make_ctx(self.tmp, {"MOCK_SCENARIO": "login"})
        s = run_tool(ctx, "desktop_snapshot", {"app": "loginapp.exe"})
        for name in ("删除账号", "确定"):
            r = run_tool(ctx, "desktop_click", {"ref": ref_of(s, name)})
            self.assertTrue(r.get("blocked"), name)
        self.assertNotIn("click", state_of(ctx)["log"])  # 桌面上根本没收到点击

    def test_click_verification_levels(self):
        ctx = make_ctx(self.tmp)
        self.launch_notepad(ctx)
        s = self.snap(ctx)
        r = run_tool(ctx, "desktop_click", {"ref": ref_of(s, "新建标签页")})
        self.assertTrue(r["ok"])
        self.assertIsNone(r["effect_verified"])  # 没给 expect_text：只能说"已投递"
        self.assertTrue(r["observation_ok"])
        s = self.snap(ctx)
        r = run_tool(ctx, "desktop_click", {"ref": ref_of(s, "新建标签页"), "expect_text": "无标题 3"})
        self.assertIs(r["effect_verified"], True)
        self.assertEqual(r["verification_scope"], "post_click_snapshot_text")
        s = self.snap(ctx)
        r = run_tool(ctx, "desktop_click", {"ref": ref_of(s, "设置"), "expect_text": "绝对不存在的文字"})
        self.assertIsNone(r["effect_verified"])  # 找不到证据 ≠ 失败，保持 null
        self.assertIn("效果尚未验证", r["verification_error"])

    def test_click_unknown_ref(self):
        ctx = make_ctx(self.tmp)
        r = run_tool(ctx, "desktop_click", {"ref": "@s9:e9"})
        self.assertEqual(r["error"]["code"], "REF_NOT_IN_LATEST_SNAPSHOT")

    def test_incomplete_snapshot_builds_no_index(self):
        ctx = make_ctx(self.tmp, {"MOCK_INCOMPLETE": "1"})
        r = run_tool(ctx, "desktop_snapshot", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "SNAPSHOT_INCOMPLETE")
        self.assertEqual(ctx.snapshot_index, {})

    def test_press_whitelist_and_diff(self):
        ctx = make_ctx(self.tmp)
        self.launch_notepad(ctx)
        self.assertEqual(run_tool(ctx, "desktop_press", {"combo": "alt+f4"})["error"]["code"], "KEY_NOT_ALLOWED")
        self.assertEqual(run_tool(ctx, "desktop_press", {"combo": "win+l"})["error"]["code"], "KEY_NOT_ALLOWED")
        self.snap(ctx)
        r = run_tool(ctx, "desktop_press", {"combo": "ctrl+t"})
        self.assertTrue(r["ok"])
        self.assertIs(r["ui_changed"], True)  # 新标签页出现
        self.assertIsNone(r["effect_verified"])  # 按键永远只是 delivered_unverified

    def test_screenshot_file_verified(self):
        ctx = make_ctx(self.tmp)
        r = run_tool(ctx, "desktop_screenshot", {"screen": 0})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["artifact_verified"])
        self.assertEqual(r["png_dimensions"], [16, 9])
        self.assertGreater(r["file_size_bytes"], 20)

    def test_find_and_windows(self):
        ctx = make_ctx(self.tmp)
        self.launch_notepad(ctx)
        w = run_tool(ctx, "desktop_windows", {})
        self.assertEqual(w["count"], 2)
        f = run_tool(ctx, "desktop_find", {"query": "标签", "app": "notepad.exe"})
        self.assertTrue(f["ok"])
        self.assertGreaterEqual(f["count"], 2)
        self.assertEqual(run_tool(ctx, "desktop_find", {"query": "  "})["error"]["code"], "INVALID_ARGS")

    def test_dry_run_touches_nothing(self):
        ctx = make_ctx(self.tmp, dry_run=True)
        r = run_tool(ctx, "desktop_launch", {"app": "notepad"})
        self.assertTrue(r["dry_run"])
        self.assertFalse(Path(ctx.desktop.env["MOCK_STATE"]).exists() and "launch" in state_of(ctx)["log"])

    def test_unknown_tool_and_bad_args(self):
        ctx = make_ctx(self.tmp)
        self.assertEqual(run_tool(ctx, "desktop_shell", {})["error"]["code"], "UNKNOWN_TOOL")
        self.assertEqual(run_tool(ctx, "desktop_click", {})["error"]["code"], "INVALID_ARGS")


# ------------------------------------------------------------------ 核心层
class CoreTests(Base):
    def test_timeout_means_unknown_delivery(self):
        d = Desktop([sys.executable, "-c", "import time; time.sleep(6)"])
        r = d.run("click", timeout=1)
        self.assertEqual(r["error"]["code"], "CLIENT_TIMEOUT")
        self.assertEqual(r["error"]["disposition"], {"delivery": "unknown", "retry": "unknown"})

    def test_bad_output_and_missing_exe(self):
        r = Desktop([sys.executable, "-c", "print('not json')"]).run("x")
        self.assertEqual(r["error"]["code"], "BAD_OUTPUT")
        r = Desktop([str(Path(self.tmp) / "nope.exe")]).run("x")
        self.assertEqual(r["error"]["code"], "EXECUTABLE_NOT_FOUND")

    def test_audit_chain_detects_tamper_delete_insert(self):
        p = Path(self.tmp) / "a.jsonl"
        a = AuditLog(p)
        for i in range(5):
            a.write("e", {"i": i})
        self.assertEqual(verify_audit(p)[:2], (True, 5))
        lines = p.read_text(encoding="utf-8").splitlines()
        p.write_text("\n".join(lines[:2] + [lines[2].replace('"i":2', '"i":9')] + lines[3:]) + "\n", encoding="utf-8")
        self.assertFalse(verify_audit(p)[0])
        p.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
        self.assertFalse(verify_audit(p)[0])
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertTrue(verify_audit(p)[0])
        a2 = AuditLog(p)  # 重新打开后继续链
        a2.write("e", {"i": 5})
        self.assertEqual(verify_audit(p)[:2], (True, 6))


# ------------------------------------------------------------------ 完整任务循环（脚本化模型）
def tc(i, name, **args):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


class LoopTests(Base):
    def test_open_notepad_and_type_full_loop(self):
        ctx = make_ctx(self.tmp)
        state = {"n": 0}

        def model(history):
            state["n"] += 1
            n = state["n"]
            if n == 1:
                return {"role": "assistant", "content": None, "tool_calls": [tc(1, "desktop_launch", app="notepad")]}
            if n == 2:
                return {"role": "assistant", "content": None, "tool_calls": [tc(2, "desktop_snapshot", app="notepad.exe")]}
            if n == 3:
                snap = json.loads(history[-1]["content"])
                return {"role": "assistant", "content": None, "tool_calls": [tc(3, "desktop_set_value", ref=ref_of(snap, "文本编辑器"),
                        value="你好 DeskAgent", app="notepad.exe", expected_selected_tab="无标题", expected_current_value="")]}
            return {"role": "assistant", "content": "已完成。"}

        hist = [{"role": "system", "content": agent.SYSTEM_PROMPT}]
        out = agent.run_task(ctx, hist, "打开记事本并输入 你好 DeskAgent", model_fn=model)
        self.assertIn("✔ 已验证", out)
        self.assertNotIn("⚠", out)
        self.assertEqual(state_of(ctx)["text"], "你好 DeskAgent")
        ok, n, _ = verify_audit(Path(self.tmp) / "audit.jsonl")
        self.assertTrue(ok)
        self.assertGreater(n, 5)
        self.assertNotIn("你好 DeskAgent", (Path(self.tmp) / "audit.jsonl").read_text(encoding="utf-8"))  # 日志不含明文

    def test_model_cannot_escape_policy_and_summary_warns(self):
        ctx = make_ctx(self.tmp, {"MOCK_SCENARIO": "login"})
        state = {"n": 0}

        def model(history):
            state["n"] += 1
            n = state["n"]
            if n == 1:
                return {"role": "assistant", "content": None, "tool_calls": [tc(1, "desktop_launch", app="cmd")]}
            if n == 2:
                return {"role": "assistant", "content": None, "tool_calls": [tc(2, "desktop_snapshot", app="loginapp.exe")]}
            if n == 3:
                snap = json.loads(history[-1]["content"])
                return {"role": "assistant", "content": None, "tool_calls": [tc(3, "desktop_click", ref=ref_of(snap, "删除账号"))]}
            return {"role": "assistant", "content": "全部成功！"}

        hist = [{"role": "system", "content": "x"}]
        out = agent.run_task(ctx, hist, "看看登录窗口，然后点击删除账号，再打开 cmd", model_fn=model)
        self.assertIn("⚠", out)  # 即使模型说"全部成功"，程序也会警告
        self.assertNotIn("click", state_of(ctx)["log"])
        self.assertNotIn("launch", state_of(ctx)["log"])

    def test_circuit_breaker(self):
        ctx = make_ctx(self.tmp)
        n = {"i": 0}

        def model(history):
            n["i"] += 1
            return {"role": "assistant", "content": None, "tool_calls": [tc(n["i"], "desktop_get", ref="@s1:e1")]}

        out = agent.run_task(ctx, [{"role": "system", "content": "x"}], "读取", model_fn=model)
        self.assertIn("熔断", out)
        self.assertEqual(n["i"], 3)

    def test_stop_file_and_max_steps(self):
        ctx = make_ctx(self.tmp)
        (Path(_TMP_HOME) / "STOP").write_text("x")
        out = agent.run_task(ctx, [{"role": "system", "content": "x"}], "列出窗口",
                             model_fn=lambda h: {"role": "assistant", "content": "不应被调用"})
        self.assertIn("急停", out)
        (Path(_TMP_HOME) / "STOP").unlink()
        k = {"i": 0}

        def model(history):
            k["i"] += 1
            return {"role": "assistant", "content": None, "tool_calls": [tc(k["i"], "desktop_windows")]}

        out = agent.run_task(ctx, [{"role": "system", "content": "x"}], "列出窗口", model_fn=model, max_steps=4)
        self.assertIn("最大步数", out)

    def test_trim_history_keeps_pairs(self):
        h = [{"role": "system", "content": "s"}]
        for i in range(40):
            h += [{"role": "user", "content": "u%d" % i},
                  {"role": "assistant", "content": None, "tool_calls": [tc(i, "desktop_windows")]},
                  {"role": "tool", "tool_call_id": f"c{i}", "content": "r" * 3000}]
        t = agent.trim_history(h, keep=30)
        self.assertEqual(t[0]["role"], "system")
        self.assertEqual(t[1]["role"], "user")
        self.assertLess(len(t), len(h))


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=2)
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    sys.exit(0 if res.result.wasSuccessful() else 1)
