"use strict";
const $ = id => document.getElementById(id);
const show = (el, on) => el.toggleAttribute("hidden", !on);

const clock = s => {
  if (s == null || !isFinite(s)) return "—";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
};
const size = b => b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`;
const tail = (p, n = 2) => p.split("/").filter(Boolean).slice(-n).join("/");
const when = s => s ? new Date(s * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

// Asks, and resolves to what was answered. window.confirm is unavailable in the
// desktop app's webview and returns falsy there, which turned every confirmation
// into a silent refusal — the button did nothing and said nothing.
function ask(message) {
  return new Promise((resolve) => {
    const box = $("confirm");
    // Moved to be a child of body before it is shown. position:fixed is relative to
    // the nearest ancestor with a transform or an animation rather than to the
    // viewport, and this page animates its containers — so left where it was
    // written, the backdrop covered a band across the middle of the screen instead
    // of the screen, while still swallowing every click behind it.
    if (box.parentNode !== document.body) document.body.appendChild(box);
    $("confirm-text").textContent = message;
    box.hidden = false;
    $("confirm-yes").focus();
    const done = (answer) => {
      box.hidden = true;
      document.removeEventListener("keydown", onKey);
      resolve(answer);
    };
    // Escape is the answer people expect from a dialog, and the one that cannot
    // destroy anything.
    const onKey = (e) => { if (e.key === "Escape") done(false); };
    document.addEventListener("keydown", onKey);
    $("confirm-yes").onclick = () => done(true);
    $("confirm-no").onclick = () => done(false);
    // Clicking away answers no as well. A dialog that covers the page and cannot be
    // dismissed by any of the three things people try is a trap, and this one
    // covered the page whether it looked like it or not.
    box.onclick = (e) => { if (e.target === box) done(false); };
  });
}

async function api(path, body, method) {
  const res = await fetch("/api" + path, {
    method: method || (body ? "POST" : "GET"),
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error("api"), { detail: data.detail || { message: res.statusText } });
  return data;
}

// The model is a dropdown of what was found on disk; the text field is the
// fallback when nothing was found or the user picked "somewhere else".
const manualModel = () => !$("model-line-manual").hidden;
const modelPath = () => (manualModel() ? $("model").value : $("model-pick").value).trim();

const form = () => ({
  source: $("source").value.trim(),
  model: modelPath(),
  language: $("language").value,
  out_dir: $("out-dir").value.trim(),
  basename: $("basename").value.trim(),
  want_txt: $("want-txt").checked,
  want_srt: $("want-srt").checked,
  keep_intermediates: $("keep").checked,
  extra_args: $("extra").value,
});

// Output values we filled in ourselves, so a new source file replaces them but a
// value the user typed is never stomped.
let auto = { out_dir: "", basename: "" };
// Files picked alongside the first one. They inherit this form's settings and are
// written next to their own sources. Declared here because paint() reads it.
let extras = [];
let pinned = false;      // user asked for the surface while a finished job exists
let pinnedPast = null;   // the job that was on screen when they asked
let bootstrapped = false; // defaults from the server applied once

// The codes whisper answers with, in words. Anything unrecognised keeps its code,
// which is still more use than nothing. Named for what it is: i18n.js already has a
// LANGUAGE_NAMES, for the language of the buttons, and these two lists are different
// questions — one is the interface, this one is what was spoken into the microphone.
const SPOKEN_NAMES = {
  en: "English", he: "Hebrew", ar: "Arabic", ru: "Russian", es: "Spanish", fr: "French",
  de: "German", it: "Italian", pt: "Portuguese", nl: "Dutch", pl: "Polish", tr: "Turkish",
  uk: "Ukrainian", ro: "Romanian", sv: "Swedish", no: "Norwegian", da: "Danish",
  fi: "Finnish", cs: "Czech", el: "Greek", hu: "Hungarian", ja: "Japanese",
  ko: "Korean", zh: "Chinese", hi: "Hindi", fa: "Persian", ur: "Urdu", id: "Indonesian",
};
function languageName(code) {
  const key = String(code || "").toLowerCase();
  return SPOKEN_NAMES[key] || (key ? key.toUpperCase() : "");
}

// Quality as a grade rather than a file name, the same words Settings uses. §5a:
// what a model is called is a question nobody outside this repository can evaluate.
function qualityOf(path) {
  const models = (lastState && lastState.models) || [];
  const model = models.find(m => m.path === path);
  if (!model) return path ? tail(path, 1) : "";
  return typeof qualityLabel === "function" ? qualityLabel(model).split(" · ")[0] : model.name;
}

function paint() {
  const f = form();
  const exts = [f.want_txt && "txt", f.want_srt && "srt"].filter(Boolean);
  const esc = text => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const chosen = $("language").selectedOptions[0];
  // One sentence rather than four controls: how well, in what language, and where
  // it lands. All three are readable; none of them is a question being asked.
  $("out-preview").innerHTML = f.out_dir && f.basename && exts.length
    ? `<b>${esc(qualityOf(f.model))}</b><i> · </i><b>${esc(chosen ? chosen.textContent : f.language)}</b>` +
      `<i> · …/${esc(tail(f.out_dir))}/</i><b>${esc(f.basename)}</b><i>.${exts.join(" + .")}</i>`
    : `<i>${t("new.outEmpty")}</i>`;
  $("start").disabled = !(f.source && f.model && f.out_dir && f.basename && exts.length);
  $("start").firstChild.textContent = extras.length
    ? t("new.startMany", { n: extras.length + 1 }) : t("new.start");
  // A file has been chosen, so the question "what do you want words from?" has
  // been answered and both ways in leave. Recording included: one subject per
  // screen, and Clear brings it back.
  show($("ways"), !f.source);
  show($("chosen"), !!f.source);
  show($("rest"), !!f.source);
  paintPhase();
}
document.addEventListener("input", paint);
document.addEventListener("change", paint);

async function inspect() {
  const path = $("source").value.trim();
  if (!path) return paint();
  try {
    const info = await api("/inspect", { path });
    $("file-name").textContent = info.name;
    $("file-meta").innerHTML = [clock(info.duration), size(info.size), info.name.split(".").pop().toUpperCase()]
      .map(x => `<span>${x}</span>`).join("<i>·</i>");
    for (const [id, key] of [["out-dir", "out_dir"], ["basename", "basename"]]) {
      if (!$(id).value || $(id).value === auto[key]) $(id).value = info[key];
      auto[key] = info[key];
    }
    formError(null);
  } catch (err) {
    $("source").value = "";
    formError(err.detail);
  }
  paint();
}
$("source").addEventListener("change", inspect);

function formError(detail) {
  show($("form-error"), !!detail);
  if (!detail) return;
  $("form-error-msg").textContent = detail.message;
  // The code belongs with the log, not beside the sentence. It was being printed in
  // the open — "capture not arriving" under a paragraph explaining the same thing in
  // English — which is the machine talking over the app.
  const technical = [(detail.code || "").replace(/_/g, " "), detail.details || ""]
    .filter(Boolean).join("\n\n");
  $("form-error-details").textContent = technical;
  show($("form-error-more"), !!technical);
  $("form-error").focus();
}

// A mark cannot change its wording to say it worked, so it changes its mark: a tick
// for a moment, then back. Without this, copying gave no sign at all that it had.
function flashCopied(button) {
  const use = button.querySelector("use");
  const label = button.querySelector("span");
  const said = label && label.textContent;
  use.setAttribute("href", "#i-check");
  if (label) label.textContent = t("job.copied");
  button.classList.add("done");
  setTimeout(() => {
    use.setAttribute("href", "#i-copy");
    if (label) label.textContent = said;
    button.classList.remove("done");
  }, 1400);
}

const OTHER = "__other__";

function useManualModel(on) {
  show($("model-line"), !on);
  show($("model-line-manual"), on);
}

function fillModels(models, saved) {
  const sel = $("model-pick");
  const esc = t => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  // Graded, the same words Settings uses. A file name is not something anybody can
  // weigh against anything.
  sel.innerHTML = models.map(m =>
    `<option value="${esc(m.path)}">${esc(typeof qualityLabel === "function"
      ? qualityLabel(m) : `${m.name} · ${size(m.size)}`)}</option>`)
    .join("") + `<option value="${OTHER}">${t("new.elsewhere")}</option>`;
  show($("model-hint"), !models.length);
  // Largest model is first and therefore preselected; a saved default wins.
  if (saved && models.some(m => m.path === saved)) sel.value = saved;
  else if (saved || !models.length) { useManualModel(true); $("model").value = saved || ""; return; }
  useManualModel(false);
}

$("model-pick").addEventListener("change", () => {
  if ($("model-pick").value !== OTHER) return paint();
  useManualModel(true);
  $("model").value = "";
  $("model").focus();
  paint();
});

function renderBatchNote() {
  show($("batch-note"), extras.length > 0);
  if (extras.length) {
    $("batch-note").textContent = t("new.batch", { n: extras.length });
  }
}

// Opening a native dialog takes a second or two. Without saying so the app looks
// broken, and a second click opens a second dialog. Every caller goes through
// here so no button anywhere is left looking dead.
async function pickPath(kind, button) {
  const label = button && button.textContent;
  if (button) { button.disabled = true; button.dataset.busy = "1"; button.textContent = t("picker.opening"); }
  document.body.classList.add("waiting");
  try {
    return await api("/pick?kind=" + kind, {}); // POST, like the other actions
  } finally {
    document.body.classList.remove("waiting");
    if (button) { button.disabled = false; delete button.dataset.busy; if (label) button.textContent = label; }
  }
}

async function browse(kind, target, button) {
  let path, paths, reason;
  try {
    ({ path, paths, reason } = await pickPath(kind, button));
  } catch (err) {
    return formError(err.detail);
  }
  if (reason) return formError({ message: reason });
  if (!path) return; // cancelled
  if (target === "source") {
    extras = (paths || []).slice(1);
    renderBatchNote();
  }
  $(target).value = path;
  if (target === "source") await inspect(); else paint();
}
$("choose-file").onclick = (e) => browse("files", "source", e.currentTarget);
$("choose-model").onclick = (e) => browse("file", "model", e.currentTarget);
$("choose-dir").onclick = (e) => browse("folder", "out-dir", e.currentTarget);
$("change-file").onclick = (e) => browse("files", "source", e.currentTarget);

// The way back out of the flow, from any beat of it. Nothing is undone — the file
// is on disk and the transcript is in the library — the surface just comes back.
function leaveFlow() {
  pin();
  readFor = null;
  ["source", "model", "out-dir", "basename"].forEach(id => ($(id).value = ""));
  auto = { out_dir: "", basename: "" };
  extras = [];
  renderBatchNote();
  formError(null);
  paint();
}
$("reset").onclick = leaveFlow;

$("start").onclick = async () => {
  const body = form();
  try {
    const { existing } = await api("/collisions", body);
    if (existing.length) {
      if (!await ask(`Files with this output name already exist. Replace them?\n\n${existing.join("\n")}`)) return;
      body.overwrite = true;
    }
    await api("/start", body);
    // The rest of the batch keeps these settings but lands next to its own files.
    const failed = [];
    for (const source of extras) {
      try {
        const info = await api("/inspect", { path: source });
        await api("/start", { ...body, source, out_dir: info.out_dir, basename: info.basename });
      } catch (err) {
        failed.push(`${source.split("/").pop()}: ${err.detail.message}`);
      }
    }
    extras = [];
    renderBatchNote();
    // Remember what was just used, so the next launch starts one field lighter.
    api("/settings", { default_model_path: body.model, default_language: body.language }, "PUT").catch(() => {});
    pinned = false;
    formError(failed.length ? { message: `${failed.length} file(s) could not be queued.`,
                                details: failed.join("\n") } : null);
    await refresh();
  } catch (err) {
    formError(err.detail);
  }
};

$("job-cancel").onclick = async () => {
  if (!await ask(t("job.cancelConfirm"))) return;
  try { await api("/cancel", {}); } catch (err) { formError(err.detail); }
  await refresh();
};
$("job-again").onclick = () => { leaveFlow(); render(lastState); };

const STAGE_KEYS = ["queued", "starting", "converting", "transcribing", "saving",
                    "completed", "cancelling", "cancelled", "failed"];

// What it is doing, in words somebody would use. "Transcribing track 2 of 2" is a
// description of the machinery; listening to one side of a conversation and then
// the other is what is actually happening.
function whatItIsDoing(job) {
  const stage = STAGE_KEYS.includes(job.stage) ? t("job." + job.stage) : job.stage;
  if (job.stage !== "transcribing") return stage;
  if (!job.track) return t("job.listening");
  return t(job.track.index === 0 ? "job.listeningYours" : "job.listeningTheirs");
}

// Roughly, and never to the second: an estimate that ticks down one second at a
// time invites being checked against a watch, and it will lose.
function inWords(seconds) {
  if (seconds < 45) return t("job.almost");
  const mins = Math.round(seconds / 60);
  if (mins < 60) return t("job.minutes", { n: mins });
  const hours = seconds / 3600;
  return t("job.hours", { n: hours < 2 ? hours.toFixed(1) : Math.round(hours) });
}

function renderJob(job) {
  const live = job.status === "running" || job.status === "cancelling";
  const pct = Math.round(job.percent);

  $("job-meta").innerHTML = [job.source.split("/").pop(), clock(job.duration), job.language,
    job.model.split("/").pop()].map(x => `<span>${x}</span>`).join("<i>·</i>");
  $("job-count").innerHTML = `${pct}<sup>%</sup>`;
  $("job-stage").textContent = whatItIsDoing(job);
  $("job-progress").value = pct;
  $("job-tape").firstElementChild.style.width = pct + "%";
  $("job-tape").classList.toggle("idle", live && pct === 0);
  $("job-tape").classList.toggle("done", job.status === "completed");

  const secs = (live ? Date.now() / 1000 : job.ended_at || 0) - job.started_at;
  $("job-elapsed").textContent = t("job.spent", { at: clock(secs) });
  // How much longer, which is the only number anybody is actually waiting on. Said
  // in words rather than to the second, because a countdown that jitters reads as a
  // promise being broken every time it moves.
  const left = live ? job.remaining : null;
  $("job-left").textContent = left == null ? clock(secs) : inWords(left);
  $("job-left-label").textContent = left == null
    ? (live ? t("job.elapsed") : t("job.total")) : t("job.left");
  show($("job-elapsed"), left != null);
  show($("job-leave"), live);

  // Lines already transcribed, arriving as they arrive. The wait has a heartbeat.
  const heard = (live && job.heard) || [];
  show($("job-heard"), heard.length > 0);
  if (heard.length) {
    const esc = x => String(x).replace(/&/g, "&amp;").replace(/</g, "&lt;");
    $("job-heard").innerHTML = heard.map(line => `<p dir="auto">${esc(line)}</p>`).join("");
  }

  show($("job-cancel"), live);
  show($("job-again"), !live);
  $("job-log").textContent = job.log.join("\n");

  show($("job-error"), !!job.error);
  if (job.error) {
    $("job-error-msg").textContent = job.error.message;
    $("job-error-code").textContent = job.error.code.replace(/_/g, " ");
    $("job-error-details").textContent = job.error.details || "";
  }

  const done = job.status === "completed";
  show($("job-result"), done);
  if (done) {
    $("job-files").innerHTML = Object.entries(job.outputs).map(([ext, p]) =>
      `<div class="artifact"><span class="ext">${ext}</span><code>${p.replace(/</g, "&lt;")}</code></div>`).join("");
    $("job-preview").textContent = job.preview;
    $("job-reveal").onclick = () => api("/reveal", { path: job.out_dir }).catch(() => {});
    $("job-copy").onclick = async (e) => {
      await navigator.clipboard.writeText(job.preview);
      flashCopied(e.currentTarget);
    };
  }
}

let lastState = null;
let lastRun = null;   // which run the library was last drawn for
let readFor = null;     // the finished job whose transcript is being read, if any
let watched = null;     // the last job this page actually saw running
let readTries = 0;      // failed attempts to open that job's transcript

// Both stories are one flow with two openings — record or choose a file, then
// transcribe, then read it — and it runs as phases so that only the beat you are
// on is on screen. Everything else, the library included, steps aside.
//
//   idle → ready → working → done, with recording in front of all of them.
//
// `pinned` is the way out: it says leave the finished job alone, I want the
// surface back. Starting or resuming something clears it.
function phaseOf(s) {
  // A pin is against one particular job. When a different one turns up the pin has
  // nothing left to hold: the second between queueing a recording and its job being
  // picked up would otherwise show the previous transcription's result, and then
  // the flow would follow the wrong run.
  if (pinned && s.job && s.job.id !== pinnedPast) pinned = false;
  if (typeof recIsLive !== "undefined" && recIsLive) return "recording";
  if (s.job && !pinned) return s.job.status === "completed" ? "done" : "working";
  return $("source").value.trim() ? "ready" : "idle";
}

// Leave the job that is on screen alone; the surface is wanted instead.
function pin() {
  pinned = true;
  pinnedPast = lastState && lastState.job ? lastState.job.id : null;
}

// The end of the flow is the transcript itself, read against its audio — the same
// reader the library opens, because there is no reason for two of them. It waits
// for the run to reach the library rather than asking for it early: history is
// written a moment after the job says completed, and a 404 here would be a flash
// of red at the happiest moment in the app.
function finish(job) {
  // Only a job this page watched run. A transcription that had already finished
  // before any of this started — one sitting in the state at load, or the previous
  // one still there for the second between queueing a recording and that job being
  // picked up — is not what anybody is waiting to read.
  if (job.id !== watched || readFor || readTries >= 3) return;
  if (typeof entries === "undefined" || !entries.some(e => e.id === job.id)) return;
  readFor = job.id;
  // Let go of the claim if the transcript could not be fetched, so the next poll
  // tries again. Keeping it cost a real transcription: the backend stalled for a
  // minute on a macOS consent dialog, this request died with it, and the flow spent
  // the rest of the session believing it had already moved on — a finished job
  // screen that never became the transcript it had just written.
  showEntry(job.id).then(ok => { if (!ok) { readFor = null; readTries += 1; } });
}

// Which beat is on screen. Called from the poll and again the moment anything the
// phase depends on changes, so that choosing a file clears the surface then rather
// than up to a second later.
function paintPhase() {
  const s = lastState;
  if (!s) return {};
  if (s.job && (s.job.status === "running" || s.job.status === "cancelling") && watched !== s.job.id) {
    watched = s.job.id;
    readTries = 0;
  }
  const at = phaseOf(s);
  if (at === "done") finish(s.job);
  // readFor is set before the transcript is fetched, so the job screen leaves in
  // the same frame the reader is asked for rather than a second after it.
  const reading = !$("reader").hidden || readFor !== null;
  show($("screen-start"), !reading && (at === "idle" || at === "ready"));
  show($("screen-job"), !reading && (at === "working" || at === "done"));
  show($("resting"), !reading && at === "idle");
  show($("notices"), !reading && at === "idle");
  return { at, reading };
}

function renderQueue(rows) {
  show($("queue-box"), rows.length > 0);
  if (!rows.length) return;
  $("queue-list").innerHTML = rows.map((r, i) => `
    <div class="artifact"><span class="ext">${i + 1}</span>
      <code>${r.source.split("/").pop().replace(/</g, "&lt;")}</code>
      <button class="link" data-dequeue="${r.id}">${t("job.remove")}</button></div>`).join("");
}

function renderResumable(rows) {
  show($("resumable"), rows.length > 0);
  if (!rows.length) return;
  $("resumable-list").innerHTML = rows.map(r => `
    <p>${t("job.reached", { name: r.source.split("/").pop().replace(/</g, "&lt;"),
                            at: clock(r.reached_ms / 1000),
                            of: r.duration ? " / " + clock(r.duration) : "", was: r.was })}
       <button class="link" data-resume="${r.id}">${t("job.resume")}</button>
       <button class="link" data-discard="${r.id}">${t("job.discard")}</button></p>`).join("");
}

document.addEventListener("click", async (e) => {
  const resumeId = e.target.dataset && e.target.dataset.resume;
  const discardId = e.target.dataset && e.target.dataset.discard;
  const dequeueId = e.target.dataset && e.target.dataset.dequeue;
  try {
    if (dequeueId) await api("/queue/" + dequeueId, null, "DELETE");
    else if (resumeId) { pinned = false; await api("/resume/" + resumeId, {}, "POST"); }
    else if (discardId) {
      if (!await ask(t("job.discardConfirm"))) return;
      await api("/resume/" + discardId, null, "DELETE");
    } else return;
    await refresh();
  } catch (err) { formError(err.detail); }
});

function render(s) {
  lastState = s;
  // A dot is enough to say everything is where it should be, and a dot is all it
  // says. Something missing is worth a sentence; nothing missing is worth none.
  const missing = Object.entries(s.environment).filter(([, v]) => !v.ok).map(([k]) => k);
  $("env").className = missing.length ? "pill bad" : "pill";
  $("env").title = missing.length ? t("env.missing", { names: missing.join(", ") }) : t("env.ready");
  $("env").textContent = missing.length ? $("env").title : "";

  if (!bootstrapped) {
    bootstrapped = true;
    $("extra").value = s.default_extra_args;
    fillModels(s.models, s.settings.default_model_path);
    if (s.settings.default_language) $("language").value = s.settings.default_language;
    paint();
  }

  // Drawn before the phase is decided, because it is what sets recIsLive.
  if (typeof renderRecording === "function") {
    renderRecording(s.recording, s.orphan_recordings, s.settings);
  }

  const { at, reading } = paintPhase();
  if (!reading && (at === "working" || at === "done")) renderJob(s.job);
  renderQueue(s.queue || []);
  renderResumable(s.resumable || []);

  // The library is the resting state, so a finished transcript has to land in it
  // without anybody navigating anywhere. The newest run rather than how many there
  // are: /state returns the last thirty, so after the thirtieth the count never
  // moves again and the list only ever loaded when the app was opened — which is
  // exactly how it behaved. Empty string covers the first paint.
  const top = s.history[0];
  const newest = top ? `${top.id}:${top.status}:${top.ended_at}` : "";
  if (newest !== lastRun) {
    lastRun = newest;
    if (typeof openLibrary === "function") openLibrary();
  }
  show($("history-box"), s.history.length > 0);
  if ($("history")) $("history").innerHTML = s.history.map(r => `
    <tr><td>${r.source.split("/").pop().replace(/</g, "&lt;")}</td>
        <td class="st" style="${r.status === "completed" ? "" : "color:var(--accent)"}">${r.status}</td>
        <td>${r.language}</td><td><time>${when(r.ended_at)}</time></td></tr>`).join("");
}

// A dead backend used to look like a frozen page: the poll failed silently and
// the percentage just stopped moving. Say so instead.
function offline() {
  $("env").className = "pill bad";
  $("env").textContent = t("env.offline");
  const job = lastState && lastState.job;
  if (job && (job.status === "running" || job.status === "cancelling")) {
    $("job-stage").textContent = t("job.lost");
    $("job-tape").classList.remove("idle");
  }
}

// --- new recordings sitting in the source folders --------------------------

let pendingDismissed = false;

async function lookForNewRecordings() {
  if (pendingDismissed) return;
  let found;
  try {
    found = await api("/pending");
  } catch {
    return;  // nothing to say if the backend is not answering
  }
  show($("pending"), found.count > 0);
  if (!found.count) return;
  $("pending-what").textContent = t("pending.what", {
    n: found.count, names: found.names.slice(0, 6).join(", ") + (found.count > 6 ? "…" : ""),
  });
}

$("pending-later").onclick = () => { pendingDismissed = true; show($("pending"), false); };
$("pending-go").onclick = async () => {
  pendingDismissed = true;
  show($("pending"), false);
  try {
    await api("/queue-pending", {});
    pinned = false;
    await refresh();
  } catch (err) {
    formError(err.detail);
  }
};

// --- views -------------------------------------------------------------------

// Record, Transcribe and Library were three destinations for one story: get words
// out of audio, arriving by two doors and leaving by a third. They are one surface
// now, and settings is the only other place there is.
const VIEWS = ["home", "settings"];

function currentView() {
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  return VIEWS.includes(name) ? name : "home";
}

function routeChanged() {
  const view = currentView();
  for (const name of VIEWS) show($("view-" + name), name === view);
  // The same link goes and comes back, so there is never a dead end and never a
  // second tab competing with the work. The key rather than the text, so that
  // switching interface language does not put it back to "Settings".
  const link = $("to-settings");
  const away = view === "settings";
  link.href = away ? "#/" : "#/settings";
  link.dataset.i18nTitle = away ? "nav.back" : "nav.settings";
  link.title = t(link.dataset.i18nTitle);
  link.setAttribute("aria-label", link.title);
  link.querySelector("use").setAttribute("href", away ? "#i-back" : "#i-gear");
  link.querySelector(".icon").classList.toggle("mirror", away);
  link.classList.toggle("here", away);
  if (view === "settings" && typeof openSettings === "function") openSettings();
}

window.addEventListener("hashchange", routeChanged);
routeChanged();

applyTranslations();
const refresh = () => api("/state").then(render).catch(offline);
refresh();
lookForNewRecordings();  // once, when the app opens — never on a timer
setInterval(refresh, 1000); // polling a loopback server is free; no SSE needed
