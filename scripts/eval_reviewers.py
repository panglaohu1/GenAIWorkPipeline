#!/usr/bin/env python3
"""视觉评审 A/B：同一批“目标|当前”对比图，分别让 Bonsai（main）与 Gemma 4（vision）按差异台账打分，
与参考评分（Claude 独立打分，代替前沿模型）比较：平均绝对偏差、排序一致性、重复打分的稳定性、速度。
用法：.venv/bin/python scripts/eval_reviewers.py [--repeat 2]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from genai_pipeline import jev  # noqa: E402
from genai_pipeline.llm import LLM, image_part  # noqa: E402

# 参考评分：Claude 对这些对比图的独立打分（0–10，按与目标图的还原度）
SAMPLES = [
    ("runs/20260925-210545-c0f2/jev/tiger_messenger_scout_aircraft_r03_compare.jpg", "侦察机", 3.0),
    ("runs/20260925-210545-c0f2/jev/tiger_messenger_scout_aircraft_r05_compare.jpg", "侦察机", 4.0),
    ("runs/20260925-210545-c0f2/jev/tiger_messenger_scout_aircraft_r07_compare.jpg", "侦察机", 4.5),
    ("runs/20260925-210545-c0f2/jev/tiger_messenger_scout_aircraft_r11_compare.jpg", "侦察机", 6.5),
    ("runs/20260925-224829-8bd1/jev/coastal_cliff_palace_city_r01_compare.jpg", "海边悬崖白石宫城", 2.5),
    ("runs/20260925-224829-8bd1/jev/coastal_cliff_palace_city_r02_compare.jpg", "海边悬崖白石宫城", 3.0),
    ("runs/20260925-224829-8bd1/jev/coastal_cliff_palace_city_r09_compare.jpg", "海边悬崖白石宫城", 3.0),
    ("runs/20260925-224829-8bd1/jev/coastal_cliff_palace_city_r18_compare.jpg", "海边悬崖白石宫城", 3.5),
]


async def score(model_key: str, path: str, subject: str) -> tuple[float, int, float]:
    msg = {"role": "user", "content": [{"type": "text", "text": f"对象：{subject}\n请观察下图并输出差异台账 JSON。"},
                                       image_part(ROOT / path, max_side=1536)]}
    t = time.time()
    obs = await LLM(model_key).json([{"role": "system", "content": jev.OBSERVE_SYSTEM}, msg], thinking_budget=4096,
                                    validate=lambda o: None if isinstance(o, dict) and isinstance(o.get("diffs"), list) else "需要 diffs")
    diffs = [d for d in obs["diffs"] if int(d.get("severity", 1)) in (1, 2, 3)]
    return jev.ledger_score(diffs), len(diffs), time.time() - t


def spearman(a, b):
    def rank(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        for k, i in enumerate(order):
            r[i] = k
        return r
    ra, rb = rank(a), rank(b)
    n = len(a)
    return 1 - 6 * sum((p - q) ** 2 for p, q in zip(ra, rb)) / (n * (n * n - 1))


async def main(repeat: int) -> None:
    out = {}
    for key in ("main", "vision"):
        rows = []
        for path, subject, ref in SAMPLES:
            runs = [await score(key, path, subject) for _ in range(repeat)]
            s = [r[0] for r in runs]
            rows.append({"image": Path(path).name, "ref": ref, "scores": s, "mean": sum(s) / len(s),
                         "n_diffs": [r[1] for r in runs], "seconds": round(sum(r[2] for r in runs) / len(runs), 1)})
            print(key, rows[-1], flush=True)
        means = [r["mean"] for r in rows]
        refs = [r["ref"] for r in rows]
        mae = sum(abs(m - r) for m, r in zip(means, refs)) / len(rows)
        spread = sum(max(r["scores"]) - min(r["scores"]) for r in rows) / len(rows)
        out[key] = {"rows": rows, "mae_vs_ref": round(mae, 2), "spearman_vs_ref": round(spearman(means, refs), 2),
                    "repeat_spread": round(spread, 2), "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1)}
    (ROOT / "docs" / "reviewer-eval.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for k, v in out.items():
        print(f"{k}: 与参考评分平均偏差 {v['mae_vs_ref']}，排序相关 {v['spearman_vs_ref']}，"
              f"重复打分波动 {v['repeat_spread']}，平均每张 {v['avg_seconds']}s")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 2))
