// ═══════════════════════════════════════════════════
// LOG — formatted orchestrator event viewer
// ═══════════════════════════════════════════════════

const KIND_BADGE = {
  run_start:           { label: "RUN",    cls: "log-badge-info" },
  prompt_start:        { label: "PROMPT", cls: "log-badge-info" },
  cache_miss:          { label: "CACHE",  cls: "log-badge-muted" },
  cache_hit:           { label: "CACHE",  cls: "log-badge-ok" },
  template_class:      { label: "TPL",    cls: "log-badge-info" },
  template_loaded:     { label: "TPL",    cls: "log-badge-ok" },
  template_explicit:   { label: "TPL",    cls: "log-badge-info" },
  template_auto_classified: { label: "TPL", cls: "log-badge-ok" },
  planner_attempt:     { label: "PLAN",   cls: "log-badge-info" },
  planner_success:     { label: "PLAN",   cls: "log-badge-ok" },
  planner_call_fail:   { label: "PLAN",   cls: "log-badge-fail" },
  planner_schema_fail: { label: "SCHEMA", cls: "log-badge-fail" },
  planner_fallback:    { label: "FALLBK", cls: "log-badge-warn" },
  stage_start:         { label: "STAGE",  cls: "log-badge-info" },
  stage_attempt:       { label: "ATTEMPT", cls: "log-badge-info" },
  stage_attempt_ok:    { label: "OK",     cls: "log-badge-ok" },
  stage_attempt_fail:  { label: "FAIL",   cls: "log-badge-fail" },
  stage_success:       { label: "DONE",   cls: "log-badge-ok" },
  stage_fail:          { label: "STAGE",  cls: "log-badge-fail" },
  call_ok:             { label: "CALL",   cls: "log-badge-ok" },
  call_fail:           { label: "FAIL",   cls: "log-badge-fail" },
  cooldown_set:        { label: "WAIT",   cls: "log-badge-warn" },
  rotation_relaxed:    { label: "RELAX",  cls: "log-badge-warn" },
  pacing_wait:         { label: "PACE",   cls: "log-badge-warn" },
  slot_blacklisted:    { label: "BLKLST", cls: "log-badge-fail" },
  family_penalty:      { label: "PENALTY", cls: "log-badge-warn" },
  prompt_done:         { label: "PROMPT", cls: "log-badge-ok" },
  prompt_fail:         { label: "PROMPT", cls: "log-badge-fail" },
  run_end:             { label: "END",    cls: "log-badge-ok" },
  checkpoint_cleared:  { label: "SAVE",    cls: "log-badge-info" },
};

let currentRunGroup = null;
let _logPollId = null;
let _logEntryCount = 0;

function startLogPolling() {
  if (_logPollId) return;
  _logPollId = setInterval(_logPollTick, 500);
  _logPollTick();
}

function stopLogPolling() {
  if (_logPollId) {
    clearInterval(_logPollId);
    _logPollId = null;
  }
}

async function _logPollTick() {
  try {
    const res = await fetch("/api/log");
    if (!res.ok) return;
    const data = await res.json();
    const entries = data.entries || [];
    if (entries.length !== _logEntryCount) {
      _logEntryCount = entries.length;
      renderLog(entries);
    }
  } catch {
    // ignore poll errors
  }
}

async function clearLog() {
  if (!confirm("Clear all orchestrator log entries?")) return;
  try {
    const res = await fetch("/api/log", { method: "DELETE" });
    if (!res.ok) throw new Error("Failed to clear log");
    _logEntryCount = 0;
    renderLog([]);
  } catch (err) {
    console.error("Clear log failed:", err);
  }
}

async function loadLogPanel() {
  const emptyEl = document.getElementById("logEmpty");
  const entriesEl = document.getElementById("logEntries");
  entriesEl.innerHTML = '<div class="log-loading">Loading...</div>';
  emptyEl.style.display = "none";

  try {
    const res = await fetch("/api/log");
    if (!res.ok) throw new Error("Failed to load log");
    const data = await res.json();
    const entries = data.entries || [];
    _logEntryCount = entries.length;
    renderLog(entries);
  } catch (err) {
    entriesEl.innerHTML =
      '<div class="log-error">Failed to load log: ' +
      escapeHtml(err.message) + '</div>';
  }
}

function renderLog(entries) {
  const entriesEl = document.getElementById("logEntries");
  const emptyEl = document.getElementById("logEmpty");

  if (!entries.length) {
    entriesEl.innerHTML = "";
    emptyEl.style.display = "";
    return;
  }

  emptyEl.style.display = "none";

  const scrollAtBottom = entriesEl.scrollHeight - entriesEl.scrollTop - entriesEl.clientHeight < 60;

  currentRunGroup = null;

  let html = '<div class="log-header">' +
             '<span class="log-time">Time</span>' +
             '<span class="log-badge log-badge-muted">Log</span>' +
             '<span class="log-stage">Name</span>' +
             '<span class="log-slot">Model</span>' +
             '<span class="log-status">Status</span>' +
             '<span class="log-duration">Duration</span>' +
             '<span class="log-reason">Error Reason</span>' +
             '</div>';

  for (const e of entries) {
    const runGroup = e.run_id || "";
    if (runGroup && runGroup !== currentRunGroup) {
      currentRunGroup = runGroup;
      html += '<div class="log-run-group">Run <span class="log-run-id">' +
              escapeHtml(runGroup) + '</span></div>';
    }

    html += renderLogEntry(e);
  }

  entriesEl.innerHTML = html;

  if (scrollAtBottom) {
    entriesEl.scrollTop = entriesEl.scrollHeight;
  }
}

function renderLogEntry(e) {
  const time = formatTime(e.ts);
  const badge = KIND_BADGE[e.kind] || { label: e.kind, cls: "log-badge-muted" };
  const stage = e.stage || "";
  const slot = shortSlot(e.slot);
  const status = statusText(e);
  const duration = e.duration_s ? e.duration_s.toFixed(1) + "s" : "";
  const reason = e.reason || "";

  let row = '<div class="log-row">';
  row += '<span class="log-time">' + escapeHtml(time) + '</span>';
  row += '<span class="log-badge ' + badge.cls + '">' + escapeHtml(badge.label) + '</span>';
  row += '<span class="log-stage" title="' + escapeHtml(stage) + '">' + escapeHtml(truncate(stage, 24)) + '</span>';
  row += '<span class="log-slot" title="' + escapeHtml(e.slot || "") + '">' + escapeHtml(slot) + '</span>';
  row += '<span class="log-status">' + escapeHtml(status) + '</span>';
  row += '<span class="log-duration">' + escapeHtml(duration) + '</span>';
  row += reason ? '<span class="log-reason log-reason-indicator">!</span>' : '<span class="log-reason"></span>';
  row += '</div>';

  if (reason) {
    row += '<div class="log-reason-full">' + escapeHtml(reason) + '</div>';
  }

  return row;
}

function formatTime(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return ts.slice(11, 19);
  }
}

function shortSlot(slot) {
  if (!slot) return "";
  const parts = slot.split("/");
  return parts[parts.length - 1];
}

function statusText(e) {
  if (e.http_status === 200) return "200";
  if (e.http_status) return String(e.http_status);
  if (e.fail_code) return String(e.fail_code);
  if (e.ok) return "ok";
  return "";
}

function truncate(s, max) {
  return s.length > max ? s.slice(0, max) + "…" : s;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}
