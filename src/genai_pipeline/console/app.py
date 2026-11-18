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
    public = path in ("/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/register", "/api/reacode/demo", "/api/reacode/planet-latest") or (request.method == "GET" and path.startswith("/api/reacode/planet-data/"))
    if path.startswith("/api/") and not public and not auth.user(request.cookies.get(COOKIE)):
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    if READ_ONLY and mutation and not path.startswith("/api/auth/"):
        return JSONResponse({"detail": "当前控制台为只读预览，不能修改或执行任务"}, status_code=403)
    response = await call_next(request)
    if path in ("/static/reacode.html", "/static/rea-workshop.css", "/static/rea-workshop.js", "/static/rea-workshop-worker.js"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


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
        if "scene_recipe" in r["options"]:
            recipe=r["options"]["scene_recipe"]
            r["options"]["scene_recipe"]={"scene":recipe.get("scene"),"steps":recipe.get("steps"),"snapshot_saved":True}
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
    if mode == "rea_scene":
        from ..reacode.scene_pipeline import validate_recipe
        try: validate_recipe(opts.get("scene_recipe"))
        except ValueError as exc: raise HTTPException(400,str(exc))
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
    related=[];scene_count=0
    if run['mode']=='rea_planet':
        related=[{'id':r['id'],'status':r['status'],'binding':r['options']['planet_binding']} for r in store.list_runs(500) if r['mode']=='rea_bound_scene' and r['options'].get('planet_binding',{}).get('planet_run')==run_id]
        scenes_file=RUNS/run_id/'lake-scenes.json'
        if scenes_file.exists():scene_count=sum(s.get('terrainVersion')==state.get('rea_planet',{}).get('version') for s in json.loads(scenes_file.read_text()))
    return {"related_scene_runs":related,"scene_instance_count":scene_count,"run": run, "stages": store.stages(run_id), "artifacts": store.artifacts(run_id), "state": state,
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


# Project-owned REA engine and learning workshop.
@app.get("/api/reacode/learning")
def rea_learning():
    from ..reacode.workshop import learning
    return learning()

@app.get("/api/reacode/candidates")
def rea_candidates():
    from ..reacode.workshop import records
    return records()

@app.post("/api/reacode/candidates")
async def rea_execute(body: dict):
    from ..reacode.workshop import execute
    try:
        return await execute(body)
    except (ValueError, KeyError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))

@app.post("/api/reacode/candidates/{cid}")
def rea_decide(cid: str, body: dict):
    from ..reacode.workshop import decide
    try:
        return decide(cid, body)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc))

app.mount("/engines/REACodeRefactoring", StaticFiles(directory=config.ROOT / "engines/REACodeRefactoring"), name="reacode")
# 湖区适配层（lake-scene.js）与 REA 建筑管线同源加载，浏览器里 import 的相对路径必须能落到这里。
app.mount("/engines/system-workshop", StaticFiles(directory=config.ROOT / "engines/system-workshop"), name="system-workshop")


@app.get("/api/reacode/demo")
def rea_demo():
    for run in store.list_runs(100):
        if run["mode"] == "rea_game":
            state = _read_state(run["id"]).get("rea_game", {})
            return {"title":run["title"],"status":run["status"],"plan":state.get("plan"),"url":state.get("url"),"runtime_verified":state.get("runtime_verified",False)}
    return {}


@app.get("/api/reacode/scene-runs/{run_id}")
def rea_scene_result(run_id: str):
    run=store.get_run(run_id)
    if not run or run["mode"]!="rea_scene":raise HTTPException(404)
    state=_read_state(run_id).get("rea_scene",{})
    result={"run_id":run_id,"status":run["status"],"stage":run.get("current_stage"),"error":run.get("error"),"state":state}
    if state.get("runtime_verified"):
        path=RUNS/run_id/"scene-candidate.json"
        candidate=json.loads(path.read_text())
        from ..reacode.workshop import _load
        current=_load(candidate['id'])
        result["candidate"]=json.loads(current.read_text()) if current.exists() else candidate
    return result


@app.get("/api/reacode/planet-latest")
def rea_planet_latest():
    for run in store.list_runs(500):
        if run['mode']=='rea_planet':
            return {'id':run['id'],'status':run['status'],'stage':run.get('current_stage'),'error':run.get('error'),'state':_read_state(run['id']).get('rea_planet',{})}
    return {}

