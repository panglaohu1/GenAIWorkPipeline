"""集成测试：用脚本化的假 LLM 驱动真实 Blender/Godot MCP 服务端，验证 agent 循环与引擎链路。

运行: .venv/bin/python -m pytest -x -q
不需要真实模型；需要本机 Blender 与 Godot。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from genai_pipeline import agent as agent_mod
from genai_pipeline.agent import RunControl, run_agent
from genai_pipeline.mcp_hub import MCPHub
from genai_pipeline.services import manager


def _sse(chunks: list[dict]) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


class FakeLLM:
    """按顺序返回预设的 assistant 回合；记录收到的请求。"""

    def __init__(self, turns: list[dict]):
        self.turns = turns
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                turn = outer.turns.pop(0) if outer.turns else {"content": "done"}
                delta: dict = {"role": "assistant", "reasoning_content": "thinking…"}
                chunks = [{"choices": [{"delta": delta}]}]
                if "tool_calls" in turn:
                    tcs = [{"index": i, "id": f"c{i}", "type": "function",
                            "function": {"name": n, "arguments": json.dumps(a)}}
                           for i, (n, a) in enumerate(turn["tool_calls"])]
                    chunks.append({"choices": [{"delta": {"tool_calls": tcs}}]})
                else:
                    chunks.append({"choices": [{"delta": {"content": turn["content"]}}]})
                chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}],
                               "usage": {"completion_tokens": 10}})
                data = _sse(chunks)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]


@pytest.fixture(autouse=True)
def _isolated_blender(monkeypatch):
    """测试使用独立的 Blender worker 端口，避免清空正在运行的流水线场景。"""
    monkeypatch.setenv("GENAI_BLENDER_PORT", "9899")


@pytest.fixture
def fake_llm(monkeypatch):
    def make(turns):
        f = FakeLLM(turns)
        svc = manager.services["llm-main"]
        monkeypatch.setattr(svc, "port", f.port)
        monkeypatch.setattr(manager, "healthy", lambda name, timeout=2.0: True)
        return f
    return make


async def test_blender_agent_loop(tmp_path: Path, fake_llm):
    code = ("m = H.mat('red', '#cc3333')\n"
            "H.box('body', size=(1, 0.6, 0.8), location=(0, 0, 0.4), material=m, bevel=0.03)\n"
            "H.cylinder('chimney', radius=0.1, depth=0.4, location=(0.3, 0, 1.0), material=m)\n"
            "H.ground_to_origin()\nresult = len(bpy.data.objects)")
    llm = fake_llm([
        {"tool_calls": [("blender_reset_scene", {})]},
        {"tool_calls": [("blender_run_python", {"code": "this is not python"})]},  # 错误后自我修正
        {"tool_calls": [("blender_run_python", {"code": code})]},
        {"tool_calls": [("blender_render", {"views": ["front", "persp"]})]},
        {"tool_calls": [("blender_render", {"views": "front"})]},  # 类型不符合 schema，应被拦截
        {"tool_calls": [("finish", {"summary": "built", "success": True})]},
    ])
    events = []
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        res, msgs = await run_agent(hub=hub, control=RunControl(), system="s", task="build",
                                    tool_prefixes=("blender_",), max_steps=10,
                                    emit=lambda k, m, d: events.append((k, m, d)))
        exp = await hub.call("blender_export_glb", {"asset_name": "test_house"})
    assert res.finished and res.success
    assert len(res.images) == 2 and all(Path(p).exists() for p in res.images)
    assert exp["ok"] and Path(exp["path"]).exists() and exp["info"]["triangles"] > 0
    assert abs(exp["info"]["bounds_min"][2]) < 1e-3  # 已落地
    kinds = [k for k, _, _ in events]
    assert "images" in kinds
    # 渲染图作为 user 消息附图发给了模型
    assert any(isinstance(m.get("content"), list) for m in llm.requests[-1]["messages"])
    # 错误代码的执行结果以失败形式回给模型
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert '"ok": false' in tool_msgs[1]["content"]  # 旧结果可能已被压缩截断，只检查内容
    assert "参数不符合要求" in tool_msgs[4]["content"]


PLAYER_GD = """extends Node3D

