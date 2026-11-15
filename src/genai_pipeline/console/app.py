"""Web 控制台：FastAPI + SSE。启动: .venv/bin/python -m genai_pipeline.console"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from functools import lru_cache

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..pipeline import STAGE_LABELS, STAGES, Orchestrator
from ..services import LOGS, manager, MemoryBudgetError, ServiceBusyError
from ..compute import compute
from ..quality import assess
from .. import style_agents, consultation, assembly, teams, agent_config
from .auth import auth, COOKIE
from ..store import Store, bus

STATIC = Path(__file__).with_name("static")
RUNS = config.resolve(config.get("pipeline.runs_dir", "runs"))

app = FastAPI(title="GenAI Work Pipeline")
store = Store()
READ_ONLY = os.environ.get("GENAI_CONSOLE_READ_ONLY") == "1"
orch = Orchestrator(store, start_worker=not READ_ONLY)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def read_only_guard(request: Request, call_next):
    path = request.url.path
    mutation = request.method not in ("GET", "HEAD", "OPTIONS")
    origin = request.headers.get("origin")
    if mutation and origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
    public = path in ("/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/register")
    if path.startswith("/api/") and not public and not auth.user(request.cookies.get(COOKIE)):
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    if READ_ONLY and mutation and not path.startswith("/api/auth/"):
        return JSONResponse({"detail": "当前控制台为只读预览，不能修改或执行任务"}, status_code=403)
    return await call_next(request)


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"configured": auth.configured, "user": auth.user(request.cookies.get(COOKIE))}


@app.post("/api/auth/{action}")
async def auth_action(action: str, request: Request):
    if action == "logout":
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response
    if action not in ("login", "setup", "register"):
        raise HTTPException(404)
    if action == "register" and (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "只能在本机上注册账户")
    body = await request.json()
    username, password = str(body.get("username", "")), str(body.get("password", ""))
    try:
        if action == "setup":
            auth.setup(username, password)
        elif action == "register":
            auth.register(username, password)
        token = auth.login(username.strip(), password, request.client.host if request.client else "local")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    response = JSONResponse({"ok": True, "user": username.strip()})
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=43200)
    return response


@app.post("/api/system/restart")
async def system_restart(request: Request):
    """重启本机控制台（加载新代码）：只接受本机请求。后台运行 scripts/restart_console.command，
    它会结束当前进程再启动新进程，输出写到 logs/console_restart.log；登录会话已持久化，重启后不必重新登录。"""
    if (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "只能在本机重启")
    import subprocess
    root = Path(__file__).resolve().parents[3]
    log = open(root / "logs" / "console_restart.log", "ab")
    subprocess.Popen(["/bin/zsh", str(root / "scripts" / "restart_console.command")], cwd=root, stdout=log,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "detail": "控制台正在重启，约 30 秒后刷新页面"}


@app.get("/api/agents")
def list_agents():
    agents = style_agents.catalog()
    for agent in agents:
        agent["profiles"] = [{k: v for k, v in p.items() if k != "sources"} for p in agent["profiles"]]
        agent["ratchet"] = teams.ratchet(agent["id"])
        if agent.get("kind") == "team":   # 名册不下发长提示词
            agent["members"] = teams.public_roster(agent)
    return agents


@app.post("/api/agents")
async def create_agent(request: Request):
    data = await request.json()
    try:
        return style_agents.create(str(data.get("name", "")), str(data.get("description", "")), data.get("principles") or [])
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/agents/{agent_id}/knowledge/{source_id}")
def agent_knowledge_source(agent_id: str, source_id: str):
    try:
        path = style_agents.knowledge_source(agent_id, source_id)
        return FileResponse(path, media_type="text/plain; charset=utf-8")
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/agents/{agent_id}/config")
def agent_config_get(agent_id: str):
    try:
        cfg = agent_config.get_config(agent_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"config": cfg, "models": agent_config.MODELS, "tools": agent_config.TOOLS,
            "autonomy": agent_config.AUTONOMY, "stages": list(agent_config.STAGES)}


@app.get("/api/skills")
def skills_list():
    return {"skills": agent_config.list_skills(), "lifecycle": agent_config.LIFECYCLE_CN,
            "tools": agent_config.TOOLS, "stages": list(agent_config.STAGES)}


@app.get("/api/skills/route")
def skills_route(agent: str, stage: str, q: str = "", member: str = ""):
    try:
        snap = style_agents.snapshot(agent)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"picked": [{k: s.get(k) for k in ("id", "name", "icon", "lifecycle", "score")}
                       for s in agent_config.route_skills(snap, stage, q, member)]}


@app.post("/api/skills")
async def skills_save(request: Request):
    try:
        return agent_config.save_skill(await request.json())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/skills/{skill_id}/{action}")
def skills_action(skill_id: str, action: str):
    try:
        if action == "delete":
            agent_config.delete_skill(skill_id)
            return {"ok": True}
        return agent_config.set_lifecycle(skill_id, action)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/agents/{agent_id}/{action}")
async def agent_action(agent_id: str, action: str, request: Request):
    data = await request.json()
    try:
        agent = style_agents.get(agent_id)
        if action == "sources":
            return style_agents.import_source(agent_id, data.get('title', ''), data.get('text', ''), data.get('url', ''))
        if action == "activate":
            return style_agents.activate(agent_id, str(data.get("version", "baseline")))
        if action == "config":   # Agent 配置：人格 / 模型 / 技能 / 工具 / 自主级别 / 预算 / 团队成员
            return agent_config.save_config(agent_id, data)
        if action == "learn":
            sources = data.get("runs") or []
            if not isinstance(sources, list) or len(sources) > 5 or any(not isinstance(r, str) or not store.get_run(r) for r in sources):
                raise ValueError("请选择最多 5 个有效历史运行")
            run_id = orch.submit(f"{agent['name']} · 风格学习", str(data.get("brief", "从参考资料提炼风格与构建规则。"))[:4000],
                                "learn", {"style_agent": agent_id, "learning_runs": sources,
                                          "learning_source_ids": data.get('sources', [])})
            return {"id": run_id}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    raise HTTPException(400, "未知 Agent 操作")


@app.get('/api/agents/{agent_id}/sources/{source_id}')
def agent_source(agent_id: str, source_id: str):
    try:
        record = style_agents.source(agent_id, source_id)
        return PlainTextResponse(record['text'], headers={'X-Content-Type-Options':'nosniff'})
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post('/api/agents/{agent_id}/sources/{source_id}/archive')
async def archive_agent_source(agent_id: str, source_id: str, request: Request):
    data = await request.json()
    try:
        if not isinstance(data.get('archived'), bool):
            raise ValueError('请指定归档或恢复')
        return style_agents.archive_source(agent_id, source_id, data['archived'])
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── Muse 文件收发箱：Muse（Meta 的 Mac 智能体）不支持 MCP，但能读写文件 ──
MUSE = config.ROOT / "muse"


def _muse_bridge() -> None:
    """muse/inbox/*.json|*.md → 自动建立运行；运行进展写入 muse/outbox/<运行ID>.md 与 STATUS.md。"""
    import shutil as _sh
    import threading
    import time as _t

    def parse(f: Path) -> dict:
        if f.suffix == ".json":
            return json.loads(f.read_text())
        # Markdown：首行“# 标题”，可选“模式: game|asset|scene”“项目: id”，其余为需求
        lines = f.read_text().splitlines()
        req = {"title": "", "brief": "", "mode": "asset", "options": {}}
        body = []
        for ln in lines:
            t = ln.strip()
            if t.startswith("# ") and not req["title"]:
                req["title"] = t[2:].strip()
            elif t.split("：")[0].split(":")[0].strip() in ("模式", "mode"):
                req["mode"] = t.replace("：", ":").split(":", 1)[1].strip()
            elif t.split("：")[0].split(":")[0].strip() in ("项目", "project"):
                req["options"]["project"] = t.replace("：", ":").split(":", 1)[1].strip()
            else:
                body.append(ln)
        req["brief"] = "\n".join(body).strip()
        return req

    def write_outbox() -> None:
        rows = []
        for r in store.list_runs(50):
            stages = store.stages(r["id"])
            st_file = RUNS / r["id"] / "state.json"
            state = json.loads(st_file.read_text()) if st_file.exists() else {}
            lines = [f"# {r['title']}", "", f"- 运行 ID：{r['id']}", f"- 状态：{r['status']}",
                     "- 阶段：" + " → ".join(f"{s['name']}({s['status']})" for s in stages),
                     f"- 控制台：http://127.0.0.1:{config.get('console.port', 8701)}/#{r['id']}"]
            if r.get("error"):
                lines.append(f"- 错误：{r['error']}")
            for aid, m in (state.get("models") or {}).items():
                if m.get("rounds"):
                    b = m["rounds"][m.get("best") or 0]
                    lines.append(f"- 资产 {aid}：{len(m['rounds'])} 轮，最佳第 {b['round']} 轮 {b['score']} 分"
                                 + (f"，GLB：{m['glb']}" if m.get("glb") else "")
                                 + (f"，对比图：{b['observation']['compare']}" if b.get("observation") else ""))
            comp = state.get("compose") or {}
            if comp.get("rounds"):
                c = comp["rounds"][comp.get("champion") or 0]
                lines.append(f"- 场景组装：{len(comp['rounds'])} 轮，冠军第 {c['round']} 轮 {c['score']} 分"
                             + (f"，GLB：{comp['glb']}" if comp.get("glb") else ""))
            zipf = RUNS / r["id"] / f"{r['id']}-deliverables.zip"
            if zipf.exists():
                lines.append(f"- 交付包：{zipf}")
            (MUSE / "outbox" / f"{r['id']}.md").write_text("\n".join(lines) + "\n")
            rows.append(f"| {r['id']} | {r['title']} | {r['status']} | {r.get('current_stage') or ''} |")
        (MUSE / "outbox" / "STATUS.md").write_text(
            "# GenAI Pipeline 运行状态\n\n更新时间：" + _t.strftime("%Y-%m-%d %H:%M:%S") +
            "\n\n| 运行 ID | 标题 | 状态 | 当前阶段 |\n|---|---|---|---|\n" + "\n".join(rows) + "\n")

    def loop() -> None:
        (MUSE / "inbox" / "processed").mkdir(parents=True, exist_ok=True)
        (MUSE / "outbox").mkdir(parents=True, exist_ok=True)
        while True:
            try:
                for f in sorted((MUSE / "inbox").glob("*")):
                    if f.suffix not in (".json", ".md") or f.name.startswith(("_", ".")) or f.name == "README.md":
                        continue
                    try:
                        req = parse(f)
                        mode = req.get("mode", "asset") if req.get("mode") in STAGES else "asset"
                        opts = req.get("options") or {}
                        if req.get("project"):
                            opts["project"] = req["project"]
                        rid = orch.submit(req.get("title", ""), req["brief"], mode, opts, start=False)
                        for ref in req.get("references") or []:
                            src = Path(ref).expanduser()
                            if src.is_file():
                                dst = RUNS / rid / "references" / src.name
                                dst.parent.mkdir(parents=True, exist_ok=True)
                                _sh.copy2(src, dst)
                                store.add_artifact(rid, "plan", "reference", str(dst), src.name)
                        if opts.get("project"):
                            _link_run(opts["project"], rid)
                        orch.enqueue(rid)
                        store.event(rid, None, "control", f"由 Muse 收件箱创建：{f.name}", req)
                        _sh.move(str(f), MUSE / "inbox" / "processed" / f"{rid}__{f.name}")
                    except Exception as e:  # 无法解析的文件改名标记，避免反复处理
                        f.rename(f.with_name(f"_error_{f.name}"))
                        (MUSE / "outbox" / f"error_{f.stem}.md").write_text(f"# 任务文件无法处理\n\n{f.name}：{e}\n")
                write_outbox()
            except Exception:
                pass
            _t.sleep(10)
    threading.Thread(target=loop, daemon=True, name="muse-bridge").start()


def _autostart() -> None:
    """后台线程：模型下载完成后自动拉起 autostart 服务（主力 LLM）。"""
    import threading
    import time

    def loop():
        warmed = set()
        while True:
            snapshot = compute.snapshot()
            if snapshot["active"] or snapshot["queued"]:
                time.sleep(2)
                continue
            for s in manager.services.values():
                if s.name not in warmed and getattr(s, "autostart", False) and not s.missing() and not manager._alive(s) \
                        and not s.last_error and not manager.healthy(s.name, timeout=1):
                    try:
                        manager.start(s.name)
                    except MemoryBudgetError:
                        pass
                    warmed.add(s.name)
            time.sleep(20)
    threading.Thread(target=loop, daemon=True, name="autostart").start()


if not READ_ONLY:
    _autostart()


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    logged_in = bool(auth.user(request.cookies.get(COOKIE)))
    # 按文件修改时间加版本号，避免浏览器缓存旧的 JS/CSS
    files = (("app.js", "style.css", "workspace.js", "workspace.css", "assembly.js", "assembly.css", "theme.css", "world.svg") if logged_in
             else ("login.js", "login.css", "theme.css", "world.svg"))
    v = int(max((STATIC / f).stat().st_mtime for f in files))
    html = (STATIC / ("index.html" if logged_in else "login.html")).read_text()
    for f in files:
        html = html.replace(f"/static/{f}", f"/static/{f}?v={v}")
    return html


# ── 服务 ──
@app.get("/api/status")
def status() -> dict:
    st = manager.status()
    st["read_only"] = READ_ONLY
    st["pipeline"] = {"current": orch.current, "queued": [r["id"] for r in store.list_runs(200)
                                                           if r["status"] == "queued"]}
    st["blender_worker"] = _port_open(config.get("blender.worker_port", 9877))
    st["stage_labels"] = STAGE_LABELS
    from ..pipeline import ROLES
    st["router"] = {r: {"label": lbl, "model": config.get(f"router.{r}", "main")} for r, lbl in ROLES.items()}
    st["modes"] = STAGES
    st["defaults"] = {k: config.get(f"pipeline.{k}") for k in
                      ("concept_candidates", "model_max_rounds", "model_pass_score", "godot_max_rounds",
                       "agent_max_steps", "approval_gates")}
    return st


@app.post("/api/services/{name}/{action}")
async def service_action(name: str, action: str) -> dict:
    if name not in manager.services:
        raise HTTPException(404)
    try:
        if action == "start":
            return await asyncio.to_thread(manager.start, name)
        if action == "stop":
            return await asyncio.to_thread(manager.stop, name)
    except (MemoryBudgetError, ServiceBusyError) as exc:
        raise HTTPException(409, str(exc))
    raise HTTPException(400, "action must be start|stop")


@app.get("/api/services/{name}/log")
def service_log(name: str, lines: int = 200) -> dict:
    f = LOGS / f"{name}.log"
    if not f.exists():
        return {"text": ""}
    data = f.read_bytes()[-200_000:].decode(errors="replace").splitlines()[-lines:]
    return {"text": "\n".join(data)}


def _port_open(port: int) -> bool:
    import socket
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


# ── 项目（验证目录）──
PROJECTS = config.ROOT / "projects"


def _projects() -> list[dict]:
    out = []
    for f in sorted(PROJECTS.glob("*/project.json")):
        try:
            out.append(json.loads(f.read_text()))
        except ValueError:
            continue
    return out


