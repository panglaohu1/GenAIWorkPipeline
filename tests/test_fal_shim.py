"""本地 FAL 兼容服务：队列提交 → 轮询 → 取结果，Bonsai 改写编辑指令后交给 Flux 图生图（替身 Flux / Bonsai）。"""
import base64
import io
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from genai_pipeline import fal_shim as S


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def handler(seen, reply):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            out = reply(self.path, body)
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    return H


def call(url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Key local"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


@pytest.fixture
def shim(tmp_path, monkeypatch):
    flux, bonsai = [], []

    def flux_reply(path, body):
        Image.new("RGB", (body["width"], body["height"]), (200, 120, 60)).save(body["out"])
        return {"path": body["out"], "seed": body["seed"]}
    s1, flux_url = serve(handler(flux, flux_reply))
    s2, bonsai_url = serve(handler(bonsai, lambda p, b: {"choices": [{"message": {
        "content": "<think>x</think>A sunlit stone courtyard with a fountain, warm orange walls, soft morning light."}}]}))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setitem(S.STATE, "base", base)
    monkeypatch.setitem(S.STATE, "out", tmp_path / "shim")
    monkeypatch.setitem(S.STATE, "flux", flux_url)
    monkeypatch.setitem(S.STATE, "bonsai", bonsai_url)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield base, flux, bonsai
    for s in (srv, s1, s2):
        s.shutdown()


def png_data_uri(w=600, h=400):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_queue_edit_uses_bonsai_rewrite_and_flux_img2img(shim):
    base, flux, bonsai = shim
    sub = call(f"{base}/fal-ai/nano-banana-2/edit",
               {"prompt": "remove the chair", "image_urls": [png_data_uri()], "num_images": 1})
    for _ in range(100):
        st = call(sub["status_url"] + "?logs=1")
        if st["status"] == "COMPLETED":
            break
        time.sleep(0.05)
    assert st["status"] == "COMPLETED" and "error" not in st
    res = call(sub["response_url"])
    img = res["images"][0]
    with urllib.request.urlopen(img["url"]) as r:
        assert Image.open(io.BytesIO(r.read())).size == (1024, 704)      # 跟随输入 3:2，64 的倍数
    assert res["local"]["prompt_source"] == "bonsai" and "<think>" not in res["local"]["flux_prompt"]
    path, body = flux[0]
    assert path == "/generate" and body["init_image"].endswith(".png") and body["strength"] == 0.6
    assert bonsai[0][1]["messages"][1]["content"][0]["type"] == "image_url"


def test_sync_run_text_to_image_without_input_image_skips_bonsai(shim):
    base, flux, bonsai = shim
    res = call(f"{base}/run/fal-ai/flux/schnell", {"prompt": "a tiny island", "num_images": 2, "seed": 7})
    assert len(res["images"]) == 2 and [b["seed"] for _, b in flux] == [7, 8]
    assert not bonsai and "init_image" not in flux[0][1]


def test_3d_and_audio_are_refused_with_reason(shim):
    base, _, _ = shim
    for ep in ("fal-ai/hunyuan3d-v3/image-to-3d", "fal-ai/elevenlabs/sound-effects/v2"):
        with pytest.raises(urllib.error.HTTPError) as e:
            call(f"{base}/{ep}", {"prompt": "x"})
        assert e.value.code == 501 and "fal.ai" in json.loads(e.value.read())["detail"]


def test_bonsai_down_falls_back_to_instruction(shim, monkeypatch):
    base, flux, _ = shim
    monkeypatch.setitem(S.STATE, "bonsai", "http://127.0.0.1:9")
    res = call(f"{base}/run/openai/gpt-image-2/edit", {"prompt": "make it night", "image_urls": [png_data_uri()]})
    assert res["local"]["prompt_source"] == "instruction" and flux[0][1]["prompt"] == "make it night"
