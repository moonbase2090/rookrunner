// SPDX-License-Identifier: MIT
// Renders the fixture bundle. Every value reaches the page through
// textContent or a class name; nothing from the bundle is parsed as markup.

import {
  call, collectRuns, countMatches, errorText, logPages, pagesText, observation,
  terminalRuns, time, verdict,
} from "./model.js";

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clock(ms) {
  return ms === null ? "unknown" : new Date(ms).toISOString().slice(11, 19) + "Z";
}

function span(seconds) {
  if (seconds === null || seconds < 0) return "";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}

// The tail of a run ID, so IDs that share a prefix stay distinct.
function shortId(id) {
  return typeof id === "string" ? id.slice(-8) : "unknown";
}

function source(item) {
  const view = item.view || {};
  const repo = typeof view.repository === "string" ? view.repository : null;
  const subject = view.subject || {};
  let label = "no source label";
  if (subject.kind === "pull_request") label = `PR ${subject.number}`;
  else if (subject.kind === "commit" && typeof subject.sha === "string") label = subject.sha.slice(0, 10);
  return { repo, label };
}

function workflow(item) {
  const input = (item.record && item.record.input) || {};
  const view = item.view || {};
  const file = input.workflow || view.workflow || null;
  const job = input.job_id || view.job_id || null;
  if (!file && !job) return input.kind === "development_fixture" ? "development fixture" : "unknown";
  return [file, job].filter(Boolean).join(" / ");
}

let bundle = null;
let runs = [];
let seen = null;
let selected = null;

