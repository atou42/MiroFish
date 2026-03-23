from app.utils.llm_client import LLMClient


def test_llm_client_disables_env_proxy_by_default(monkeypatch):
    monkeypatch.delenv("LLM_TRUST_ENV_PROXY", raising=False)
    monkeypatch.delenv("OPENAI_TRUST_ENV_PROXY", raising=False)

    client = LLMClient(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="gpt-5.4",
    )

    assert client.trust_env_proxy is False


def test_llm_client_can_enable_env_proxy_via_env(monkeypatch):
    monkeypatch.setenv("LLM_TRUST_ENV_PROXY", "true")

    assert LLMClient._resolve_trust_env_proxy(None) is True
