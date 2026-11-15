"""阶段 1 补充验收（在 Blender --background 里跑，或测试时用 SMOKE_PKG=scenario 直接跑）：
用本地 fork 的原版作业链路——
  1) 上传参考图：prepare → initialize → 逐段 transfer → finalize → refresh，得到资产 id；
  2) 用上传的图跑图生 3D（本机 Hunyuan3D，8098 在线时）；
  3) 翻译（中 → 英）与 Prompt Spark（看图写提示词）：报价 → 提交 → 跟踪 → 读出文字（本机 LLM，8081）。
用法：Blender --background --python scripts/scenario_local/local_upload_text_smoke.py -- <out_dir> <参考图>
"""
import importlib, json, os, sys, time, urllib.request, uuid
from pathlib import Path

args = sys.argv[sys.argv.index("--") + 1:]
out = Path(args[0]).resolve()
ref = Path(args[1]).resolve()
out.mkdir(parents=True, exist_ok=True)
pkg = os.environ.get("SMOKE_PKG", "bl_ext.user_default.genai_scene_stage")
M = lambda name: importlib.import_module(pkg + "." + name)
lb, sdk, cs, co, st = M("core.api.local_backend"), M("core.api.sdk_adapter"), M("core.jobs.credential_storage"), M("core.jobs.coordinator"), M("core.jobs.store")
tr, dl, upl = M("core.api.local_backend.transport"), M("core.api.local_backend.downloader"), M("core.api.local_backend.uploader")
us, ust = M("core.jobs.upload_sources"), M("core.jobs.upload_store")
US = M("core.jobs.upload_store").UploadState


def up(port, path="/health"):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(f"http://127.0.0.1:{port}{path}", timeout=3).status == 200
    except OSError:
        return False


report = {"llm_service": up(8081, "/v1/models"), "shape3d_service": up(8098)}
print("services:", report, flush=True)
root = out / "session"
root.mkdir(mode=0o700, exist_ok=True)
creds = sdk.Credentials(lb.LOCAL_KEY, lb.LOCAL_SECRET)
store = cs.open_credential_store(root, creds)
(root / "results").mkdir(mode=0o700, exist_ok=True)
(root / "upload-src").mkdir(mode=0o700, exist_ok=True)
backend = tr.shared_transport()
adapter = sdk.SDKAdapter(creds, account_id=store.scope.account_id, project_id=store.scope.project_id, online=lambda: True)
c = co.JobCoordinator(adapter, store, result_downloader=dl.LocalResultDownloader(backend.state),
                      result_root=(root / "results").resolve(),
                      upload_store=ust.UploadStore(root / "uploads.sqlite3", store.scope),
                      upload_sources=us.UploadSources(root / "upload-src"),
                      part_uploader=upl.LocalPartUploader(backend.state))
origin = st.JobOrigin(uuid.uuid4().hex, "local-smoke", "1", "download-only")


def follow(rec, tag, t0):
    S = st.JobState
    while True:
        if rec.state in {S.REMOTE, S.CANCEL_REQUESTED}:
            rec = c.refresh_remote(rec.intent.request_id, expected_revision=rec.revision).record
            if rec.state in {S.REMOTE, S.CANCEL_REQUESTED}:
                time.sleep(2)
        elif rec.state in {S.SUCCEEDED, S.DOWNLOAD_FAILED} and rec.intent.operation in ("prompt", "translate"):
            return rec
        elif rec.state in {S.SUCCEEDED, S.DOWNLOAD_FAILED}:
            rec = c.download_results(rec.intent.request_id, expected_revision=rec.revision)
        elif rec.state == S.DOWNLOADING:
            rec = c.recover_downloads(rec.intent.request_id, expected_revision=rec.revision)
        elif rec.state == S.READY:
            v = c.verify_results(rec.intent.request_id, expected_revision=rec.revision)
            print(f"[{tag}] READY in {time.time()-t0:.1f}s: {[str(p) for p in v.paths]}", flush=True)
            return v
        else:
            job = backend.state.get("jobs", rec.remote_job_id) if rec.remote_job_id else None
            print(f"[{tag}] ended {rec.state.value}: {job and job['metadata'].get('error')}", flush=True)
            return None


def submit(quote, tag):
    t0 = time.time()
    print(f"[{tag}] quote {quote.estimate.cost} CU", flush=True)
    before = {r.intent.request_id for r in store.records()}
    prepared = c.prepare_quote(quote)
    c.submit(prepared, origin=origin, operation=prepared.intent.operation,
             target_id=prepared.intent.target_id, payload=prepared.estimate.payload)
    rec = next(r for r in store.records() if r.intent.request_id not in before)
    return follow(rec, tag, t0), t0


try:
    # 1) upload
    t0 = time.time()
    u = c.prepare_upload(ref, origin=origin, kind="image", content_type="image/png")
    u = c.initialize_upload(u.intent.request_id, expected_revision=u.revision)
    while u.state == US.UPLOADING and len(u.receipts) < len(u.intent.part_sha256):
        u = c.transfer_upload_part(u.intent.request_id, expected_revision=u.revision)
    u = c.finalize_upload(u.intent.request_id, expected_revision=u.revision)
    for _ in range(10):
        if u.state == US.IMPORTED:
            break
        u = c.refresh_upload(u.intent.request_id, expected_revision=u.revision)
    asset_id = getattr(u, "asset_id", None)
    print(f"[upload] {u.state.value} in {time.time()-t0:.1f}s asset={asset_id}", flush=True)
    report["upload"] = {"state": u.state.value, "asset": asset_id}
    # 2) image → 3D from the uploaded asset
    if asset_id and report["shape3d_service"]:
        v, _ = submit(c.quote_model("model_local-hunyuan3d-2mini", {"image": asset_id, "seed": 7}, origin=origin), "hunyuan3d")
        report["model"] = [str(p) for p in (v.paths if v else [])]
    # 3) translate + Prompt Spark
    if report["llm_service"]:
        rec, t0 = submit(c.quote_translate({"prompt": "悬崖上红白条纹的灯塔，低多边形风格"}, origin=origin), "translate")
        if rec is not None:
            res = c.read_prompt_results(rec.intent.request_id, expected_revision=rec.revision)
            print(f"[translate] {time.time()-t0:.1f}s: {res.prompts}", flush=True)
            report["translate"] = list(res.prompts)
        if asset_id:
            rec, t0 = submit(c.quote_prompt({"mode": "contextual", "images": [asset_id], "numResults": 2}, origin=origin), "prompt")
            if rec is not None:
                res = c.read_prompt_results(rec.intent.request_id, expected_revision=rec.revision)
                print(f"[prompt] {time.time()-t0:.1f}s: {res.prompts}", flush=True)
                report["prompt"] = list(res.prompts)
finally:
    adapter.close()
(out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
print("report:", json.dumps(report, ensure_ascii=False), flush=True)
