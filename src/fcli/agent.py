"""The in-app agent: a model, with exactly the tools an MCP client gets.

A turn streams the model's answer as it is written, runs the tools it asks
for - concurrently when it asks for several - over the same
:class:`~fcli.service.Service` the rest of fcli uses, and loops until it has
an answer or its rounds run out. Every tool is read-only, so nothing it does
needs confirming.

Which model is :mod:`fcli.llm`'s business: Claude, OpenAI, or anything that
speaks OpenAI's Chat Completions. The loop is the same for all of them.

Conversations live server-side and are only ever appended to, in the
provider's own format: what the model said goes back to it exactly as it
came, which is what multi-turn reasoning and prompt caching depend on. The
system prompt and the tool list never change; what changes from turn to turn -
the fabric's name, what the user is looking at - rides in the user's message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .llm import MAX_TOKENS, PAUSE, REFUSAL, Call, Provider, ProviderError, Reply, create
from .service import Service
from .tools import TOOLS, ToolError, call

logger = logging.getLogger(__name__)

#: Tool rounds one turn may take before it is made to answer with what it has.
MAX_ROUNDS = 12
#: Retries of a turn whose tool input came back as JSON that could not be parsed.
JSON_RETRIES = 2

SYSTEM_PROMPT = f"""\
You are fcli's assistant: you help network engineers understand and troubleshoot an SR Linux \
fabric. You have read-only tools over the fabric's live state and the history fcli keeps; you \
cannot change anything, and must never suggest that you did.

How to work:
- For "what is wrong", a triage, or any question about health, start with get_incidents. It \
groups every check's findings by root cause: an incident's root is the cause to explain, its \
related findings are consequences of it, not separate problems. A 'pattern' incident is one \
cause repeated in many places - say so rather than listing each place.
- Then get_history since a little before the incident started, for what changed and when. A \
change just before a fault is usually its trigger. A change with a 'cause' was likely made by \
that configuration commit: name the commit and its user.
- get_drift compares the fabric with a named baseline (list_baselines) or a time: what is \
different from when it was known good.
- For anything else, list_reports, then get_report - narrowed to the nodes, labels (inv) and \
rows (where) the question is about, rather than reading the whole fabric. Sample one or two \
representative nodes per role, and say which you looked at.
- For one address, a host or a service, the fabric-wide tools are quicker than reports: \
locate_address (where an IP or MAC is), trace_path (how traffic would be forwarded from a node or \
host to an address), service_detail (one mac-vrf/ip-vrf on every node), list_endpoints (the hosts) \
and get_topology (tiers, cables, clients).
- Every tool takes at='30m' (or a time) to answer how the fabric was then.
- Reports are operational state, not configuration. When a question needs configuration you \
cannot see, say what to check on the node instead.
- If a tool returns an error, do not repeat the same call: change approach, or answer with what \
you have.
- You get at most {MAX_ROUNDS} rounds of tool calls per question; leave room to answer. A \
partial answer that says what you checked beats no answer.

How to answer: lead with the conclusion, then the evidence - node names, the fields and values \
you used, and times as the tools give them. Be concise; plain markdown, short lists, a small \
table where it helps. Never invent a value you did not read."""


def tool_definitions() -> list[dict[str, Any]]:
    """The tools, in a fixed order: a cached prefix depends on it."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in TOOLS.values()
    ]


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    #: Append-only: what was said, exactly as it was said.
    messages: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    used: float = field(default_factory=time.time)
    #: One turn at a time: two at once would interleave their messages.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The provider its messages are written for: set by the first turn.
    provider: str = ""


class Conversations:
    """The conversations a server keeps, the least recently used dropped beyond *limit*."""

    def __init__(self, limit: int = 200) -> None:
        self.limit = limit
        self._items: OrderedDict[str, Conversation] = OrderedDict()

    def get(self, id_: str | None) -> Conversation:
        if id_ and id_ in self._items:
            self._items.move_to_end(id_)
            conversation = self._items[id_]
        else:
            conversation = Conversation()
            self._items[conversation.id] = conversation
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
        conversation.used = time.time()
        return conversation


def _context_line(fabric: str, context: Mapping[str, Any] | None) -> str:
    parts = [f"fabric {fabric}"] if fabric else []
    for key, value in (context or {}).items():
        if value:
            parts.append(f"{key} {value}")
    return f"[{'; '.join(parts)}]\n\n" if parts else ""


