"""The models the agent can run on: Claude, OpenAI, and anything that speaks OpenAI's Chat Completions.

A :class:`Provider` turns the agent's loop into one provider's requests and
back: it writes the user's message, the tool results and the system prompt
the way its API takes them, streams a request, and reads the answer into a
:class:`Reply` - the text, the tool calls, why it stopped. The conversation
itself stays in the provider's own format and is only ever appended to.

Which provider: ``FCLI_LLM_PROVIDER`` (``claude``, ``openai``, ``grok``), or
the first whose key is set, Claude first. Each takes its model, effort and -
for the OpenAI-compatible ones - base URL from its own variables, or from
``FCLI_MODEL`` / ``FCLI_EFFORT``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

#: What a provider's answer stopped on, in the agent's words.
END, TOOL_USE, MAX_TOKENS, REFUSAL, PAUSE = "end", "tool_use", "max_tokens", "refusal", "pause"


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    #: The arguments as parsed, or ``None`` when they were not valid JSON.
    input: dict[str, Any] | None


@dataclass
class Reply:
    calls: list[Call]
    stop: str
    #: What the assistant said, in the provider's format, to append as it is:
    #: one message, or for the Responses API the turn's output items.
    messages: list[Any]
    usage: dict[str, int] = field(default_factory=dict)


class Provider(Protocol):
    name: str
    model: str
    effort: str | None

    def user(self, text: str) -> Any: ...

    def results(self, results: Sequence[tuple[Call, str, bool]]) -> list[Any]:
        """Messages carrying every result of one round: ``(call, content, is_error)``."""
        ...

    def stream(
        self, system: str, tools: Sequence[dict[str, Any]], messages: list[Any], final: bool
    ) -> AsyncIterator[tuple[str, Any]]:
        """``("text", delta)`` as the answer streams, then ``("reply", Reply)``.
        On *final*, no tools: the model has to answer with what it has."""
        ...

    def error(self, exc: Exception) -> str:
        """An API failure in words, without the stack trace."""
        ...


@dataclass(frozen=True)
class Spec:
    """One provider, and where its settings come from."""

    name: str
    label: str
    keys: tuple[str, ...]
    models: tuple[str, ...]
    default_model: str
    efforts: tuple[str, ...]
    effort_envs: tuple[str, ...]
    #: Sent when nobody chose an effort; ``None`` leaves it to the model.
    default_effort: str | None = None
    base_urls: tuple[str, ...] = ()
    default_base_url: str | None = None
    #: ``anthropic``, ``responses`` or ``chat``; the variables that override it.
    api: str = "chat"
    api_envs: tuple[str, ...] = ()

    def chosen_api(self) -> str:
        api = (_env(self.api_envs) or self.api).strip().lower()
        if api not in ("anthropic", "responses", "chat"):
            raise ProviderError(f"{self.api_envs[0]}={api!r}: responses or chat")
        return api

    def key(self) -> str | None:
        return next((os.environ[k] for k in self.keys if os.environ.get(k)), None)

    def configured(self) -> bool:
        if self.key():
            return True
        # An `ant auth login` profile is credentials too, with no variable set.
        return self.name == "claude" and (Path.home() / ".config" / "anthropic").is_dir()


SPECS: dict[str, Spec] = {
    "claude": Spec(
        name="claude",
        label="Claude",
        keys=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"),
        models=("ANTHROPIC_MODEL", "CLAUDE_MODEL"),
        default_model="claude-opus-5-5",
        efforts=("low", "medium", "high", "xhigh", "max"),
        effort_envs=("ANTHROPIC_EFFORT", "CLAUDE_EFFORT"),
        # Interactive troubleshooting: the time goes on tool rounds, not the reasoning between them.
        default_effort="medium",
        api="anthropic",
    ),
    "openai": Spec(
        name="openai",
        label="OpenAI",
        keys=("OPENAI_API_KEY",),
        models=("OPENAI_MODEL",),
        default_model="gpt-5.6-sol",
        efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        effort_envs=("OPENAI_REASONING_EFFORT",),
        base_urls=("OPENAI_BASE_URL",),
        # Reasoning models take tools only through the Responses API; a proxy
        # in front of OpenAI often speaks only Chat Completions: OPENAI_API=chat.
        api="responses",
        api_envs=("OPENAI_API",),
    ),
    "grok": Spec(
        name="grok",
        label="Grok",
        keys=("XAI_API_KEY", "GROK_API_KEY"),
        models=("XAI_MODEL", "GROK_MODEL"),
        default_model="grok-4.6",
        efforts=("low", "medium", "high", "xhigh"),
        effort_envs=("XAI_REASONING_EFFORT", "GROK_REASONING_EFFORT"),
        base_urls=("XAI_BASE_URL",),
        default_base_url="https://api.x.ai/v1",
        api_envs=("XAI_API", "GROK_API"),
    ),
}

#: Which provider is used when nobody says, of those with a key.
ORDER = ("claude", "openai", "grok")


class ProviderError(ValueError):
    pass


def _env(names: Sequence[str]) -> str | None:
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


def choose(name: str | None = None) -> Spec:
    """The provider asked for, or ``FCLI_LLM_PROVIDER``, or the first with a key."""
    wanted = (name or os.environ.get("FCLI_LLM_PROVIDER") or "").strip().lower()
    if wanted:
        if wanted not in SPECS:
            raise ProviderError(f"no provider {wanted!r}: one of {', '.join(ORDER)}")
        return SPECS[wanted]
    return next((SPECS[n] for n in ORDER if SPECS[n].configured()), SPECS["claude"])


def create(
    name: str | None = None, model: str | None = None, effort: str | None = None, client: Any = None
) -> Provider:
    spec = choose(name)
    model = model or os.environ.get("FCLI_MODEL") or _env(spec.models) or spec.default_model
    effort = effort or os.environ.get("FCLI_EFFORT") or _env(spec.effort_envs) or spec.default_effort
    if effort is not None and effort not in spec.efforts:
        raise ProviderError(f"{spec.label} takes effort {', '.join(spec.efforts)}, not {effort!r}")
    api = spec.chosen_api()
    if api == "anthropic":
        return ClaudeProvider(model, effort, client)
    if api == "responses":
        return ResponsesProvider(spec, model, effort, client)
    return ChatProvider(spec, model, effort, client)


def missing_credentials() -> str:
    keys = ", ".join(SPECS[n].keys[0] for n in ORDER)
    return f"no model provider is configured: set one of {keys} (or `ant auth login` for Claude)"


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #

#: Server-side retry on another model when this one declines.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CLAUDE_MAX_TOKENS = 64_000

_CLAUDE_STOPS = {
    "end_turn": END,
    "stop_sequence": END,
    "tool_use": TOOL_USE,
    "max_tokens": MAX_TOKENS,
    "refusal": REFUSAL,
    "pause_turn": PAUSE,
}


class ClaudeProvider:
    """Claude on the Anthropic SDK: adaptive thinking, the system prompt and the
    tools cached, a decline retried server-side on another model."""

    name = "claude"

    def __init__(self, model: str, effort: str | None, client: Any = None) -> None:
        self.model = model
        self.effort = effort
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            # Credentials as the SDK finds them: ANTHROPIC_API_KEY, or an `ant auth login` profile.
            self._client = anthropic.AsyncAnthropic()
        return self._client

    def user(self, text: str) -> Any:
        return {"role": "user", "content": text}

    def results(self, results: Sequence[tuple[Call, str, bool]]) -> list[Any]:
        # Every result of the round in one message.
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": c.id,
                        "content": content,
                        **({"is_error": True} if err else {}),
                    }
                    for c, content, err in results
                ],
            }
        ]

    async def stream(
        self, system: str, tools: Sequence[dict[str, Any]], messages: list[Any], final: bool
    ) -> AsyncIterator[tuple[str, Any]]:
        extra: dict[str, Any] = {"tool_choice": {"type": "none"}} if final else {}
        if self.effort:
            extra["output_config"] = {"effort": self.effort}
        async with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=system,
            # Inputs stream as they are written; the tools validate every one.
            tools=[{**t, "eager_input_streaming": True} for t in tools],
            messages=messages,
            thinking={"type": "adaptive"},
            # Caches the stable prefix - tools, system, the conversation so far.
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **extra,
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    yield "text", event.text
            response = await stream.get_final_message()
        u = getattr(response, "usage", None)
        usage = {
            "input": getattr(u, "input_tokens", 0) or 0,
            "output": getattr(u, "output_tokens", 0) or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        calls = [
            Call(b.id, b.name, b.input if isinstance(b.input, dict) else None)
            for b in response.content
            if b.type == "tool_use"
        ]
        yield (
            "reply",
            Reply(
                calls=calls,
                stop=_CLAUDE_STOPS.get(response.stop_reason, END),
                # What Claude said goes back exactly as it came, thinking included.
                messages=[{"role": "assistant", "content": response.content}],
                usage=usage,
            ),
        )

    def error(self, exc: Exception) -> str:
        try:
            import anthropic
        except ImportError:
            return f"{type(exc).__name__}: {exc}"
        if isinstance(exc, anthropic.AuthenticationError) or "Could not resolve authentication method" in str(
            exc
        ):
            return "no usable Claude credentials: set ANTHROPIC_API_KEY, or run `ant auth login`"
        if isinstance(exc, anthropic.RateLimitError):
            return "rate limited by the Claude API: try again shortly"
        if isinstance(exc, anthropic.APIStatusError):
            return f"Claude API error {exc.status_code}: {exc.message}"
        if isinstance(exc, anthropic.APIConnectionError):
            return "could not reach the Claude API"
        return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# OpenAI Chat Completions: OpenAI, Grok, and the proxies that speak it
# --------------------------------------------------------------------------- #

_CHAT_STOPS = {
    "stop": END,
    "tool_calls": TOOL_USE,
    "function_call": TOOL_USE,
    "length": MAX_TOKENS,
    "content_filter": REFUSAL,
}


class _OpenAIBase:
    """What OpenAI's two APIs share: the client, and what its errors mean."""

    def __init__(self, spec: Spec, model: str, effort: str | None, client: Any = None) -> None:
        self.spec = spec
        self.name = spec.name
        self.model = model
        self.effort = effort
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import openai

            key = self.spec.key()
            if not key:
                raise ProviderError(f"no {self.spec.label} key: set {self.spec.keys[0]}")
            self._client = openai.AsyncOpenAI(
                api_key=key, base_url=_env(self.spec.base_urls) or self.spec.default_base_url
            )
        return self._client

    def error(self, exc: Exception) -> str:
        if isinstance(exc, ProviderError):
            return str(exc)
        try:
            import openai
        except ImportError:
            return f"{type(exc).__name__}: {exc}"
        label = self.spec.label
        if isinstance(exc, openai.AuthenticationError):
            return f"{label} rejected the key in {self.spec.keys[0]}"
        if isinstance(exc, openai.RateLimitError):
            return f"rate limited by {label}: try again shortly"
        if isinstance(exc, openai.APIStatusError):
            return f"{label} API error {exc.status_code}: {exc.message}"
        if isinstance(exc, openai.APIConnectionError):
            return f"could not reach {label}"
        return f"{type(exc).__name__}: {exc}"