async function load() {
  let reply;
  try {
    reply = await fetch("bundle.json", { cache: "no-store" });
  } catch {
    return fail("The preview server did not answer. Start serve.py and reload.");
  }
  if (!reply.ok) return fail(`The preview server answered ${reply.status} for the bundle.`);
  try {
    bundle = await reply.json();
  } catch {
    return fail("The bundle is not valid JSON. Check the file passed to --bundle.");
  }
  let override = null;
  try {
    const answer = await fetch("observation.json", { cache: "no-store" });
    if (answer.ok) override = (await answer.json()).mode;
  } catch {
    override = null;
  }
  seen = observation(bundle, override);
  // An unreachable worker answers nothing; keep only the bundle's times.
  if (seen.mode === "unavailable") bundle = { ...bundle, responses: [] };
  runs = collectRuns(bundle);
  renderMasthead();
  renderLedger();
  renderQueue();
  const fromHash = decodeURIComponent(location.hash.replace(/^#run=/, ""));
  if (fromHash && runs.some((r) => r.run_id === fromHash)) select(fromHash, false);
}

function fail(message) {
  const notice = $("notice");
  notice.hidden = false;
  notice.className = "notice notice-down";
  notice.textContent = message;
}

function renderMasthead() {
  const view = call(bundle, "status.view");
  const worker = $("worker");
  worker.replaceChildren();
  const info = view.result && view.result.worker;
  if (info) {
    const ready = el("span", info.ready ? "ready" : "not-ready", info.ready ? "Ready" : "Not ready");
    worker.append(el("strong", "worker-name", "Worker"), ready);
    if (info.version) worker.append(el("span", "fact", `version ${info.version}`));
    if (info.runner_image) worker.append(el("span", "fact mono", info.runner_image.slice(0, 19)));
    if (!info.ready && info.readiness_error) worker.append(el("span", "fact", info.readiness_error));
    const poll = view.result.poll || {};
    worker.append(el("span", "fact", poll.completed_at
      ? `last poll ${clock(time(poll.completed_at))}` : "no completed poll recorded"));
  } else {
    worker.append(el("strong", "worker-name", "Worker"), el("span", "not-ready", "Status unknown"));
  }

  const observed = $("observed");
  observed.replaceChildren();
  const notice = $("notice");
  notice.hidden = true;
  if (seen.mode === "current") {
    observed.append(el("span", "obs-label", "Observed"), el("time", "obs-time", clock(time(seen.at))));
    return;
  }
  observed.append(el("span", "obs-label", "Last observed"),
    el("time", "obs-time", clock(time(seen.last))));
  notice.hidden = false;
  if (seen.mode === "stale") {
    notice.className = "notice notice-stale";
    notice.textContent = `Stale. This data was read at ${clock(time(seen.last))} and the worker has not answered since. ` +
      "Runs shown as queued or running may have finished.";
  } else if (seen.mode === "unavailable") {
    notice.className = "notice notice-down";
    notice.textContent = `The worker did not answer at ${clock(time(seen.at))}. ` +
      "Check that the worker is running on this machine, then reload this page.";
  } else {
    notice.className = "notice notice-down";
    notice.textContent = "This bundle does not say when it was observed, so nothing below is treated as current.";
  }
}

function bounds() {
  const dataAt = time(seen.mode === "current" ? seen.at : seen.last);
  const pageAt = time(seen.at) ?? dataAt;
  let start = Infinity;
  for (const item of runs) {
    for (const field of ["accepted_at", "started_at"]) {
      const t = time(item.record && item.record[field]);
      if (t !== null && t < start) start = t;
    }
  }
  const end = Math.max(pageAt ?? -Infinity, dataAt ?? -Infinity);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return { start, end, dataAt, pageAt };
}

function renderLedger() {
  const list = $("runs");
  list.replaceChildren();
  const range = bounds();
  $("scope").textContent = runs.length
    ? `${runs.length} run${runs.length === 1 ? "" : "s"} in this observation, newest accepted first.`
    : "";
  renderAxis(range);
  if (!runs.length) {
    list.append(el("li", "empty", seen.mode === "unavailable"
      ? "No runs are shown because the worker did not answer."
      : "The worker has no runs in this observation."));
    return;
  }
  for (const item of runs) list.append(row(item, range));
}

function renderAxis(range) {
  const axis = $("axis");
  axis.replaceChildren();
  if (!range) return;
  const track = el("div", "axis-track");
  const width = range.end - range.start;
  const step = [60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 86400]
    .map((s) => s * 1000).find((s) => width / s <= 6) || 86400000;
  for (let t = Math.ceil(range.start / step) * step; t <= range.end; t += step) {
    const at = (t - range.start) / width;
    const tick = el("span", at < 0.05 ? "tick tick-start" : at > 0.95 ? "tick tick-end" : "tick", clock(t).slice(0, 5));
    tick.style.left = `${at * 100}%`;
    track.append(tick);
  }
  if (range.dataAt !== null && range.pageAt !== null && range.pageAt > range.dataAt) {
    const gap = el("span", "axis-gap", "no observation");
    gap.style.left = `${((range.dataAt - range.start) / width) * 100}%`;
    gap.style.width = `${((range.pageAt - range.dataAt) / width) * 100}%`;
    track.append(gap);
  }
  axis.append(el("span", "axis-label", "UTC"), track);
}

function row(item, range) {
  const record = item.record || {};
  const v = verdict(record);
  const li = el("li", `run v-${v.key}`);
  li.setAttribute("role", "option");
  li.setAttribute("aria-selected", "false");
  li.dataset.run = item.run_id;
  li.id = `run-${item.run_id}`;

  const mark = el("span", "mark");
  mark.setAttribute("aria-hidden", "true");
  const state = el("span", "state");
  state.append(mark, el("span", "state-text", v.text));

  const src = source(item);
  const what = el("span", "what");
  what.append(el("span", "run-id mono", shortId(item.run_id)),
    el("span", "src", src.repo ? `${src.repo}  ${src.label}` : src.label),
    el("span", "wf", workflow(item)));

  li.append(state, what, bar(record, v, range), el("span", "dur", elapsed(record)));
  li.addEventListener("click", () => select(item.run_id, true));
  return li;
}

function dataClock() {
  return time(seen.mode === "current" ? seen.at : seen.last);
}

function elapsed(record) {
  const started = time(record.started_at);
  const finished = time(record.finished_at);
  if (started === null) return record.state === "queued" ? "waiting" : "not started";
  if (finished !== null) return span((finished - started) / 1000);
  const now = dataClock();
  return now === null ? "" : `${span((now - started) / 1000)} so far`;
}

function bar(record, v, range) {
  const cell = el("span", "lane");
  if (!range) return cell;
  const width = range.end - range.start;
  const pos = (t) => `${((t - range.start) / width) * 100}%`;
  const accepted = time(record.accepted_at);
  const started = time(record.started_at);
  const finished = time(record.finished_at);
  const until = finished ?? range.dataAt;
  if (accepted !== null) {
    const wait = el("span", "wait");
    const waitEnd = started ?? range.dataAt ?? accepted;
    wait.style.left = pos(accepted);
    wait.style.width = `${Math.max(0, ((waitEnd - accepted) / width) * 100)}%`;
    cell.append(wait);
  }
  if (started !== null && until !== null) {
    const run = el("span", finished === null ? "exec open" : "exec");
    run.style.left = pos(started);
    run.style.width = `${Math.max(0.6, ((until - started) / width) * 100)}%`;
    cell.append(run);
  }
  if (range.dataAt !== null && range.pageAt > range.dataAt) {
    const gap = el("span", "lane-gap");
    gap.style.left = pos(range.dataAt);
    gap.style.width = `${((range.pageAt - range.dataAt) / width) * 100}%`;
    cell.append(gap);
  }
  return cell;
}

function renderQueue() {
  const box = $("queue");
  box.replaceChildren();
  const view = call(bundle, "status.view").result;
  if (!view) return;
  const queue = Array.isArray(view.queue) ? view.queue : [];
  const heading = el("h2", null, queue.length ? `Queue, ${queue.length} waiting` : "Queue is empty");
  box.append(heading);
  if (!queue.length) return;
  const list = el("ul", "queue-list");
  for (const q of queue) {
    const li = el("li");
    li.append(el("span", "mono", shortId(q.run_id)), el("span", null, [q.workflow, q.job_id].filter(Boolean).join(" / ")));
    const groups = Array.isArray(q.groups) ? q.groups : [];
    const names = groups.map((g) => (g && typeof g.group === "string" ? g.group : null)).filter(Boolean);
    li.append(el("span", "why", q.waiting
      ? `waiting on ${names.length ? names.join(", ") : "a concurrency group"}`
      : "waiting for the worker"));
    list.append(li);
  }
  box.append(list);
}

function select(runId, push) {
  selected = runId;
  for (const li of $("runs").children) {
    li.setAttribute("aria-selected", String(li.dataset.run === runId));
  }
  $("runs").setAttribute("aria-activedescendant", `run-${runId}`);
  if (push) history.replaceState(null, "", `#run=${encodeURIComponent(runId)}`);
  const item = runs.find((r) => r.run_id === runId);
  renderDetail(item);
}

function close() {
  selected = null;
  for (const li of $("runs").children) li.setAttribute("aria-selected", "false");
  $("detail").hidden = true;
  history.replaceState(null, "", location.pathname);
}

function fact(list, name, value, mono) {
  list.append(el("dt", null, name));
  list.append(el("dd", mono ? "mono" : null, value === null || value === undefined || value === "" ? "unknown" : value));
}

function renderDetail(item) {
  const detail = $("detail");
  detail.hidden = false;
  const record = item.record || {};
  const v = verdict(record);
  $("detail-title").textContent = `Run ${shortId(item.run_id)}`;
  const line = $("detail-verdict");
  line.className = `verdict-line v-${v.key}`;
  line.textContent = v.text;

  const facts = $("facts");
  facts.replaceChildren();
  const input = record.input || {};
  const src = source(item);
  if (item.detailError) {
    fact(facts, "Detail", `run.get answered ${errorText(item.detailError)}`);
  }
  fact(facts, "Run", item.run_id, true);
  fact(facts, "Source", src.repo ? `${src.repo}, ${src.label}` : src.label);
  fact(facts, "Workflow", workflow(item));
  fact(facts, "State", typeof record.state === "string" ? record.state : null);
  fact(facts, "Exit code", Number.isInteger(record.exit_code) ? String(record.exit_code) : "none");
  fact(facts, "Accepted", record.accepted_at, true);
  fact(facts, "Started", record.started_at, true);
  fact(facts, "Finished", record.finished_at, true);
  fact(facts, "Cleanup", record.cleanup);
  fact(facts, "Snapshot", input.snapshot_id, true);
  fact(facts, "Workflow digest", input.workflow_digest, true);
  fact(facts, "Plan digest", input.plan_digest, true);
  fact(facts, "Event digest", input.event_digest, true);
  fact(facts, "Image", input.image_digest || input.image_reference, true);
  fact(facts, "Worker", record.worker_id, true);
  fact(facts, "Attempt", record.attempt_id, true);

  renderArtifacts(item.run_id);
  renderLog(item.run_id, record);
}

function renderArtifacts(runId) {
  const box = $("artifacts");
  box.replaceChildren();
  const reply = call(bundle, "run.artifacts", { run_id: runId });
  if (reply.missing) return box.append(el("p", "quiet", "Not read in this observation."));
  if (reply.error) return box.append(el("p", "quiet", `run.artifacts answered ${errorText(reply.error)}`));
  const items = Array.isArray(reply.result.artifacts) ? reply.result.artifacts : [];
  if (!items.length) return box.append(el("p", "quiet", "This run stored no artifacts."));
  const list = el("ul", "artifact-list");
  for (const a of items) {
    const li = el("li");
    li.append(el("span", "mono", a.path), el("span", "quiet", `${a.size} bytes`));
    list.append(li);
  }
  box.append(list);
  if (reply.result.next_cursor) box.append(el("p", "quiet", "More artifacts exist beyond this page."));
}

let logText = "";

function renderLog(runId, record) {
  const out = $("log");
  const end = $("log-end");
  out.replaceChildren();
  const result = logPages(bundle, runId);
  logText = pagesText(result.pages);
  for (const piece of terminalRuns(logText)) {
    const s = el("span", [piece.fg ? `fg-${piece.fg}` : "", piece.bold ? "bold" : ""].join(" ").trim() || null, piece.text);
    out.append(s);
  }
  if (!logText) out.append(el("span", "quiet", "No stored output."));
  const pages = `${result.pages.length} page${result.pages.length === 1 ? "" : "s"}`;
  if (result.missing) end.textContent = "Output was not read in this observation.";
  else if (result.error) end.textContent = `run.logs answered ${errorText(result.error)} after ${pages}.`;
  else if (result.ended) end.textContent = `End of stored output, ${pages}.`;
  else if (record.state === "running" || record.state === "queued") {
    end.textContent = `${pages} read; the stream has not ended. This worker stores output when the run finishes, so a running run shows none yet.`;
  } else end.textContent = `${pages} read; the stream did not report an end.`;
  $("find").value = "";
  $("find-count").textContent = "";
}

function find() {
  const needle = $("find").value;
  const count = countMatches(logText, needle);
  $("find-count").textContent = needle ? `${count} match${count === 1 ? "" : "es"} in loaded text` : "";
  const out = $("log");
  for (const node of out.querySelectorAll("mark")) node.replaceWith(document.createTextNode(node.textContent));
  out.normalize();
  if (!needle || !count) return;
  const walker = document.createTreeWalker(out, NodeFilter.SHOW_TEXT);
  const hits = [];
  while (walker.nextNode()) hits.push(walker.currentNode);
  let first = null;
  for (const node of hits) {
    let text = node.textContent;
    let at = text.indexOf(needle);
    if (at === -1) continue;
    const frag = document.createDocumentFragment();
    while (at !== -1) {
      frag.append(document.createTextNode(text.slice(0, at)));
      const m = el("mark", null, needle);
      first = first || m;
      frag.append(m);
      text = text.slice(at + needle.length);
      at = text.indexOf(needle);
    }
    frag.append(document.createTextNode(text));
    node.replaceWith(frag);
  }
  if (first) first.scrollIntoView({ block: "nearest" });
}

function move(delta) {
  if (!runs.length) return;
  const at = runs.findIndex((r) => r.run_id === selected);
  const next = Math.min(runs.length - 1, Math.max(0, at === -1 ? 0 : at + delta));
  select(runs[next].run_id, true);
  document.getElementById(`run-${runs[next].run_id}`).scrollIntoView({ block: "nearest" });
}

$("runs").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); move(1); }
  else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); move(-1); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && selected) close();
});
$("find").addEventListener("input", find);

load();
