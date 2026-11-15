#!/usr/bin/env python3
"""Oskar 顾问 Agent × Bonsai 端到端试做（与流水线 oskar 模式共用 genai_pipeline.oskar.agent）。

用法：GENAI_BLENDER_PORT=9899 PYTHONPATH=src .venv/bin/python scripts/oskar_bonsai.py TARGET.png OUT_DIR
      [--rounds 3] [--no-sam] [--model main] [--program PROG.json]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from genai_pipeline.mcp_hub import MCPHub  # noqa: E402
from genai_pipeline.oskar import agent  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("out")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--no-sam", action="store_true")
    ap.add_argument("--model", default="main")
    ap.add_argument("--program")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    prog = json.loads(Path(a.program).read_text()) if a.program else None
    async with MCPHub(out, servers=["blender"]) as hub:
        res = await agent.run(str(Path(a.target).resolve()), hub, out, model=a.model, rounds=a.rounds,
                              use_sam=not a.no_sam, program=prog,
                              emit=lambda k, m, d=None: print(f"[{k}] {m}", file=sys.stderr))
    hist = res["history"]
    best = res["best"]
    log = {"read": res.get("read"), "facts": res.get("facts"), "steps": res.get("steps"),
           "history": [{k: v for k, v in h.items() if k != "program"} for h in hist],
           "best_round": best["round"], "best_program": best["program"]}
    (out / "log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1))
    print(json.dumps({"best_round": best["round"], "best_metrics": best.get("metrics"), "checks_ok": best.get("checks_ok"),
                      "rounds": [(h["round"], h.get("objective") and round(h["objective"], 1), h.get("accepted"),
                                  h.get("checks_ok"), h.get("error")) for h in hist], "steps": res.get("steps")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
