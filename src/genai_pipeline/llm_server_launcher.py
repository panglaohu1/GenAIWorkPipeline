"""在 envs/llm 中启动 mlx_vlm.server，并先设置 MLX 内存上限。

不设上限时，MLX 的空闲缓冲缓存会持续增长（实测 agent 多轮后单进程 GPU 占用 49 GB，
逼近 64 GB 机器约 48 GB 的 GPU 可用上限，解码从 44 tok/s 掉到 8 tok/s）。

用法: python llm_server_launcher.py --cache-limit-gb 4 --memory-limit-gb 26 -- <mlx_vlm.server 参数>
"""
import runpy
import sys


def main() -> None:
    argv = sys.argv[1:]
    sep = argv.index("--") if "--" in argv else len(argv)
    own, rest = argv[:sep], argv[sep + 1:]
    opts = {own[i].lstrip("-"): float(own[i + 1]) for i in range(0, len(own), 2)}
    import mlx.core as mx
    gb = 1 << 30
    if "cache-limit-gb" in opts:
        mx.set_cache_limit(int(opts["cache-limit-gb"] * gb))
    if "memory-limit-gb" in opts:
        mx.set_memory_limit(int(opts["memory-limit-gb"] * gb))
    print(f"[launcher] mlx cache_limit={opts.get('cache-limit-gb')}GB memory_limit={opts.get('memory-limit-gb')}GB",
          flush=True)
    sys.argv = ["mlx_vlm.server", *rest]
    runpy.run_module("mlx_vlm.server", run_name="__main__")


if __name__ == "__main__":
    main()
