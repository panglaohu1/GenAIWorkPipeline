"""本机多账户：主账户之后可注册更多账户；用户名不可重复；只接受本机注册请求。"""
import pytest
from httpx import ASGITransport, AsyncClient

from genai_pipeline.console.auth import LocalAuth


def test_register_after_primary_and_login(tmp_path):
    a = LocalAuth(tmp_path / "auth.json")
    a.setup("artist", "primary-password")
    a.register("helper", "helper-password-1")
    assert a.user(a.login("helper", "helper-password-1"))
    assert a.user(a.login("artist", "primary-password"))
    with pytest.raises(ValueError):
        a.login("helper", "wrong-password")
    with pytest.raises(ValueError):
        a.register("artist", "another-password")
    with pytest.raises(ValueError):
        a.register("helper", "another-password")
    with pytest.raises(ValueError):
        a.register("x", "short")
    assert (tmp_path / "auth_users.json").stat().st_mode & 0o077 == 0


def test_register_without_primary_creates_primary(tmp_path):
    a = LocalAuth(tmp_path / "auth.json")
    a.register("first", "first-password")
    assert a.configured and a.user(a.login("first", "first-password"))


@pytest.mark.asyncio
async def test_register_endpoint(tmp_path, monkeypatch):
    from genai_pipeline.console import app as web
    monkeypatch.setattr(web, "auth", LocalAuth(tmp_path / "auth.json"))
    web.auth.setup("artist", "primary-password")
    async with AsyncClient(transport=ASGITransport(app=web.app, client=("127.0.0.1", 5000)), base_url="http://t") as c:
        r = await c.post("/api/auth/register", json={"username": "helper", "password": "helper-password-1"})
        assert r.status_code == 200 and c.cookies.get("genai_session")
        assert (await c.get("/api/auth/status")).json()["user"] == "helper"
    async with AsyncClient(transport=ASGITransport(app=web.app, client=("192.168.1.9", 5000)), base_url="http://t") as c:
        r = await c.post("/api/auth/register", json={"username": "remote", "password": "remote-password-1"})
        assert r.status_code == 403
