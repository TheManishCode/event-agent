"""NVIDIA and OpenRouter clients, fallback and credit guard (ADR-032). No test here may reach a real provider or spend a credit."""

import json
import urllib.error
from pathlib import Path

import pytest

from eventmate import env, llm
from eventmate.llm import FallbackClient, LLMError, OpenAICompatClient, UsageMeter, client_from_env

KEY = "nvapi-SECRET1234567890"


def reply(content="hello", finish="stop"):
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]}).encode()


class Net:
    def __init__(self, body=None, exc=None):
        self.body, self.exc, self.calls = body if body is not None else reply(), exc, []

    def __call__(self, url, headers, data, timeout):
        self.calls.append((url, headers, json.loads(data)))
        if self.exc:
            raise self.exc
        return self.body


def client(net=None, name="nvidia", model="nvidia/some-model", meter=None, **kw):
    return OpenAICompatClient(name, "https://api.example.test/v1", KEY, model, transport=net or Net(), meter=meter or UsageMeter(100), **kw)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "EVENTMATE_ENABLE_LLM", "EVENTMATE_NVIDIA_MODEL",
              "EVENTMATE_OPENROUTER_MODEL_HEAVY", "EVENTMATE_OPENROUTER_MODEL_LIGHT", "EVENTMATE_LLM_MAX_TOKENS", "EVENTMATE_LLM_MAX_CALLS"):
        monkeypatch.delenv(k, raising=False)
    llm.METER.reset()


# ------------------------------------------------------------------- one client


def test_request_shape_is_openai_compatible_and_uses_zero_temperature():
    net = Net()
    out = client(net).complete("be brief", "hi", 50)
    url, headers, body = net.calls[0]
    assert out == "hello" and url == "https://api.example.test/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {KEY}" and headers["Content-Type"] == "application/json"
    assert body == {"model": "nvidia/some-model", "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}], "max_tokens": 50, "temperature": 0}


def test_reasoning_blocks_are_stripped_and_missing_content_is_empty():
    assert client(Net(reply("<think>secret plan</think>The answer."))).complete("s", "u", 50) == "The answer."
    assert client(Net(json.dumps({"choices": [{"message": {"content": None}, "finish_reason": "length"}]}).encode())).complete("s", "u", 50) == ""


def test_max_tokens_is_clamped_so_one_call_cannot_burn_credits(monkeypatch):
    net = Net()
    client(net).complete("s", "u", 10**6)
    assert net.calls[0][2]["max_tokens"] == 2000
    monkeypatch.setenv("EVENTMATE_LLM_MAX_TOKENS", "500")
    client(net).complete("s", "u", 10**6)
    assert net.calls[1][2]["max_tokens"] == 500
    with pytest.raises(LLMError):
        client(net).complete("s", "u", 0)


@pytest.mark.parametrize("exc, text", [
    (urllib.error.HTTPError("https://x", 401, "no", {}, None), "rejected the API key"),
    (urllib.error.HTTPError("https://x", 402, "no", {}, None), "out of credit"),
    (urllib.error.HTTPError("https://x", 429, "no", {}, None), "rate limit"),
    (urllib.error.HTTPError("https://x", 503, "no", {}, None), "failed (HTTP 503)"),
    (OSError(f"reset while sending {KEY}"), "could not be reached"),
    (TimeoutError(), "could not be reached"),
])
def test_failures_are_translated_and_never_contain_the_key(exc, text):
    with pytest.raises(LLMError) as e:
        client(Net(exc=exc)).complete("s", "u", 50)
    assert text in str(e.value) and KEY not in str(e.value) and KEY not in repr(e.value) and "nvidia" in str(e.value)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"choices": []}', b'{"choices": [{"message": {"content": 5}}]}', b"x" * 3_000_000])
def test_malformed_or_oversized_replies_fail_closed(body):
    with pytest.raises(LLMError):
        client(Net(body)).complete("s", "u", 50)


def test_the_client_never_prints_its_key():
    assert KEY not in repr(client()) and KEY not in str(client().__dict__.get("name"))


def test_endpoints_must_be_https():
    with pytest.raises(ValueError):
        OpenAICompatClient("x", "http://insecure.test/v1", KEY, "m")


# -------------------------------------------------------------------- fallback


def test_fallback_uses_the_first_working_provider_and_remembers_it():
    a, b = client(Net(exc=OSError("down")), name="nvidia"), client(Net(reply("from b")), name="openrouter")
    fb = FallbackClient([a, b])
    assert fb.complete("s", "u", 50) == "from b" and fb.last_used == "openrouter" and "nvidia" in fb.name and "openrouter" in fb.name
    fb2 = FallbackClient([client(name="nvidia"), b])
    fb2.complete("s", "u", 50)
    assert fb2.last_used == "nvidia"