def _link_run(project: str, run_id: str) -> None:
    """在 projects/<项目>/runs/ 下建立指向运行目录的链接，形成独立的验证目录。"""
    d = PROJECTS / project / "runs"
    d.mkdir(parents=True, exist_ok=True)
    link = d / run_id
    if not link.exists():
        link.symlink_to(RUNS / run_id, target_is_directory=True)
    pj = PROJECTS / project / "project.json"
    if pj.exists():
        data = json.loads(pj.read_text())
        if run_id not in data.setdefault("runs", []):
            data["runs"].append(run_id)
            pj.write_text(json.dumps(data, ensure_ascii=False, indent=1))


@app.get("/api/projects")
def projects() -> list[dict]:
    return _projects()


@app.post("/api/projects")
async def create_project(request: Request) -> dict:
    import re
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "需要项目名称")
    pid = re.sub(r"[^a-z0-9-]+", "-", str(body.get("id") or "").lower()).strip("-") or \
        f"project-{len(_projects()) + 1}"
    d = PROJECTS / pid
    (d / "inputs").mkdir(parents=True, exist_ok=True)
    data = {"id": pid, "name": name, "mode": body.get("mode", "asset"), "description": body.get("description", ""),
            "target": None, "runs": []}
    (d / "project.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return data


# ── 运行 ──
@lru_cache(maxsize=256)
def _cached_state(path: str, mtime_ns: int) -> dict:
    return json.loads(Path(path).read_text())


def _read_state(run_id: str) -> dict:
    path = RUNS / run_id / "state.json"
    try:
        return _cached_state(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError):
        return {}


@app.get("/api/capabilities")
async def capabilities_catalog() -> dict:
    """能力目录：本管线的能力 + TigerInBamboo 能力库（体素 / 物件 / 通用场景 / 球系统），附每个能力的经验汇总。"""
    from .. import capabilities as CAPS
    out = await CAPS.catalog()
    tiers = {"scene3d": out.get("native") or []}
    ext = out.get("external") if isinstance(out.get("external"), dict) else {}
    for t in ("voxel", "object", "gscene", "planet"):
        if isinstance(ext.get(t), list):
            tiers[t] = ext[t]
    for t, caps in tiers.items():
        exp = CAPS.experience(t)
        for c in caps:
            c["experience"] = exp.summary(c["id"])
    return out


@app.get("/api/runs")
def runs() -> list[dict]:
    out = []
    for r in store.list_runs(200):
        r["stages"] = [{"name": s["name"], "status": s["status"]} for s in store.stages(r["id"])]
        quality = assess(r, _read_state(r["id"]))
        r["quality"] = {k: quality[k] for k in ("status", "label", "failed_count", "unknown_count", "suggested_stage")}
        if 'learning_sources' in r['options']:
            r['options']['learning_sources'] = [{k:v for k,v in s.items() if k != 'text'}
                                                for s in r['options']['learning_sources']]
        out.append(r)
    return out


@app.get("/api/assembly/catalog")
def assembly_catalog():
    return assembly.catalog(store)


@app.post("/api/assembly")
async def create_assembly(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, '请提供组装信息')
    try:
        return {'id': orch.compose_game(data.get('title'), data.get('brief'), data.get('selections'),
                                       data.get('agent_id', 'oskar'), data.get('request_id'),
                                       data.get('form') or 'levels')}
    except (ValueError, OSError, KeyError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/runs")
async def create_run(brief: str = Form(...), mode: str = Form("game"), title: str = Form(""),
                     options: str = Form("{}"), references: list[UploadFile] | None = File(None)) -> dict:
    if mode not in STAGES:
        raise HTTPException(400, f"mode 必须是 {list(STAGES)} 之一")
    if mode == 'consult':
        raise HTTPException(400, '请从已有作品的邀请顾问入口提交')
    opts = json.loads(options or "{}")
    try:
        run_id = orch.submit(title, brief, mode, opts, start=False)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    ref_dir = RUNS / run_id / "references"
    project = opts.get("project")
    if project:
        _link_run(project, run_id)
        pj = PROJECTS / project / "project.json"
        tgt = json.loads(pj.read_text()).get("target") if pj.exists() else None
        if tgt and not any(f.filename for f in (references or [])):  # 未上传参考图时使用项目目标图
            src = PROJECTS / project / tgt
            if src.exists():
                ref_dir.mkdir(parents=True, exist_ok=True)
                dst = ref_dir / src.name
                dst.write_bytes(src.read_bytes())
                store.add_artifact(run_id, {"scene": "decompose", "scene3d": "decompose", "oskar": "oskar_plan"}.get(mode, "plan"), "reference", str(dst),
                                   f"项目目标图 {src.name}")
    for f in references or []:
        if not f.filename:
            continue
        ref_dir.mkdir(parents=True, exist_ok=True)
        dst = ref_dir / Path(f.filename).name
        dst.write_bytes(await f.read())
        store.add_artifact(run_id, "plan", "reference", str(dst), f.filename)
    orch.enqueue(run_id)  # 输入全部就绪后才开始
    return {"id": run_id}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404)
    state = _read_state(run_id)
    c = orch.control(run_id)
    return {"run": run, "stages": store.stages(run_id), "artifacts": store.artifacts(run_id), "state": state,
            "quality": assess(run, state),
            "comparison": consultation.comparison(store, run, state),
            "control": {"paused": c.paused,
                        "model": c.model_key if orch.current == run_id else run['options'].get('model','main'),
                        "cancelled": c.cancelled}}


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = 0, limit: int = 500) -> list[dict]:
    return store.events(run_id, after, limit)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    """删除已结束的运行：运行目录移到 runs/.trash/（可找回），清除数据库记录与项目链接。"""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404)
    if run["status"] in ("running", "queued", "waiting", "paused", "preparing") or orch.current == run_id:
        raise HTTPException(409, "运行尚未结束，请先取消再删除")
    src = RUNS / run_id
    if src.exists():
        trash = RUNS / ".trash"
        trash.mkdir(exist_ok=True)
        dst = trash / run_id
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
    for pj in PROJECTS.glob("*/project.json"):
        link = pj.parent / "runs" / run_id
        if link.is_symlink():
            link.unlink()
        data = json.loads(pj.read_text())
        if run_id in data.get("runs", []):
            data["runs"].remove(run_id)
            pj.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    orch.controls.pop(run_id, None)
    store.delete_run(run_id)
    return {"ok": True, "trash": str(RUNS / ".trash" / run_id)}


