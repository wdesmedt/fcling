import pytest

_PROVIDER_ENV = (
    "FCLI_LLM_PROVIDER",
    "FCLI_MODEL",
    "FCLI_EFFORT",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_API_KEY",
    "ANTHROPIC_MODEL",
    "CLAUDE_MODEL",
    "ANTHROPIC_EFFORT",
    "CLAUDE_EFFORT",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_REASONING_EFFORT",
    "XAI_API_KEY",
    "GROK_API_KEY",
    "XAI_MODEL",
    "GROK_MODEL",
    "XAI_BASE_URL",
    "XAI_REASONING_EFFORT",
    "GROK_REASONING_EFFORT",
)


@pytest.fixture(autouse=True)
def _no_model_provider(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Which model provider is picked must not depend on the machine the tests run on."""
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
