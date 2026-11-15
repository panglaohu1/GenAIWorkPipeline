"""调用 TigerInBamboo 能力代码（JavaScript）的桥：一次调用起一个 Node 子进程跑 bridge.mjs。

能力代码原样从 TigerInBamboo 目录 import（config [capabilities] reacode / workshop），不复制进本仓库：
那边新学到的能力登记进 registry.js / capabilities.js 后，这里下一次扫描就能用上。

Watchdog：子进程每个阶段往 stderr 写一行心跳；超过 idle 秒没有心跳、或超过 total 秒，杀掉子进程，重试一次。
Node 或能力目录不在时 available() 为 False，调用方跳过外部能力（管线照常跑，记一条提示）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import config

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "bridge.mjs"
DEFAULT_ROOTS = {
    "reacode": "/Users/panglaohu/Downloads/TigerInBamboo/townscaper-web/REACodeRefactoring/src/capabilities",
    "workshop": "/Users/panglaohu/Downloads/TigerInBamboo/TigerMessenger/src/system-workshop",
}
NODE_CANDIDATES = ("envs/node/bin/node",            # scripts/setup_node.command 装的（官方二进制，校验过）
                   "/opt/homebrew/bin/node", "/usr/local/bin/node", "~/.local/bin/node", "~/.volta/bin/node",
                   "/opt/local/bin/node", "/usr/bin/node")


class BridgeError(RuntimeError):
    pass


def node_path() -> str | None:
    cfg = config.get("capabilities.node") or os.environ.get("GENAI_NODE")
    for c in ([cfg] if cfg else []) + ["node"]:
        p = shutil.which(c) if c and "/" not in c else (c if c and Path(c).exists() else None)
        if p:
            return p
    for c in NODE_CANDIDATES:
        p = config.resolve(Path(c).expanduser())
        if p.exists():
            return str(p)
    return None


def roots() -> dict[str, str]:
    r = dict(DEFAULT_ROOTS)
    for k in r:
        v = os.environ.get(f"GENAI_CAP_{k.upper()}") or config.get(f"capabilities.{k}")   # 环境变量优先（测试用）
        if v:
            r[k] = str(config.resolve(v))
    return r


def available() -> tuple[bool, str]:
    node = node_path()
    if not node:
        return False, "找不到 Node：双击 scripts/setup_node.command 安装到 envs/node（或在 config [capabilities] node 写路径）"
    r = roots()
    missing = [k for k, p in r.items() if not Path(p).is_dir()]
    if missing:
        return False, "能力目录不在：" + "、".join(f"{k}={r[k]}" for k in missing)
    return True, node


def _payload(op: str, data: dict) -> bytes:
    return json.dumps({"op": op, "roots": roots(), **data}, ensure_ascii=False).encode()


def _parse(out: bytes, err: bytes, op: str) -> Any:
    try:
        res = json.loads(out.decode() or "{}")
    except json.JSONDecodeError:
        raise BridgeError(f"{op}：Node 输出无法解析：{out[:300]!r} {err[-500:]!r}")
    if not res.get("ok"):
        raise BridgeError(f"{op}：{res.get('error', '未知错误')}")
    return res.get("result")


async def call(op: str, data: dict | None = None, *, total: float | None = None, idle: float | None = None,
               emit=None) -> Any:
    """异步调用（管线里用）：心跳 idle 秒无更新或总时长超 total → 杀进程，重试一次。"""
    ok, node = available()
    if not ok:
        raise BridgeError(node)
    total = float(total or config.get("capabilities.timeout_s", 60))
    idle = float(idle or config.get("capabilities.idle_s", 20))
    payload = _payload(op, data or {})
    for attempt in (1, 2):
        proc = await asyncio.create_subprocess_exec(node, str(SCRIPT), stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        last = [asyncio.get_running_loop().time()]
        err_buf = bytearray()

        async def pump_err():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                last[0] = asyncio.get_running_loop().time()
                err_buf.extend(line)
                if emit:
                    try:
                        hb = json.loads(line)
                        if hb.get("heartbeat"):
                            emit({"type": "beat", "name": op, "stage": hb.get("stage")})
                    except json.JSONDecodeError:
                        pass

        async def run():
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
            t_err = asyncio.create_task(pump_err())
            out = await proc.stdout.read()
            await proc.wait()
            await t_err
            return out

        task = asyncio.create_task(run())
        t0 = asyncio.get_running_loop().time()
        why = None
        while not task.done():
            await asyncio.sleep(0.05)
            now = asyncio.get_running_loop().time()
            if now - t0 > total:
                why = f"{op} 超过 {total:g} 秒"
            elif now - last[0] > idle:
                why = f"{op} {idle:g} 秒无心跳"
            if why:
                proc.kill()
                task.cancel()
                break
        if why is None:
            return _parse(task.result(), bytes(err_buf), op)
        if emit:
            emit({"type": "recover", "name": op, "why": why, "attempt": attempt})
        if attempt == 2:
            raise BridgeError(why + "（已重试一次）")


def call_sync(op: str, data: dict | None = None, *, total: float = 60) -> Any:
    """同步调用（Blender 工作进程里用，那里没有事件循环）：超时重试一次。"""
    ok, node = available()
    if not ok:
        raise BridgeError(node)
    payload = _payload(op, data or {})
    for attempt in (1, 2):
        try:
            p = subprocess.run([node, str(SCRIPT)], input=payload, capture_output=True, timeout=total)
            return _parse(p.stdout, p.stderr, op)
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise BridgeError(f"{op} 两次都超过 {total:g} 秒")
