import uvicorn

from .. import config

if __name__ == "__main__":
    # 控制台页面的 SSE 长连接不会自己断开：不设优雅关停超时的话，SIGTERM 后进程会一直等连接关闭，
    # 端口已释放但后台线程（运行队列、自动启动）仍在跑，和新控制台抢服务（实测导致 Qwen 被反复停掉）。
    uvicorn.run("genai_pipeline.console.app:app", host=config.get("console.host", "127.0.0.1"),
                port=int(config.get("console.port", 8700)), log_level="warning", timeout_graceful_shutdown=5)