def _node_failure(text: str) -> str:
    """把 Node 的堆栈压成一句可读原因：界面只需要「建筑台基重叠」这类结论。"""
    import re
    lines = [line.strip() for line in (text or '').strip().splitlines() if line.strip()]
    for line in reversed(lines):
        match = re.search(r'(?:Error|RangeError|TypeError|SyntaxError):\s*(.+)$', line)
        if match:
            return match.group(1).strip()[:300]
    return (lines[-1] if lines else '建筑计算失败')[:300]


def _lake_scene_revision(scene):
    import hashlib
    # lake-world 的网格有几十万字符，逐次哈希太贵；体素本身已经代表了内容。
    skip = {'sceneRevision', 'mesh', 'worldState'} if scene.get('kind') == 'lake-world' else {'sceneRevision'}
    return hashlib.sha256(json.dumps({k:v for k,v in scene.items() if k not in skip},sort_keys=True,separators=(',',':')).encode()).hexdigest()

@app.get("/api/reacode/planet-data/{run_id}")
def rea_planet_data(run_id: str, candidate: str | None = None):
    run=store.get_run(run_id)
    if not run or run['mode']!='rea_planet':raise HTTPException(404)
    p=RUNS/run_id/'planet.json'
    if not p.exists():raise HTTPException(409,'全球地形还未编译完成')
    state=_read_state(run_id).get('rea_planet',{})
    data=json.loads(p.read_text());scene_file=RUNS/run_id/'lake-scenes.json'
    scenes=json.loads(scene_file.read_text()) if scene_file.exists() else []
    data['sceneInstances']=[scene for scene in scenes if scene.get('terrainVersion')==state.get('version')]
    if candidate:
        if not candidate.startswith('lake-town-') or not candidate[10:].isalnum():raise HTTPException(400,'无效候选ID')
        path=RUNS/run_id/(candidate+'.candidate.json')
        if not path.exists():raise HTTPException(404,'候选不存在')
        record=json.loads(path.read_text());scene=record['scene']
        if record['status']!='candidate' or scene['terrainVersion']!=state.get('version'):raise HTTPException(409,'候选已经失效')
        data['sceneInstances']=[s for s in data['sceneInstances'] if s['lakeIndex']!=scene['lakeIndex']]+[scene]

    for scene in data['sceneInstances']:scene['sceneRevision']=_lake_scene_revision(scene)
    data['sceneTasks']=[{'id':r['id'],'status':r['status'],'stage':r.get('current_stage'),'binding':r['options']['planet_binding']} for r in store.list_runs(500) if r['mode']=='rea_bound_scene' and r['options'].get('planet_binding',{}).get('planet_run')==run_id]
    return {'run_id':run_id,'state':state,'planet':data}

@app.post("/api/reacode/planet-region/{run_id}")
def rea_planet_region(run_id: str, body: dict):
    run=store.get_run(run_id)
    if not run or run['mode']!='rea_planet':raise HTTPException(404)
    state=_read_state(run_id).get('rea_planet',{})
    if state.get('status')!='terrain_checked':raise HTTPException(409,'请先完成全球地形检查')
    if body.get('terrain_version')!=state.get('version'):raise HTTPException(409,'地形版本已变化，请重新选区')
    data=json.loads((RUNS/run_id/'planet.json').read_text());region=next((r for r in data['regions'] if r['id']==body.get('region_id')),None)
    if not region:raise HTTPException(400,'区域不存在')
    binding={'planet_run':run_id,'terrain_version':state['version'],'region':region,'status':'reserved_for_scene','scene_created':False}
    path=RUNS/run_id/'selected-region.json';path.write_text(json.dumps(binding,ensure_ascii=False,indent=2))
    store.event(run_id,None,'result','已选择后续场景区域：'+region['id'],{'region_id':region['id']})
    return binding

# Edits are serialized per globe and revision-bound, so stale browser tabs cannot overwrite work.
_planet_edit_locks: dict[str, asyncio.Lock] = {}

