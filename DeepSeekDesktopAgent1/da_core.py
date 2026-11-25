# -*- coding: utf-8 -*-
"""da_core.py — DeepSeekDesktopAgent 核心层

职责：
  1. 定位并调用 agent-desktop 原生可执行文件（Windows UI Automation）
  2. 统一规范化 JSON 信封（envelope），永远返回 dict，永远带 ok/error.disposition
  3. 防篡改审计日志（SHA-256 哈希链），日志中不记录明文输入内容
  4. 控件树遍历、快照索引等公共工具
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

ROOT = Path(os.environ.get("DESKAGENT_HOME") or Path(__file__).resolve().parent)
LOG_DIR = ROOT / "logs"
STOP_FILE = ROOT / "STOP"
GENESIS = "0" * 64


# --------------------------------------------------------------------------
# 统一错误信封
# --------------------------------------------------------------------------
def fail(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    """客户端侧拒绝/失败：动作从未发送给桌面，因此 not_delivered / retry=safe。"""
    r: Dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "disposition": {"delivery": "not_delivered", "retry": "safe"},
        },
    }
    r.update(extra)
    return r


def blocked(reason: str, **extra: Any) -> Dict[str, Any]:
    return fail("BLOCKED_BY_POLICY", reason, blocked=True, **extra)


def disposition_of(result: Any) -> Tuple[Optional[str], Optional[str]]:
    """从成功或失败信封里取 (delivery, retry)。"""
    if not isinstance(result, dict):
        return None, None
    for holder in (result.get("data"), result.get("error")):
        if isinstance(holder, dict):
            d = holder.get("disposition")
            if isinstance(d, dict):
                return d.get("delivery"), d.get("retry")
    return None, None


def digest(text: Any) -> Dict[str, Any]:
    """日志脱敏：只记录长度和哈希前缀，不记录明文。"""
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return {"len": len(s), "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]}


# --------------------------------------------------------------------------
# 定位 agent-desktop.exe
# --------------------------------------------------------------------------
def find_agent_desktop() -> Optional[Path]:
    """查找原生 agent-desktop 可执行文件（不使用 .cmd 垫片，避免参数被 cmd.exe 二次解析）。"""
    cands: List[Path] = []
    env = os.environ.get("AGENT_DESKTOP_EXE")
    if env:
        cands.append(Path(env))
    w = shutil.which("agent-desktop.exe")
    if w:
        cands.append(Path(w))

    roots: List[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules" / "agent-desktop")
    roots.append(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "agent-desktop")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        try:
            p = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=20)
            if p.returncode == 0 and p.stdout.strip():
                roots.append(Path(p.stdout.strip()) / "agent-desktop")
        except Exception:
            pass

    for r in roots:
        cands.append(r / "agent-desktop.exe")
        cands.append(r / "bin" / "agent-desktop-win32-x64.exe")
        cands.append(r / "bin" / "agent-desktop-win32-arm64.exe")
    for r in roots:
        if r.is_dir():
            try:
                cands.extend(sorted(r.rglob("agent-desktop*.exe")))
            except OSError:
                pass
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# 防篡改审计日志（哈希链）
# --------------------------------------------------------------------------
def _canon(rec: Dict[str, Any]) -> str:
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    """每条记录包含 prev 与 hash=SHA256(规范化JSON)。任何一行被改、删、插都会被 verify_audit 检出。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq, self._prev = self._load_tail()

    def _load_tail(self) -> Tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if not last:
            return 0, GENESIS
        try:
            rec = json.loads(last)
            return int(rec.get("seq", 0)), str(rec.get("hash", GENESIS))
        except Exception:
            return 0, GENESIS

    def write(self, event: str, data: Dict[str, Any]) -> None:
        with self._lock:
            rec = {
                "seq": self._seq + 1,
                "time": datetime.now().isoformat(timespec="milliseconds"),
                "event": event,
                "data": data,
                "prev": self._prev,
            }
            h = hashlib.sha256(_canon(rec).encode("utf-8")).hexdigest()
            rec["hash"] = h
            with self.path.open("a", encoding="utf-8") as f:
                f.write(_canon(rec) + "\n")
            self._seq += 1
            self._prev = h


def verify_audit(path: Path) -> Tuple[bool, int, str]:
    path = Path(path)
    if not path.exists():
        return False, 0, "日志文件不存在"
    prev, n = GENESIS, 0
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                return False, n, f"第 {i} 行不是有效 JSON"
            h = rec.pop("hash", None)
            if rec.get("prev") != prev:
                return False, n, f"第 {i} 行 prev 链断裂（有记录被删除或插入）"
            if hashlib.sha256(_canon(rec).encode("utf-8")).hexdigest() != h:
                return False, n, f"第 {i} 行哈希不匹配（内容被篡改）"
            prev, n = h, n + 1
    return True, n, "哈希链完整"


