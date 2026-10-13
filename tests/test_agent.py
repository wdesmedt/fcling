"""The agent's loop, against a scripted stand-in for the Claude API."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace as NS
from typing import Any

import pytest

from fcli import agent as agent_module
from fcli.agent import Agent, Conversation, Conversations

from .fake_service import FakeService


def _message(*blocks: Any, stop: str = "end_turn") -> Any:
    return NS(
        content=list(blocks),
        stop_reason=stop,
        usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=7, cache_creation_input_tokens=0),
    )


def _text(t: str) -> Any:
    return NS(type="text", text=t)


def _use(id_: str, name: str, input_: dict[str, Any]) -> Any:
    return NS(type="tool_use", id=id_, name=name, input=input_)


class _Stream:
    def __init__(self, message: Any) -> None:
        self.message = message

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def __aiter__(self) -> Any:
        for block in self.message.content:
            if block.type == "text":
                yield NS(type="text", text=block.text)

    async def get_final_message(self) -> Any:
        return self.message


class _Client:
    """``client.beta.messages.stream(...)``, answering from a script and keeping each request."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.beta = self.messages = self

    def stream(self, **request: Any) -> _Stream:
        self.requests.append({**request, "messages": copy.deepcopy(request["messages"])})
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Stream(answer)


async def _turn(
    client: _Client, question: str = "what is wrong?"
) -> tuple[list[dict[str, Any]], Conversation]:
    conversation = Conversation()
    agent = Agent(FakeService(), fabric="lab", provider="claude", client=client)
    events = [e async for e in agent.turn(conversation, question, {"viewing": "bgp-peers"})]
    return events, conversation


async def test_a_turn_runs_the_tools_it_asks_for_and_answers() -> None:
    client = _Client(
        _message(
            _text("Checking. "),
            _use("t1", "get_incidents", {}),
            _use("t2", "get_report", {"name": "bgp-peers"}),
            stop="tool_use",
        ),
        _message(_text("leaf1 has a BGP session down.")),
    )
    events, conversation = await _turn(client)
    kinds = [e["type"] for e in events]
    assert kinds == ["text", "tool", "tool", "tool_result", "tool_result", "text", "done"]
    assert events[-1]["usage"] == {"input": 20, "output": 10, "cache_read": 14, "cache_write": 0}

    first, second = client.requests
    assert first["messages"][0]["content"].startswith("[fabric lab; viewing bgp-peers]\n\nwhat is wrong?")
    assert first["model"] == "claude-opus-5-5" and first["output_config"] == {"effort": "medium"}
    assert first["thinking"] == {"type": "adaptive"} and first["fallbacks"] == "default"
    assert first["cache_control"] == {"type": "ephemeral"}
    # The cached prefix: the same tools and system prompt on every request.
    assert first["tools"] == second["tools"] and first["system"] == second["system"]
    assert all(t["eager_input_streaming"] for t in first["tools"])
    # Every result of the round in one message, in order.
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert json.loads(results[1]["content"])["count"] == 3
    # Append-only: user, assistant, tool results, assistant.
    assert [m["role"] for m in conversation.messages] == ["user", "assistant", "user", "assistant"]


async def test_a_bad_tool_input_is_an_error_result_not_a_crash() -> None:
    client = _Client(
        _message(_use("t1", "get_report", {"name": "bgp-peers", "limit": 0}), stop="tool_use"),
        _message(_text("I could not read that.")),
    )
    events, _ = await _turn(client)
    assert next(e for e in events if e["type"] == "tool_result")["error"] is True
    (result,) = client.requests[1]["messages"][-1]["content"]
    assert result["is_error"] and "limit" in result["content"]


async def test_out_of_rounds_it_must_answer(monkeypatch: Any) -> None:
    monkeypatch.setattr(agent_module, "MAX_ROUNDS", 2)
    loop = _message(_use("t", "fabric_status", {}), stop="tool_use")
    client = _Client(loop, loop, _message(_text("Here is what I found.")))
    events, _ = await _turn(client)
    assert events[-1]["type"] == "done"
    assert [r.get("tool_choice") for r in client.requests] == [None, None, {"type": "none"}]


async def test_a_refusal_or_an_api_error_ends_the_turn_cleanly() -> None:
    client = _Client(_message(_use("t1", "fabric_status", {}), stop="refusal"))
    events, conversation = await _turn(client)
    assert events[-1] == {"type": "error", "message": "the model declined to answer this"}
    # The unanswered call is closed, so the next question can follow.
    assert conversation.messages[-1]["content"][0]["is_error"] is True

    events, _ = await _turn(_Client(RuntimeError("boom")))
    assert events == [{"type": "error", "message": "RuntimeError: boom"}]


def test_conversations_are_kept_and_the_oldest_dropped() -> None:
    kept = Conversations(limit=2)
    a = kept.get(None)
    assert kept.get(a.id) is a
    kept.get(None)
    kept.get(None)
    assert kept.get(a.id) is not a


