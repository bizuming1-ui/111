import os
import sys
import json
import subprocess
import requests
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = Path(os.environ.get(
    "AGENT_DESKTOP_EXE",
    str(Path.home() / "AppData/Roaming/npm/node_modules/agent-desktop/agent-desktop.exe")
))
if not TARGET.exists():
    TARGET = Path.home() / "AppData/Roaming/npm/node_modules/agent-desktop/bin/agent-desktop-win32-x64.exe"

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
SESSION = os.environ.get("AGENT_DESKTOP_SESSION", "")
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
SNAPSHOT_INDEX = {}

# 高风险控件：自动化一律禁止点击，必须由用户手动操作
HIGH_RISK_CLICK_TERMS = (
    "删除", "移除", "清空", "销毁", "卸载",
    "发送", "发布", "提交", "确认付款", "付款", "支付",
    "下单", "购买", "转账", "退款", "提现",
    "delete", "remove", "clear", "destroy", "uninstall",
    "send", "publish", "submit", "pay", "payment",
    "purchase", "buy", "checkout", "transfer", "refund",
    "withdraw", "place order"
)

SYSTEM_PROMPT = """你是 Windows 桌面自动化 Agent。
你只能通过提供的桌面工具操作电脑，不得要求执行任意 shell、PowerShell、Python 命令。
工作流程：先观察，再规划；每次只执行必要动作；界面变化后重新观察；验证操作结果。
不得擅自发送消息、删除文件、付款、提交订单、发布内容或执行其他不可逆操作。
遇到这些操作必须先向用户说明并请求明确确认。
不要把 delivered_unverified 当成成功，必须重新观察验证。
回答使用简体中文，简洁说明正在执行的步骤和结果。
回答使用简体中文，简洁说明正在执行的步骤和结果。

操作状态与完成判定规则：
1. 工具返回 ok=true 只表示执行器报告动作已执行，不代表用户目标已经达成。
2. observation_ok=true 只表示操作后成功取得新桌面快照，不代表预期效果已经发生。
3. effect_verified=true 仅允许在新快照或其他明确读取结果中找到与本次操作目标相符的证据时设置。
4. effect_verified=false 仅在有明确证据证明预期效果未发生或与预期相反时设置。
5. 缺少验证证据、无法判断或尚未检查时，effect_verified 必须保持 null。
6. 不得根据工具调用成功、observation_ok=true 或界面仍可见，就推断任务完成。
7. 对尚未验证效果的操作，应明确告知用户“动作已执行，但效果尚未验证”，并继续进行安全的只读检查；无法检查时如实说明。

工具选择与证据规则：
1. 用户要求列出、枚举或统计当前桌面窗口时，必须调用 desktop_windows。不得用 desktop_snapshot 或截图代替窗口枚举。
2. 用户要求读取某个窗口的控件、内容或界面状态时，调用 desktop_snapshot；需要视觉信息时才调用 desktop_screenshot。
3. 同时要求窗口清单和具体界面内容时，先调用 desktop_windows，再按需调用 desktop_snapshot。
4. desktop_windows 返回的是窗口列表（JSON 数组），必须逐项读取其中的 title、app_name、is_focused 等字段；不得把数组当作对象处理。
5. 只有工具实际返回的数据才能作为本轮事实依据。没有调用 desktop_windows，就不得声称已枚举全部窗口；快照或截图只能说明其实际覆盖的界面。
6. 不得把历史对话或旧日志中的错误描述为本轮工具调用错误。若工具没有返回某项信息，明确说明未取得，不得编造。
7. 工具返回空列表时如实报告；工具调用失败时报告本轮真实错误，不得推测原因。"""

