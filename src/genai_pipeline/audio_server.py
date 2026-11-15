"""音效服务（在 envs/audio 中运行）：MOSS-SoundEffect（OpenMOSS，Apache-2.0）的 MLX 4-bit 版，文字描述 → 环境音 / 音效 WAV。

开源、本机算力：权重由 scripts/setup_audio.command 预先下载到 Hugging Face 缓存（HF_HOME=models/audio/hf），
服务以离线模式加载，不联网。

  POST /generate {"prompt": 英文描述, "seconds": 8, "out": 输出 .wav, "seed": 可选, "loop": 首尾交叉淡化成循环素材}
       -> {"path", "seconds", "sample_rate", "elapsed"}
  GET  /health
命令行自检：python audio_server.py --selftest "gentle ocean waves" --out 输出.wav
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HF_HUB_OFFLINE", "1")

MODEL = os.environ.get("GENAI_AUDIO_MODEL", "moss-sound-effect")
_model = None
_lock = threading.Lock()


def model():
    global _model
    if _model is None:
        import mlx_speech
        _model = mlx_speech.tts.load(MODEL)
    return _model


def _write_wav(path: str, waveform, rate: int) -> float:
    """浮点波形 → 16 位 PCM WAV（单声道或双声道）；峰值超过 1 时整体归一。返回时长（秒）。"""
    import numpy as np
    a = np.array(waveform, dtype=np.float32)
    a = np.squeeze(a)
    if a.ndim == 2 and a.shape[0] in (1, 2) and a.shape[0] < a.shape[1]:
        a = a.T                      # (通道, 样本) → (样本, 通道)
    if a.ndim == 1:
        a = a[:, None]
    ch = a.shape[1]
    peak = float(np.abs(a).max()) if a.size else 0.0
    if peak > 1.0:
        a = a / peak
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())
    return pcm.shape[0] / float(rate)


def make_loop(a, rate: int, fade: float = 0.5):
    """首尾交叉淡化：把末尾 fade 秒等功率淡入到开头，再截掉末尾，得到可无缝循环的片段。"""
    import numpy as np
    a = np.array(a, dtype=np.float32)
    n = int(min(fade * rate, a.shape[0] // 4))
    if n < 2:
        return a
    t = np.linspace(0, np.pi / 2, n, dtype=np.float32)
    fin, fout = np.sin(t), np.cos(t)
    if a.ndim == 2:
        fin, fout = fin[:, None], fout[:, None]
    head = a[:n] * fin + a[-n:] * fout
    return np.concatenate([head, a[n:-n]], 0)


def generate(prompt: str, seconds: float, out: str, seed: int | None = None, loop: bool = False) -> dict:
    t0 = time.time()
    seconds = max(1.0, min(30.0, float(seconds)))
    with _lock:
        m = model()
        if seed is not None:
            try:
                import mlx.core as mx
                mx.random.seed(int(seed))
            except Exception:  # noqa: BLE001 — 种子只影响可复现性
                pass
        r = m.generate(prompt, duration_seconds=seconds)
    rate = int(getattr(r, "sample_rate", 24000))
    wav = r.waveform
    if loop:
        import numpy as np
        wav = np.squeeze(np.array(wav, dtype=np.float32))
        if wav.ndim == 2 and wav.shape[0] in (1, 2) and wav.shape[0] < wav.shape[1]:
            wav = wav.T
        wav = make_loop(wav, rate)
    dur = _write_wav(out, wav, rate)
    return {"path": out, "seconds": round(dur, 2), "sample_rate": rate, "elapsed": round(time.time() - t0, 1)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "model": MODEL, "loaded": _model is not None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/generate":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if not req.get("prompt") or not req.get("out"):
                self._send(400, {"error": "需要 prompt 和 out"})
                return
            self._send(200, generate(str(req["prompt"]), req.get("seconds", 8), str(req["out"]), req.get("seed"),
                                       bool(req.get("loop"))))
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"[:400]})

    def log_message(self, fmt, *args):
        sys.stderr.write("[audio] " + fmt % args + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--selftest", default="")
    ap.add_argument("--out", default="audio_selftest.wav")
    ap.add_argument("--seconds", type=float, default=6)
    a = ap.parse_args()
    if a.selftest:
        res = generate(a.selftest, a.seconds, a.out)
        print("[audio] 自检通过：" + json.dumps(res, ensure_ascii=False), flush=True)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"audio listening on 127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
