// The HTTP API and its events, as the UI uses them; and small formatting helpers.

const query = new URLSearchParams(location.search);
let token = query.get("token");
try {
  if (token) sessionStorage.setItem("fcli-token", token);
  else token = sessionStorage.getItem("fcli-token");
} catch {
  // Storage may be blocked: the token then lasts as long as the page.
}
if (query.has("token")) {
  // Keep the token out of the address bar, bookmarks and screenshots.
  query.delete("token");
  const rest = query.toString();
  history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function url(path, params = {}) {
  const u = new URL(path, location.origin);
  for (const [key, value] of Object.entries(params)) {
    for (const v of Array.isArray(value) ? value : [value]) {
      if (v !== undefined && v !== null && v !== "") u.searchParams.append(key, v);
    }
  }
  return u;
}

export async function get(path, params = {}) {
  const response = await fetch(url(path, params), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    // An error without a JSON body: the status line says enough.
  }
  if (!response.ok) {
    const message = response.status === 401 ? "unauthorized: open the UI with ?token=..." : body?.error;
    throw new ApiError(response.status, message || response.statusText);
  }
  return body;
}

/** Follow the server's events; the browser reconnects, and the server replays what was missed. */
export function follow(reports, onEvent, onConnection) {
  const source = new EventSource(url("/api/events", { report: reports, token }));
  source.onopen = () => onConnection("live");
  source.onerror = () => onConnection(source.readyState === EventSource.CLOSED ? "closed" : "reconnecting");
  for (const type of ["status", "node", "change"]) {
    source.addEventListener(type, (e) => onEvent(type, JSON.parse(e.data)));
  }
  return () => source.close();
}

export const natural = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true });

export function ago(ts, now = Date.now() / 1000) {
  if (!ts) return "";
  const s = Math.max(0, now - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function clock(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString([], { hour12: false })
    : d.toLocaleString([], { hour12: false, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export const SEVERITIES = ["error", "warning", "info"];
export const bySeverity = (a, b) => SEVERITIES.indexOf(a.severity) - SEVERITIES.indexOf(b.severity);