TOOLS = [
    {"type": "function", "function": {
        "name": "desktop_snapshot",
        "description": "读取桌面应用的无障碍控件树。可指定 app、window_id 或 root 限定读取范围；界面变化后应重新快照。",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string", "description": "应用名称，可留空"},
            "window_id": {"type": "string", "description": "限定窗口 ID，可留空"},
            "root": {"type": "string", "description": "限定快照根控件引用，可留空"},
            "timeout_ms": {"type": "integer", "description": "读取超时，默认10000"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "desktop_get",
        "description": "只读获取最新成功快照中指定控件的属性。不得读取快照之外的引用。",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "最近一次成功快照中的完整控件引用"},
            "property": {"type": "string", "enum": ["value", "text", "title", "bounds", "role", "states"], "description": "读取属性，默认value"}
        }, "required": ["ref"]}
    }},
    {"type": "function", "function": {
        "name": "desktop_windows",
        "description": "列出当前桌面窗口。",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }},
    {"type": "function", "function": {
        "name": "desktop_screenshot",
        "description": "截取桌面屏幕，返回截图文件路径。",
        "parameters": {"type": "object", "properties": {
            "screen": {"type": "integer", "description": "屏幕编号，默认0"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "desktop_click",
        "description": "点击快照中观察到的唯一控件引用。界面变化后必须重新快照。",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "完整控件引用，例如 @snapshot:e12"}
        }, "required": ["ref"]}
    }},
    {"type": "function", "function": {
        "name": "desktop_set_value",
        "description": "安全设置指定应用中唯一文本编辑器的内容。必须提供应用、预期选中标签和预期原值；工具会重新快照并验证后才写入。",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "最近快照中的文本编辑器引用"},
            "value": {"type": "string", "description": "准备写入的新内容"},
            "app": {"type": "string", "description": "目标应用名称，例如 Notepad.exe"},
            "expected_selected_tab": {"type": "string", "description": "预期唯一选中标签的完整名称"},
            "expected_current_value": {"type": "string", "description": "预期编辑器当前原值；空白编辑器填写空字符串"}
        }, "required": ["ref", "value", "app", "expected_selected_tab", "expected_current_value"]}
    }},
    {"type": "function", "function": {
        "name": "desktop_press",
        "description": "发送受限键盘快捷键，例如 ctrl+t、return、escape、tab。",
        "parameters": {"type": "object", "properties": {
            "combo": {"type": "string"}
        }, "required": ["combo"]}
    }},

    {"type": "function", "function": {
        "name": "desktop_find",
        "description": "在最新控件快照中递归搜索控件名称、正文文本、value、title、description、角色或 Automation ID。可限定 app、window_id、root。只读操作。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词，不能为空"},
            "field": {"type": "string", "enum": ["all", "name", "role", "automation_id", "text", "value", "title", "description"], "description": "搜索字段，默认all"},
            "exact": {"type": "boolean", "description": "是否精确匹配，默认false"},
            "limit": {"type": "integer", "description": "最多返回匹配数，1至50，默认20"},
            "app": {"type": "string", "description": "限定应用名称，可留空"},
            "window_id": {"type": "string", "description": "限定窗口 ID，可留空"},
            "root": {"type": "string", "description": "限定快照根控件引用，可留空"}
        }, "required": ["query"]}
    }}
]

ALLOWED_KEYS = {
    "escape", "tab", "up", "down", "left", "right",
    "ctrl+t", "ctrl+l", "ctrl+tab", "ctrl+shift+tab",
    "home", "end"
}

# 任务授权基础设施：本阶段仅定义状态与分类，不接入执行入口。
READ_ONLY_TOOLS = frozenset({
    "desktop_snapshot", "desktop_get", "desktop_windows", "desktop_find"
})

SIDE_EFFECT_TOOLS = frozenset({
    "desktop_screenshot", "desktop_click", "desktop_set_value", "desktop_press"
})

TASK_AUTHORIZATION = None


def begin_task_authorization(user_request):
    """建立当前用户任务的授权上下文；不代表自动批准任何工具调用。"""
    global TASK_AUTHORIZATION
    if not isinstance(user_request, str) or not user_request.strip():
        raise ValueError("任务请求不能为空")
    TASK_AUTHORIZATION = {
        "active": True,
        "user_request": user_request.strip()
    }
    return True


def end_task_authorization():
    """结束当前任务授权上下文。"""
    global TASK_AUTHORIZATION
    TASK_AUTHORIZATION = None


def classify_tool_risk(name, args):
    """保守分类工具风险；分类结果本身不授予执行权限。"""
    if name in READ_ONLY_TOOLS:
        return "read_only"
    if name == "desktop_screenshot":
        return "local_side_effect"
    if name == "desktop_click":
        return "target_review"
    if name == "desktop_set_value":
        return "task_write"
    if name == "desktop_press":
        return "key_review"
    return "deny"


