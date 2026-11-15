"""测试隔离：在导入任何项目模块之前，把数据库与登录文件指到临时目录。
console.app 在导入时就会打开 Store() 与 LocalAuth()；不隔离的话，测试会和正在运行的控制台同时写 data/pipeline.db，
跨进程（尤其经虚拟机共享目录）写同一个 SQLite 会把库写坏（2026-10-20 实际发生过一次）。"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="genai-tests-")
os.environ.setdefault("GENAI_DB_PATH", os.path.join(_tmp, "pipeline.db"))
os.environ.setdefault("GENAI_AUTH_FILE", os.path.join(_tmp, "auth.json"))
