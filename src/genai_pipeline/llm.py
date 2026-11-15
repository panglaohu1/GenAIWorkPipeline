"""OpenAI 兼容的本地 LLM 客户端（mlx_vlm.server）。

- 流式读取：思考内容 / 正文 / tool_calls 增量，通过 on_delta 回调推送到控制台
- 图片输入：本地路径 -> 缩放到 max_side -> data URL
- 主力(Bonsai) / 兜底(Qwen3.8 oQ6) 切换由调用方通过 model_key 指定
"""
from __future__ import annotations

import base64
import io
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image

from . import config
from .services import manager

DeltaCB = Callable[[str, str], Awaitable[None] | None]  # (kind: "reasoning"|"content", text)


@dataclass
class LLMReply:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0
    model_key: str = "main"

    def as_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


def image_part(path: str | Path, max_side: int = 1024) -> dict:
    img = Image.open(path)
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def user_message(text: str, images: list[str | Path] | None = None, max_side: int = 1024) -> dict:
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict] = [{"type": "text", "text": text}]
    parts += [image_part(p, max_side) for p in images]
    return {"role": "user", "content": parts}


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, model_key: str = "main") -> None:
        self.model_key = model_key

    def _svc(self):
        return manager.services[f"llm-{self.model_key}"]

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None, thinking: bool = True,
                   thinking_budget: int | None = None, max_tokens: int | None = None,
                   temperature: float | None = None, on_delta: DeltaCB | None = None,
                   tool_choice: str | None = None) -> LLMReply:
        return await manager.run(f"llm-{self.model_key}", lambda: self._chat(
            messages, tools=tools, thinking=thinking, thinking_budget=thinking_budget,
            max_tokens=max_tokens, temperature=temperature, on_delta=on_delta, tool_choice=tool_choice))

    async def _chat(self, messages: list[dict], *, tools: list[dict] | None = None, thinking: bool = True,
                    thinking_budget: int | None = None, max_tokens: int | None = None,
                    temperature: float | None = None, on_delta: DeltaCB | None = None,
                    tool_choice: str | None = None) -> LLMReply:
        svc = self._svc()
        s = {**config.get("llm.sampling", {}), **(getattr(svc, "cfg", {}) or {}).get("sampling", {})}   # 每个模型可覆盖采样参数
        body: dict[str, Any] = {
            "model": str(svc.model_path),
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens or s.get("max_tokens", 16384),
            "temperature": s.get("temperature", 1.0) if temperature is None else temperature,
            "top_p": s.get("top_p", 0.95),
            "top_k": s.get("top_k", 20),
            "enable_thinking": thinking,
            **{k: s[k] for k in ("presence_penalty", "min_p", "repetition_penalty") if k in s},
            # 每次请求用不同 seed：实测相同输入会得到完全相同的输出，回退重试时会原地打转
            "seed": random.randint(0, 2**31 - 1),
        }
        if thinking and thinking_budget:
            body["thinking_budget"] = thinking_budget
        if getattr(svc, "backend", "mlx") == "llamacpp":   # llama-server 通过对话模板参数开关思考
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        reply = LLMReply(model_key=self.model_key)
        t0 = time.time()
        timeout = httpx.Timeout(s.get("request_timeout_s", 900), connect=10)
        tc_acc: dict[int, dict] = {}
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{svc.url}/v1/chat/completions", json=body) as r:
                if r.status_code != 200:
                    raise LLMError(f"HTTP {r.status_code}: {(await r.aread()).decode()[:2000]}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        reply.usage = chunk["usage"]
                    for ch in chunk.get("choices", []):
                        d = ch.get("delta") or ch.get("message") or {}
                        rs = d.get("reasoning_content") or d.get("reasoning")
                        if rs:
                            reply.reasoning += rs
                            await _call(on_delta, "reasoning", rs)
                        if d.get("content"):
                            reply.content += d["content"]
                            await _call(on_delta, "content", d["content"])
                        for tc in d.get("tool_calls") or []:
                            idx = tc.get("index", len(tc_acc))
                            acc = tc_acc.setdefault(idx, {"id": tc.get("id") or f"call_{idx}", "type": "function",
                                                          "function": {"name": "", "arguments": ""}})
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                acc["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                a = fn["arguments"]
                                acc["function"]["arguments"] += a if isinstance(a, str) else json.dumps(a)
                            if tc.get("id"):
                                acc["id"] = tc["id"]
                        if ch.get("finish_reason"):
                            reply.finish_reason = ch["finish_reason"]
        reply.tool_calls = [tc_acc[i] for i in sorted(tc_acc)]
        if not reply.tool_calls and tools:
            reply.tool_calls = parse_inline_tool_calls(reply.content)
        # 某些模板把思考留在正文里
        if not reply.reasoning and "<think>" in reply.content:
            m = re.search(r"<think>(.*?)</think>", reply.content, re.S)
            if m:
                reply.reasoning = m.group(1).strip()
                reply.content = reply.content.replace(m.group(0), "").strip()
        reply.seconds = time.time() - t0
        from . import teams   # token 记账：归到当前发言成员 / 阶段负责人（不在任务里调用时不记）
        teams.record_usage(reply.usage)
        return reply

    async def json(self, messages: list[dict], *, schema_hint: str = "", retries: int = 2,
                   on_delta: DeltaCB | None = None, thinking_budget: int | None = None,
                   validate: Callable[[Any], str | None] | None = None) -> Any:
        """要求模型输出 JSON；解析失败或 validate 返回错误信息时带反馈重试。"""
        msgs = list(messages)
        last_err = ""
        for attempt in range(retries + 1):
            # 兜底：最后一次重试关闭思考（Bonsai 实测：思考耗尽预算后正文为空 / 无 JSON）
            no_think = attempt == retries and attempt > 0 and "没有可解析的 JSON" in last_err
            rep = await self.chat(msgs, on_delta=on_delta, thinking_budget=thinking_budget, thinking=not no_think)
            try:
                obj = extract_json(rep.content)
                err = validate(obj) if validate else None
                if not err:
                    return obj
                last_err = err
            except ValueError as e:
                last_err = str(e)
                try:  # 记录无法解析的原始回复，便于针对性修提示词
                    with open(config.ROOT / "logs" / "llm-json-failures.log", "a", encoding="utf-8") as fh:
                        fh.write(f"\n===== {time.strftime('%F %T')} {self.model_key} think={len(rep.reasoning or '')} "
                                 f"content={len(rep.content or '')}\n{(rep.content or '')[:4000]}\n----- reasoning tail\n"
                                 f"{(rep.reasoning or '')[-1500:]}\n")
                except OSError:
                    pass
            msgs = msgs + [rep.as_message(), {"role": "user", "content":
                           f"输出无效：{last_err}\n请只输出一个符合要求的 JSON 对象，不要其他文字。{schema_hint}"}]
        raise LLMError(f"JSON 输出失败: {last_err}（最后一次回复 正文 {len(rep.content or '')} 字 / 思考 "
                       f"{len(rep.reasoning or '')} 字，开头：{(rep.content or '')[:160]!r}）")


def extract_json(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            chunk = text[start:end + 1]
            try:
                return json.loads(chunk)
            except ValueError:
                try:  # 容错：模型模仿提示词在 JSON 里写 // 注释、尾逗号（Bonsai 实测）
                    return json.loads(_loosen_json(chunk))
                except ValueError:
                    pass
                try:  # 容错：深层嵌套时括号数错（Bonsai 实测多写一个 }）
                    return json.loads(_repair_brackets(_loosen_json(text[start:])))
                except ValueError:
                    continue
    raise ValueError("回复中没有可解析的 JSON")


def _repair_brackets(s: str) -> str:
    """按括号栈修复：丢弃与栈顶不匹配的闭括号，结尾补齐未闭合的括号，截掉最外层闭合后的多余文字。"""
    pair = {"}": "{", "]": "["}
    close = {"{": "}", "[": "]"}
    out, stack, in_str, esc = [], [], False, False
    for c in s:
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]":
            if not stack or stack[-1] != pair[c]:
                continue          # 多余或错配的闭括号：丢弃
            stack.pop()
            out.append(c)
            if not stack:
                break             # 最外层已闭合
            continue
        out.append(c)
    out += [close[b] for b in reversed(stack)]
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def _loosen_json(s: str) -> str:
    out, i, in_str = [], 0, False
    while i < len(s):
        c = s[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < len(s):
                out.append(s[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == "/" and s[i:i + 2] == "//":
            while i < len(s) and s[i] != "\n":
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


_INLINE_TC = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def parse_inline_tool_calls(text: str) -> list[dict]:
    """服务端未解析时，从正文里的 <tool_call>{...}</tool_call> 兜底提取。"""
    out = []
    for i, m in enumerate(_INLINE_TC.finditer(text or "")):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        args = obj.get("arguments", obj.get("parameters", {}))
        out.append({"id": f"inline_{i}", "type": "function",
                    "function": {"name": obj.get("name", ""),
                                 "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}})
    return out


async def _call(cb, *args):
    if cb is None:
        return
    res = cb(*args)
    if hasattr(res, "__await__"):
        await res


async def _to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)