@app.post('/api/reacode/planet-edit/{run_id}')
async def rea_planet_edit(run_id: str, body: dict):
    import hashlib
    import uuid
    from ..capabilities.bridge import node_path
    lock=_planet_edit_locks.setdefault(run_id,asyncio.Lock())
    async with lock:
        run=store.get_run(run_id)
        if not run or run['mode']!='rea_planet':raise HTTPException(404)
        if run['status'] in ('running','pending'):raise HTTPException(409,'当前任务仍在运行，请完成后再编辑')
        state=_read_state(run_id);terrain=state.get('rea_planet',{})
        if terrain.get('status') not in ('terrain_checked','terrain_edited'):raise HTTPException(409,'请先完成初始全球地形检查')
        if body.get('terrain_version')!=terrain.get('version'):raise HTTPException(409,'地形版本已变化，请刷新后再编辑')
        directory=RUNS/run_id;planet=directory/'planet.json'
        if not planet.exists():raise HTTPException(409,'尚无可编辑的全球地形')
        history=directory/'terrain-history';history.mkdir(exist_ok=True)
        backups=sorted(history.glob('*.json'))
        temporary=directory/('terrain-'+uuid.uuid4().hex+'.tmp')
        if body.get('undo'):
            if not backups:raise HTTPException(409,'没有可以撤销的地形修改')
            temporary.write_bytes(backups[-1].read_bytes())
        else:
            proc=await asyncio.create_subprocess_exec(node_path(),str(config.ROOT/'src/genai_pipeline/reacode/planet-edit.mjs'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                _,error=await asyncio.wait_for(proc.communicate(json.dumps({'input':str(planet),'out':str(temporary),'operation':body.get('operation')}).encode()),30)
            except asyncio.TimeoutError:
                proc.kill();await proc.wait();temporary.unlink(missing_ok=True);raise HTTPException(408,'地形编辑计算超时')
            if proc.returncode:
                temporary.unlink(missing_ok=True);raise HTTPException(400,_node_failure(error.decode()))
            shutil.copy2(planet,history/f'{time.time_ns()}.json')
        data=json.loads(temporary.read_text());temporary.replace(planet)
        if body.get('undo'):backups[-1].unlink()
        version=hashlib.sha256(planet.read_bytes()).hexdigest()
        terrain.update(version=version,status='terrain_edited' if data.get('editRevision') else 'terrain_checked',stats=data['stats'],checks=data['checks'],navigation=data['navigation'],capabilities=data.get('capabilities',[]))
        if data.get('editRevision'):terrain.pop('browser',None)
        state_file=directory/'state.json';temp_state=directory/'state.terrain.tmp';temp_state.write_text(json.dumps(state,ensure_ascii=False,indent=2));temp_state.replace(state_file)
        (directory/'selected-region.json').unlink(missing_ok=True)
        store.event(run_id,None,'result','撤销地形修改' if body.get('undo') else '已执行地形编辑能力',{'version':version,'revision':data.get('editRevision',0)})
        return {'run_id':run_id,'state':terrain,'planet':data}

@app.post('/api/reacode/planet-lake-scene/{run_id}')
async def rea_planet_lake_scene(run_id: str, body: dict):
    import uuid
    from ..capabilities.bridge import node_path
    from ..llm import LLM
    lock=_planet_edit_locks.setdefault(run_id,asyncio.Lock())
    async with lock:
        run=store.get_run(run_id)
        if not run or run['mode']!='rea_planet':raise HTTPException(404)
        if run['status'] in ('running','pending'):raise HTTPException(409,'请等待全球任务完成')
        state=_read_state(run_id).get('rea_planet',{})
        if body.get('terrain_version')!=state.get('version'):raise HTTPException(409,'地形版本已变化，请重新选择湖泊')
        if state.get('status') not in ('terrain_checked','terrain_edited'):raise HTTPException(409,'全球地形尚未就绪')
        lake=body.get('lake_index')
        if type(lake) is not int or lake<0:raise HTTPException(400,'请选择有效湖泊')
        count=body.get('count',8);floors=body.get('floors',2)
        def validate_plan(p):
            if not isinstance(p,dict) or type(p.get('count')) is not int or not 1<=p['count']<=32 or type(p.get('floors')) is not int or not 1<=p['floors']<=5:return 'count需为1–32整数，floors需为1–5整数'
        plan={'count':count,'floors':floors,'rationale':'按用户参数调用本机湖区建筑能力'}
        if validate_plan(plan):raise HTTPException(400,validate_plan(plan))
        if body.get('use_model',True):
            plan=await LLM('main').json([{'role':'system','content':'你是本地湖区建筑规划器。可执行能力：REA多层房屋模块、湖底承托台基、局部平面坐标映射。必须使用湖底承托台基和局部平面坐标映射，它们只生成建筑几何，不修改湖底地形或湖面。你只能根据要求选择count整数1–32、floors整数1–5、rationale中文说明，不能声称关闭、禁用、跳过承托或变形。不要声称尚未执行的检查通过。仅输出JSON。'},{'role':'user','content':json.dumps({'goal':str(body.get('brief','湖面建造彩色水上小镇'))[:2000],'preferred_count':count,'preferred_floors':floors},ensure_ascii=False)}],validate=validate_plan,thinking_budget=512,retries=1)
        if body.get('use_model',True):
            plan['model_note']=plan.get('rationale','')
            plan['rationale']=f"本地模型选择{plan['count']}栋、每栋{plan['floors']}层；固定执行REA房屋模块、湖底承托台基与局部平面坐标映射，不修改地形和湖面。"
        directory=RUNS/run_id;scene_id='lake-town-'+uuid.uuid4().hex[:12];temporary=directory/(scene_id+'.tmp')
        options={'lakeIndex':lake,'count':plan['count'],'floors':plan['floors'],'terrainVersion':state['version'],'sceneId':scene_id}
        proc=await asyncio.create_subprocess_exec(node_path(),str(config.ROOT/'src/genai_pipeline/reacode/lake-town.mjs'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:_,error=await asyncio.wait_for(proc.communicate(json.dumps({'input':str(directory/'planet.json'),'out':str(temporary),'options':options}).encode()),90)
        except asyncio.TimeoutError:
            proc.kill();await proc.wait();temporary.unlink(missing_ok=True);raise HTTPException(408,'湖区建造超时')
        if proc.returncode:
            temporary.unlink(missing_ok=True);raise HTTPException(400,_node_failure(error.decode()))
        scene=json.loads(temporary.read_text());temporary.unlink();scene['plan']=plan
        if body.get('candidate_only'):
            import hashlib
            current=directory/'lake-scenes.json'
            before=current.read_bytes() if current.exists() else b'[]'
            record={'status':'candidate','scene':scene,'before':json.loads(before),'before_hash':hashlib.sha256(before).hexdigest()}
            (directory/(scene_id+'.candidate.json')).write_text(json.dumps(record,ensure_ascii=False))
            store.event(run_id,None,'result','场景能力管线已生成湖区候选，尚未写回球系统',{'scene_id':scene_id,'terrain_version':state['version']})
            return {'scene':scene,'candidate_id':scene_id,'placed_count':len(scene['buildings']),'requested_count':plan['count'],'status':'candidate'}

        scenes_file=directory/'lake-scenes.json';scenes=json.loads(scenes_file.read_text()) if scenes_file.exists() else []
        # Replace this lake's previous scene; other lakes remain untouched.
        scenes=[s for s in scenes if not(s.get('terrainVersion')==state['version'] and s.get('lakeIndex')==lake)]+[scene]
        tmp=directory/'lake-scenes.tmp';tmp.write_text(json.dumps(scenes,ensure_ascii=False));tmp.replace(scenes_file)
        store.event(run_id,None,'result','湖区场景已建造并绑定球面',{'scene_id':scene_id,'lake_index':lake,'buildings':len(scene['buildings']),'terrain_version':state['version'],'plan':plan,'checks':scene['checks']})
        return {'scene':scene,'placed_count':len(scene['buildings']),'requested_count':plan['count']}

@app.post('/api/reacode/planet-lake-candidate/{run_id}/{candidate_id}')
async def rea_planet_lake_candidate(run_id: str, candidate_id: str, body: dict):
    import hashlib
    if not candidate_id.startswith('lake-town-') or not candidate_id[10:].isalnum():raise HTTPException(400,'无效候选ID')
    async with _planet_edit_locks.setdefault(run_id,asyncio.Lock()):
        run=store.get_run(run_id)
        if not run or run['mode']!='rea_planet':raise HTTPException(404)
        if run['status'] in ('running','pending'):raise HTTPException(409,'星球任务仍在运行')
        state=_read_state(run_id).get('rea_planet',{});directory=RUNS/run_id
        path=directory/(candidate_id+'.candidate.json')
        if not path.exists():raise HTTPException(404,'候选不存在')
        record=json.loads(path.read_text());scene=record['scene'];action=body.get('action')
        if state.get('version')!=scene['terrainVersion'] or body.get('terrain_version')!=state.get('version'):raise HTTPException(409,'地形版本已变化，禁止写回旧候选')
        current=directory/'lake-scenes.json';raw=current.read_bytes() if current.exists() else b'[]'
        digest=hashlib.sha256(raw).hexdigest()
        if action=='adopt':
            if record['status']!='candidate' or digest!=record['before_hash']:raise HTTPException(409,'当前场景已变化，请重新生成候选')
            scenes=[s for s in json.loads(raw) if not(s.get('terrainVersion')==scene['terrainVersion'] and s.get('lakeIndex')==scene['lakeIndex'])]+[scene]
            tmp=directory/'lake-scenes.tmp';tmp.write_text(json.dumps(scenes,ensure_ascii=False));tmp.replace(current)
            record.update(status='adopted',after_hash=hashlib.sha256(current.read_bytes()).hexdigest())
        elif action=='discard':
            if record['status']!='candidate':raise HTTPException(409,'候选已被处理')
            record['status']='discarded'
        elif action=='undo':
            if record['status']!='adopted' or digest!=record.get('after_hash'):raise HTTPException(409,'后续场景已变化，无法撤销覆盖')
            tmp=directory/'lake-scenes.tmp';tmp.write_text(json.dumps(record['before'],ensure_ascii=False));tmp.replace(current);record['status']='undone'
        else:raise HTTPException(400,'无效操作')
        path.write_text(json.dumps(record,ensure_ascii=False))
        child=record.get('scene_run')
        if child:
            child_state=_read_state(child);child_state.setdefault('bound_scene',{})['writeback']=record['status']
            child_file=RUNS/child/'state.json';tmp_child=child_file.with_suffix('.writeback.tmp');tmp_child.write_text(json.dumps(child_state,ensure_ascii=False));tmp_child.replace(child_file)
            store.event(child,None,'result','场景管线产物写回状态：'+record['status'],{'parent_planet':run_id,'candidate_id':candidate_id})
        store.event(run_id,None,'result','湖区场景候选：'+action,{'candidate_id':candidate_id,'terrain_version':state['version']})
        return {'status':record['status'],'scene_id':candidate_id,'terrain_version':state['version']}

@app.post('/api/reacode/planet-lake-edit/{run_id}/{scene_id}')
async def rea_planet_lake_edit(run_id: str, scene_id: str, body: dict):
    import uuid
    from ..capabilities.bridge import node_path
    if not scene_id.startswith('lake-town-') or not scene_id[10:].isalnum():raise HTTPException(400,'无效场景ID')
    async with _planet_edit_locks.setdefault(run_id,asyncio.Lock()):
        run=store.get_run(run_id)
        if not run or run['mode']!='rea_planet':raise HTTPException(404)
        if run['status'] in ('running','pending'):raise HTTPException(409,'星球任务仍在运行')
        state=_read_state(run_id).get('rea_planet',{});directory=RUNS/run_id
        if body.get('terrain_version')!=state.get('version'):raise HTTPException(409,'地形版本已变化，请重新进入湖区')
        file=directory/'lake-scenes.json';scenes=json.loads(file.read_text()) if file.exists() else []
        current=next((s for s in scenes if s.get('id')==scene_id and s.get('terrainVersion')==state['version']),None)
        if current is None:raise HTTPException(404,'未找到当前地形版本的湖区场景；请先生成并采用一个候选')
        if body.get('scene_revision')!=_lake_scene_revision(current):raise HTTPException(409,'建筑已被另一个页面修改，请刷新后重试')
        history=directory/'lake-building-history'/scene_id;history.mkdir(parents=True,exist_ok=True)
        action=body.get('action');undo_file=None
        if action=='undo':
            backups=sorted(history.glob('*.json'))
            if not backups:raise HTTPException(409,'没有可撤销的建筑编辑')
            undo_file=backups[-1];updated=json.loads(undo_file.read_text())
            if updated.get('terrainVersion')!=state['version']:raise HTTPException(409,'历史建筑来自旧地形，禁止恢复')
        else:
            placements=[{'id':b['id'],'x':b['localPosition'][0],'z':b['localPosition'][2],'floors':b['floors'],'color':b.get('color',i%5)} for i,b in enumerate(current['buildings'])]
            building_id=body.get('building_id');target=next((b for b in placements if b['id']==building_id),None)
            if action=='add':
                placements.append({'id':scene_id+'-b'+uuid.uuid4().hex[:10],'x':body.get('x'),'z':body.get('z'),'floors':body.get('floors',2),'color':body.get('color',0)})
            elif action in ('update','delete'):
                if target is None:raise HTTPException(404,'请先选择一栋当前湖区的建筑')
                if action=='delete':placements.remove(target)
                else:
                    for name in ('x','z','floors','color'):
                        if name in body:target[name]=body[name]
            else:raise HTTPException(400,'无效建筑编辑操作')
            out=directory/('lake-edit-'+uuid.uuid4().hex+'.tmp')
            opts={'lakeIndex':current['lakeIndex'],'count':max(1,min(32,len(placements))),'floors':2,'terrainVersion':state['version'],'sceneId':scene_id,'placements':placements}
            proc=await asyncio.create_subprocess_exec(node_path(),str(config.ROOT/'src/genai_pipeline/reacode/lake-town.mjs'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:_,error=await asyncio.wait_for(proc.communicate(json.dumps({'input':str(directory/'planet.json'),'out':str(out),'options':opts}).encode()),90)
            except asyncio.TimeoutError:
                proc.kill();await proc.wait();out.unlink(missing_ok=True);raise HTTPException(408,'建筑编辑计算超时')
            if proc.returncode:
                out.unlink(missing_ok=True);raise HTTPException(400,_node_failure(error.decode()))
            updated=json.loads(out.read_text());out.unlink();updated['plan']=current.get('plan',{});updated['edited_by']='manual-building-editor'
            (history/f'{time.time_ns()}.json').write_text(json.dumps(current,ensure_ascii=False))
        scenes=[updated if s.get('id')==scene_id else s for s in scenes]
        tmp=directory/'lake-scenes.tmp';tmp.write_text(json.dumps(scenes,ensure_ascii=False));tmp.replace(file)
        if undo_file:undo_file.unlink()
        updated['sceneRevision']=_lake_scene_revision(updated)
        store.event(run_id,None,'result','湖区建筑编辑：'+str(action),{'scene_id':scene_id,'building_id':body.get('building_id'),'buildings':len(updated['buildings']),'terrain_version':state['version']})
        return {'scene':updated,'scene_revision':updated['sceneRevision'],'status':'saved'}

@app.post('/api/reacode/planet-lake-world/{run_id}/{lake_index}')
async def rea_planet_lake_world(run_id: str, lake_index: int, body: dict):
    """湖区建造工坊保存：接收编辑器里的整份体素场，用 REA 建筑管线烘成球面网格。

    与 planet-lake-edit（按栋增删）不是一条路：这里存的是 World 的 solid/rock/color，
    所以叠层、削层、拱、悬空都由建筑管线自己决定，不再有第二套房子生成器。
    """
    import uuid
    from ..capabilities.bridge import node_path
    async with _planet_edit_locks.setdefault(run_id, asyncio.Lock()):
        run = store.get_run(run_id)
        if not run or run['mode'] != 'rea_planet': raise HTTPException(404)
        if run['status'] in ('running', 'pending'): raise HTTPException(409, '星球任务仍在运行')
        state = _read_state(run_id).get('rea_planet', {})
        directory = RUNS / run_id
        lake_state = body.get('state')
        if not isinstance(lake_state, dict) or lake_state.get('v') != 1: raise HTTPException(400, '无效的湖区体素存档')
        cells = lake_state.get('cells')
        arrays = [lake_state.get(k) for k in ('solid', 'rock', 'color')]
        if type(cells) is not int or not 1 <= cells <= 4096 or any(not isinstance(a, list) or len(a) != cells for a in arrays): raise HTTPException(400, '湖区体素存档长度不一致')
        if lake_state.get('terrainVersion') != state.get('version'): raise HTTPException(409, '地形版本已变化，请重新进入湖区')
        scene_id = str(body.get('sceneId') or ('lake-town-' + uuid.uuid4().hex[:12]))
        if not scene_id.startswith('lake-town-') or not scene_id[10:].isalnum(): raise HTTPException(400, '无效场景ID')
        out = directory / ('lake-world-' + uuid.uuid4().hex + '.tmp')
        options = {'lakeIndex': lake_index, 'terrainVersion': state['version'], 'sceneId': scene_id, 'state': lake_state}
        proc = await asyncio.create_subprocess_exec(node_path(), str(config.ROOT / 'src/genai_pipeline/reacode/lake-world.mjs'), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, error = await asyncio.wait_for(proc.communicate(json.dumps({'input': str(directory / 'planet.json'), 'out': str(out), 'options': options}).encode()), 120)
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait(); out.unlink(missing_ok=True); raise HTTPException(408, '湖区网格烘焙超时')
        if proc.returncode:
            out.unlink(missing_ok=True); raise HTTPException(400, _node_failure(error.decode()))
        scene = json.loads(out.read_text()); out.unlink()
        file = directory / 'lake-scenes.json'
        scenes = json.loads(file.read_text()) if file.exists() else []
        # 同一个湖只保留一份：旧的按栋场景（lake-town）与旧体素场景都让位给这次保存。
        scenes = [s for s in scenes if not (s.get('terrainVersion') == state['version'] and s.get('lakeIndex') == lake_index)] + [scene]
        tmp = directory / 'lake-scenes.tmp'; tmp.write_text(json.dumps(scenes, ensure_ascii=False)); tmp.replace(file)
        scene['sceneRevision'] = _lake_scene_revision(scene)
        store.event(run_id, None, 'result', '湖区建造工坊保存', {'scene_id': scene_id, 'lake_index': lake_index, 'buildings': scene['checks']['buildings'], 'blocks': scene['checks']['blocks'], 'terrain_version': state['version']})
        return {'scene': scene, 'scene_revision': scene['sceneRevision'], 'status': 'saved'}

@app.post('/api/reacode/planet-scene-runs/{planet_run}')
async def create_planet_scene_run(planet_run: str, body: dict):
    parent=store.get_run(planet_run)
    if not parent or parent['mode']!='rea_planet':raise HTTPException(404)
    if parent['status'] in ('running','pending'):raise HTTPException(409,'球管线尚未完成，不能进入场景管线')
    state=_read_state(planet_run).get('rea_planet',{});directory=RUNS/planet_run
    if state.get('version')!=body.get('terrain_version'):raise HTTPException(409,'球地形版本已变化，请重新选区')
    planet=json.loads((directory/'planet.json').read_text());lake=body.get('lake_index')
    if type(lake) is not int or lake<0 or lake>=len(planet.get('inlandWater',[])) or planet['inlandWater'][lake].get('frame',{}).get('kind')!='local-tangent-plane':raise HTTPException(400,'请从球管线选择有效平面湖区')
    steps=body.get('steps')
    if not isinstance(steps,list) or len(steps)!=1 or steps[0].get('id')!='planet.lake-town':raise HTTPException(400,'当前湖区只支持已适配的planet.lake-town能力')
    params=steps[0].get('params',{})
    if type(params.get('count',8)) is not int or not 1<=params.get('count',8)<=32 or type(params.get('floors',2)) is not int or not 1<=params.get('floors',2)<=5:raise HTTPException(400,'建筑数量或层数越界')
    binding={'planet_run':planet_run,'terrain_version':state['version'],'lake_index':lake}
    rid=orch.submit('湖泊'+str(lake+1)+' · 来源球 '+planet_run,str(body.get('brief') or '在来源球的湖区构建场景'),'rea_bound_scene',{'planet_binding':binding,'steps':steps,'model':'main','approval_gates':[]},start=False)
    store.event(planet_run,None,'result','球管线已将区域交给独立场景管线',{'scene_run':rid,'binding':binding})
    orch.enqueue(rid)
    return {'id':rid,'parent_planet':planet_run,'binding':binding}

@app.get('/api/reacode/bound-scene-runs/{run_id}')
def bound_scene_run(run_id: str):
    run=store.get_run(run_id)
    if not run or run['mode']!='rea_bound_scene':raise HTTPException(404)
    state=_read_state(run_id).get('bound_scene',{})
    return {'run':run,'stages':store.stages(run_id),'state':state,'artifacts':store.artifacts(run_id)}