class Agent:
    def __init__(
        self,
        service: Service,
        *,
        fabric: str = "",
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        client: Any = None,
    ) -> None:
        self.service = service
        self.fabric = fabric
        self.llm: Provider = create(provider, model, effort, client)
        self._tools = tool_definitions()

    @property
    def provider(self) -> str:
        return self.llm.name

    @property
    def model(self) -> str:
        return self.llm.model

    @property
    def effort(self) -> str | None:
        return self.llm.effort

    async def turn(
        self, conversation: Conversation, question: str, context: Mapping[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Answer *question* in *conversation*, as events: ``text``, ``tool``,
        ``tool_result``, and last ``done`` or ``error``."""
        async with conversation.lock:
            if conversation.provider and conversation.provider != self.provider:
                yield {
                    "type": "error",
                    "message": f"this conversation is with {conversation.provider}, "
                    f"not {self.provider}: start a new one",
                }
                return
            conversation.provider = self.provider
            async for event in self._turn(conversation, question, context):
                yield event

    async def _turn(
        self, conversation: Conversation, question: str, context: Mapping[str, Any] | None
    ) -> AsyncIterator[dict[str, Any]]:
        messages = conversation.messages
        messages.append(self.llm.user(_context_line(self.fabric, context) + question))
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        rounds = 0
        retries = 0
        while True:
            try:
                reply: Reply | None = None
                async for kind, payload in self.llm.stream(
                    SYSTEM_PROMPT, self._tools, messages, rounds >= MAX_ROUNDS
                ):
                    if kind == "text":
                        yield {"type": "text", "text": payload}
                    else:
                        reply = payload
            except ValueError as exc:
                if isinstance(exc, ProviderError):
                    yield {"type": "error", "message": str(exc)}
                    return
                # Tool input the SDK could not parse at all: no call to answer,
                # so the turn is asked again.
                retries += 1
                if retries > JSON_RETRIES:
                    yield {"type": "error", "message": "the model's tool call could not be read"}
                    return
                continue
            except Exception as exc:  # the API's own errors, said as they are
                yield {"type": "error", "message": self.llm.error(exc)}
                return
            retries = 0
            assert reply is not None
            for k, v in reply.usage.items():
                usage[k] = usage.get(k, 0) + v
            messages.extend(reply.messages)
            if reply.stop == PAUSE:
                continue
            if reply.stop == REFUSAL:
                yield {"type": "error", "message": "the model declined to answer this"}
                if reply.calls:
                    messages.extend(self.llm.results(_unrun(reply.calls, "the turn was declined")))
                return
            if not reply.calls:
                yield {
                    "type": "done",
                    "stop": reply.stop,
                    "rounds": rounds,
                    "usage": usage,
                    "conversation": conversation.id,
                }
                return
            if reply.stop == MAX_TOKENS:
                # A tool input cut off mid-way can parse as a valid partial one: never run it.
                messages.extend(self.llm.results(_unrun(reply.calls, "the call was cut off")))
                yield {"type": "error", "message": "the answer ran past its length limit"}
                return
            rounds += 1
            for c in reply.calls:
                yield {"type": "tool", "id": c.id, "name": c.name, "input": c.input}
            results = await asyncio.gather(*(self._run(c) for c in reply.calls))
            for c, (content, error) in zip(reply.calls, results, strict=True):
                yield {
                    "type": "tool_result",
                    "id": c.id,
                    "name": c.name,
                    "error": error,
                    "size": len(content),
                }
            # Every result of the round, together.
            messages.extend(
                self.llm.results(
                    [(c, content, error) for c, (content, error) in zip(reply.calls, results, strict=True)]
                )
            )

    async def _run(self, c: Call) -> tuple[str, bool]:
        if c.input is None:
            return "the arguments were not valid JSON: call again with a JSON object", True
        try:
            return await call(self.service, c.name, c.input), False
        except ToolError as exc:
            return str(exc), True
        except Exception as exc:
            logger.exception("tool %s failed", c.name)
            return f"the tool failed: {type(exc).__name__}: {exc}", True


def _unrun(calls: list[Call], why: str) -> list[tuple[Call, str, bool]]:
    """Results for calls that were not run, so the conversation stays well-formed."""
    return [(c, f"not run: {why}", True) for c in calls]


def as_json(event: Mapping[str, Any]) -> str:
    return json.dumps(event, separators=(",", ":"), default=str)
