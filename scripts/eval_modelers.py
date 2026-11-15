#!/usr/bin/env python3
"""建模能力 A/B：同一目标、同一评审（observe/verify/decide 固定 Bonsai），蓝图创建+补丁分别由 Bonsai（A 组）/ Gemma（B、D 组）/ Qwen3.8（C 组）/ Sage-A1.5（E 组）负责。
  submit：提交评测运行（项目 router-eval）；  report：汇总已完成的评测运行。
用法：.venv/bin/python scripts/eval_modelers.py submit | report
"""
import json
import sys
import urllib.request
import http.cookiejar
import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8701"
_client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
_signed_in = False


def api_open(request):
    """Reuse a console login session; never store the plaintext password."""
    global _signed_in
    if not _signed_in:
        if not sys.stdin.isatty():
            raise RuntimeError("控制台需要登录；请在交互终端运行此评测脚本")
        username = input("控制台用户名：").strip()
        password = getpass.getpass("控制台密码：")
        login = urllib.request.Request(API + "/api/auth/login", method="POST",
                                       data=json.dumps({"username": username, "password": password}).encode(),
                                       headers={"Content-Type": "application/json"})
        with _client.open(login):
            _signed_in = True
    return _client.open(request)
TARGETS = [
    ("scout-target.png", "侦察飞机", "低多边形玩具感单座侦察机：浅蓝色机身，米白色尖锥机头带深灰装饰条，半球形透明座舱罩内有深灰座椅，"
     "平直主翼和水平尾翼，一片竖直尾翼，翼尖有橙黄色小块。"),
    ("main-hall-target.png", "圣城旧城主殿", "地中海白石宫殿主殿：象牙白石砌殿身，中央浅金色大穹顶坐在开放拱廊鼓座上，两侧带小穹顶的圆塔，"
     "立面成排拱窗，屋顶城垛。低多边形、明亮。"),
]
# v3：SAM 抠出的干净目标图（水晶城拆分验证 20260927-203133-33f1），每个模型各跑一次
TARGETS_V3 = [
    ("sam-crystal-spire.png", "水晶尖塔", "莫比斯风格透明浅蓝六棱水晶尖塔：细长多面晶柱，顶部收尖，表面有棱线分面，底部带小段岩基。"),
    ("sam-rock-isle.png", "深蓝岩岛", "莫比斯风格深蓝色陡峭岩岛：多座尖锐岩峰簇拥成山，岩面有竖向裂脊，底部没入水面。"),
    ("sam-umbrella-tree.png", "粉伞花树", "莫比斯风格伞状花树：细长直立的浅色茎干，顶部是扁平张开的粉色伞状花冠，花冠边缘有放射状褶纹。"),
]
GROUPS = {"A-bonsai": {}, "B-gemma": {"blueprint": "vision", "patch": "vision"},
          "C-qwen38": {"blueprint": "fallback", "patch": "fallback"},
          "D-gemma31": {"blueprint": "gemma31", "patch": "gemma31"},
          "E-sage": {"blueprint": "sage", "patch": "sage"}}   # Sage-A1.5（llama.cpp）负责建模；评审仍固定 Bonsai
RUNS_FILE = ROOT / "docs" / ("modeler-eval-runs-v2.json" if len(sys.argv) > 2 else "modeler-eval-runs.json")
FIXED = {"observe": "main", "verify": "main", "decide": "main", "plan": "main"}


def post_run(title, brief, target, router, rounds=6, patience=4):
    import uuid
    boundary = uuid.uuid4().hex
    opts = {"project": "router-eval", "model_max_rounds": rounds, "plateau_patience": patience, "model_pass_score": 8.5,
            "approval_gates": [], "reference_as_target": True, "router": {**FIXED, **router}}
    parts = []
    for k, v in (("title", title), ("brief", brief), ("mode", "asset"), ("options", json.dumps(opts, ensure_ascii=False))):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    img = (ROOT / "projects/router-eval/inputs" / target).read_bytes()
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"references\"; filename=\"{target}\"\r\n"
                 "Content-Type: image/png\r\n\r\n".encode() + img + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{API}/api/runs", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with api_open(req) as r:
        return json.load(r)["id"]


def get(path):
    with api_open(API + path) as r:
        return json.load(r)


def submit3():
    """submit3 [组名 ...]：v3 干净目标图，每组最多 4 轮、连续 3 轮无进步即停。"""
    runs_file = ROOT / "docs" / "modeler-eval-runs-v3.json"
    ids = json.loads(runs_file.read_text()) if runs_file.exists() else {}
    for target, name, brief in TARGETS_V3:
        for g in sys.argv[2:]:
            ids[f"{name}/{g}"] = post_run(f"[评测v3 {g}] {name}", brief, target, GROUPS[g], rounds=4, patience=3)
    runs_file.write_text(json.dumps(ids, ensure_ascii=False, indent=1))
    print(json.dumps(ids, ensure_ascii=False, indent=1))


def submit():
    """submit [组名 ...]：只提交指定组（默认 A/B 两组）；指定组时写入 v2 运行清单（客观评分版）。"""
    groups = sys.argv[2:] or ["A-bonsai", "B-gemma"]
    ids = json.loads(RUNS_FILE.read_text()) if RUNS_FILE.exists() and len(sys.argv) > 2 else {}
    for target, name, brief in TARGETS:
        for g in groups:
            ids[f"{name}/{g}"] = post_run(f"[评测 {g}] {name}", brief, target, GROUPS[g])
    RUNS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1))
    print(json.dumps(ids, ensure_ascii=False, indent=1))


def report():
    """report [v2]：v2 读取客观评分版的运行清单，并输出各轮客观相似度。"""
    ids = json.loads(RUNS_FILE.read_text())
    out = {}
    for key, rid in ids.items():
        d = get(f"/api/runs/{rid}")
        m = next(iter(d["state"].get("models", {}).values()), {})
        rounds = m.get("rounds", [])
        best = rounds[m["best"]] if rounds and m.get("best") is not None else None
        out[key] = {"run": rid, "status": d["run"]["status"], "rounds": len(rounds),
                    "scores": [r["score"] for r in rounds], "best_round": best and best["round"],
                    "best_score": best and best["score"],
                    "sec_per_round": round(sum(r["agent"]["seconds"] for r in rounds) / len(rounds)) if rounds else None,
                    "build_errors": sum(len(r["build"].get("errors") or []) for r in rounds),
                    "best_compare": best and best["observation"]["compare"],
                    "routes": d["state"].get("_routes"),
                    "dino": [r.get("metrics", {}).get("dino") if r.get("metrics") else None for r in rounds],
                    "siglip": [r.get("metrics", {}).get("siglip") if r.get("metrics") else None for r in rounds],
                    "best_dino": best and (best.get("metrics") or {}).get("dino"),
                    "best_siglip": best and (best.get("metrics") or {}).get("siglip"),
                    "best_checks_ok": best and all(c.get("passed") for c in best.get("checks", []) if c.get("required"))}
        print(key, json.dumps(out[key], ensure_ascii=False))
    (ROOT / "docs" / ("modeler-eval-v2.json" if len(sys.argv) > 2 else "modeler-eval.json")).write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    {"submit": submit, "submit3": submit3, "report": report}[sys.argv[1]]()
