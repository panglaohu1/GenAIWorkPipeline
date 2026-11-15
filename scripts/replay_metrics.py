"""回放评测：用客观图像特征相似度（SigLIP-2 / DINOv2）给已有运行的每轮渲染重新打分，
与当时的 LLM 台账分并排输出，用来校准新评分。用 envs/vision 的 Python 运行：

    envs/vision/bin/python scripts/replay_metrics.py RUN_ID [RUN_ID ...] > docs/metric-replay.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from genai_pipeline.metrics_server import similarity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def score_round(target: str, renders: list[str]) -> dict:
    return {k: {"max": v["max"], "mean": v["mean"]} for k, v in similarity(target, renders).items()}


def main(run_ids: list[str]) -> None:
    result = []
    for rid in run_ids:
        st = json.loads((ROOT / "runs" / rid / "state.json").read_text())
        concepts = st.get("concepts", {})
        for aid, m in (st.get("models") or {}).items():
            c = concepts.get(aid)
            if not c:
                continue
            target = c["candidates"][c.get("selected", 0)]["path"]
            for r in m.get("rounds", []):
                rs = [p for p in r.get("renders", []) if Path(p).exists()]
                if not rs:
                    continue
                result.append({"run": rid, "asset": aid, "round": r["round"], "ledger": r["score"],
                               "checks_ok": all(x["passed"] for x in r.get("checks", []) if x.get("required")),
                               **score_round(target, rs)})
                print(json.dumps(result[-1], ensure_ascii=False), file=sys.stderr)
        comp = st.get("compose")
        scene = concepts.get("_scene")
        if comp and scene:
            target = scene["candidates"][scene.get("selected", 0)]["path"]
            for r in comp.get("rounds", []):
                rs = [p for p in r.get("renders", []) if Path(p).exists()]
                if rs:
                    result.append({"run": rid, "asset": "_scene", "round": r["round"], "ledger": r["score"],
                                   **score_round(target, rs)})
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