def authorize_tool_call(name, args):
    """根据用户原始任务意图限制副作用工具；不接受模型自授予权限。"""
    auth = TASK_AUTHORIZATION
    if not isinstance(auth, dict) or not auth.get("active"):
        return False, "当前没有有效任务授权"

    if name in READ_ONLY_TOOLS:
        return True, "只读操作"

    request = str(auth.get("user_request", "")).casefold()
    if not request.strip():
        return False, "任务意图为空"

    def has(*terms):
        return any(term.casefold() in request for term in terms)

    def explicitly_forbids(*terms):
        negations = (
            "不得", "禁止", "不允许", "不要", "严禁", "切勿",
            "勿", "不可", "不能", "无需", "不需要",
            "do not", "don't", "must not", "mustn't",
            "never", "without", "no"
        )
        resets = ("但允许", "但是允许", "不过允许",
                  "but allow", "however allow")
        for term in terms:
            term = str(term).casefold()
            if not term:
                continue
            pos = request.find(term)
            while pos >= 0:
                prefix = request[max(0, pos - 48):pos]
                for reset in resets:
                    if reset in prefix:
                        prefix = prefix.rsplit(reset, 1)[-1]
                if any(neg in prefix for neg in negations):
                    return True
                pos = request.find(term, pos + 1)
        return False

    read_only_only = has(
        "只允许使用只读工具", "只允许使用只读桌面工具",
        "仅允许使用只读工具", "仅允许只读操作",
        "只允许只读桌面工具", "只读模式",
        "only use read-only tools", "read-only tools only"
    )
    if name in SIDE_EFFECT_TOOLS and read_only_only:
        return False, "用户将任务限定为只读操作，禁止执行任何副作用工具"

    if name == "desktop_screenshot":
        if explicitly_forbids("截图", "截屏", "屏幕截图",
                              "屏幕画面", "screenshot", "screen capture",
                              "screen shot"):
            return False, "用户明确禁止截图，否定指令优先"
        if has("截图", "截屏", "屏幕截图", "屏幕画面", "screenshot", "screen capture"):
            return True, "用户明确要求查看屏幕图像"
        return False, "任务未明确要求截图；截图会覆盖 logs/latest-screen.png"

    if name == "desktop_click":
        if explicitly_forbids("点击", "单击", "click"):
            return False, "用户明确禁止点击，否定指令优先"
        ref = args.get("ref")
        target = SNAPSHOT_INDEX.get(ref) if isinstance(ref, str) else None
        if not isinstance(target, dict):
            return False, "点击目标不属于最新成功快照"

        label = " ".join(str(target.get(k, "")) for k in ("name", "role")).casefold()
        native_id = target.get("native_id")
        if isinstance(native_id, dict):
            label += " " + str(native_id.get("value", "")).casefold()
        else:
            label += " " + str(native_id or "").casefold()

        if any(term.casefold() in label for term in HIGH_RISK_CLICK_TERMS):
            return False, "目标属于高风险控件，禁止自动点击"

        if not has("打开", "点击", "单击", "选择", "进入", "切换", "启动",
                   "访问", "open", "click", "select", "switch", "launch", "navigate"):
            return False, "用户任务没有明确的点击或导航意图"

        aliases = (
            (("浏览器", "网页浏览器"), ("chrome", "msedge", "firefox", "browser")),
            (("记事本",), ("notepad",)),
            (("文件资源管理器", "资源管理器"), ("explorer", "file explorer")),
            (("新建标签", "新标签页"), ("new tab",)),
            (("搜索", "查找"), ("search", "find")),
            (("地址栏",), ("address bar",)),
            (("设置",), ("settings",)),
        )
        if any(any(x in request for x in left) and any(x in label for x in right)
               for left, right in aliases):
            return True, "点击目标与任务中的应用或操作意图匹配"

        target_name = str(target.get("name", "")).strip().casefold()
        if target_name and len(target_name) >= 2 and target_name in request:
            return True, "点击目标名称与用户任务匹配"

        return False, "无法确认点击目标属于用户指定任务，已阻止"

    if name == "desktop_set_value":
        if explicitly_forbids("输入", "写入", "填写", "编辑", "粘贴",
                              "录入", "type", "write", "fill", "enter",
                              "paste", "edit"):
            return False, "用户明确禁止文本输入或编辑，否定指令优先"
        if not has("输入", "写入", "填写", "编辑", "粘贴", "录入",
                   "type", "write", "fill", "enter", "paste", "edit"):
            return False, "用户任务没有明确的文本输入或编辑意图"

        app = args.get("app")
        if not isinstance(app, str) or not app.strip():
            return False, "缺少明确的目标应用"

        app_name = Path(app.strip()).name.casefold()
        aliases = (
            (("记事本",), ("notepad",)),
            (("浏览器",), ("chrome", "msedge", "firefox", "browser")),
            (("代码", "编辑器", "vscode"), ("code", "vscode")),
            (("文件资源管理器", "资源管理器"), ("explorer",)),
        )
        app_match = app_name in request or any(
            any(term in request for term in left)
            and any(term in app_name for term in right)
            for left, right in aliases
        )
        if not app_match:
            return False, "目标应用无法从用户任务中确认，已阻止文本写入"

        if not isinstance(args.get("value"), str):
            return False, "写入内容不是有效字符串"

        return True, "文本输入意图和目标应用匹配；继续执行原有写入前后验证"

    if name == "desktop_press":
        if explicitly_forbids("快捷键", "按键", "keyboard shortcut",
                              "hotkey", str(args.get("combo", "")).strip().casefold()):
            return False, "用户明确禁止使用快捷键或该按键，否定指令优先"
        combo = str(args.get("combo", "")).strip().casefold()
        key_intents = {
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
            "end": ("移到末尾", "跳到末尾", "end"),
        }
        terms = key_intents.get(combo)
        if not terms or not has(*terms):
            return False, "用户任务没有明确授权该快捷键的具体用途"
        return True, "快捷键用途与用户任务匹配"

    return False, "未知副作用工具，默认拒绝"


