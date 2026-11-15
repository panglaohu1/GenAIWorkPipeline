"""Agent 循环：LLM ⇄ MCP 工具。

要点（针对 27B 本地模型做的稳健性设计）:
- 工具参数用 JSON Schema 校验，错误作为工具结果返回让模型自我修正
- 工具结果里的 images（渲染图/截图）自动作为下一条 user 消息附图发给模型；旧图片从上下文中剥离
- 内置工具 finish(summary) 显式结束；连续多次无工具调用则提示后结束
- 连续失败过多时（可选）切换到兜底模型
- 每步检查 RunControl（暂停/取消）
"""
from __future__ import annotations

import asyncio
import json
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .llm import LLM, LLMError, image_part
from .mcp_hub import MCPHub


class Cancelled(Exception):
    pass


class RunControl:
    """由控制台修改，流水线在安全点检查。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.paused = False
        self.cancelled = False
        self.model_key = "main"
        self.notes: list[str] = []  # 用户在运行中追加的指示
        self.approvals: dict[str, dict] = {}  # 审批关卡 -> 控制台提交的确认内容
        self.requests: list[dict] = []  # 等待审批期间的请求（如重新生成概念图）

    async def checkpoint(self, on_wait: Callable[[], Any] | None = None) -> None:
        announced = False
        while self.paused and not self.cancelled:
            if not announced and on_wait:
                on_wait()
                announced = True
            await asyncio.sleep(0.5)
        if self.cancelled:
            raise Cancelled()

    def take_notes(self) -> list[str]:
        n, self.notes = self.notes, []
        return n


FINISH_TOOL = {"type": "function", "function": {
    "name": "finish",
    "description": "任务完成（或无法继续）时调用，给出简短总结。",
    "parameters": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "做了什么、结果如何"},
        "success": {"type": "boolean"}}, "required": ["summary"]}}}

VIEW_IMAGE_TOOL = {"type": "function", "function": {
    "name": "view_image",
    "description": "查看一张本地图片（例如目标概念图）。",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}


@dataclass
class AgentResult:
    finished: bool
    success: bool
    summary: str
    steps: int
    tool_calls: int
    images: list[str] = field(default_factory=list)
    last_results: dict[str, dict] = field(default_factory=dict)
    seconds: float = 0.0


EventFn = Callable[[str, str, Any], None]  # (kind, message, data)


async def run_agent(*, hub: MCPHub, control: RunControl, system: str, task: str, model_key: str | None = None,
                    task_images: list[str] | None = None, tool_prefixes: tuple[str, ...],
                    max_steps: int = 40, emit: EventFn, thinking_budget: int | None = None,
                    image_max_side: int = 768, allow_fallback: bool = True,
                    history: list[dict] | None = None) -> tuple[AgentResult, list[dict]]:
    """执行一个 agent 任务；返回结果与消息历史（可传回 history 继续同一上下文）。"""
    from . import config
    thinking_budget = thinking_budget or config.get("llm.sampling.tool_step_thinking_budget", 2048)
    tools = hub.openai_tools(tool_prefixes) + [FINISH_TOOL, VIEW_IMAGE_TOOL]
    if history:
        messages = history + [_user(task, task_images, image_max_side)]
    else:
        messages = [{"role": "system", "content": system}, _user(task, task_images, image_max_side)]
    t0 = time.time()
    steps = n_calls = 0
    consecutive_fail = 0
    idle_turns = 0
    all_images: list[str] = []
    last_results: dict[str, dict] = {}

    while steps < max_steps:
        await control.checkpoint(lambda: emit("paused", "已暂停，等待继续", None))
        for note in control.take_notes():
            messages.append({"role": "user", "content": f"【用户追加指示】{note}"})
            emit("note", f"用户指示已送达模型: {note}", None)
        steps += 1
        # 路由器指定的模型；连续失败触发兜底后（control.model_key 变为 fallback）改用兜底模型
        llm = LLM(control.model_key if control.model_key != "main" or not model_key else model_key)
        _strip_old_images(messages)
        _compact_history(messages)
        buf = {"reasoning": "", "content": ""}

        def on_delta(kind: str, text: str) -> None:
            buf[kind] += text
            if len(buf[kind]) > 400 or "\n" in text:
                emit("stream", "", {"kind": kind, "text": buf[kind]})
                buf[kind] = ""

        try:
            reply = await llm.chat(messages, tools=tools, thinking_budget=thinking_budget,
                                   max_tokens=None, on_delta=on_delta)
        except LLMError as e:
            emit("error", f"LLM 调用失败: {e}", None)
            raise
        for k, v in buf.items():
            if v:
                emit("stream", "", {"kind": k, "text": v})
        emit("llm", f"第 {steps} 步 · {reply.seconds:.1f}s · {reply.usage.get('completion_tokens', '?')} tokens",
             {"model": llm.model_key, "content": reply.content[-2000:], "reasoning_chars": len(reply.reasoning),
              "tool_calls": [tc["function"]["name"] for tc in reply.tool_calls], "usage": reply.usage})
        messages.append(reply.as_message())

        if not reply.tool_calls:
            idle_turns += 1
            if idle_turns >= 2:
                return AgentResult(True, False, reply.content[-1500:], steps, n_calls, all_images, last_results,
                                   time.time() - t0), messages
            messages.append({"role": "user", "content": "请继续：调用工具推进任务；若已完成请调用 finish。"})
            continue
        idle_turns = 0

        new_images: list[str] = []
        for tc in reply.tool_calls:
            name = tc["function"]["name"]
            raw = tc["function"].get("arguments") or "{}"
            n_calls += 1
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(args, dict):
                    raise ValueError("arguments 必须是 JSON 对象")
            except ValueError as e:
                result = {"ok": False, "error": f"参数不是合法 JSON: {e}"}
                _append_tool(messages, tc, result)
                emit("tool_error", f"{name}: 参数 JSON 无效", {"raw": str(raw)[:500]})
                consecutive_fail += 1
                continue

            if name == "finish":
                _append_tool(messages, tc, {"ok": True})
                ok = bool(args.get("success", True))
                emit("finish", args.get("summary", ""), {"success": ok})
                return AgentResult(True, ok, args.get("summary", ""), steps, n_calls, all_images, last_results,
                                   time.time() - t0), messages
            if name == "view_image":
                p = Path(args.get("path", ""))
                result = {"ok": p.exists(), "images": [str(p)] if p.exists() else [],
                          **({} if p.exists() else {"error": "文件不存在"})}
            elif name not in hub.tools:
                result = {"ok": False, "error": f"没有工具 {name}。可用: {[t['function']['name'] for t in tools]}"}
            else:
                err = _validate(hub.schema(name), args)
                if err:
                    result = {"ok": False, "error": f"参数不符合要求: {err}"}
                else:
                    emit("tool_call", name, {"args": _preview_args(args)})
                    t1 = time.time()
                    try:
                        result = await hub.call(name, args)
                    except Exception as e:  # MCP 传输错误也作为结果返回给模型
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    result.setdefault("ok", True)
                    emit("tool_result", f"{name} {'成功' if result.get('ok') else '失败'} · {time.time() - t1:.1f}s",
                         {"result": _preview_result(result)})
            last_results[name] = result
            if result.get("ok"):
                consecutive_fail = 0
            else:
                consecutive_fail += 1
            imgs = [i for i in result.get("images", []) if Path(i).exists()]
            new_images += imgs
            _append_tool(messages, tc, result)

        if new_images:
            all_images += new_images
            shown = new_images[-4:]
            messages.append(_user("以下是刚才工具产生的图片，请仔细观察后继续。", shown, image_max_side))
            emit("images", f"{len(shown)} 张图片已发送给模型", {"images": shown})

        if consecutive_fail >= 6:
            from .services import manager
            if allow_fallback and control.model_key == "main" and manager.available("llm-fallback"):
                control.model_key = "fallback"
                emit("fallback", "连续失败 6 次，切换到兜底模型", None)
                consecutive_fail = 0
            elif consecutive_fail >= 10:
                emit("error", "连续失败过多，终止本次 agent 任务", None)
                break

    return AgentResult(False, False, "达到最大步数", steps, n_calls, all_images, last_results,
                       time.time() - t0), messages


def _user(text: str, images: list[str] | None, max_side: int) -> dict:
    if not images:
        return {"role": "user", "content": text}
    return {"role": "user", "content": [{"type": "text", "text": text}] +
            [image_part(p, max_side) for p in images if Path(p).exists()]}


def _strip_old_images(messages: list[dict], keep_last: int = 2, trigger: int = 5) -> None:
    """带图消息超过 trigger 条时，一次性剥离到只剩最近 keep_last 条（首条任务参考图始终保留）。

    批量剥离而不是每步剥离：保持消息前缀稳定，服务端前缀缓存才能命中。
    """
    with_img = [i for i, m in enumerate(messages)
                if isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])]
    first_user = next((i for i, m in enumerate(messages) if m["role"] == "user"), None)
    candidates = [i for i in with_img if i != first_user]
    if len(candidates) <= trigger:
        return
    for i in candidates[:-keep_last]:
        if i == first_user:
            continue
        parts = messages[i]["content"]
        n = sum(1 for p in parts if p.get("type") == "image_url")
        text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
        messages[i] = {"role": messages[i]["role"], "content": f"{text} [此处 {n} 张旧图片已省略]"}


def _compact_history(messages: list[dict], keep_turns: int = 3, code_keep: int = 240, result_keep: int = 500) -> None:
    """压缩较早的步骤：只保留最近 keep_turns 个 assistant 回合的完整代码与结果。

    代码已经在 Blender/Godot 里执行，结果体现在场景/工程中；旧代码全文只会拉长上下文。
    实测上下文从 ~5k 涨到 ~24k 时解码从 ~40 tok/s 降到 15–30 tok/s。
    """
    idx = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
    if len(idx) <= keep_turns:
        return
    cutoff = idx[-keep_turns]
    for i in range(cutoff):
        m = messages[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc["function"]
                raw = fn.get("arguments") or ""
                if len(raw) <= code_keep + 40 or raw.endswith('"_compacted": true}'):
                    continue
                try:
                    args = json.loads(raw)
                except ValueError:
                    args = {}
                short = {k: (v[:code_keep] + " …(已执行，全文省略)" if isinstance(v, str) and len(v) > code_keep else v)
                         for k, v in args.items()} if isinstance(args, dict) else {}
                short["_compacted"] = True
                fn["arguments"] = json.dumps(short, ensure_ascii=False)
        elif m["role"] == "tool" and len(m.get("content") or "") > result_keep + 20:
            m["content"] = m["content"][:result_keep] + "…(旧结果已截断)"


def _append_tool(messages: list[dict], tc: dict, result: dict) -> None:
    body = json.dumps(result, ensure_ascii=False)
    if len(body) > 6000:
        body = body[:6000] + "…(截断)"
    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "name": tc["function"]["name"],
                     "content": body})


def _validate(schema: dict | None, args: dict) -> str | None:
    if not schema:
        return None
    try:
        jsonschema.validate(args, schema)
        return None
    except jsonschema.ValidationError as e:
        return e.message[:500]


def _preview_args(args: dict) -> dict:
    return {k: (v if not isinstance(v, str) or len(v) < 4000 else v[:4000] + "…") for k, v in args.items()}


def _preview_result(r: dict) -> dict:
    s = json.dumps(r, ensure_ascii=False)
    return r if len(s) < 3000 else json.loads(json.dumps({k: (str(v)[:600] if len(str(v)) > 600 else v)
                                                          for k, v in r.items()}, ensure_ascii=False))
