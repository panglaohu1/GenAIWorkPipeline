"""后台服务管理：LLM（主力/兜底）、生图服务，以及 Blender/Godot 可用性检查。

每个服务是本控制台启动的子进程，日志写入 logs/<name>.log。
模型驻留预算 = memory.gpu_budget_gb - memory.engine_reserve_gb。
只驱逐空闲且允许卸载的服务；预算不足时拒绝启动。请求通过 run() 获取
统一算力槽和服务租约，确保请求期间不被自动驱逐或手动停止。
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import httpx
import psutil

from . import config
from .config import ENVS, ROOT
from .compute import compute

LOGS = ROOT / "logs"
T = TypeVar("T")


class MemoryBudgetError(RuntimeError):
    pass


class ServiceBusyError(RuntimeError):
    pass


class ServiceUnavailableError(RuntimeError):
    pass


@dataclass
class Service:
    name: str
    label: str
    port: int
    mem_gb: float
    health_path: str
    evict_priority: int  # 越大越先被驱逐；<0 永不驱逐
    proc: subprocess.Popen | None = None
    started_at: float | None = None
    last_error: str | None = None
    extra: dict = field(default_factory=dict)

    def command(self) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        return {}

    def ready_files(self) -> list[Path]:
        return []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def missing(self) -> list[str]:
        out = [str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p) for p in self.ready_files() if not p.exists()]
        for d in self.model_dirs():
            if d.exists() and any(d.rglob("*.parts")):
                out.append(f"{d.name}（仍在下载）")
        return out

    def model_dirs(self) -> list[Path]:
        return []


class LLMService(Service):
    def __init__(self, key: str) -> None:
        c = config.get(f"llm.{key}")
        super().__init__(name=f"llm-{key}", label=c["label"], port=c["port"], mem_gb=c["mem_gb"],
                         health_path="/v1/models", evict_priority={"main": 0, "vision": 1}.get(key, 1))
        self.model_path = config.resolve(c["model_path"])
        self.cfg = c
        self.autostart = c.get("autostart", False)

    @property
    def backend(self) -> str:
        return self.cfg.get("backend", "mlx")

    def llama_server(self) -> Path:
        """llama.cpp 预编译包（scripts/setup_sage.command 解压到 vendor/llama.cpp）里的 llama-server。"""
        hits = sorted((ROOT / "vendor" / "llama.cpp").rglob("llama-server"))
        return hits[0] if hits else ROOT / "vendor" / "llama.cpp" / "llama-server"

    def ready_files(self) -> list[Path]:
        if self.backend == "llamacpp":
            files = [self.llama_server(), self.model_path / self.cfg["gguf"]]
            if self.cfg.get("mmproj"):
                files.append(self.model_path / self.cfg["mmproj"])
            return files
        files = [self.model_path / "config.json", self.model_path / "tokenizer.json"]
        index = self.model_path / "model.safetensors.index.json"
        if index.exists():
            import json
            shards = set(json.loads(index.read_text()).get("weight_map", {}).values())
            files += [self.model_path / s for s in sorted(shards)]
        else:
            files.append(self.model_path / "model.safetensors")
        return files

    def model_dirs(self) -> list[Path]:
        return [self.model_path]

    def env(self) -> dict[str, str]:
        if self.backend == "llamacpp":
            return {}
        # 自动前缀缓存：agent 多轮对话每步只需预填充新增部分。
        # Bonsai/Qwen3.8 是混合注意力（线性注意力有递归状态），需要较密的状态检查点才能复用前缀。
        c = config.get("llm.prefix_cache", {})
        if not c.get("enabled", True):
            return {}
        return {"APC_ENABLED": "1", "APC_DISK_ENABLED": "0",
                "APC_MEMORY_MAX_GB": str(c.get("memory_gb", 4)),
                "APC_CHECKPOINT_ENTRIES": str(c.get("checkpoint_entries", 16)),
                "APC_CHECKPOINT_INTERVAL_TOKENS": str(c.get("checkpoint_interval_tokens", 1024))}

    def command(self) -> list[str]:
        c = self.cfg
        if self.backend == "llamacpp":
            # llama.cpp：GGUF 权重 + 视觉投影（mmproj）；--jinja 用模型自带对话模板（含工具调用），
            # --reasoning-format deepseek 把思考放进 reasoning_content（llm.py 按此字段接收）
            cmd = [str(self.llama_server()), "-m", str(self.model_path / c["gguf"]),
                   "--host", "127.0.0.1", "--port", str(self.port), "-c", str(c.get("ctx", 32768)),
                   "-ngl", "999", "-np", "1", "--jinja", "--reasoning-format", "deepseek",
                   "-n", str(config.get("llm.sampling.max_tokens", 16384))]
            if c.get("mmproj"):
                cmd += ["--mmproj", str(self.model_path / c["mmproj"])]
            return cmd + [str(x) for x in c.get("extra_args", [])]
        launcher = ROOT / "src" / "genai_pipeline" / "llm_server_launcher.py"
        return [str(ENVS["llm"]), str(launcher),
                "--cache-limit-gb", str(c.get("mlx_cache_limit_gb", 4)),
                "--memory-limit-gb", str(c.get("mlx_memory_limit_gb", 24)), "--",
                "--model", str(self.model_path),
                "--host", "127.0.0.1", "--port", str(self.port), "--enable-thinking",
                "--max-tokens", str(config.get("llm.sampling.max_tokens", 16384)),
                "--log-level", "INFO"]


class ImageService(Service):
    def __init__(self) -> None:
        super().__init__(name="image", label="生图服务 (FLUX.1-schnell)",
                         port=config.get("image.port", 8090), mem_gb=config.get("image.mem_gb", 12),
                         health_path="/health", evict_priority=2)

    def ready_files(self) -> list[Path]:
        d = config.resolve(config.get("image.flux.model_path"))
        return [d / "transformer" / "0.safetensors", d / "vae" / "0.safetensors", d / "text_encoder_2" / "0.safetensors"]

    def model_dirs(self) -> list[Path]:
        return [config.resolve(config.get("image.flux.model_path"))]

    def command(self) -> list[str]:
        return [str(ENVS["image"]), str(ROOT / "src" / "genai_pipeline" / "imagegen_server.py"),
                "--port", str(self.port), "--config", str(config.CONFIG_PATH)]


class DecisionService(Service):
    def __init__(self) -> None:
        c = config.get("decision", {})
        super().__init__(name="decision", label="System One 决策 (Laya multilingual)", port=c.get("port", 8095),
                         mem_gb=c.get("mem_gb", 2), health_path="/health", evict_priority=3)
        self.model_path = config.resolve(c.get("model_path", "models/decision/laya-multilingual"))
        self.autostart = c.get("autostart", True) and c.get("backend", "laya") == "laya"

    def ready_files(self) -> list[Path]:
        return [self.model_path / "model.safetensors", self.model_path / "tokenizer" / "tokenizer.json"]

    def model_dirs(self) -> list[Path]:
        return [self.model_path]

    def env(self) -> dict[str, str]:
        return {"USE_TF": "0"}

    def command(self) -> list[str]:
        return [str(ROOT / "envs" / "laya" / "bin" / "python"), str(ROOT / "src" / "genai_pipeline" / "laya_server.py"),
                "--port", str(self.port), "--model", str(self.model_path)]


class MetricsService(Service):
    """客观评分：SigLIP-2 / DINOv2 图像特征相似度（envs/vision）。"""

    def __init__(self) -> None:
        c = config.get("metrics", {})
        super().__init__(name="metrics", label="客观评分 (SigLIP-2 / DINOv2)", port=c.get("port", 8096),
                         mem_gb=c.get("mem_gb", 3), health_path="/health", evict_priority=3)
        self.model_root = config.resolve(c.get("model_root", "models/vision"))
        self.autostart = c.get("autostart", False)  # 按需启动（流水线评分时 ensure），不常驻抢预算

    def ready_files(self) -> list[Path]:
        return [self.model_root / "siglip2-base-patch16-224" / "model.safetensors",
                self.model_root / "dinov2-base" / "model.safetensors"]

    def model_dirs(self) -> list[Path]:
        return [self.model_root]

    def env(self) -> dict[str, str]:
        return {"PYTORCH_ENABLE_MPS_FALLBACK": "1"}

    def command(self) -> list[str]:
        return [str(ROOT / "envs" / "vision" / "bin" / "python"),
                str(ROOT / "src" / "genai_pipeline" / "metrics_server.py"), "--port", str(self.port)]


class SamService(Service):
    """实例分割：SAM 3 MLX（envs/sam3），场景拆分时按文字提示找出每类子资产的全部实例。"""

    def __init__(self) -> None:
        c = config.get("sam", {})
        super().__init__(name="sam", label="实例分割 (SAM 3 MLX)", port=c.get("port", 8097),
                         mem_gb=c.get("mem_gb", 6), health_path="/health", evict_priority=3)
        self.weights = config.resolve(c.get("model_path", "models/vision/sam3-image")) / "model.safetensors"
        self.autostart = c.get("autostart", False)  # 只在场景拆分时用：按需启动

    def ready_files(self) -> list[Path]:
        return [self.weights]

    def model_dirs(self) -> list[Path]:
        return [self.weights.parent]

    def command(self) -> list[str]:
        return [str(ROOT / "envs" / "sam3" / "bin" / "python"), str(ROOT / "src" / "genai_pipeline" / "sam_server.py"),
                "--port", str(self.port), "--weights", str(self.weights)]


class Shape3DService(Service):
    """图生 3D：Hunyuan3D-2mini Turbo（envs/shape3d，MPS），单张物体图 → 形体网格。场景拆分后的核心资产按需启动。"""

    def __init__(self) -> None:
        c = config.get("shape3d", {})
        super().__init__(name="shape3d", label="图生 3D (Hunyuan3D-2mini Turbo)", port=c.get("port", 8098),
                         mem_gb=c.get("mem_gb", 10), health_path="/health", evict_priority=3)
        self.models = config.resolve(c.get("model_dir", "models/shape3d"))
        self.autostart = False

    def ready_files(self) -> list[Path]:
        base = self.models / "tencent" / "Hunyuan3D-2mini"
        return [base / "hunyuan3d-dit-v2-mini-turbo" / "model.fp16.safetensors",
                base / "hunyuan3d-vae-v2-mini-turbo" / "model.fp16.safetensors",
                ROOT / "envs" / "shape3d" / "bin" / "python"]

    def model_dirs(self) -> list[Path]:
        return [self.models]

    def env(self) -> dict[str, str]:
        return {"HY3DGEN_MODELS": str(self.models), "PYTORCH_ENABLE_MPS_FALLBACK": "1"}

    def command(self) -> list[str]:
        return [str(ROOT / "envs" / "shape3d" / "bin" / "python"),
                str(ROOT / "src" / "genai_pipeline" / "shape3d_server.py"), "--port", str(self.port)]


class AudioService(Service):
    """音效：MOSS-SoundEffect（OpenMOSS，Apache-2.0）MLX 4-bit（envs/audio），文字 → 环境音 WAV。交付前按需启动。"""

    def __init__(self) -> None:
        c = config.get("audio", {})
        super().__init__(name="audio", label="音效 (MOSS-SoundEffect, MLX)", port=c.get("port", 8099),
                         mem_gb=c.get("mem_gb", 4), health_path="/health", evict_priority=3)
        self.hf_home = config.resolve(c.get("hf_home", "models/audio/hf"))
        self.autostart = False

    def ready_files(self) -> list[Path]:
        return [ROOT / "envs" / "audio" / "bin" / "python", self.hf_home / ".ready"]

    def model_dirs(self) -> list[Path]:
        return [self.hf_home]

    def env(self) -> dict[str, str]:
        return {"HF_HOME": str(self.hf_home), "HF_HUB_OFFLINE": "1"}

    def command(self) -> list[str]:
        return [str(ROOT / "envs" / "audio" / "bin" / "python"),
                str(ROOT / "src" / "genai_pipeline" / "audio_server.py"), "--port", str(self.port)]


class ServiceManager:
    def __init__(self) -> None:
        self.services: dict[str, Service] = {
            s.name: s for s in (LLMService("main"), ImageService(), DecisionService(),
                                MetricsService(), SamService(), Shape3DService(), AudioService(),
                                *[LLMService(k) for k, v in (config.get("llm") or {}).items()
                                  if k != "main" and isinstance(v, dict) and v.get("model_path")])
        }
        self._lock = threading.RLock()
        self._leases: dict[str, int] = {}
        LOGS.mkdir(exist_ok=True)

    async def run(self, name: str, call: Callable[[], Awaitable[T]], *, timeout: float = 900) -> T:
        """Hold the service through startup and the full response/stream lifetime."""
        async def execute() -> T:
            with self._lock:
                self._leases[name] = self._leases.get(name, 0) + 1
            try:
                if not await asyncio.to_thread(self.ensure, name, timeout):
                    raise ServiceUnavailableError(f"{name} 不可用：{self.services[name].last_error}")
                return await call()
            finally:
                with self._lock:
                    self._leases[name] -= 1
        return await compute.run(name, execute)

    # ── 进程控制 ──
    def _alive(self, s: Service) -> bool:
        return (s.proc is not None and s.proc.poll() is None) or self._external_pid(s) is not None

    def _external_pid(self, s: Service) -> int | None:
        """控制台重启后，之前启动的服务仍在监听端口：按端口找到它，纳入管理（计入内存、可停止/驱逐）。"""
        if s.proc is not None and s.proc.poll() is None:
            return None
        if not self._port_open(s.port):
            return None
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{s.port}", "-sTCP:LISTEN"], capture_output=True,
                                 text=True, timeout=5).stdout.split()
            return int(out[0]) if out else None
        except (subprocess.SubprocessError, ValueError, OSError):
            return None

    def _port_open(self, port: int) -> bool:
        import socket
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            return False

    def healthy(self, name: str, timeout: float = 2.0) -> bool:
        s = self.services[name]
        try:
            r = httpx.get(s.url + s.health_path, timeout=timeout)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def start(self, name: str, wait: bool = False, timeout: float = 600) -> dict:
        with self._lock:
            s = self.services[name]
            if self._alive(s) or self.healthy(name):
                return self.status_one(name)
            if s.missing():
                s.last_error = "模型文件未就绪: " + ", ".join(s.missing())
                return self.status_one(name)
            try:
                self._make_room(s)
            except MemoryBudgetError as exc:
                s.last_error = str(exc)
                raise
            log = open(LOGS / f"{name}.log", "ab")
            log.write(f"\n=== start {time.ctime()} ===\n".encode())
            env = dict(os.environ, PYTHONUNBUFFERED="1", HF_HUB_OFFLINE="1", **s.env())
            s.proc = subprocess.Popen(s.command(), stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, env=env,
                                      start_new_session=True)
            s.started_at = time.time()
            s.last_error = None
        if wait:
            self.wait_ready(name, timeout)
        return self.status_one(name)

    def wait_ready(self, name: str, timeout: float = 600) -> bool:
        s = self.services[name]
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.healthy(name):
                return True
            if s.proc is not None and s.proc.poll() is not None:
                s.last_error = f"进程退出，返回码 {s.proc.returncode}，见 logs/{name}.log"
                return False
            if s.proc is None and not self._alive(s):  # 启动后被 stop/驱逐：立即报错，不空等到超时
                s.last_error = "启动后被停止（可能被其他服务驱逐）"
                return False
            time.sleep(2)
        s.last_error = "启动超时"
        return False

    def ensure(self, name: str, timeout: float = 900) -> bool:
        """确保服务在线（流水线调用）。"""
        if self.healthy(name):
            return True
        s = self.services[name]
        if s.missing():
            s.last_error = "模型文件未就绪: " + ", ".join(s.missing())
            return False
        self.start(name)
        return self.wait_ready(name, timeout)

    def available(self, name: str) -> bool:
        """服务是否可以使用（在线，或模型文件齐全可以启动）。未配置的服务（如已移除的兜底模型）视为不可用。"""
        if name not in self.services:
            return False
        return self.healthy(name, timeout=1) or not self.services[name].missing()

    def stop(self, name: str) -> dict:
        with self._lock:
            if self._leases.get(name, 0):
                raise ServiceBusyError(f"{name} 正在处理请求，请等待当前调用结束后再停止")
            s = self.services[name]
            ext = self._external_pid(s)
            if ext:
                try:
                    os.kill(ext, signal.SIGTERM)
                    for _ in range(30):
                        time.sleep(0.5)
                        os.kill(ext, 0)
                    os.kill(ext, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif s.proc is not None and s.proc.poll() is None:
                try:
                    os.killpg(s.proc.pid, signal.SIGTERM)
                    s.proc.wait(15)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    try:
                        os.killpg(s.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            s.proc = None
            s.started_at = None
        return self.status_one(name)

    def stop_all(self) -> None:
        for name in self.services:
            self.stop(name)

    def _make_room(self, incoming: Service) -> None:
        budget = float(config.get("memory.gpu_budget_gb", 48)) - float(config.get("memory.engine_reserve_gb", 8))
        running = [s for s in self.services.values() if s is not incoming and self._alive(s)]
        used = sum(s.mem_gb for s in running)
        victims = []
        for s in sorted(running, key=lambda x: -x.evict_priority):
            if used + incoming.mem_gb <= budget:
                break
            if s.evict_priority < 0 or self._leases.get(s.name, 0):
                continue
            victims.append(s)
            used -= s.mem_gb
        if used + incoming.mem_gb > budget:
            raise MemoryBudgetError(f"{incoming.name} 无法启动：需 {incoming.mem_gb:g} GB，"
                                    f"不可驱逐服务占用 {used:g} GB，模型驻留预算 {budget:g} GB（已预留引擎内存）")
        # Plan first: impossible requests must not evict otherwise useful models.
        for s in victims:
            self.stop(s.name)

    # ── 状态 ──
    def status_one(self, name: str) -> dict:
        s = self.services[name]
        alive = self._alive(s)
        healthy = self.healthy(name, timeout=1.0) if alive or self._port_open(s.port) else False
        state = "ready" if healthy else ("starting" if alive else "stopped")
        if not alive and not healthy and s.last_error:
            state = "error"
        rss = None
        if alive:
            try:
                p = psutil.Process(s.proc.pid if s.proc is not None and s.proc.poll() is None
                                   else self._external_pid(s))
                rss = sum(c.memory_info().rss for c in [p, *p.children(recursive=True)]) / 1e9
            except psutil.Error:
                pass
        return {"name": name, "label": s.label, "state": state, "port": s.port, "mem_gb": s.mem_gb,
                "rss_gb": round(rss, 2) if rss else None, "started_at": s.started_at,
                "active_requests": self._leases.get(name, 0),
                "error": s.last_error, "missing": s.missing(),
                "external": alive and not (s.proc is not None and s.proc.poll() is None)}

    def status(self) -> dict:
        vm = psutil.virtual_memory()
        engines = {
            "blender": _binary_status(config.get("blender.binary")),
            "godot": _binary_status(config.get("godot.binary")),
        }
        return {
            "services": [self.status_one(n) for n in self.services],
            "engines": engines,
            "memory": {"total_gb": round(vm.total / 1e9, 1), "used_gb": round((vm.total - vm.available) / 1e9, 1),
                       "percent": vm.percent, "budget_gb": config.get("memory.gpu_budget_gb"),
                       "engine_reserve_gb": config.get("memory.engine_reserve_gb", 8),
                       "service_budget_gb": float(config.get("memory.gpu_budget_gb", 48)) - float(config.get("memory.engine_reserve_gb", 8))},
            "downloads": _download_status(),
            "compute": compute.snapshot(),
        }


_bin_cache: dict[str, dict] = {}


def _binary_status(path: str) -> dict:
    if path in _bin_cache:
        return _bin_cache[path]
    p = Path(path)
    info = {"path": path, "present": p.exists(), "version": None}
    if p.exists():
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30).stdout
            info["version"] = out.strip().splitlines()[0] if out.strip() else None
        except (subprocess.TimeoutExpired, OSError):
            pass
    _bin_cache[path] = info
    return info


def _download_status() -> dict:
    f = ROOT / "models" / ".fetch-status.json"
    if not f.exists():
        return {}
    try:
        import json
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


manager = ServiceManager()