func _ready() -> void:
\tvar sun := DirectionalLight3D.new()
\tsun.rotation_degrees = Vector3(-50, 30, 0)
\tadd_child(sun)
\tvar ground := StaticBody3D.new()
\tvar gm := MeshInstance3D.new()
\tvar pm := PlaneMesh.new()
\tpm.size = Vector2(40, 40)
\tgm.mesh = pm
\tground.add_child(gm)
\tvar gc := CollisionShape3D.new()
\tvar gs := BoxShape3D.new()
\tgs.size = Vector3(40, 0.2, 40)
\tgc.shape = gs
\tgc.position.y = -0.1
\tground.add_child(gc)
\tadd_child(ground)
\tvar p := CharacterBody3D.new()
\tp.set_script(load("res://scripts/player.gd"))
\tp.position = Vector3(0, 1, 0)
\tadd_child(p)
\tvar cam := Camera3D.new()
\tcam.position = Vector3(0, 8, 10)
\tadd_child(cam)
\tcam.look_at(Vector3.ZERO)
"""

PLAYER_SCRIPT = """extends CharacterBody3D

func _ready() -> void:
\tadd_to_group("player")
\tvar col := CollisionShape3D.new()
\tcol.shape = CapsuleShape3D.new()
\tadd_child(col)
\tvar mi := MeshInstance3D.new()
\tmi.mesh = CapsuleMesh.new()
\tadd_child(mi)

