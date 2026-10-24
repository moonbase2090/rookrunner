// SPDX-License-Identifier: MIT
// Pure data rules for the web dashboard option. No DOM access here, so the
// same functions run in the browser and under `node --test`.

const SUCCESS = "succeeded";

// Sorted-key JSON, so {a, b} and {b, a} name the same request.
export function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort()
      .map((k) => JSON.stringify(k) + ":" + canonical(value[k])).join(",") + "}";
  }
  return JSON.stringify(value);
}

// Answer one worker read from the bundle. A request the bundle does not
// contain is reported as missing, never as an empty result.
export function call(bundle, method, params = {}) {
  const key = canonical(params);
  const responses = Array.isArray(bundle && bundle.responses) ? bundle.responses : [];
  for (const entry of responses) {
    if (entry && entry.method === method && canonical(entry.params || {}) === key) {
      const reply = replyOf(entry);
      if (reply.error !== undefined) return { error: reply.error };
      if (reply.result !== undefined) return { result: reply.result };
    }
  }
  return { missing: true };
}

// Each entry holds the literal JSON-RPC reply the worker sent.
function replyOf(entry) {
  return entry.reply && typeof entry.reply === "object" ? entry.reply : {};
}

export function responsesFor(bundle, method) {
  const responses = Array.isArray(bundle && bundle.responses) ? bundle.responses : [];
  return responses
    .filter((e) => e && e.method === method && replyOf(e).result !== undefined)
    .map((e) => ({ params: e.params || {}, result: replyOf(e).result }));
}

// A worker error names its kind in data.kind; the numeric code is generic.
export function errorText(error) {
  if (!error || typeof error !== "object") return "an error";
  const kind = error.data && typeof error.data.kind === "string" ? error.data.kind : String(error.code ?? "error");
  return typeof error.message === "string" && error.message ? `${kind}: ${error.message}` : kind;
}

// The verdict a person reads. Only a terminal succeeded state with exit
// code 0 is a success; every other reported state keeps its own name.
export function verdict(record) {
  const state = record && typeof record.state === "string" && record.state !== ""
    ? record.state : null;
  const code = record && Number.isInteger(record.exit_code) ? record.exit_code : null;
  if (state === SUCCESS && code === 0) {
    return { key: "succeeded", success: true, text: "Succeeded, exit 0" };
  }
  if (state === SUCCESS) {
    const shown = code === null ? "no exit code" : `exit ${code}`;
    return { key: "malformed", success: false, text: `Not a success: succeeded with ${shown}` };
  }
  if (state === "failed") {
    return { key: "failed", success: false,
      text: code === null ? "Failed, no exit code" : `Failed, exit ${code}` };
  }
  if (state === "lost") {
    const unresolved = record.cleanup === "unresolved";
    return { key: "lost", success: false,
      text: unresolved ? "Lost, cleanup unresolved" : "Lost" };
  }
  if (state === "cancelled") return { key: "cancelled", success: false, text: "Cancelled" };
  if (state === "running") return { key: "running", success: false, text: "Running" };
  if (state === "queued") return { key: "queued", success: false, text: "Queued" };
  if (state === null) return { key: "unknown", success: false, text: "Unknown state" };
  return { key: "unknown", success: false, text: `Reported ${state}` };
}

// What the page may claim about freshness. Anything but an explicit
// "current" mode is treated as not current.
export function observation(bundle, override = null) {
  const mode = override || (bundle && bundle.mode);
  const at = bundle && typeof bundle.observed_at === "string" ? bundle.observed_at : null;
  const last = bundle && typeof bundle.last_observed_at === "string"
    ? bundle.last_observed_at : null;
  if (mode === "current" && at) return { mode: "current", at, current: true };
  if (mode === "stale") return { mode: "stale", at, last: last || at, current: false };
  if (mode === "unavailable") return { mode: "unavailable", at, last, current: false };
  return { mode: "unknown", at, last, current: false };
}