# --------------------------------------------------------------------------
# agent-desktop 调用器
# --------------------------------------------------------------------------
class Desktop:
    """每次调用都返回 dict 信封；绝不抛异常；超时 => delivery=unknown（动作可能已落地，禁止盲目重试）。"""

    def __init__(self, cmd_prefix: List[str], session: str = "", audit: Optional[AuditLog] = None,
                 env: Optional[Dict[str, str]] = None):
        self.prefix = list(cmd_prefix)
        self.session = session
        self.audit = audit
        self.env = env

    def run(self, command: str, *args: Any, timeout: float = 30, stdin: Optional[str] = None,
            headed: bool = False) -> Dict[str, Any]:
        cmd = list(self.prefix)
        if self.session:
            cmd += ["--session", self.session]
        if headed:
            cmd.append("--headed")
        cmd += [command, *[str(a) for a in args]]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=self.env)
        except FileNotFoundError:
            return self._done(command, args, t0, fail("EXECUTABLE_NOT_FOUND",
                              f"找不到 agent-desktop 可执行文件：{self.prefix}"))
        except subprocess.TimeoutExpired:
            return self._done(command, args, t0, {"ok": False, "error": {
                "code": "CLIENT_TIMEOUT", "message": f"{command} 超过 {timeout}s 未返回；动作可能已落地，请先观察再决定",
                "disposition": {"delivery": "unknown", "retry": "unknown"}}})
        except OSError as e:
            return self._done(command, args, t0, fail("EXECUTABLE_START_FAILED", str(e)))

        result = self._parse(p.stdout, p.stderr, p.returncode)
        result["_exit_code"] = p.returncode
        return self._done(command, args, t0, result)

    @staticmethod
    def _parse(stdout: str, stderr: str, code: int) -> Dict[str, Any]:
        raw = (stdout or "").strip()
        obj: Any = None
        if raw:
            try:
                obj = json.loads(raw)
            except Exception:
                for line in reversed(raw.splitlines()):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            obj = json.loads(line)
                            break
                        except Exception:
                            continue
        if not isinstance(obj, dict):
            return {"ok": False, "error": {
                "code": "BAD_OUTPUT", "message": "agent-desktop 未返回有效 JSON 信封",
                "stderr_tail": (stderr or "")[-1500:], "stdout_head": raw[:300], "exit_code": code,
                "disposition": {"delivery": "unknown", "retry": "unknown"}}}
        if isinstance(obj.get("error"), str):
            obj["error"] = {"code": "ERROR", "message": obj["error"],
                            "disposition": {"delivery": "unknown", "retry": "unknown"}}
        ver = str(obj.get("version", ""))
        if ver and not ver.startswith("2."):
            obj["contract_warning"] = f"未识别的 agent-desktop 契约版本 {ver}（本程序按 2.x 编写）"
        return obj

    def _done(self, command: str, args: Tuple[Any, ...], t0: float, result: Dict[str, Any]) -> Dict[str, Any]:
        if self.audit:
            delivery, retry = disposition_of(result)
            err = result.get("error")
            self.audit.write("desktop." + command, {
                "arg_count": len(args), "ok": result.get("ok"), "delivery": delivery, "retry": retry,
                "error_code": err.get("code") if isinstance(err, dict) else None,
                "ms": int((time.time() - t0) * 1000)})
        return result


# --------------------------------------------------------------------------
# 上下文
# --------------------------------------------------------------------------
class Context:
    def __init__(self, desktop: Desktop, audit: AuditLog, root: Path = ROOT, dry_run: bool = False):
        self.desktop = desktop
        self.audit = audit
        self.root = Path(root)
        self.dry_run = dry_run
        self.snapshot_index: Dict[str, Dict[str, Any]] = {}
        self.request: Optional[str] = None
        self.sleep = time.sleep
        self.receipts: List[Dict[str, Any]] = []
        self.launch_resolver = None  # 测试可注入

    def begin(self, request: str) -> None:
        if not isinstance(request, str) or not request.strip():
            raise ValueError("任务请求不能为空")
        self.request = request.strip()
        self.receipts = []

    def end(self) -> None:
        self.request = None


# --------------------------------------------------------------------------
# 控件树工具
# --------------------------------------------------------------------------
def walk_nodes(tree: Any, ancestors: Optional[List[Dict[str, Any]]] = None) -> Iterator[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """深度优先遍历，yield (节点, 祖先节点列表)。"""
    if ancestors is None:
        ancestors = []
    if isinstance(tree, list):
        for c in tree:
            yield from walk_nodes(c, ancestors)
        return
    if not isinstance(tree, dict):
        return
    yield tree, ancestors
    kids = tree.get("children")
    if kids:
        yield from walk_nodes(kids, ancestors + [tree])


def node_states(node: Dict[str, Any]) -> List[str]:
    s = node.get("states")
    return [str(x) for x in s] if isinstance(s, list) else []


def native_id_value(node: Dict[str, Any]) -> str:
    nid = node.get("native_id")
    if isinstance(nid, dict):
        return str(nid.get("value", ""))
    return str(nid or "")


def identity(node: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": node.get("name"), "role": node.get("role"), "native_id": node.get("native_id")}


def same_identity(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return a.get("role") == b.get("role") and a.get("name") == b.get("name") and a.get("native_id") == b.get("native_id")


def valid_ref(ref: Any) -> bool:
    return isinstance(ref, str) and ref.startswith("@") and ":" in ref and not any(c.isspace() for c in ref)