def log(event, data):
    from datetime import datetime
    item = {"time": datetime.now().isoformat(), "event": event, "data": data}
    with (LOG_DIR / "agent.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

def desktop(command, *args, timeout=30):
    if not TARGET.exists():
        return {"ok": False, "error": f"找不到 agent-desktop 可执行文件：{TARGET}"}
    cmd = [str(TARGET)]
    if SESSION:
        cmd += ["--session", SESSION]
    cmd += [command, *map(str, args)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        raw = p.stdout.strip()
        result = json.loads(raw) if raw else {
            "ok": False, "error": p.stderr[-2000:] or "CLI没有返回结果"
        }
        data = result.get("data") if isinstance(result, dict) else None
        disposition = data.get("disposition") if isinstance(data, dict) else None
        log("desktop." + command, {
            "arg_count": len(args),
            "ok": result.get("ok") if isinstance(result, dict) else None,
            "delivery": disposition.get("delivery") if isinstance(disposition, dict) else None
        })
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}

def run_tool(name, a):
    if name == "desktop_find":
        query = a.get("query", "")
        if not isinstance(query, str) or not query.strip():
            return {"ok": False, "error": "搜索关键词不能为空"}

        field = a.get("field", "all")
        allowed_fields = {
            "all", "name", "role", "automation_id",
            "text", "value", "title", "description"
        }
        if field not in allowed_fields:
            return {"ok": False, "error": "不支持的搜索字段"}

        exact = bool(a.get("exact", False))
        try:
            limit = max(1, min(int(a.get("limit", 20)), 50))
        except (TypeError, ValueError):
            return {"ok": False, "error": "limit必须是有效整数"}

        snapshot_args = {
            "app": a.get("app", ""),
            "window_id": a.get("window_id", ""),
            "root": a.get("root", ""),
            "timeout_ms": 20000
        }
        snapshot = run_tool("desktop_snapshot", snapshot_args)

        if not isinstance(snapshot, dict) or not snapshot.get("ok"):
            return {"ok": False, "error": "获取最新控件快照失败", "snapshot": snapshot}

        data = snapshot.get("data")
        tree = data.get("tree") if isinstance(data, dict) else None
        if not isinstance(tree, (dict, list)):
            return {"ok": False, "error": "快照中没有可遍历的控件树"}

        if isinstance(data, dict) and data.get("complete") is False:
            return {
                "ok": False,
                "error": "快照不完整，拒绝将部分搜索结果作为完整结果",
                "snapshot_id": data.get("snapshot_id")
            }

        needle = query.strip().casefold()
        matches = []
        candidates_by_field = {
            "name": lambda n: str(n.get("name") or ""),
            "role": lambda n: str(n.get("role") or ""),
            "automation_id": lambda n: str(
                (n.get("native_id") or {}).get("value", "")
                if isinstance(n.get("native_id"), dict)
                else (n.get("native_id") or "")
            ),
            "text": lambda n: " ".join(
                x for x in (str(n.get("name") or ""), str(n.get("value") or ""))
                if x
            ),
            "value": lambda n: str(n.get("value") or ""),
            "title": lambda n: str(n.get("title") or ""),
            "description": lambda n: " ".join(
                x for x in (
                    str(n.get("description") or ""),
                    str(n.get("role_description") or "")
                ) if x
            )
        }

        def walk(node):
            if len(matches) >= limit:
                return
            if isinstance(node, list):
                for child in node:
                    walk(child)
                    if len(matches) >= limit:
                        break
                return
            if not isinstance(node, dict):
                return

            fields = list(candidates_by_field) if field == "all" else [field]
            matched_fields = []
            for key in fields:
                candidate = candidates_by_field[key](node).casefold()
                if (candidate == needle) if exact else (needle in candidate):
                    matched_fields.append(key)

            if matched_fields:
                matches.append({
                    "ref_id": node.get("ref_id"),
                    "name": node.get("name"),
                    "value": node.get("value"),
                    "title": node.get("title"),
                    "description": node.get("description"),
                    "role": node.get("role"),
                    "native_id": node.get("native_id"),
                    "role_description": node.get("role_description"),
                    "states": node.get("states", []),
                    "available_actions": node.get("available_actions", []),
                    "matched_fields": matched_fields
                })

            walk(node.get("children", []))

        walk(tree)
        return {
            "ok": True,
            "query": query,
            "field": field,
            "exact": exact,
            "count": len(matches),
            "limit": limit,
            "snapshot_id": data.get("snapshot_id"),
            "complete": data.get("complete"),
            "matches": matches
        }

    if name == "desktop_snapshot":
        app = a.get("app", "")
        window_id = a.get("window_id", "")
        root = a.get("root", "")
        if not isinstance(app, str) or not isinstance(window_id, str) or not isinstance(root, str):
            return {"ok": False, "error": "app、window_id和root必须是字符串"}
        timeout = max(3000, min(int(a.get("timeout_ms", 10000)), 30000))
        args = ["--timeout-ms", str(timeout)]
        if app.strip():
            args += ["--app", app.strip()]
        if window_id.strip():
            args += ["--window-id", window_id.strip()]
        if root.strip():
            if not root.startswith("@") or ":" not in root or any(c.isspace() for c in root):
                return {"ok": False, "error": "root控件引用格式无效"}
            args += ["--root", root.strip()]
        args += ["--compact"]
        result = desktop("snapshot", *args, timeout=timeout / 1000 + 10)

        if isinstance(result, dict) and result.get("ok"):
            data = result.get("data", {})
            tree = data.get("tree") if isinstance(data, dict) else None
            snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None

            if isinstance(data, dict) and data.get("complete") is False:
                SNAPSHOT_INDEX.clear()
                return {
                    "ok": False,
                    "error": "快照不完整，已拒绝建立可操作控件索引",
                    "snapshot": result
                }

            if snapshot_id and isinstance(tree, (dict, list)):
                index = {}

                def node_identity(node):
                    return {
                        "name": node.get("name"),
                        "role": node.get("role"),
                        "native_id": node.get("native_id")
                    }

                def index_nodes(node, parent_path=None):
                    if parent_path is None:
                        parent_path = []

                    if isinstance(node, list):
                        for child in node:
                            index_nodes(child, parent_path)
                        return
                    if not isinstance(node, dict):
                        return

                    ref = node.get("ref_id")
                    if isinstance(ref, str):
                        index[ref] = {
                            "name": node.get("name", ""),
                            "role": node.get("role", ""),
                            "native_id": node.get("native_id"),
                            "role_description": node.get("role_description"),
                            "description": node.get("description"),
                            "title": node.get("title"),
                            "states": node.get("states", []),
                            "value": node.get("value"),
                            "available_actions": node.get("available_actions", []),
                            "parent_path": parent_path
                        }

                    child_path = parent_path + [node_identity(node)]
                    index_nodes(node.get("children", []), child_path)

                index_nodes(tree)
                SNAPSHOT_INDEX.clear()
                SNAPSHOT_INDEX.update(index)

        return result

    if name == "desktop_get":
        ref = a.get("ref")
        if not isinstance(ref, str) or not ref.startswith("@") or ":" not in ref or any(c.isspace() for c in ref):
            return {"ok": False, "error": "拒绝读取：控件引用格式无效"}
        if ref not in SNAPSHOT_INDEX:
            return {"ok": False, "error": "拒绝读取：引用不属于最近一次成功快照，请重新观察界面"}

        prop = a.get("property", "value")
        allowed_properties = {"value", "text", "title", "bounds", "role", "states"}
        if prop not in allowed_properties:
            return {"ok": False, "error": "不支持读取该控件属性"}
        return desktop("get", ref, "--property", prop, timeout=15)

    if name == "desktop_windows":
        return desktop("list-windows")

    if name == "desktop_screenshot":
        path = ROOT / "logs" / "latest-screen.png"
        result = desktop("screenshot", str(path), "--screen",
                         str(max(0, min(int(a.get("screen", 0)), 8))),
                         timeout=30)
        if result.get("ok"):
            result["saved_path"] = str(path)
            try:
                result["file_exists"] = path.is_file()
                result["file_size_bytes"] = path.stat().st_size
            except OSError as e:
                result["file_exists"] = False
                result["file_size_bytes"] = None
                result["file_metadata_error"] = str(e)
        return result

    if name == "desktop_click":
        ref = a["ref"].strip()
        if not ref.startswith("@") or ":" not in ref or any(c.isspace() for c in ref):
            return {"ok": False, "error": "拒绝无效控件引用"}

        target = SNAPSHOT_INDEX.get(ref)
        if target is None:
            return {"ok": False, "error": "拒绝点击：引用不属于最近一次成功快照，请重新观察界面"}

        actions = target.get("available_actions", [])
        if "Click" not in actions:
            return {
                "ok": False,
                "error": "拒绝点击：该控件没有 Click 操作权限",
                "control": target
            }

        label = " ".join(str(target.get(k, "")) for k in ("name", "role"))
        native_id = target.get("native_id")
        if isinstance(native_id, dict):
            label += " " + str(native_id.get("value", ""))
        label = label.casefold()

        risk_term = next(
            (term for term in HIGH_RISK_CLICK_TERMS if term.casefold() in label),
            None
        )
        if risk_term:
            return {
                "ok": False,
                "error": "高风险控件禁止自动点击，请由用户手动操作",
                "matched_term": risk_term,
                "control": target
            }

        result = desktop("click", ref)
        verification = run_tool("desktop_snapshot", {})
        result["verification"] = verification
        result["effect_verified"] = None

        if not isinstance(verification, dict) or not verification.get("ok"):
            result["observation_ok"] = False
            result["target_present"] = None
            result["match_count"] = None
            result["state_changed"] = None
            result["value_changed"] = None
            result["actions_changed"] = None
            result["verification_error"] = "点击后的界面快照失败，操作结果未经验证"
            return result

        result["observation_ok"] = True
        data = verification.get("data", {})
        tree = data.get("tree") if isinstance(data, dict) else None

        if not isinstance(tree, (dict, list)):
            result["target_present"] = None
            result["match_count"] = None
            result["state_changed"] = None
            result["value_changed"] = None
            result["actions_changed"] = None
            result["verification_error"] = "点击后的快照没有可用控件树"
            return result

        matches = []
        old_parent_path = target.get("parent_path")

        def node_identity(node):
            return {
                "name": node.get("name"),
                "role": node.get("role"),
                "native_id": node.get("native_id")
            }

        def find_target(node, parent_path=None):
            if parent_path is None:
                parent_path = []
            if isinstance(node, list):
                for child in node:
                    find_target(child, parent_path)
                return
            if not isinstance(node, dict):
                return

            same_identity = (
                node.get("role") == target.get("role")
                and node.get("name") == target.get("name")
                and node.get("native_id") == target.get("native_id")
            )
            if (
                isinstance(old_parent_path, list)
                and same_identity
                and parent_path == old_parent_path
            ):
                matches.append(node)

            child_path = parent_path + [node_identity(node)]
            find_target(node.get("children", []), child_path)

        find_target(tree)
        result["match_count"] = len(matches)

        if len(matches) == 0:
            result["target_present"] = False
            result["state_changed"] = None
            result["value_changed"] = None
            result["actions_changed"] = None
        elif len(matches) > 1:
            result["target_present"] = None
            result["state_changed"] = None
            result["value_changed"] = None
            result["actions_changed"] = None
            result["verification_error"] = "点击后目标控件匹配存在歧义"
        else:
            observed = matches[0]
            result["target_present"] = True
            result["state_changed"] = (
                target.get("states", []) != observed.get("states", [])
            )
            result["value_changed"] = (
                target.get("value") != observed.get("value")
            )
            result["actions_changed"] = (
                target.get("available_actions", [])
                != observed.get("available_actions", [])
            )

        return result

    if name == "desktop_set_value":
        ref, value = a["ref"].strip(), a["value"]
        if not ref.startswith("@") or ":" not in ref or any(c.isspace() for c in ref):
            return {"ok": False, "error": "拒绝无效控件引用"}

        target = SNAPSHOT_INDEX.get(ref)
        if target is None:
            return {"ok": False, "error": "拒绝设置：引用不属于最近一次成功快照，请重新观察界面"}

        actions = target.get("available_actions", [])
        if "SetValue" not in actions:
            return {
                "ok": False,
                "error": "拒绝设置：该控件没有 SetValue 操作权限",
                "control": target
            }

        if not isinstance(value, str):
            return {"ok": False, "error": "输入值必须是字符串"}
        if len(value) > 10000:
            return {"ok": False, "error": "输入内容超过10000字符限制"}

        app = a.get("app")
        expected_tab = a.get("expected_selected_tab")
        expected_current = a.get("expected_current_value")

        if not isinstance(app, str) or not app.strip():
            return {"ok": False, "error": "拒绝设置：必须明确指定目标应用"}
        if not isinstance(expected_tab, str) or not expected_tab:
            return {"ok": False, "error": "拒绝设置：必须提供预期选中标签名称"}
        if not isinstance(expected_current, str):
            return {"ok": False, "error": "拒绝设置：必须提供预期编辑器原值"}
        if target.get("role") != "textfield":
            return {"ok": False, "error": "拒绝设置：原引用不是文本编辑器"}

        original_target = target
        preflight = run_tool("desktop_snapshot", {"app": app})
        if not isinstance(preflight, dict) or not preflight.get("ok"):
            return {"ok": False, "error": "拒绝设置：写入前重新快照失败", "preflight": preflight}

        pre_data = preflight.get("data", {})
        if not isinstance(pre_data, dict) or pre_data.get("complete") is not True:
            return {"ok": False, "error": "拒绝设置：写入前快照不完整"}

        pre_tree = pre_data.get("tree")
        selected_tabs = []
        editors = []

        def collect_preflight(node):
            if isinstance(node, list):
                for child in node:
                    collect_preflight(child)
                return
            if not isinstance(node, dict):
                return

            if node.get("role") == "tab" and "selected" in (node.get("states") or []):
                selected_tabs.append(node)

            if node.get("role") == "textfield" and "SetValue" in (node.get("available_actions") or []):
                editors.append(node)

            collect_preflight(node.get("children", []))

        collect_preflight(pre_tree)

        if len(selected_tabs) != 1:
            return {
                "ok": False,
                "error": "拒绝设置：无法唯一确认当前选中标签",
                "selected_tab_count": len(selected_tabs)
            }

        selected_tab = selected_tabs[0]
        if selected_tab.get("name") != expected_tab:
            return {
                "ok": False,
                "error": "拒绝设置：当前选中标签与预期不一致",
                "expected_selected_tab": expected_tab,
                "observed_selected_tab": selected_tab.get("name")
            }

        if len(editors) != 1:
            return {
                "ok": False,
                "error": "拒绝设置：可写文本编辑器不是唯一的",
                "editor_count": len(editors)
            }

        fresh_editor = editors[0]
        if (
            fresh_editor.get("name") != original_target.get("name")
            or fresh_editor.get("role") != original_target.get("role")
            or fresh_editor.get("native_id") != original_target.get("native_id")
        ):
            return {"ok": False, "error": "拒绝设置：重新快照中的编辑器身份与原引用不一致"}

        fresh_ref = fresh_editor.get("ref_id")
        if not isinstance(fresh_ref, str) or fresh_ref not in SNAPSHOT_INDEX:
            return {"ok": False, "error": "拒绝设置：重新快照未提供有效的新控件引用"}

        current_read = run_tool("desktop_get", {"ref": fresh_ref})
        current_data = current_read.get("data", {}) if isinstance(current_read, dict) else {}
        if not isinstance(current_read, dict) or not current_read.get("ok") or not isinstance(current_data, dict) or not isinstance(current_data.get("value"), str):
            return {
                "ok": False,
                "error": "拒绝设置：无法从当前编辑器读取真实原值",
                "current_read": current_read
            }

        observed_current = current_data["value"]
        if observed_current != expected_current:
            return {
                "ok": False,
                "error": "拒绝设置：编辑器当前原值与预期不一致",
                "expected_current_value": expected_current,
                "observed_current_value": observed_current
            }

        target = SNAPSHOT_INDEX[fresh_ref]
        if "SetValue" not in target.get("available_actions", []):
            return {"ok": False, "error": "拒绝设置：新快照中的编辑器没有 SetValue 权限"}

        result = desktop("set-value", fresh_ref, value)
        result["preflight_verified"] = True
        result["fresh_ref"] = fresh_ref
        result["verified_selected_tab"] = expected_tab
        verification = run_tool("desktop_snapshot", {"app": app})
        result["verification"] = verification

        if not isinstance(verification, dict) or not verification.get("ok"):
            result["observation_ok"] = False
            result["effect_verified"] = None
            result["verification_error"] = "设置后的界面快照失败，操作结果未经验证"
            return result

        result["observation_ok"] = True
        result["effect_verified"] = None

        if not result.get("ok"):
            result["verification_error"] = "执行器未确认写入成功"
            return result

        verification_data = verification.get("data", {})
        if (
            not isinstance(verification_data, dict)
            or verification_data.get("complete") is not True
        ):
            result["verification_error"] = "写入后快照不完整，无法安全读回"
            return result

        tree = verification_data.get("tree")
        matches = []

        def find_target(node):
            if isinstance(node, list):
                for child in node:
                    find_target(child)
                return
            if not isinstance(node, dict):
                return

            same_identity = (
                node.get("role") == target.get("role")
                and node.get("name") == target.get("name")
                and node.get("native_id") == target.get("native_id")
            )
            if same_identity:
                matches.append(node)

            find_target(node.get("children", []))

        find_target(tree)

        if len(matches) != 1:
            result["verification_error"] = (
                "新快照中目标控件不存在或不唯一，无法安全读回"
            )
            return result

        readback_ref = matches[0].get("ref_id")
        if not isinstance(readback_ref, str) or readback_ref not in SNAPSHOT_INDEX:
            result["verification_error"] = "写入后快照未提供有效的新控件引用，无法读回"
            return result

        readback = run_tool("desktop_get", {"ref": readback_ref})
        readback_data = readback.get("data", {}) if isinstance(readback, dict) else {}
        if not isinstance(readback, dict) or not readback.get("ok") or not isinstance(readback_data, dict) or not isinstance(readback_data.get("value"), str):
            result["verification_error"] = "写入后无法从 UIA 读取编辑器真实 value"
            result["readback"] = readback
            return result

        observed = readback_data["value"]
        result["readback_value"] = observed
        result["readback_ref"] = readback_ref

        if observed == value:
            result["effect_verified"] = True
            result["verification_scope"] = "fresh_uia_value_readback"
        else:
            result["effect_verified"] = False
            result["verification_error"] = "读回值与目标值不一致"

        return result

    if name == "desktop_press":
        combo = a["combo"].strip().lower()
        if combo not in ALLOWED_KEYS:
            return {"ok": False, "error": f"快捷键不在白名单中：{combo}"}

        before = run_tool("desktop_snapshot", {})
        before_summary = None

        def summarize_tree(root):
            summary = []

            def walk(node, parent_path=None):
                if parent_path is None:
                    parent_path = []
                if isinstance(node, list):
                    for child in node:
                        walk(child, parent_path)
                    return
                if not isinstance(node, dict):
                    return

                identity = {
                    "name": node.get("name"),
                    "role": node.get("role"),
                    "native_id": node.get("native_id")
                }
                summary.append({
                    "path": parent_path + [identity],
                    "states": node.get("states", []),
                    "value": node.get("value"),
                    "available_actions": node.get("available_actions", [])
                })
                walk(node.get("children", []), parent_path + [identity])

            walk(root)
            return summary

        if isinstance(before, dict) and before.get("ok"):
            before_data = before.get("data", {})
            before_tree = before_data.get("tree") if isinstance(before_data, dict) else None
            if isinstance(before_tree, (dict, list)):
                before_summary = summarize_tree(before_tree)

        result = desktop("press", combo, timeout=15)
        verification = run_tool("desktop_snapshot", {})
        result["verification"] = verification
        result["effect_verified"] = None
        result["before_observation_ok"] = before_summary is not None

        if not isinstance(verification, dict) or not verification.get("ok"):
            result["observation_ok"] = False
            result["ui_changed"] = None
            result["verification_error"] = "按键后的界面快照失败，操作结果未经验证"
            return result

        result["observation_ok"] = True
        after_data = verification.get("data", {})
        after_tree = after_data.get("tree") if isinstance(after_data, dict) else None

        if not isinstance(after_tree, (dict, list)):
            result["ui_changed"] = None
            result["verification_error"] = "按键后的快照没有可用控件树"
            return result

        after_summary = summarize_tree(after_tree)
        result["before_control_count"] = (
            len(before_summary) if before_summary is not None else None
        )
        result["after_control_count"] = len(after_summary)
        result["ui_changed"] = (
            before_summary != after_summary
            if before_summary is not None else None
        )

        if before_summary is None:
            result["verification_error"] = "按键前快照不可用，无法比较界面变化"

        return result

    return {"ok": False, "error": "工具不在白名单中"}

def call_model(messages):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    response = requests.post(
        API_URL,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "tools": TOOLS,
              "tool_choice": "auto", "thinking": {"type": "disabled"},
              "max_tokens": 2048},
        timeout=90
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]

