"""MCP 客户端集线器：为一次运行启动 Blender/Godot MCP 服务端，汇总工具并转发调用。"""
from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import ENVS, ROOT
from .compute import compute

SERVERS = {
    "blender": "genai_pipeline.blender.mcp_server",
    "godot": "genai_pipeline.godot.mcp_server",
}


class MCPHub:
    def __init__(self, run_dir: Path, godot_project: Path | None = None, servers: list[str] | None = None) -> None:
        self.run_dir = run_dir
        self.godot_project = godot_project
        self.wanted = servers or list(SERVERS)
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, tuple[str, dict]] = {}  # tool name -> (server, tool def)
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "MCPHub":
        try:
            return await self._open()
        except BaseException:
            await self._stack.aclose()
            raise

    async def _open(self) -> "MCPHub":
        env = {**os.environ, "GENAI_RUN_DIR": str(self.run_dir), "PYTHONPATH": str(ROOT / "src")}
        if self.godot_project:
            env["GENAI_GODOT_PROJECT"] = str(self.godot_project)
        errlog = open(ROOT / "logs" / "mcp-servers.log", "a")
        self._stack.callback(errlog.close)
        for name in self.wanted:
            params = StdioServerParameters(command=str(ENVS["main"]), args=["-m", SERVERS[name]], env=env, cwd=ROOT)
            read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session
            listed = await session.list_tools()
            for t in listed.tools:
                self.tools[t.name] = (name, {"name": t.name, "description": t.description or "",
                                             "parameters": t.input_schema or {"type": "object", "properties": {}}})
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    def openai_tools(self, prefixes: tuple[str, ...] | None = None) -> list[dict]:
        out = []
        for name, (_, t) in self.tools.items():
            if prefixes and not name.startswith(prefixes):
                continue
            out.append({"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": _clean_schema(t["parameters"])}})
        return out

    def schema(self, name: str) -> dict | None:
        return self.tools[name][1]["parameters"] if name in self.tools else None

    async def call(self, name: str, args: dict[str, Any], timeout: float = 900) -> dict:
        """调用工具，返回解析后的 dict（服务端返回 JSON 文本；非 JSON 时包装为 {"text": ...}）。"""
        server, _ = self.tools[name]
        res = await compute.run(name, lambda: self.sessions[server].call_tool(name, args, read_timeout_seconds=timeout))
        texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
        raw = "\n".join(texts)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"text": raw}
        if res.is_error:
            data = {"ok": False, "error": raw[:4000]}
        return data


def _clean_schema(s: dict) -> dict:
    """去掉 title 等冗余字段，减少提示 token。"""
    if isinstance(s, dict):
        return {k: _clean_schema(v) for k, v in s.items() if k not in ("title",)}
    if isinstance(s, list):
        return [_clean_schema(x) for x in s]
    return s


if __name__ == "__main__":  # 手动自检: python -m genai_pipeline.mcp_hub
    import asyncio

    async def _main():
        async with MCPHub(ROOT / "runs" / "_selftest") as hub:
            print(json.dumps([t["function"]["name"] for t in hub.openai_tools()], indent=1))
            print(await hub.call("blender_scene_info", {}))
            print(await hub.call("godot_project_files", {}))

    asyncio.run(_main())
    sys.exit(0)