func _physics_process(delta: float) -> void:
\tif not is_on_floor():
\t\tvelocity += get_gravity() * delta
\tvar v := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
\tvelocity.x = v.x * 5.0
\tvelocity.z = v.y * 5.0
\tif Input.is_action_just_pressed("jump") and is_on_floor():
\t\tvelocity.y = 5.0
\tmove_and_slide()
"""


async def test_godot_tools_and_probe(tmp_path: Path):
    async with MCPHub(tmp_path, servers=["godot"]) as hub:
        assert (await hub.call("godot_write_file", {"path": "res://main.gd", "content": PLAYER_GD}))["ok"]
        assert (await hub.call("godot_write_file", {"path": "res://scripts/player.gd",
                                                    "content": PLAYER_SCRIPT}))["ok"]
        bad = await hub.call("godot_write_file", {"path": "../escape.gd", "content": "x"})
        assert bad["ok"] is False
        chk = await hub.call("godot_check", {})
        assert chk["ok"], chk
        run = await hub.call("godot_run", {"seconds": 3.0, "shot_times": [2.5],
                                           "inputs": [{"action": "move_forward", "start": 0.5, "duration": 1.0}]})
    assert run["probe"]["players"] == 1, run
    track = run["player_track_sample"]
    z0, z1 = track[0]["pos"][2], min(t["pos"][2] for t in track)
    assert z0 - z1 > 2.0, track  # 前进 1 秒约 5 米
    assert run["images"] and Path(run["images"][0]).exists()


async def test_godot_check_reports_errors(tmp_path: Path):
    async with MCPHub(tmp_path, servers=["godot"]) as hub:
        await hub.call("godot_write_file", {"path": "res://main.gd",
                                            "content": "extends Node3D\n\nfunc _ready():\n\tundefined_call()\n"})
        chk = await hub.call("godot_check", {})
    assert chk["ok"] is False
    assert chk["script_errors"] or chk["runtime_errors"]


def test_strip_old_images():
    img = {"type": "image_url", "image_url": {"url": "data:x"}}
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": [{"type": "text", "text": "task"}, img]}]
    for i in range(4):
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"r{i}"}, img, img]})
    agent_mod._strip_old_images(msgs, keep_last=2)
    assert all(isinstance(m["content"], list) for m in msgs[1:])  # 未达阈值：不剥离，保持前缀稳定
    for i in range(4, 6):
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"r{i}"}, img, img]})
    agent_mod._strip_old_images(msgs, keep_last=2)
    assert isinstance(msgs[1]["content"], list)  # 任务参考图保留
    assert isinstance(msgs[2]["content"], str) and "省略" in msgs[2]["content"]
    assert isinstance(msgs[-1]["content"], list) and isinstance(msgs[-2]["content"], list)


async def test_render_color_fidelity(tmp_path: Path):
    """渲染灯光校准：正对主光的面颜色应接近材质本色（不过曝、不过暗），保证颜色验收可信。"""
    from PIL import Image
    colors = ["#7fb8d6", "#e8e2d4", "#3a3f47", "#e0a030"]
    code = "\n".join(f"H.box('c{i}', size=(0.8, 0.8, 0.8), location=({i}, 0, 0.4), "
                     f"material=H.mat('m{i}', '{c}', roughness=0.8))" for i, c in enumerate(colors))
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        await hub.call("blender_reset_scene", {})
        assert (await hub.call("blender_run_python", {"code": code}))["ok"]
        r = await hub.call("blender_render", {"views": ["front"]})
    im = Image.open(r["images"][0]).convert("RGB")
    w, h = im.size
    for i, c in enumerate(colors):
        want = [int(c[k:k + 2], 16) for k in (1, 3, 5)]
        got = im.getpixel((int(w * (0.5 + (i - 1.5) * 0.215)), h // 2))
        ratio = sum(got) / max(sum(want), 1)
        assert 0.8 < ratio < 1.15, (c, got, ratio)


async def test_snap_helper(tmp_path: Path):
    code = ("body = H.box('body', size=(2, 0.5, 0.5), location=(0, 0, 1))\n"
            "fin = H.box('fin', size=(0.2, 0.05, 0.6))\n"
            "H.snap(fin, body, '-X', overlap=0.05)\n"
            "roof = H.cone('roof', radius1=0.4, depth=0.5)\n"
            "H.snap(roof, body, '+Z')\n"
            "result = [H.bounds(fin), H.bounds(body), H.bounds(roof)]")
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        await hub.call("blender_reset_scene", {})
        r = await hub.call("blender_run_python", {"code": code})
    fin, body, roof = r["result"]
    assert abs(fin["max"][0] - (body["min"][0] + 0.05)) < 1e-3   # 尾翼嵌入机身后端 5cm
    assert abs(fin["center"][2] - body["center"][2]) < 1e-3       # 其余轴居中
    assert abs(roof["min"][2] - body["max"][2]) < 1e-3            # 屋顶坐在顶面上


def test_compact_history():
    import json as _j
    code = "x = 1\n" * 200
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for i in range(5):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "blender_run_python",
                                                            "arguments": _j.dumps({"code": code})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r" * 2000})
    agent_mod._compact_history(msgs, keep_turns=3)
    args = [_j.loads(m["tool_calls"][0]["function"]["arguments"]) for m in msgs if m["role"] == "assistant"]
    assert args[0]["_compacted"] and len(args[0]["code"]) < 300      # 旧回合已压缩
    assert args[-1]["code"] == code and "_compacted" not in args[-1]  # 最近回合保留全文
    tools = [m for m in msgs if m["role"] == "tool"]
    assert len(tools[0]["content"]) < 600 and len(tools[-1]["content"]) == 2000


KIT_MAIN = """extends Node3D
const Kit = preload("res://addons/pipeline/kit.gd")
var score := 0
var label: Label

