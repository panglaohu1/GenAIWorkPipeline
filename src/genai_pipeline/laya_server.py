"""System One 决策服务（在 envs/laya 中运行）：开源 Laya（Convai，Apache 2.0）typed decisions。

  POST /decide {"state": {...}, "questions": {...}}  -> Laya 原始输出 {"answers": {...}, "usage": {...}}
  GET  /health
与 TypeSafe Jev 相同的三种题型（choice / score / noul），本地离线运行，不调用任何外部 API。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("USE_TF", "0")  # 避免 transformers 探测 TensorFlow 导致加载卡死（Laya 文档说明）
os.environ.setdefault("HF_HUB_OFFLINE", "1")

_agent = None
_lock = threading.Lock()
MODEL_DIR = ""


def agent():
    global _agent
    if _agent is None:
        import laya
        _agent = laya.load(MODEL_DIR)
    return _agent


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "loaded": _agent is not None, "model": MODEL_DIR})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/decide":
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            t = time.time()
            with _lock:
                res = agent().predict(req["state"], req["questions"], max_len=int(req.get("max_len", 2048)))
            res["elapsed_ms"] = round((time.time() - t) * 1000)
            self._send(200, res)
        except Exception as e:  # 返回错误，由调用方转交生成式模型复核
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[laya] " + fmt % args + "\n")


def main() -> None:
    global MODEL_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--model", required=True)
    a = ap.parse_args()
    MODEL_DIR = a.model
    agent()  # 预加载（约 12 秒），之后每次决策约 1–2 秒
    print(f"laya listening on 127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
