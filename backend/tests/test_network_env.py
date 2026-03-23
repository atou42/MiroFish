import os

from app.utils.network_env import clear_proxy_env, drop_proxy_env_inplace


def test_clear_proxy_env_returns_copy_without_proxy_keys():
    env = {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "https_proxy": "http://127.0.0.1:7890",
        "CUSTOM_KEY": "value",
    }

    cleaned = clear_proxy_env(env)

    assert cleaned == {"CUSTOM_KEY": "value"}
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"


def test_drop_proxy_env_inplace_removes_proxy_keys(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("all_proxy", "socks5h://127.0.0.1:7891")
    monkeypatch.setenv("CUSTOM_KEY", "value")

    cleared = drop_proxy_env_inplace()

    assert "HTTP_PROXY" in cleared
    assert "all_proxy" in cleared
    assert os.environ["CUSTOM_KEY"] == "value"
    assert "HTTP_PROXY" not in os.environ
    assert "all_proxy" not in os.environ
