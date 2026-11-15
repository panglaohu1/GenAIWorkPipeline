"""在 Blender（--background）里跑：用本地 fork 的原版作业链路（估价 → 提交 → 跟踪 → 下载 → 校验）
真实调用本机 FLUX（8090）出一张图，再（若 8098 在线）把这张图送进 Hunyuan3D 出 GLB。
用法：Blender --background --python scripts/scenario_local/local_job_smoke.py -- <out_dir>
"""
import importlib, json, sys, time, urllib.request, uuid
from pathlib import Path

out = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
out.mkdir(parents=True, exist_ok=True)
pkg = "bl_ext.user_default.genai_scene_stage"
lb = importlib.import_module(pkg + ".core.api.local_backend")
sdk = importlib.import_module(pkg + ".core.api.sdk_adapter")
cs = importlib.import_module(pkg + ".core.jobs.credential_storage")
co = importlib.import_module(pkg + ".core.jobs.coordinator")
st = importlib.import_module(pkg + ".core.jobs.store")
tr = importlib.import_module(pkg + ".core.api.local_backend.transport")
dl = importlib.import_module(pkg + ".core.api.local_backend.downloader")


def up(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(f"http://127.0.0.1:{port}/health", timeout=3).status == 200
    except OSError:
        return False


def run(model, params, tag):
    root = out / tag
    root.mkdir(mode=0o700, exist_ok=True)
    creds = sdk.Credentials(lb.LOCAL_KEY, lb.LOCAL_SECRET)
    store = cs.open_credential_store(root, creds)
    (root / "results").mkdir(mode=0o700, exist_ok=True)
    backend = tr.shared_transport()
    adapter = sdk.SDKAdapter(creds, account_id=store.scope.account_id, project_id=store.scope.project_id,
                             online=lambda: True)
    t0 = time.time()
    try:
        c = co.JobCoordinator(adapter, store, result_downloader=dl.LocalResultDownloader(backend.state),
                              result_root=(root / "results").resolve())
        origin = st.JobOrigin(uuid.uuid4().hex, "local-smoke", "1", "download-only")
        quote = c.quote_model(model, params, origin=origin)
        print(f"[{tag}] quote {quote.estimate.cost} CU", flush=True)
        prepared = c.prepare_quote(quote)
        c.submit(prepared, origin=origin, operation=prepared.intent.operation,
                 target_id=prepared.intent.target_id, payload=prepared.estimate.payload)
        rec = store.records()[0]
        while True:
            S = st.JobState
            if rec.state in {S.REMOTE, S.CANCEL_REQUESTED}:
                rec = c.refresh_remote(rec.intent.request_id, expected_revision=rec.revision).record
                if rec.state in {S.REMOTE, S.CANCEL_REQUESTED}:
                    time.sleep(2)
            elif rec.state in {S.SUCCEEDED, S.DOWNLOAD_FAILED}:
                rec = c.download_results(rec.intent.request_id, expected_revision=rec.revision)
            elif rec.state == S.DOWNLOADING:
                rec = c.recover_downloads(rec.intent.request_id, expected_revision=rec.revision)
            elif rec.state == S.READY:
                v = c.verify_results(rec.intent.request_id, expected_revision=rec.revision)
                print(f"[{tag}] READY in {time.time()-t0:.1f}s: {[str(p) for p in v.paths]}", flush=True)
                return [Path(p) for p in v.paths]
            else:
                job = backend.state.get("jobs", rec.remote_job_id) if rec.remote_job_id else None
                print(f"[{tag}] ended {rec.state.value}: {job and job['metadata'].get('error')}", flush=True)
                return []
    finally:
        adapter.close()


report = {"image_service": up(8090), "shape3d_service": up(8098)}
print("services:", report, flush=True)
if report["image_service"]:
    imgs = run("model_local-flux-schnell",
               {"prompt": "a single red and white striped lighthouse, low poly game asset, plain white background",
                "width": 512, "height": 512, "seed": 7}, "flux")
    report["image"] = [str(p) for p in imgs]
    if imgs and report["shape3d_service"]:
        asset = tr.shared_transport().import_file(str(imgs[0]))
        glbs = run("model_local-hunyuan3d-2mini", {"image": asset, "seed": 7}, "hunyuan3d")
        report["model"] = [str(p) for p in glbs]
(out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
print("report:", json.dumps(report, ensure_ascii=False), flush=True)