// Merge run records from every run.list page with status.view labels.
// Records keep the bundle's values; status.view only adds display labels.
export function collectRuns(bundle) {
  const byId = new Map();
  for (const entry of responsesFor(bundle, "run.list")) {
    const runs = Array.isArray(entry.result.runs) ? entry.result.runs : [];
    for (const record of runs) {
      if (record && typeof record.run_id === "string") byId.set(record.run_id, { record });
    }
  }
  const view = call(bundle, "status.view").result;
  const labelled = view && Array.isArray(view.runs) ? view.runs : [];
  const queued = view && Array.isArray(view.queue) ? view.queue : [];
  for (const row of [...labelled, ...queued]) {
    if (!row || typeof row.run_id !== "string") continue;
    const item = byId.get(row.run_id) || { record: null };
    item.view = { ...(item.view || {}), ...row };
    byId.set(row.run_id, item);
  }
  for (const [id, item] of byId) {
    const detail = call(bundle, "run.get", { run_id: id });
    if (detail.result) item.record = detail.result;
    item.detailError = detail.error || null;
    item.run_id = id;
    if (!item.record && item.view) {
      item.record = { run_id: id, state: item.view.state, exit_code: item.view.exit_code };
    }
  }
  return [...byId.values()].sort((a, b) => stamp(b.record, "accepted_at") - stamp(a.record, "accepted_at"));
}

function stamp(record, field) {
  const value = record && record[field];
  const ms = typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isNaN(ms) ? -Infinity : ms;
}

export function time(value) {
  const ms = typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isNaN(ms) ? null : ms;
}

// Follow one run's log cursor chain as far as the bundle holds pages.
// `ended` is true only when a page said end_of_stream; an empty page
// without that marker is "no more stored output yet".
export function logPages(bundle, runId) {
  const pages = [];
  let params = { run_id: runId };
  const seen = new Set();
  for (;;) {
    const reply = call(bundle, "run.logs", params);
    if (reply.missing || reply.error) {
      return { pages, ended: false, error: reply.error || null, missing: pages.length === 0 && !!reply.missing };
    }
    pages.push(reply.result);
    if (reply.result.end_of_stream === true) return { pages, ended: true, error: null, missing: false };
    const next = reply.result.next_cursor;
    if (typeof next !== "string" || seen.has(next)) return { pages, ended: false, error: null, missing: false };
    seen.add(next);
    params = { run_id: runId, cursor: next };
  }
}

export function decodeBase64(text) {
  if (typeof text !== "string") return "";
  const bin = typeof atob === "function" ? atob(text) : Buffer.from(text, "base64").toString("binary");
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}

// Split terminal text into styled runs. SGR colour codes become class
// names; every other control byte becomes a visible Control Picture so
// nothing in a log can move the cursor, clear the view or inject markup.
const SGR_COLOURS = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];

export function terminalRuns(text) {
  const runs = [];
  let style = { fg: null, bold: false };
  let buffer = "";
  const flush = () => {
    if (buffer) runs.push({ text: buffer, fg: style.fg, bold: style.bold });
    buffer = "";
  };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    const code = ch.charCodeAt(0);
    if (ch === "\x1b" && text[i + 1] === "[") {
      let j = i + 2;
      while (j < text.length && /[0-9;?]/.test(text[j])) j++;
      const final = text[j];
      if (final === "m") {
        flush();
        style = applySgr(style, text.slice(i + 2, j));
        i = j;
        continue;
      }
      buffer += "␛";
      continue;
    }
    if (ch === "\n" || ch === "\t") { buffer += ch; continue; }
    if (ch === "\r") { buffer += text[i + 1] === "\n" ? "" : "␍"; continue; }
    if (code < 0x20) { buffer += String.fromCharCode(0x2400 + code); continue; }
    if (code === 0x7f) { buffer += "␡"; continue; }
    buffer += ch;
  }
  flush();
  return runs;
}

function applySgr(style, params) {
  const next = { ...style };
  const parts = params === "" ? ["0"] : params.split(";");
  for (const part of parts) {
    const n = Number(part);
    if (n === 0) { next.fg = null; next.bold = false; }
    else if (n === 1) next.bold = true;
    else if (n === 22) next.bold = false;
    else if (n === 39) next.fg = null;
    else if (n >= 30 && n <= 37) next.fg = SGR_COLOURS[n - 30];
    else if (n >= 90 && n <= 97) next.fg = SGR_COLOURS[n - 90];
  }
  return next;
}

export function countMatches(text, needle) {
  if (!needle) return 0;
  let count = 0;
  for (let at = text.indexOf(needle); at !== -1; at = text.indexOf(needle, at + needle.length)) count++;
  return count;
}