# --------------------------------------------------------------------------- #
# OpenAI Chat Completions
# --------------------------------------------------------------------------- #


def _chunk(
    content: str | None = None,
    calls: list[Any] | None = None,
    finish: str | None = None,
    refusal: str | None = None,
    usage: Any = None,
) -> Any:
    choices = (
        []
        if usage is not None
        else [NS(finish_reason=finish, delta=NS(content=content, tool_calls=calls, refusal=refusal))]
    )
    return NS(choices=choices, usage=usage)


def _piece(index: int, id_: str | None = None, name: str | None = None, args: str | None = None) -> Any:
    return NS(index=index, id=id_, function=NS(name=name, arguments=args))


class _Chat:
    """``client.chat.completions.create(stream=True)``, from a script of chunk lists."""

    def __init__(self, *script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = self.completions = self

    async def create(self, **request: Any) -> Any:
        self.requests.append({**request, "messages": copy.deepcopy(request["messages"])})
        chunks = self.script.pop(0)

        async def stream() -> Any:
            for c in chunks:
                yield c

        return stream()


@pytest.fixture
def chat_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI through Chat Completions, as a proxy in front of it would take it."""
    monkeypatch.setenv("OPENAI_API", "chat")


async def _openai_turn(client: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], Conversation]:
    conversation = Conversation()
    agent = Agent(FakeService(), fabric="lab", provider="openai", client=client, **kwargs)
    events = [e async for e in agent.turn(conversation, "what is down?")]
    return events, conversation


USAGE = NS(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=NS(cached_tokens=64))


async def test_openai_tool_calls_arrive_in_pieces_and_results_go_back_as_tool_messages(
    chat_api: None,
) -> None:
    client = _Chat(
        [
            _chunk(calls=[_piece(0, "c1", "get_report", '{"name": "bgp'), _piece(1, "c2", "get_incid", "")]),
            _chunk(
                calls=[
                    _piece(0, args='-peers", "where": ["state=active"]}'),
                    _piece(1, name="ents", args="{}"),
                ]
            ),
            _chunk(finish="tool_calls"),
            _chunk(usage=USAGE),
        ],
        [_chunk("10.0.0.2 "), _chunk("is down.", finish="stop"), _chunk(usage=USAGE)],
    )
    events, conversation = await _openai_turn(client)
    assert [e["type"] for e in events] == [
        "tool",
        "tool",
        "tool_result",
        "tool_result",
        "text",
        "text",
        "done",
    ]
    assert [(e["name"], e["input"]) for e in events if e["type"] == "tool"] == [
        ("get_report", {"name": "bgp-peers", "where": ["state=active"]}),
        ("get_incidents", {}),
    ]
    assert events[-1]["usage"] == {"input": 200, "output": 40, "cache_read": 128, "cache_write": 0}

    first, second = client.requests
    assert first["model"] == "gpt-5.6-sol" and "reasoning_effort" not in first
    assert first["messages"][0] == {"role": "system", "content": agent_module.SYSTEM_PROMPT}
    assert first["tools"][0]["type"] == "function" and first["tool_choice"] == "auto"
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "tool", "tool"]
    assert second["messages"][2]["tool_calls"][0]["function"]["arguments"].startswith('{"name": "bgp-peers"')
    report = json.loads(second["messages"][3]["content"])
    assert report["rows"][0]["peer"] == "10.0.0.2"
    # The system prompt is sent, not kept: the conversation is what was said.
    assert [m["role"] for m in conversation.messages] == ["user", "assistant", "tool", "tool", "assistant"]


async def test_openai_bad_arguments_are_answered_not_run(monkeypatch: Any, chat_api: None) -> None:
    monkeypatch.setattr(agent_module, "MAX_ROUNDS", 1)
    client = _Chat(
        [_chunk(calls=[_piece(0, "c1", "get_report", "{not json")]), _chunk(finish="tool_calls")],
        [_chunk("Sorry.", finish="stop")],
    )
    events, _ = await _openai_turn(client, effort="low")
    assert next(e for e in events if e["type"] == "tool_result")["error"] is True
    second = client.requests[1]
    assert second["messages"][-1]["content"].startswith("error: the arguments were not valid JSON")
    assert client.requests[0]["reasoning_effort"] == "low"
    # Out of rounds: the last request may not call tools.
    assert second["tool_choice"] == "none"


async def test_openai_refusal_ends_the_turn(chat_api: None) -> None:
    client = _Chat([_chunk(refusal="I can't help with that.", finish="stop")])
    events, _ = await _openai_turn(client)
    assert events == [{"type": "error", "message": "the model declined to answer this"}]


def test_the_provider_is_the_one_asked_for_or_the_first_with_a_key(monkeypatch: Any) -> None:
    from fcli.llm import ProviderError, choose, create

    assert choose().name == "claude"  # nothing configured: Claude, which says what is missing
    monkeypatch.setenv("XAI_API_KEY", "x")
    assert choose().name == "grok"
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert choose().name == "openai"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert choose().name == "claude"
    monkeypatch.setenv("FCLI_LLM_PROVIDER", "grok")
    grok = create()
    assert (grok.name, grok.model, grok.effort) == ("grok", "grok-4.6", None)
    monkeypatch.setenv("XAI_MODEL", "grok-5")
    assert create().model == "grok-5"
    try:
        create(effort="max")
    except ProviderError as exc:
        assert "Grok takes effort low, medium, high, xhigh" in str(exc)
    else:
        raise AssertionError("an effort Grok does not take was accepted")


async def test_without_a_key_openai_says_which() -> None:
    conversation = Conversation()
    agent = Agent(FakeService(), provider="openai")
    events = [e async for e in agent.turn(conversation, "hi")]
    assert events == [{"type": "error", "message": "no OpenAI key: set OPENAI_API_KEY"}]


async def test_a_conversation_stays_with_its_provider() -> None:
    conversation = Conversation(provider="openai")
    agent = Agent(FakeService(), provider="claude", client=_Client())
    events = [e async for e in agent.turn(conversation, "hi")]
    assert "this conversation is with openai" in events[0]["message"]


# --------------------------------------------------------------------------- #
# OpenAI Responses
# --------------------------------------------------------------------------- #


class _Item(dict):  # type: ignore[type-arg]
    """An output item, as the SDK's models dump themselves."""

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return dict(self)


def _response(*items: dict[str, Any], status: str = "completed", reason: str | None = None) -> Any:
    return NS(
        status=status,
        error=NS(message="server overloaded") if status == "failed" else None,
        incomplete_details=NS(reason=reason) if reason else None,
        usage=NS(input_tokens=50, output_tokens=10, input_tokens_details=NS(cached_tokens=32)),
        output=[_Item(i) for i in items],
    )


REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}


