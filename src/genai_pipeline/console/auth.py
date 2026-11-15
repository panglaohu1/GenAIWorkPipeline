"""Local account authentication. No default password or cloud account.

主账户（data/auth.json）首次使用时创建；之后可在登录页「注册新账户」添加更多本机账户（data/auth_users.json，
只接受来自本机 127.0.0.1 的注册请求）。密码一律加盐 scrypt 哈希存储。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from ..config import ROOT

COOKIE = "genai_session"


class LocalAuth:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("GENAI_AUTH_FILE", ROOT / "data" / "auth.json"))
        self._lock = threading.RLock()
        self._sessions: dict[str, tuple[str, float]] = self._load_sessions()
        self._attempts: dict[str, list[float]] = {}

    # 会话只存令牌的 SHA-256（与内存中一致），控制台重启后仍有效，12 小时过期
    @property
    def sessions_path(self) -> Path:
        return self.path.with_name("auth_sessions.json")

    def _load_sessions(self) -> dict[str, tuple[str, float]]:
        try:
            raw = json.loads(self.sessions_path.read_text())
            now = time.time()
            return {k: (v[0], float(v[1])) for k, v in raw.items() if float(v[1]) > now}
        except (OSError, ValueError, TypeError, IndexError):
            return {}

    def _save_sessions(self) -> None:
        try:
            tmp = self.sessions_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({k: list(v) for k, v in self._sessions.items()}, f)
            tmp.replace(self.sessions_path)
        except OSError:
            pass

    @property
    def configured(self):
        return self.path.exists()

    @property
    def users_path(self) -> Path:
        return self.path.with_name("auth_users.json")

    def _users(self) -> dict:
        return json.loads(self.users_path.read_text()) if self.users_path.exists() else {}

    def register(self, username: str, password: str):
        """添加一个本机账户（主账户已存在时）；尚无主账户时等同首次创建。"""
        username = username.strip()
        if not self.configured:
            return self.setup(username, password)
        if not 2 <= len(username) <= 64 or not 10 <= len(password) <= 128:
            raise ValueError("用户名需 2–64 个字符，密码需 10–128 个字符")
        with self._lock:
            primary = json.loads(self.path.read_text())["username"]
            users = self._users()
            if username == primary or username in users:
                raise ValueError("这个用户名已经存在，请直接登录")
            if len(users) >= 20:
                raise ValueError("本机账户已达上限")
            salt = secrets.token_hex(16)
            users[username] = {"salt": salt, "hash": self._hash(password, salt), "created": time.time()}
            tmp = self.users_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(users, f)
            tmp.replace(self.users_path)

    @staticmethod
    def _hash(password, salt):
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

    def setup(self, username: str, password: str):
        username = username.strip()
        if not 2 <= len(username) <= 64 or not 10 <= len(password) <= 128:
            raise ValueError("用户名需 2–64 个字符，密码需 10–128 个字符")
        with self._lock:
            salt = secrets.token_hex(16)
            data = {"username": username, "salt": salt, "hash": self._hash(password, salt)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise ValueError("本机账户已创建，请直接登录")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)

    def login(self, username: str, password: str, client: str = "local") -> str:
        with self._lock:
            now = time.time()
            attempts = [t for t in self._attempts.get(client, []) if now - t < 60]
            if len(attempts) >= 8:
                raise ValueError("尝试过于频繁，请在一分钟后重试")
            attempts.append(now)
            self._attempts[client] = attempts
            if not self.configured:
                raise ValueError("请先创建本机账户")
            data = json.loads(self.path.read_text())
            if hmac.compare_digest(username.encode(), data["username"].encode()):
                record = data
            else:
                record = self._users().get(username)
            if len(password) > 128 or record is None \
                    or not hmac.compare_digest(self._hash(password, record["salt"]), record["hash"]):
                raise ValueError("用户名或密码不正确")
            self._attempts.pop(client, None)
            self._sessions = {k: v for k, v in self._sessions.items() if v[1] > now}
            if len(self._sessions) >= 64:
                self._sessions.pop(next(iter(self._sessions)))
            token = secrets.token_urlsafe(32)
            self._sessions[hashlib.sha256(token.encode()).hexdigest()] = (username, now + 12 * 3600)
            self._save_sessions()
            return token

    def user(self, token: str | None):
        key = hashlib.sha256((token or "").encode()).hexdigest()
        with self._lock:
            session = self._sessions.get(key)
            if session and session[1] > time.time():
                return session[0]
            self._sessions.pop(key, None)
        return None

    def logout(self, token: str | None):
        with self._lock:
            self._sessions.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)
            self._save_sessions()


auth = LocalAuth()
