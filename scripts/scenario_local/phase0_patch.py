"""阶段 0：把 vendor/scenario-local（上游 scenario-labs/blender-plugin @ d2a03de，GPL-3.0）改成本地管线阶段。
幂等：已改过的地方跳过。只改文本，不执行上游代码。
- 换扩展 id / 名称（上游 TRADEMARKS.md 要求改名改 id）
- 关遥测：relay.post_json 直接拒绝，usage.activate 不再开启发送
- 关云端：SDK 客户端构造直接报错（阶段 1 由 LocalAdapter 取代）
- 无界面 MCP 命令改名 genai_scene，且不再要求 Blender「Allow Online Access」（只监听 127.0.0.1）
- manifest 只留 macOS arm64 依赖轮子（scenario_sdk 为 MIT，阶段 1 起保留：本地后端复用它的客户端）
顺序：phase0_patch.py → patch -p1 < phase1_local_backend.patch（之后再跑本脚本只会跳过并重写 manifest）
"""
import pathlib, re, sys
R = pathlib.Path(sys.argv[1]) / "scenario"

def sub(rel, old, new, count=1, done_if=None):
    p = R / rel
    s = p.read_text()
    if new in s or (done_if and done_if in s):
        print("skip", rel); return
    assert old in s, (rel, old[:60])
    p.write_text(s.replace(old, new, count)); print("ok  ", rel)

sub("mcp/cli.py", 'CLI_COMMAND = "scenario_blender"', 'CLI_COMMAND = "genai_scene"')
sub("mcp/cli.py", 'return f"scenario-blender MCP server on {url}"', 'return f"genai-scene-stage MCP server on {url}"')
sub("blender/mcp_service.py",
    '    if not runtime.online():\n        parser.error("Allow Online Access is off; enable it before starting local MCP")\n',
    '    # 本地管线阶段：只监听 127.0.0.1，不依赖 Allow Online Access。\n')
sub("core/telemetry/relay.py", 'BASE_URL = "https://blender.scenario.com/telemetry/v1"', 'BASE_URL = ""  # 遥测已关闭（本地 fork）')
sub("core/telemetry/relay.py",
    '    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")\n    if len(body) > MAX_BODY:',
    '    raise RuntimeError("telemetry disabled in local fork")\n    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")\n    if len(body) > MAX_BODY:')
sub("blender/usage.py",
    '    global _active, _send\n    _active, _send = True, send\n',
    '    global _active, _send\n    _active, _send = False, None  # 本地 fork：从不记录、从不发送\n')
sub("core/api/sdk_adapter.py", 'API_URL = "https://api.cloud.scenario.com/v1"', 'API_URL = "https://cloud-removed.invalid/v1"  # 云端已移除（.invalid 永不解析）；作业库仍要合法 HTTPS 地址作键；阶段 1 换成 LocalAdapter', done_if="LOCAL_API_URL")
sub("core/api/sdk_adapter.py",
    'def _client(credentials, base_url, timeout, transport):\n    import httpx\n',
    'def _client(credentials, base_url, timeout, transport):\n    raise RuntimeError("Scenario cloud backend removed in local fork; use the local pipeline adapter")\n    import httpx\n', done_if="shared_transport()")

m = R / "blender_manifest.toml"
s = m.read_text()
s = re.sub(r'^id = ".*"$', 'id = "genai_scene_stage"', s, flags=re.M)
s = re.sub(r'^name = ".*"$', 'name = "GenAI Scene Stage"', s, flags=re.M)
s = re.sub(r'^tagline = ".*"$', 'tagline = "Local pipeline scene stage, based on Scenario for Blender"', s, flags=re.M)
s = re.sub(r'^maintainer = ".*"$', 'maintainer = "GenAIWorkPipeline (local fork)"', s, flags=re.M)
s = re.sub(r'^website = ".*"\n', '', s, flags=re.M)
s = re.sub(r'^copyright = \[.*\]$', 'copyright = ["2026 Scenario Inc.", "2026 GenAIWorkPipeline local modifications"]', s, flags=re.M)
s = re.sub(r'^platforms = \[.*\]$', 'platforms = ["macos-arm64"]', s, flags=re.M)
wheels = sorted(p.name for p in (R / "wheels").glob("*.whl"))
s = re.sub(r'^wheels = \[.*?\]\n', "wheels = [\n" + "".join(f'  "./wheels/{w}",\n' for w in wheels) + "]\n", s, flags=re.M | re.S)
s = re.sub(r'^network = ".*"$', 'network = "127.0.0.1 only (local pipeline services)"', s, flags=re.M)
m.write_text(s); print("ok   blender_manifest.toml", len(wheels), "wheels")
