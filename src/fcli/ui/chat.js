// The agent, beside whatever view is open: it is told what the user is looking at.

import { html, useEffect, useRef, useState } from "./vendor/preact-htm.module.js";
import { ask } from "./api.js";
import { Markdown } from "./markdown.js";

const SUGGESTIONS = [
  "What is wrong with the fabric, and what changed just before?",
  "Is BGP healthy everywhere?",
  "Which hosts are multihomed, and on which segments?",
];

function Tool({ step }) {
  const state = step.done ? (step.error ? "failed" : "done") : "running";
  const args = Object.entries(step.input || {})
    .filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" ");
  return html`<div class=${`tool ${state}`} title=${args}>
    <span class="tool-state"></span><b>${step.name}</b> <span class="mono muted">${args}</span>
    ${step.error && html`<span class="error-text"> failed</span>`}
  </div>`;
}

function Turn({ turn }) {
  return html`<div class="turn">
    <div class="question">${turn.question}</div>
    <div class="answer">
      ${turn.parts.map((p) =>
        p.type === "text" ? html`<${Markdown} text=${p.text} />` : html`<${Tool} step=${p} />`,
      )}
      ${turn.error && html`<p class="error-text">${turn.error}</p>`}
      ${turn.running && !turn.parts.length && html`<p class="muted thinking">Thinking…</p>`}
      ${turn.usage &&
      html`<div class="usage muted">${turn.usage.input + turn.usage.cache_read + turn.usage.cache_write} in · ${turn.usage.output} out tokens · ${turn.rounds} tool round${turn.rounds === 1 ? "" : "s"}</div>`}
    </div>
  </div>`;
}

export function ChatPanel({ open, onClose, context }) {
  const [turns, setTurns] = useState([]);
  const [question, setQuestion] = useState("");
  const [conversation, setConversation] = useState(null);
  const [model, setModel] = useState("");
  const [running, setRunning] = useState(null);
  const scroller = useRef(null);
  const input = useRef(null);

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  const update = (fn) =>
    setTurns((all) => {
      const last = { ...all[all.length - 1] };
      fn(last);
      return [...all.slice(0, -1), last];
    });

  const send = async (text) => {
    const q = (text ?? question).trim();
    if (!q || running) return;
    setQuestion("");
    setTurns((all) => [...all, { question: q, parts: [], running: true }]);
    const controller = new AbortController();
    setRunning(controller);
    try {
      await ask({ question: q, conversation, context }, (event) => {
        if (event.type === "start") {
          setConversation(event.conversation);
          setModel(`${event.provider} · ${event.model}${event.effort ? ` · ${event.effort}` : ""}`);
        } else if (event.type === "text") {
          update((t) => {
            const parts = [...t.parts];
            const last = parts[parts.length - 1];
            if (last?.type === "text") parts[parts.length - 1] = { ...last, text: last.text + event.text };
            else parts.push({ type: "text", text: event.text });
            t.parts = parts;
          });
        } else if (event.type === "tool") {
          update((t) => {
            t.parts = [...t.parts, { type: "tool", id: event.id, name: event.name, input: event.input }];
          });
        } else if (event.type === "tool_result") {
          update((t) => {
            t.parts = t.parts.map((p) => (p.id === event.id ? { ...p, done: true, error: event.error } : p));
          });
        } else if (event.type === "done") {
          update((t) => {
            t.usage = event.usage;
            t.rounds = event.rounds;
          });
        } else if (event.type === "error") {
          update((t) => {
            t.error = event.message;
          });
        }
      }, controller.signal);
    } catch (e) {
      update((t) => {
        t.error = e.name === "AbortError" ? "Stopped." : e.message;
      });
    } finally {
      update((t) => {
        t.running = false;
      });
      setRunning(null);
    }
  };

  const reset = () => {
    running?.abort();
    setTurns([]);
    setConversation(null);
  };

  return html`<aside class=${`chat${open ? " open" : ""}`} aria-hidden=${!open}>
    <header>
      <b>Ask</b>
      <span class="muted model">${model}</span>
      <span class="spacer"></span>
      ${turns.length > 0 && html`<button onClick=${reset} title="Start a new conversation">New</button>`}
      <button class="icon" onClick=${onClose} title="Close">✕</button>
    </header>
    <div class="chat-scroll" ref=${scroller}>
      ${turns.length === 0 &&
      html`<div class="chat-empty">
        <p>Ask about the fabric. The agent uses the same read-only tools as MCP clients, and is told what you are looking at.</p>
        ${SUGGESTIONS.map((s) => html`<button class="suggestion" onClick=${() => send(s)}>${s}</button>`)}
        <p class="muted small">Each question is sent to the model provider the server is configured with, and is billed to its key.</p>
      </div>`}
      ${turns.map((t) => html`<${Turn} turn=${t} />`)}
    </div>
    <form class="chat-input" onSubmit=${(e) => {
      e.preventDefault();
      send();
    }}>
      <textarea ref=${input} value=${question} rows="2" placeholder="Ask a question… (Enter to send, Shift+Enter for a new line)"
                onInput=${(e) => setQuestion(e.target.value)}
                onKeyDown=${(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}></textarea>
      ${running
        ? html`<button type="button" onClick=${() => running.abort()}>Stop</button>`
        : html`<button class="primary" type="submit" disabled=${!question.trim()}>Send</button>`}
    </form>
    ${context?.view && html`<div class="context muted">Looking at: ${context.view}${context.at ? ` at ${context.at}` : ""}</div>`}
  </aside>`;
}