func _ready() -> void:
\tKit.add_environment(self)
\tKit.add_ground(self, 30.0)
\tKit.spawn(self, "res://assets/crate.glb", Vector3(4, 0, 0), 1.0)
\tKit.make_player(self, "res://assets/hero.glb", Vector3(0, 0.2, 0), 1.2)
\tvar p := Kit.make_pickup(self, "res://assets/crate.glb", Vector3(0, 0.3, -3), 0.4)
\tp.collected.connect(func(_x): score += 1; label.text = "letters: %d" % score; print("PICKED ", score))
\tlabel = Kit.make_label(self, "letters: 0")
\tvar trees := Kit.scatter(self, func(parent, pos): return Kit.bamboo(parent, pos, 3.0), 8, Vector2(-10, -10), Vector2(10, 10), [Vector3.ZERO, Vector3(0, 0, -3)], 2.0)
\tKit.box(self, Vector3(-4, 0, 0), Vector3(1, 1, 1), Color.GRAY, true)
\tprint("TREES ", trees.size())
"""


async def test_godot_kit_game(tmp_path: Path):
    """Blender 导出两个 GLB → Godot 用 Kit 搭游戏 → 模拟前进应拾取收集物。"""
    async with MCPHub(tmp_path, servers=["blender", "godot"]) as hub:
        await hub.call("blender_reset_scene", {})
        await hub.call("blender_run_python", {"code": "H.box('hero', size=(0.5,0.4,1.0), location=(0,0,0.5), "
                                                       "material=H.mat('o', '#e08030'))"})
        assert (await hub.call("blender_export_glb", {"asset_name": "hero"}))["ok"]
        await hub.call("blender_reset_scene", {})
        await hub.call("blender_run_python", {"code": "H.box('crate', size=(1,1,1), location=(0,0,0.5), "
                                                       "material=H.mat('w', '#8b5a2b'))"})
        assert (await hub.call("blender_export_glb", {"asset_name": "crate"}))["ok"]
        imp = await hub.call("godot_import_assets", {})
        assert set(imp["assets"]) == {"res://assets/hero.glb", "res://assets/crate.glb"}, imp
        await hub.call("godot_write_file", {"path": "res://main.gd", "content": KIT_MAIN})
        chk = await hub.call("godot_check", {})
        assert chk["ok"], chk
        run = await hub.call("godot_run", {"seconds": 3.5, "inputs": [
            {"action": "move_forward", "start": 0.5, "duration": 1.2}]})
    assert run["probe"]["players"] == 1, run
    assert any("PICKED 1" in ln for ln in run["game_output"]), run["game_output"]
    assert any("TREES 8" in ln for ln in run["game_output"]), (run["game_output"], run["errors"], run.get("log_tail"))
    assert not run["errors"], run["errors"]
    track = run["player_track_sample"]
    assert min(t["pos"][2] for t in track) < -2.0, track  # 走到了收集物位置


async def test_character_helpers(tmp_path: Path):
    code = ("body = H.capsule('body', radius=0.2, length=0.8, location=(0, 0, 0.6), axis='X')\n"
            "leg = H.limb('leg', start=(0.2, 0, 0.45), end=(0.25, 0, 0.0), radius=0.06)\n"
            "tail = H.limb('tail', start=(-0.4, 0, 0.6), end=(-0.8, 0, 0.9), radius=0.05, radius_end=0.01)\n"
            "head = H.rounded_box('head', size=(0.3, 0.3, 0.3), location=(0.55, 0, 0.8))\n"
            "result = [H.bounds(body), H.bounds(leg), H.bounds(tail)]")
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        await hub.call("blender_reset_scene", {})
        r = await hub.call("blender_run_python", {"code": code})
    assert r["ok"], r
    body, leg, tail = r["result"]
    assert abs(body["size"][0] - 0.8) < 0.02 and abs(body["size"][2] - 0.4) < 0.02   # 沿 X 的胶囊
    assert abs(leg["min"][2] - 0.0) < 0.07 and abs(leg["max"][2] - 0.45) < 0.07       # 腿连接两点
    assert tail["min"][0] < -0.75 and tail["max"][2] > 0.85                            # 尾巴朝后上方


async def test_common_misuse_is_accepted(tmp_path: Path):
    code = ("import H\nfrom H import box\n"
            "o = box('b', size=(1, 1, 1))\n"
            "o.material = H.mat('red', '#ff0000')\n"
            "o.rotation = (0, 0, 90)\n"
            "result = [o.active_material.name, round(o.rotation_euler[2], 4)]")
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        await hub.call("blender_reset_scene", {})
        r = await hub.call("blender_run_python", {"code": code})
    assert r["ok"], r
    assert r["result"] == ["red", round(1.5707963, 4)]


BLUEPRINT = {"parts": [
    {"name": "torso", "shape": "capsule", "radius": 0.2, "length": 0.7, "axis": "X", "at": [0, 0, 0.55],
     "color": "#e8822a"},
    {"name": "head", "shape": "rounded_box", "size": [0.34, 0.34, 0.3], "color": "#e8822a",
     "attach": {"to": "torso", "side": "+X", "overlap": 0.08, "offset": [0, 0, 0.15]}},
    {"name": "leg_front", "shape": "limb", "from": [0.2, 0.1, 0.45], "to": [0.2, 0.1, 0.0], "radius": 0.06,
     "color": "#f3e6cc", "mirror": "X"},
    {"name": "tail", "shape": "limb", "from": [-0.35, 0, 0.6], "to": [-0.7, 0, 0.85], "radius": 0.05,
     "radius_end": 0.02, "color": "#e8822a"},
    {"name": "lost_ball", "shape": "sphere", "size": [0.1, 0.1, 0.1], "at": [3, 3, 3], "color": "#ffffff"},
    {"name": "bad", "shape": "blob"},
]}


async def test_blueprint_build(tmp_path: Path):
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        r = await hub.call("blender_build_blueprint", {"blueprint": BLUEPRINT})
        img = await hub.call("blender_render", {"views": ["persp"], "label": "bp"})
    assert r["ok"], r
    assert r["built"] == 6                                   # 5 个有效部件 + 1 个镜像腿
    assert any("blob" in e for e in r["errors"])             # 未知形状报错但不影响其他部件
    assert r["floating"] == ["lost_ball"]                    # 悬空检测
    assert "leg_front_mirror" in r["parts"]
    assert abs(r["parts"]["leg_front_mirror"]["center"][0] + r["parts"]["leg_front"]["center"][0]) < 1e-3
    assert img["ok"] and img["images"]


async def test_blueprint_scene_shapes(tmp_path: Path):
    bp = {"parts": [
        {"name": "cliff", "shape": "rock", "size": [10, 6, 4], "at": [0, 0, 2], "color": "#8a8f96"},
        {"name": "hall", "shape": "box", "size": [4, 3, 2], "color": "#efe6d2",
         "attach": {"to": "cliff", "side": "+Z", "overlap": 0.3}},
        {"name": "dome", "shape": "dome", "size": [2.4, 2.4, 1.2], "color": "#e8c878",
         "attach": {"to": "hall", "side": "+Z", "overlap": 0.02}},
        {"name": "merlon", "shape": "box", "size": [0.3, 0.3, 0.3], "at": [-1.8, -1.6, 6.2], "color": "#efe6d2",
         "array": {"count": 8, "offset": [0.5, 0, 0]}},
    ]}
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        r = await hub.call("blender_build_blueprint", {"blueprint": bp})
    assert r["ok"] and not r["errors"], r
    assert r["built"] == 11                                         # 4 部件 + 7 个 array 副本
    d = r["parts"]["dome"]
    assert abs(d["size"][2] - 1.2) < 0.05 and abs(d["size"][0] - 2.4) < 0.1   # 半球：高=1.2
    assert abs(r["parts"]["merlon_7"]["center"][0] - (-1.8 + 3.5)) < 1e-3


async def test_blueprint_tolerates_sloppy_numbers(tmp_path: Path):
    bp = {"parts": [
        {"name": "plat", "shape": "rounded_box", "size": [10, 8, 2], "radius": [0.5, 0.5], "at": [0, 0, 1]},
        {"name": "tower", "shape": "cylinder", "radius": "0.8", "depth": [4], "color": "#eee",
         "attach": {"to": "plat", "side": "+Z", "overlap": [0.1]}},
    ]}
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        r = await hub.call("blender_build_blueprint", {"blueprint": bp})
    assert r["ok"] and not r["errors"], r
    assert r["built"] == 2 and abs(r["parts"]["tower"]["size"][2] - 4) < 0.01


async def test_array_along_and_clip(tmp_path: Path):
    bp = {"parts": [
        {"name": "wall", "shape": "box", "size": [10, 1, 3], "at": [0, 0, 1.5]},
        {"name": "merlon", "shape": "box", "size": [0.5, 0.5, 0.5],
         "attach": {"to": "wall", "side": "+Z"}, "array": {"count": 6, "along": "X"}},
        {"name": "wall2", "shape": "box", "size": [4, 1, 3], "at": [0, 5, 1.5]},
        {"name": "m2", "shape": "box", "size": [0.5, 0.5, 0.5],
         "attach": {"to": "wall2", "side": "+Z"}, "array": {"count": 20, "offset": [1, 0, 0]}},
    ]}
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        r = await hub.call("blender_build_blueprint", {"blueprint": bp})
    assert r["ok"] and not r["errors"], r
    xs = sorted(v["center"][0] for k, v in r["parts"].items() if k.startswith("merlon"))
    assert len(xs) == 6 and abs(xs[0] + 4.75) < 1e-3 and abs(xs[-1] - 4.75) < 1e-3   # 首尾贴齐墙端
    assert not r["floating"], r["floating"]                                           # 超出 wall2 的已裁掉
    assert any("已裁掉" in w for w in r["warnings"])


async def test_floating_uses_real_surface_distance(tmp_path: Path):
    # 两块球形岩石：包围盒角点相交，但球面之间实际有空隙 → 应判为悬空
    bp = {"parts": [
        {"name": "rock_a", "shape": "sphere", "size": [4, 4, 4], "at": [0, 0, 2]},
        {"name": "rock_b", "shape": "sphere", "size": [4, 4, 4], "at": [3.6, 3.6, 5.6]},
        {"name": "base", "shape": "box", "size": [6, 6, 0.5], "at": [0, 0, -0.25]},
    ]}
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        r = await hub.call("blender_build_blueprint", {"blueprint": bp})
    assert r["ok"], r
    assert r["floating"] == ["rock_b"], r["floating"]


def test_blueprint_patch():
    from genai_pipeline.pipeline import _apply_patch, _validate_blueprint
    bp = {"parts": [{"name": "wall", "shape": "box", "size": [10, 1, 3], "at": [0, 0, 1.5]},
                    {"name": "tower", "shape": "cylinder", "radius": 1, "depth": 6,
                     "attach": {"to": "wall", "side": "+X"}},
                    {"name": "gate", "shape": "box", "size": [2, 1, 2], "attach": {"to": "wall", "side": "-Y"}}]}
    nb, probs = _apply_patch(bp, {"remove": ["tower"], "update": {"wall": {"size": [12, 1, 4]}},
                                  "add": [{"name": "flag", "shape": "box", "size": [1, 0.1, 1],
                                           "attach": {"to": "keep", "side": "+Z"}},
                                          {"name": "keep", "shape": "box", "size": [3, 3, 8],
                                           "attach": {"to": "wall", "side": "+Z"}}]})
    assert not probs and _validate_blueprint(nb) is None
    names = [p["name"] for p in nb["parts"]]
    assert names.index("keep") < names.index("flag")          # 依赖顺序自动修正
    assert "tower" not in names and nb["parts"][0]["size"] == [12, 1, 4]
    assert nb["parts"][names.index("gate")] == bp["parts"][2]  # 未提及的部件原样保留
    assert _apply_patch(bp, {"update": {"ghost": {"size": [1, 1, 1]}}})[1]


async def test_gravity_snap(tmp_path: Path):
    bp = {"parts": [
        {"name": "ground", "shape": "box", "size": [10, 10, 1], "at": [0, 0, 0.5]},
        {"name": "shrub", "shape": "sphere", "size": [0.5, 0.5, 0.5], "at": [2, 2, 3]},     # 悬在空中 → 自动落地
        {"name": "moon", "shape": "sphere", "size": [1, 1, 1], "at": [30, 30, 30]},        # 下方无表面 → 仍悬空
    ]}
    async with MCPHub(tmp_path, servers=["blender"]) as hub:
        r = await hub.call("blender_build_blueprint", {"blueprint": bp})
    assert r["ok"], r
    assert r["auto_grounded"] == ["shrub"]
    assert abs(r["parts"]["shrub"]["center"][2] - (1.0 + 0.25 - 0.01)) < 0.03
    assert r["floating"] == ["moon"]