class ChatProvider(_OpenAIBase):
    """Chat Completions, streamed: Grok (an OpenAI-compatible endpoint), any proxy
    that speaks it - and OpenAI itself with OPENAI_API=chat."""

    def user(self, text: str) -> Any:
        return {"role": "user", "content": text}

    def results(self, results: Sequence[tuple[Call, str, bool]]) -> list[Any]:
        # Chat Completions has no error flag on a tool result: the content says so.
        return [
            {"role": "tool", "tool_call_id": c.id, "content": f"error: {content}" if err else content}
            for c, content, err in results
        ]

    async def stream(
        self, system: str, tools: Sequence[dict[str, Any]], messages: list[Any], final: bool
    ) -> AsyncIterator[tuple[str, Any]]:
        request: dict[str, Any] = {
            "model": self.model,
            # The system prompt is not part of the conversation: it is sent with each request.
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ],
            "tool_choice": "none" if final else "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.effort:
            request["reasoning_effort"] = self.effort
        text: list[str] = []
        refusal: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish = ""
        usage: dict[str, int] = {}
        stream = await self.client.chat.completions.create(**request)
        async for chunk in stream:
            if chunk.usage is not None:
                cached = getattr(getattr(chunk.usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                usage = {
                    "input": chunk.usage.prompt_tokens or 0,
                    "output": chunk.usage.completion_tokens or 0,
                    "cache_read": cached,
                    "cache_write": 0,
                }
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            finish = choice.finish_reason or finish
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                text.append(delta.content)
                yield "text", delta.content
            if getattr(delta, "refusal", None):
                refusal.append(delta.refusal)
            for piece in delta.tool_calls or ():
                # A call arrives in pieces, keyed by its index: id and name first, arguments after.
                slot = calls.setdefault(piece.index, {"id": "", "name": "", "arguments": ""})
                if piece.id:
                    slot["id"] = piece.id
                if piece.function is not None:
                    slot["name"] += piece.function.name or ""
                    slot["arguments"] += piece.function.arguments or ""
        ordered = [calls[i] for i in sorted(calls)]
        for i, c in enumerate(ordered):
            c["id"] = c["id"] or f"call_{i}"
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
        if ordered:
            message["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in ordered
            ]
        stop = REFUSAL if refusal and not text else _CHAT_STOPS.get(finish, END)
        if ordered and stop == END:
            stop = TOOL_USE  # some endpoints say 'stop' with calls in the answer
        yield (
            "reply",
            Reply(
                calls=[Call(c["id"], c["name"], _arguments(c["arguments"])) for c in ordered],
                stop=stop,
                messages=[message],
                usage=usage,
            ),
        )


def _arguments(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text) if text.strip() else {}
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- #
# OpenAI Responses
# --------------------------------------------------------------------------- #


class ResponsesProvider(_OpenAIBase):
    """OpenAI's Responses API, streamed: what its reasoning models take tools through.

    Stateless - ``store`` off, so the fabric's data is not kept in OpenAI's
    response store - and so the reasoning comes back encrypted, to be replayed
    with the rest of the turn's output items exactly as they came.
    """

    def user(self, text: str) -> Any:
        return {"role": "user", "content": text}

    def results(self, results: Sequence[tuple[Call, str, bool]]) -> list[Any]:
        return [
            {
                "type": "function_call_output",
                "call_id": c.id,
                "output": f"error: {content}" if err else content,
            }
            for c, content, err in results
        ]

    async def stream(
        self, system: str, tools: Sequence[dict[str, Any]], messages: list[Any], final: bool
    ) -> AsyncIterator[tuple[str, Any]]:
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": messages,
            "tools": [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                    # Strict mode would make every optional filter mandatory.
                    "strict": False,
                }
                for t in tools
            ],
            "tool_choice": "none" if final else "auto",
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "stream": True,
        }
        if self.effort:
            request["reasoning"] = {"effort": self.effort}
        response = None
        stream = await self.client.responses.create(**request)
        async for event in stream:
            if event.type == "response.output_text.delta":
                yield "text", event.delta
            elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                response = event.response
        if response is None:
            raise ProviderError("the response stream ended without a response")
        if response.status == "failed":
            message = getattr(response.error, "message", None) or "the response failed"
            raise ProviderError(f"{self.spec.label}: {message}")
        items = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        calls = [
            Call(item["call_id"], item["name"], _arguments(item.get("arguments") or ""))
            for item in items
            if item.get("type") == "function_call"
        ]
        said = [
            part.get("type")
            for item in items
            if item.get("type") == "message"
            for part in item.get("content") or ()
        ]
        reason = getattr(response.incomplete_details, "reason", None) if response.incomplete_details else None
        if reason == "content_filter" or ("refusal" in said and "output_text" not in said):
            stop = REFUSAL
        elif reason == "max_output_tokens":
            stop = MAX_TOKENS
        else:
            stop = TOOL_USE if calls else END
        usage: dict[str, int] = {}
        if response.usage is not None:
            u = response.usage
            cached = getattr(u.input_tokens_details, "cached_tokens", 0) or 0
            usage = {
                "input": u.input_tokens or 0,
                "output": u.output_tokens or 0,
                "cache_read": cached,
                "cache_write": 0,
            }
        yield "reply", Reply(calls=calls, stop=stop, messages=items, usage=usage)
