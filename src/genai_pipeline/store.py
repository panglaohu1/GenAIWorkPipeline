"""SQLite 持久化：运行、阶段、事件、交付物。另含进程内事件总线供控制台 SSE 推送。"""
from __future__ import annotations

import asyncio
import os
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, title TEXT, brief TEXT, mode TEXT, status TEXT,
  options TEXT, current_stage TEXT, created REAL, updated REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS stages (
  run_id TEXT, name TEXT, idx INTEGER, status TEXT, started REAL, finished REAL,
  detail TEXT, PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, stage TEXT,
  kind TEXT, message TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, run_id TEXT, stage TEXT, kind TEXT, path TEXT,
  label TEXT, meta TEXT, created REAL
);
CREATE INDEX IF NOT EXISTS artifacts_run ON artifacts(run_id, created);
"""


class Bus:
    """线程安全的发布/订阅：流水线线程 publish，SSE 协程 subscribe。"""

    def __init__(self) -> None:
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s[1] is not q]

    def publish(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            def put(q=q):
                if not q.full():
                    q.put_nowait(msg)
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                pass


bus = Bus()


class Store:
    def __init__(self, path: Path | None = None) -> None:
        # GENAI_DB_PATH：测试等场合改用别的库，绝不碰正在运行的控制台的 data/pipeline.db
        env = os.environ.get("GENAI_DB_PATH")
        self.path = path or (Path(env) if env else config.resolve(config.get("pipeline.db_path", "data/pipeline.db")))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    # ── runs ──
    def create_run(self, title: str, brief: str, mode: str, options: dict, stages: list[str], *, status: str = 'queued') -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        now = time.time()
        c = self._conn()
        c.execute('BEGIN IMMEDIATE')
        try:
            c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (run_id, title, brief, mode, status, json.dumps(options, ensure_ascii=False), None, now, now, None))
            for i, s in enumerate(stages):
                c.execute("INSERT INTO stages VALUES (?,?,?,?,?,?,?)", (run_id, s, i, "pending", None, None, "{}"))
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise
        bus.publish({"type": "run", "run_id": run_id})
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> None:
        if "options" in fields:
            fields["options"] = json.dumps(fields["options"], ensure_ascii=False)
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn().execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))
        bus.publish({"type": "run", "run_id": run_id, **{k: v for k, v in fields.items() if k != "options"}})

    def get_run(self, run_id: str) -> dict | None:
        r = self._conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["options"] = json.loads(d["options"] or "{}")
        return d

    def list_runs(self, limit: int = 100) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM runs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = json.loads(d["options"] or "{}")
            out.append(d)
        return out

    def delete_run(self, run_id: str) -> None:
        c = self._conn()
        for t, col in (("events", "run_id"), ("artifacts", "run_id"), ("stages", "run_id"), ("runs", "id")):
            c.execute(f"DELETE FROM {t} WHERE {col}=?", (run_id,))
        bus.publish({"type": "run_deleted", "run_id": run_id})

    # ── stages ──
    def stages(self, run_id: str) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM stages WHERE run_id=? ORDER BY idx", (run_id,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]

    def set_stage(self, run_id: str, name: str, status: str, detail: dict | None = None) -> None:
        c = self._conn()
        now = time.time()
        row = c.execute("SELECT detail FROM stages WHERE run_id=? AND name=?", (run_id, name)).fetchone()
        merged = json.loads(row["detail"]) if row and row["detail"] else {}
        if status in ("running", "pending"):
            merged.pop("error", None)  # 重跑时清掉上一次的失败信息
        if detail:
            merged.update(detail)
        sets = ["status=?", "detail=?"]
        vals: list[Any] = [status, json.dumps(merged, ensure_ascii=False)]
        if status == "running":
            sets += ["started=?", "finished=NULL"]
            vals.append(now)
        elif status in ("done", "failed", "skipped"):
            sets.append("finished=?")
            vals.append(now)
        c.execute(f"UPDATE stages SET {', '.join(sets)} WHERE run_id=? AND name=?", (*vals, run_id, name))
        bus.publish({"type": "stage", "run_id": run_id, "stage": name, "status": status, "detail": merged})

    # ── events ──
    def event(self, run_id: str, stage: str | None, kind: str, message: str, data: Any = None) -> None:
        ts = time.time()
        payload = json.dumps(data, ensure_ascii=False, default=str) if data is not None else None
        cur = self._conn().execute("INSERT INTO events (run_id, ts, stage, kind, message, data) VALUES (?,?,?,?,?,?)",
                                   (run_id, ts, stage, kind, message, payload))
        bus.publish({"type": "event", "run_id": run_id, "id": cur.lastrowid, "ts": ts, "stage": stage,
                     "kind": kind, "message": message, "data": data})

    def events(self, run_id: str, after: int = 0, limit: int = 500) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id DESC LIMIT ?", (run_id, after, limit)).fetchall()
        out = [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in rows]
        return out[::-1]

    # ── artifacts ──
    def add_artifact(self, run_id: str, stage: str, kind: str, path: Path | str, label: str = "",
                     meta: dict | None = None) -> str:
        aid = uuid.uuid4().hex[:12]
        self._conn().execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)",
                             (aid, run_id, stage, kind, str(path), label,
                              json.dumps(meta or {}, ensure_ascii=False, default=str), time.time()))
        bus.publish({"type": "artifact", "run_id": run_id, "id": aid, "stage": stage, "kind": kind,
                     "label": label, "meta": meta or {}})
        return aid

    def update_artifact_meta(self, aid: str, **meta: Any) -> None:
        c = self._conn()
        row = c.execute("SELECT meta, run_id FROM artifacts WHERE id=?", (aid,)).fetchone()
        if not row:
            return
        m = json.loads(row["meta"] or "{}")
        m.update(meta)
        c.execute("UPDATE artifacts SET meta=? WHERE id=?", (json.dumps(m, ensure_ascii=False, default=str), aid))
        bus.publish({"type": "artifact_meta", "run_id": row["run_id"], "id": aid, "meta": m})

    def artifacts(self, run_id: str) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY created", (run_id,)).fetchall()
        return [{**dict(r), "meta": json.loads(r["meta"] or "{}")} for r in rows]

    def get_artifact(self, aid: str) -> dict | None:
        r = self._conn().execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        return {**dict(r), "meta": json.loads(r["meta"] or "{}")} if r else None
