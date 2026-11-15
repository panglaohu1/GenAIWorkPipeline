"""加载 config/pipeline.toml，并把相对路径解析到项目根目录。"""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("GENAI_PIPELINE_CONFIG", ROOT / "config" / "pipeline.toml"))


def resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    with open(CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


def get(dotted: str, default: Any = None) -> Any:
    node: Any = load()
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


ENVS = {
    "llm": ROOT / "envs" / "llm" / "bin" / "python",
    "image": ROOT / "envs" / "image" / "bin" / "python",
    "main": ROOT / ".venv" / "bin" / "python",
}
