# -*- coding: utf-8 -*-
"""da_policy.py — 任务授权与风险策略（纯函数，无副作用，可单元测试）

相对原版修复：
  * 英文否定词 "no" 原为子串匹配，"notepad" 里的 "no" 会让所有点击被误判为"用户禁止"。现改为词边界匹配。
  * 否定范围原为"前 48 个字符"，会跨句：「不要发送消息，点击记事本」中的"不要"误伤"点击"。现按分句截断。
  * 新增 desktop_launch（受控应用白名单，禁止任意路径/任意命令）。
  * 高风险判定覆盖 name/role/native_id/description/title 以及所在对话框标题。
  * secure（密码框）与敏感字段名一律禁止自动写入。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from da_core import native_id_value, node_states

READ_ONLY_TOOLS = frozenset({"desktop_snapshot", "desktop_get", "desktop_windows", "desktop_find",
                             "desktop_status", "desktop_apps"})
SIDE_EFFECT_TOOLS = frozenset({"desktop_screenshot", "desktop_click", "desktop_set_value",
                               "desktop_press", "desktop_launch"})

HIGH_RISK_CLICK_TERMS = (
    "删除", "移除", "清空", "销毁", "卸载", "发送", "发布", "提交", "确认付款", "付款", "支付",
    "下单", "购买", "转账", "退款", "提现", "格式化", "重置", "覆盖", "安装", "关机", "重启", "注销",
    "delete", "remove", "clear", "destroy", "uninstall", "send", "publish", "submit", "pay",
    "payment", "purchase", "buy", "checkout", "transfer", "refund", "withdraw", "place order",
    "format", "reset", "overwrite", "install", "shut down", "shutdown", "restart", "sign out", "log off",
)
DIALOG_ROLES = {"dialog", "alert", "sheet", "alertdialog"}

SENSITIVE_FIELD_TERMS = ("password", "passwd", "密码", "口令", "密钥", "验证码", "卡号", "secret",
                         "token", "card number", "security code")
SENSITIVE_WORDS = ("pin", "cvv", "cvc", "otp")

# ---- 受控应用目录：模型只能按键名启动，不能传路径 -------------------------
APP_CATALOG: Dict[str, Dict[str, Any]] = {
    "notepad": {"aliases": ("记事本", "notepad"), "launch": ["notepad.exe"],
                "process": ("notepad.exe", "notepad"), "title": ("记事本", "notepad")},
    "calculator": {"aliases": ("计算器", "calculator", "calc"), "launch": ["calc.exe"],
                   "process": ("calculator.exe", "calculatorapp.exe", "calc.exe", "applicationframehost.exe"),
                   "title": ("计算器", "calculator")},
    "paint": {"aliases": ("画图", "paint", "mspaint"), "launch": ["mspaint.exe"],
              "process": ("mspaint.exe", "paintapp.exe"), "title": ("画图", "paint")},
    "explorer": {"aliases": ("文件资源管理器", "资源管理器", "file explorer", "explorer"),
                 "launch": ["explorer.exe"], "process": ("explorer.exe",), "title": ()},
    "edge": {"aliases": ("edge", "msedge", "浏览器", "browser"),
             "launch": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
             "process": ("msedge.exe",), "title": ("edge",)},
    "chrome": {"aliases": ("chrome", "谷歌浏览器", "浏览器", "browser"),
               "launch": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                          r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                          r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
               "process": ("chrome.exe",), "title": ("chrome",)},
    "vscode": {"aliases": ("vscode", "vs code", "visual studio code", "代码编辑器"),
               "launch": [r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe"],
               "process": ("code.exe",), "title": ("visual studio code",)},
}


def resolve_app_key(app: Any) -> Optional[str]:
    if not isinstance(app, str) or not app.strip():
        return None
    a = Path(app.strip()).name.casefold()
    a = a[:-4] if a.endswith(".exe") else a
    if a in APP_CATALOG:
        return a
    for key, e in APP_CATALOG.items():
        if a in {x.casefold() for x in e["aliases"]}:
            return key
    return None


# ---- 文本意图匹配 -------------------------------------------------------
_CLAUSE_SPLIT = re.compile(r"[，,。；;!！?？\n]|\.(?=\s|$)|然后|并且|而且|接着|之后|但是|但|"
                           r"\bhowever\b|\bbut\b|\band then\b|\bthen\b")
_NEG_CN = ("不得", "禁止", "不允许", "不要", "严禁", "切勿", "不可", "不能", "无需", "不需要", "不用", "请勿")
_NEG_EN = re.compile(r"(?<![a-z])(do not|don't|dont|must not|mustn't|never|without|no|not|avoid)(?![a-z])")


def _positions(text: str, term: str) -> Iterable[int]:
    term = term.casefold()
    if not term:
        return
    if term.isascii():
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(term), text):
            yield m.start()
    else:
        i = text.find(term)
        while i >= 0:
            yield i
            i = text.find(term, i + 1)


def has(request: str, *terms: str) -> bool:
    t = request.casefold()
    return any(next(iter(_positions(t, x)), None) is not None for x in terms)


def forbids(request: str, *terms: str) -> bool:
    """某个词是否被"本分句内、位于它之前"的否定词否定。"""
    for clause in _CLAUSE_SPLIT.split(request.casefold()):
        for term in terms:
            for pos in _positions(clause, term):
                prefix = clause[:pos]
                if any(n in prefix for n in _NEG_CN) or prefix.rstrip().endswith("别") or _NEG_EN.search(prefix):
                    return True
    return False


READ_ONLY_PHRASES = ("只允许使用只读工具", "只允许使用只读桌面工具", "仅允许使用只读工具", "仅允许只读操作",
                     "只允许只读桌面工具", "只读模式", "only use read-only tools", "read-only tools only")

LAUNCH_TERMS = ("打开", "启动", "运行", "开启", "open", "launch", "start", "run")
CLICK_TERMS = ("打开", "点击", "单击", "选择", "进入", "切换", "启动", "访问",
               "open", "click", "select", "switch", "launch", "navigate")
TYPE_TERMS = ("输入", "写入", "填写", "编辑", "粘贴", "录入", "type", "write", "fill", "enter", "paste", "edit")
SHOT_TERMS = ("截图", "截屏", "屏幕截图", "屏幕画面", "screenshot", "screen capture", "screen shot")

CLICK_ALIASES = (
    (("浏览器", "网页浏览器"), ("chrome", "msedge", "firefox", "browser")),
    (("记事本",), ("notepad",)),
    (("文件资源管理器", "资源管理器"), ("explorer", "file explorer")),
    (("新建标签", "新标签页"), ("new tab",)),
    (("搜索", "查找"), ("search", "find")),
    (("地址栏",), ("address bar",)),
    (("设置",), ("settings",)),
)
APP_ALIASES = (
    (("记事本",), ("notepad",)),
    (("浏览器",), ("chrome", "msedge", "firefox", "browser")),
    (("代码", "编辑器", "vscode"), ("code", "vscode")),
    (("文件资源管理器", "资源管理器"), ("explorer",)),
)

ALLOWED_KEYS = {"escape", "tab", "up", "down", "left", "right", "home", "end", "return",
                "ctrl+t", "ctrl+l", "ctrl+tab", "ctrl+shift+tab", "f5"}
KEY_INTENTS = {
    "ctrl+t": ("新建标签", "新标签页", "打开新标签", "new tab"),
    "ctrl+l": ("地址栏", "输入网址", "定位地址", "address bar"),
    "ctrl+tab": ("下一个标签", "切换到下一标签", "next tab"),
    "ctrl+shift+tab": ("上一个标签", "切换到上一标签", "previous tab"),
    "escape": ("取消", "退出弹窗", "关闭菜单", "cancel", "escape"),
    "tab": ("下一个输入框", "切换焦点", "下一个字段", "next field"),
    "up": ("向上", "上一项", "上移", "move up"),
    "down": ("向下", "下一项", "下移", "move down"),
    "left": ("向左", "向左移动", "move left"),
    "right": ("向右", "向右移动", "move right"),
    "home": ("移到开头", "跳到开头", "beginning"),
    "end": ("移到末尾", "跳到末尾"),
    "return": ("回车", "按回车", "确认输入", "press enter", "press return", "hit enter"),
    "f5": ("刷新", "refresh", "reload"),
}


# ---- 风险判定 -----------------------------------------------------------
def _text_of(target: Dict[str, Any]) -> str:
    parts = [target.get(k) for k in ("name", "role", "description", "title", "role_description")]
    parts.append(native_id_value(target))
    return " ".join(str(p) for p in parts if p).casefold()


def risk_term(target: Dict[str, Any]) -> Optional[str]:
    """返回命中的高风险词；同时检查所在对话框/告警的标题（如"确认删除"对话框里的"确定"按钮）。"""
    texts = [_text_of(target)]
    for anc in target.get("ancestors", []) or []:
        if str(anc.get("role", "")).casefold() in DIALOG_ROLES:
            texts.append(str(anc.get("name") or "").casefold())
    blob = " | ".join(texts)
    for term in HIGH_RISK_CLICK_TERMS:
        if term.casefold() in blob:
            return term
    return None


def sensitive_field(target: Dict[str, Any]) -> Optional[str]:
    if "secure" in node_states(target):
        return "secure"
    blob = _text_of(target)
    for t in SENSITIVE_FIELD_TERMS:
        if t.casefold() in blob:
            return t
    for w in SENSITIVE_WORDS:
        if re.search(r"(?<![a-z])" + w + r"(?![a-z])", blob):
            return w
    return None


# ---- 总授权入口 ---------------------------------------------------------
def authorize(name: str, args: Dict[str, Any], request: Optional[str],
              index: Dict[str, Dict[str, Any]]) -> Tuple[bool, str]:
    """根据用户原始任务文本限制副作用工具；模型无法为自己授权。"""
    if not isinstance(request, str) or not request.strip():
        return False, "当前没有有效任务授权"
    if name in READ_ONLY_TOOLS:
        return True, "只读操作"
    if name not in SIDE_EFFECT_TOOLS:
        return False, "未知工具，默认拒绝"
    if has(request, *READ_ONLY_PHRASES):
        return False, "用户将任务限定为只读操作，禁止执行任何副作用工具"

    req = request.casefold()

    if name == "desktop_screenshot":
        if forbids(request, *SHOT_TERMS):
            return False, "用户明确禁止截图，否定指令优先"
        if has(request, *SHOT_TERMS):
            return True, "用户明确要求查看屏幕图像"
        return False, "任务未明确要求截图"

    if name == "desktop_launch":
        key = resolve_app_key(args.get("app"))
        if key is None:
            return False, "应用不在受控白名单内（禁止启动任意程序/路径）"
        if forbids(request, *LAUNCH_TERMS):
            return False, "用户明确禁止打开/启动应用，否定指令优先"
        if not has(request, *LAUNCH_TERMS):
            return False, "用户任务没有明确的打开/启动意图"
        if not has(request, *APP_CATALOG[key]["aliases"]):
            return False, "用户任务中没有提到该应用，已阻止启动"
        return True, "启动意图与受控应用匹配"

    if name == "desktop_click":
        if forbids(request, "点击", "单击", "click"):
            return False, "用户明确禁止点击，否定指令优先"
        ref = args.get("ref")
        target = index.get(ref) if isinstance(ref, str) else None
        if not isinstance(target, dict):
            return False, "点击目标不属于最新成功快照"
        term = risk_term(target)
        if term:
            return False, f"目标属于高风险控件（{term}），禁止自动点击"
        if not has(request, *CLICK_TERMS):
            return False, "用户任务没有明确的点击或导航意图"
        label = _text_of(target)
        if any(any(x in req for x in left) and any(x in label for x in right) for left, right in CLICK_ALIASES):
            return True, "点击目标与任务中的应用或操作意图匹配"
        tname = str(target.get("name", "")).strip().casefold()
        if len(tname) >= 2 and tname in req:
            return True, "点击目标名称与用户任务匹配"
        return False, "无法确认点击目标属于用户指定任务，已阻止"

    if name == "desktop_set_value":
        if forbids(request, *TYPE_TERMS):
            return False, "用户明确禁止文本输入或编辑，否定指令优先"
        if not has(request, *TYPE_TERMS):
            return False, "用户任务没有明确的文本输入或编辑意图"
        app = args.get("app")
        if not isinstance(app, str) or not app.strip():
            return False, "缺少明确的目标应用"
        app_name = Path(app.strip()).name.casefold()
        key = resolve_app_key(app)
        ok_app = app_name in req or (key is not None and has(request, *APP_CATALOG[key]["aliases"])) or any(
            any(t in req for t in left) and any(t in app_name for t in right) for left, right in APP_ALIASES)
        if not ok_app:
            return False, "目标应用无法从用户任务中确认，已阻止文本写入"
        if not isinstance(args.get("value"), str):
            return False, "写入内容不是有效字符串"
        return True, "文本输入意图和目标应用匹配；继续执行写入前后验证"

    if name == "desktop_press":
        combo = str(args.get("combo", "")).strip().casefold()
        if forbids(request, "快捷键", "按键", "keyboard shortcut", "hotkey", combo):
            return False, "用户明确禁止使用快捷键或该按键，否定指令优先"
        terms = KEY_INTENTS.get(combo)
        if not terms or not has(request, *terms):
            return False, "用户任务没有明确授权该快捷键的具体用途"
        return True, "快捷键用途与用户任务匹配"

    return False, "未知副作用工具，默认拒绝"
