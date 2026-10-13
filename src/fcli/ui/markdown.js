// Just enough Markdown for the agent's answers, built as elements: model text never becomes HTML.

import { html } from "./vendor/preact-htm.module.js";

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))/;

function inline(text) {
  const out = [];
  let rest = text;
  while (rest) {
    const m = INLINE.exec(rest);
    if (!m) {
      out.push(rest);
      break;
    }
    if (m.index) out.push(rest.slice(0, m.index));
    const [token] = m;
    if (m[1]) out.push(html`<code>${token.slice(1, -1)}</code>`);
    else if (m[2]) out.push(html`<strong>${inline(token.slice(2, -2))}</strong>`);
    else if (m[3]) out.push(html`<em>${inline(token.slice(1, -1))}</em>`);
    else {
      const label = token.slice(1, token.indexOf("]("));
      out.push(html`<a href=${m[5]} target="_blank" rel="noopener noreferrer">${label}</a>`);
    }
    rest = rest.slice(m.index + token.length);
  }
  return out;
}

const cells = (line) =>
  line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());

export function Markdown({ text }) {
  const lines = text.replace(/\r/g, "").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
    } else if (line.startsWith("```")) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      blocks.push(html`<pre><code>${code.join("\n")}</code></pre>`);
    } else if (/^#{1,6}\s/.test(line)) {
      const level = Math.min(4, line.match(/^#+/)[0].length + 2);
      const content = inline(line.replace(/^#+\s*/, ""));
      blocks.push(level === 3 ? html`<h3>${content}</h3>` : html`<h4>${content}</h4>`);
      i++;
    } else if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
      blocks.push(html`<div class="md-table"><table class="grid">
        <thead><tr>${head.map((h) => html`<th>${inline(h)}</th>`)}</tr></thead>
        <tbody>${rows.map((r) => html`<tr>${r.map((c) => html`<td>${inline(c)}</td>`)}</tr>`)}</tbody>
      </table></div>`);
    } else if (/^\s*([-*]|\d+\.)\s/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s/.test(lines[i])) {
        let item = lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, "");
        // A wrapped item continues on indented lines.
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s/.test(lines[i])) {
          item += ` ${lines[i++].trim()}`;
        }
        items.push(html`<li>${inline(item)}</li>`);
      }
      blocks.push(ordered ? html`<ol>${items}</ol>` : html`<ul>${items}</ul>`);
    } else {
      const para = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^(```|#{1,6}\s|\s*([-*]|\d+\.)\s|\s*\|)/.test(lines[i])
      ) {
        para.push(lines[i++].trim());
      }
      if (!para.length) para.push(lines[i++]);
      blocks.push(html`<p>${inline(para.join(" "))}</p>`);
    }
  }
  return html`<div class="md">${blocks}</div>`;
}