class _Responses:
    """``client.responses.create(stream=True)``, from a script of (text deltas, final response)."""

    def __init__(self, *script: tuple[list[str], Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.responses = self

    async def create(self, **request: Any) -> Any:
        self.requests.append({**request, "input": copy.deepcopy(request["input"])})
        deltas, response = self.script.pop(0)

        async def stream() -> Any:
            for d in deltas:
                yield NS(type="response.output_text.delta", delta=d)
            kind = {"completed": "response.completed", "incomplete": "response.incomplete"}.get(
                response.status, "response.failed"
            )
            yield NS(type=kind, response=response)

        return stream()


async def test_openai_responses_replays_its_output_and_answers_calls_by_call_id() -> None:
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "get_report",
        "arguments": '{"name": "bgp-peers", "where": ["state=active"]}',
    }
    said = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "10.0.0.2 is down."}],
    }
    client = _Responses(([], _response(REASONING, call)), (["10.0.0.2 ", "is down."], _response(said)))
    events, conversation = await _openai_turn(client, effort="high")
    assert [e["type"] for e in events] == ["tool", "tool_result", "text", "text", "done"]
    assert events[-1]["usage"] == {"input": 100, "output": 20, "cache_read": 64, "cache_write": 0}

    first, second = client.requests
    assert first["model"] == "gpt-5.6-sol" and first["instructions"] == agent_module.SYSTEM_PROMPT
    assert first["store"] is False and first["include"] == ["reasoning.encrypted_content"]
    assert first["reasoning"] == {"effort": "high"} and first["tool_choice"] == "auto"
    assert first["tools"][0] == {**first["tools"][0], "type": "function", "strict": False}
    # The turn's output - reasoning included - goes back as it came, then the result by call_id.
    user, reasoning, function_call, output = second["input"]
    assert reasoning == REASONING and function_call == call
    assert output["type"] == "function_call_output" and output["call_id"] == "call_1"
    assert json.loads(output["output"])["rows"][0]["peer"] == "10.0.0.2"
    assert conversation.messages[-1] == said


async def test_openai_responses_stops_said_as_they_are() -> None:
    refused = {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "No."}]}
    events, _ = await _openai_turn(_Responses(([], _response(refused))))
    assert events == [{"type": "error", "message": "the model declined to answer this"}]

    cut = {"type": "function_call", "call_id": "c", "name": "get_report", "arguments": '{"name": "bgp'}
    events, conversation = await _openai_turn(
        _Responses(([], _response(cut, status="incomplete", reason="max_output_tokens")))
    )
    assert events == [{"type": "error", "message": "the answer ran past its length limit"}]
    assert conversation.messages[-1]["output"] == "error: not run: the call was cut off"

    events, _ = await _openai_turn(_Responses(([], _response(status="failed"))))
    assert events == [{"type": "error", "message": "OpenAI: server overloaded"}]