def _agent_turn_impl(history, user_text):
    history.append({"role": "user", "content": user_text})
    repeated_failures = {}

    # 不设置固定交互轮数上限；相同工具调用连续失败 3 次时熔断。
    while True:
        message = call_model(history)
        history.append(message)
        calls = message.get("tool_calls") or []

        if not calls:
            return message.get("content") or "任务结束。"

        stop_after_batch = False

        for call in calls:
            name = call["function"]["name"]
            args = {}

            if stop_after_batch:
                result = {
                    "ok": False,
                    "blocked": True,
                    "error": "相同工具调用连续失败达到 3 次，已停止本批次后续工具调用。"
                }
            else:
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                    allowed, auth_reason = authorize_tool_call(name, args)
                    if not allowed:
                        result = {"ok": False, "blocked": True, "error": auth_reason}
                    else:
                        result = run_tool(name, args)
                except Exception as e:
                    result = {"ok": False, "error": str(e)}

                signature = name + "|" + json.dumps(
                    args, sort_keys=True, ensure_ascii=False, default=str
                )

                if isinstance(result, dict) and result.get("ok") is True:
                    repeated_failures.pop(signature, None)
                else:
                    repeated_failures[signature] = repeated_failures.get(signature, 0) + 1
                    if repeated_failures[signature] >= 3:
                        stop_after_batch = True

            log("tool.result", {
                "tool": name,
                "ok": result.get("ok") if isinstance(result, dict) else None,
                "effect_verified": result.get("effect_verified") if isinstance(result, dict) else None,
                "verification_scope": result.get("verification_scope") if isinstance(result, dict) else None,
                "observation_ok": result.get("observation_ok") if isinstance(result, dict) else None,
                "verification_error": result.get("verification_error") if isinstance(result, dict) else None
            })

            history.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False)[:18000]
            })

        if stop_after_batch:
            return "检测到相同工具调用连续失败 3 次，已触发熔断。请检查工具错误后重试。"



def agent_turn(history, user_text):
    begin_task_authorization(user_text)
    try:
        return _agent_turn_impl(history, user_text)
    finally:
        end_task_authorization()

def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误：当前 PowerShell 会话没有 DEEPSEEK_API_KEY。")
        return
    print("DeepSeek Desktop Agent MVP")
    print("输入自然语言指令；输入 /exit 退出；输入 /stop 急停当前会话。")
    print("桌面执行器：", TARGET)
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    while True:
        try:
            text = input("\n你 > ").strip()
            if not text:
                continue
            if text.lower() in ("/exit", "exit", "quit"):
                break
            if text.lower() == "/stop":
                end_task_authorization()
                history = [{"role": "system", "content": SYSTEM_PROMPT}]
                print("已清空 Agent 对话上下文；不会终止已发送给系统的动作。")
                continue
            try:
                answer = agent_turn(history, text)
                print("\nAgent >", answer)
                log("assistant", {"reply": answer})
            except requests.HTTPError as e:
                print("DeepSeek API 错误：", str(e))
                if e.response is not None:
                    print(e.response.text[:1000])
            except Exception as e:
                print("执行错误：", repr(e))
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            break

if __name__ == "__main__":
    main()