def test_fallback_reports_every_failure_without_keys_when_all_fail():
    fb = FallbackClient([client(Net(exc=OSError()), name="nvidia"), client(Net(exc=urllib.error.HTTPError("u", 429, "x", {}, None)), name="openrouter")])
    with pytest.raises(LLMError) as e:
        fb.complete("s", "u", 50)
    assert "nvidia" in str(e.value) and "openrouter" in str(e.value) and KEY not in str(e.value)


# ----------------------------------------------------------------- credit guard


def test_the_call_budget_stops_runaway_spend():
    meter = UsageMeter(max_calls=2)
    c = client(meter=meter)
    c.complete("s", "u", 50)
    c.complete("s", "u", 50)
    with pytest.raises(LLMError, match="call budget"):
        c.complete("s", "u", 50)
    assert meter.status()["calls"] == 2 and meter.status()["by_provider"] == {"nvidia": 2}


def test_a_failed_call_still_counts_because_the_provider_may_have_billed_it():
    meter = UsageMeter(max_calls=5)
    with pytest.raises(LLMError):
        client(Net(exc=urllib.error.HTTPError("u", 500, "x", {}, None)), meter=meter).complete("s", "u", 50)
    assert meter.status()["calls"] == 1


# ------------------------------------------------------------ building from env


def test_nothing_is_created_without_keys_or_with_only_an_anthropic_key(monkeypatch):
    assert client_from_env() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("EVENTMATE_ENABLE_LLM", "1")
    assert client_from_env() is None  # Anthropic is no longer a provider


def test_heavy_tier_prefers_nvidia_and_light_tier_prefers_openrouter(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-aaaaaaaa")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbb")
    heavy = client_from_env(tier="heavy", transport=Net())
    light = client_from_env(tier="light", transport=Net())
    assert [c.name for c in heavy.clients] == ["nvidia", "openrouter"] and [c.name for c in light.clients] == ["openrouter", "nvidia"]
    assert heavy.clients[0].model == "nvidia/nemotron-3-ultra-550b-a55b" and heavy.clients[1].model.endswith(":free") and light.clients[0].model.endswith(":free")


def test_a_single_key_gives_a_single_provider_and_models_can_be_overridden(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbb")
    monkeypatch.setenv("EVENTMATE_OPENROUTER_MODEL_HEAVY", "vendor/model:free")
    c = client_from_env(tier="heavy", transport=Net())
    assert [x.name for x in c.clients] == ["openrouter"] and c.clients[0].model == "vendor/model:free"


def test_the_kill_switch_wins_over_keys(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-aaaaaaaa")
    monkeypatch.setenv("EVENTMATE_ENABLE_LLM", "0")
    assert client_from_env() is None


def test_malformed_keys_are_ignored_rather_than_sent(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "bad key with spaces\nand newline")
    assert client_from_env() is None


def test_anthropic_is_gone_from_the_code_and_the_env_allow_list():
    src = Path(llm.__file__).read_text()
    assert "import anthropic" not in src and "claude-" not in src
    assert "ANTHROPIC_API_KEY" not in env.ALLOWED and {"NVIDIA_API_KEY", "OPENROUTER_API_KEY"} <= env.ALLOWED
    assert "claude-" not in (Path(llm.__file__).parent / "assistant" / "agent.py").read_text()


def test_langfuse_accepts_either_host_variable_name(monkeypatch):
    from eventmate.assistant.langfuse import LangfuseExporter

    for k in ("LANGFUSE_HOST", "LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert LangfuseExporter.from_env() is None
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-aaaaaaaa")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-bbbbbbbb")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.example")
    assert LangfuseExporter.from_env() is not None
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://insecure.example")
    assert LangfuseExporter.from_env() is None  # still https-only for non-local hosts


def test_the_light_nvidia_model_is_a_fast_non_reasoning_choice_with_low_effort_and_a_short_timeout(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-aaaaaaaa")
    seen = []

    def t(url, headers, data, timeout):
        seen.append((json.loads(data), timeout))
        return reply()

    heavy = client_from_env(tier="heavy", transport=t)
    light = client_from_env(tier="light", transport=t)
    heavy.complete("s", "u", 50)
    light.complete("s", "u", 50)
    assert seen[0][0]["model"] == "nvidia/nemotron-3-ultra-550b-a55b" and "reasoning_effort" not in seen[0][0] and seen[0][1] == 90
    assert seen[1][0]["model"] == "openai/gpt-oss-20b" and seen[1][0]["reasoning_effort"] == "low" and seen[1][1] == 30
    monkeypatch.setenv("EVENTMATE_NVIDIA_MODEL_LIGHT", "vendor/other-light")
    assert client_from_env(tier="light", transport=t).clients[0].model == "vendor/other-light"