@app.post("/api/play/planet-demo")
async def play_planet_demo() -> dict:
    """星球样例（projects/oskar-trials/planet_game）：在本机直接用 Godot 运行。"""
    proj = config.ROOT / "projects" / "oskar-trials" / "planet_game"
    if not (proj / "project.godot").exists():
        raise HTTPException(400, "星球样例工程不存在：projects/oskar-trials/planet_game")
    _launch_godot(proj)
    return {"ok": True}


def _launch_godot(proj, editor: bool = False) -> None:
    """运行前确保资源已导入（新拷贝的工程没有 .godot 缓存时，直接运行会找不到 GLB），然后在本机打开游戏窗口。"""
    import threading
    binary = config.get("godot.binary")

    def go():
        if not editor and not (proj / ".godot" / "imported").exists():
            subprocess.run([binary, "--headless", "--path", str(proj), "--import"], timeout=600,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen([binary, "--path", str(proj)] + (["--editor"] if editor else []), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=go, daemon=True).start()


@app.post("/api/runs/{run_id}/{action}")
async def run_action(run_id: str, action: str, request: Request) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404)
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
    c = orch.control(run_id)
    if action == 'consult':
        try:
            return {'id':orch.invite(run_id,body.get('agent_id','oskar'),body.get('brief',''),
                                      body.get('areas',[]),body.get('preserve',''))}
        except (ValueError, OSError) as exc:
            raise HTTPException(400,str(exc))
    elif action == 'revise':
        try:
            return {'id':orch.revise(run_id,body.get('version',''),body.get('selected',[]))}
        except (ValueError,OSError) as exc:
            raise HTTPException(400,str(exc))
    elif action in ('pause', 'resume', 'cancel'):
        try:
            orch.control_action(run_id, action)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
    elif action == "retry":
        try:
            orch.retry(run_id, body.get("from_stage"), body.get("options"))
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e))
    elif action == "approve":
        if body.get("gate") == "journey":
            if run["mode"] != "assembly" or run["status"] != "waiting" or run["current_stage"] != "journey":
                raise HTTPException(409, "当前任务不在完整流程试玩确认阶段")
            if body.get("selections", {}).get("journey_played") is not True:
                raise HTTPException(400, "请实际试玩每关目标、出口、关卡切换、通关与重开后确认")
        c.approvals[body.get("gate", "concept")] = {"selections": body.get("selections", {})}
    elif action == "regenerate":
        c.requests.append({"type": "regenerate", "asset": body["asset"], "prompt": body.get("prompt"),
                           "count": int(body.get("count", 2))})
    elif action == "note":
        c.notes.append(str(body.get("text", ""))[:2000])
    elif action == "model":
        c.model_key = body.get("model", "main")
        opts = run["options"]
        opts["model"] = c.model_key
        store.update_run(run_id, options=opts)
    elif action == "open-godot":
        proj = RUNS / run_id / "godot_project"
        if not (proj / "project.godot").exists():
            raise HTTPException(400, "该运行还没有 Godot 工程")
        _launch_godot(proj, editor=bool(body.get("editor")))
    elif action == "planetize":
        # 旧的「关卡串联」组装：用同一批来源改建为星球组装（新任务；原任务保留）
        import uuid
        m = run["options"].get("assembly") or {}
        if run["mode"] != "assembly" or not m.get("parts"):
            raise HTTPException(400, "只有游戏组装任务可以改为星球方式")
        seen, sel = set(), []
        for p in m["parts"]:
            if p["role"] == "level" and p["source_id"] not in seen:
                seen.add(p["source_id"])
                sel.append({"id": p["source_id"], "role": "level", "goal": p.get("goal", "")})
        try:
            new_id = orch.compose_game(f"{m['title']} · 星球"[:120], m["brief"], sel,
                                       run["options"].get("style_agent", "oskar"), str(uuid.uuid4()), "planet")
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc))
        store.event(run_id, None, "control", f"已用同一批场景创建星球组装任务 {new_id}", None)
        return {"ok": True, "id": new_id}
    elif action == "playtested":
        from .. import release
        if not release.playable(run, RUNS / run_id / "godot_project"):
            raise HTTPException(400, "该任务没有可玩的 Godot 工程")
        release.confirm_playtest(store, run, body.get("note", ""))
    elif action == "publish":
        import threading
        from .. import release
        proj = RUNS / run_id / "godot_project"
        if not release.playable(run, proj):
            raise HTTPException(400, "该任务没有可玩的 Godot 工程")
        if not (run["options"].get("playtest") or {}).get("confirmed"):
            raise HTTPException(409, "请先进入游戏试玩，确认后才能下载与发布")
        opts = dict(run["options"])
        opts["release"] = {"status": "running", "at": time.time()}
        store.update_run(run_id, options=opts)

        def job():
            try:
                release.publish(store, store.get_run(run_id), RUNS / run_id, proj)
            except Exception as exc:   # noqa: BLE001
                r = store.get_run(run_id)
                o = dict(r["options"])
                o["release"] = {"status": "failed", "at": time.time(), "notes": [f"{type(exc).__name__}: {exc}"]}
                store.update_run(run_id, options=o)
        threading.Thread(target=job, daemon=True).start()
    elif action == "reveal":
        subprocess.Popen(["open", str(RUNS / run_id)])
    else:
        raise HTTPException(400, f"unknown action {action}")
    store.event(run_id, None, "control", f"控制操作：{action}", body or None)
    return {"ok": True}


# ── 文件 ──
@app.get("/api/artifacts/{aid}")
def artifact_file(aid: str):
    a = store.get_artifact(aid)
    if not a:
        raise HTTPException(404)
    return _safe_file(Path(a["path"]))


@app.get("/api/file")
def any_file(path: str):
    return _safe_file(Path(path))


def _safe_file(p: Path):
    p = p.resolve()
    if not p.is_relative_to(RUNS.resolve()) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


# ── 实时推送 ──
@app.get("/api/stream")
async def stream(request: Request):
    q = bus.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if not READ_ONLY:
    _muse_bridge()  # 放在文件末尾：线程里用到的 _link_run 等函数都已定义
