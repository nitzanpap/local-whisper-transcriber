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
let pinned = false;      // user asked for the compose screen while a finished job exists
let bootstrapped = false; // defaults from the server applied once

function paint() {
  const f = form();
  const exts = [f.want_txt && "txt", f.want_srt && "srt"].filter(Boolean);
  $("out-preview").innerHTML = f.out_dir && f.basename && exts.length
    ? `<i>…/${tail(f.out_dir).replace(/&/g, "&amp;").replace(/</g, "&lt;")}/</i><b>${f.basename.replace(/</g, "&lt;")}</b><i>.${exts.join(" + .")}</i>`
    : `<i>${t("new.outEmpty")}</i>`;
  $("start").disabled = !(f.source && f.model && f.out_dir && f.basename && exts.length);
  $("start").firstChild.textContent = extras.length
    ? t("new.startMany", { n: extras.length + 1 }) : t("new.start");
  show($("choose-file"), !f.source);
  show($("paste-row"), !f.source);
  show($("file-card"), !!f.source);
  show($("rest"), !!f.source);
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
  $("form-error-details").textContent = detail.details || "";
  show($("form-error-details"), !!detail.details);
  $("form-error").focus();
}

const OTHER = "__other__";

function useManualModel(on) {
  show($("model-line"), !on);
  show($("model-line-manual"), on);
}

function fillModels(models, saved) {
  const sel = $("model-pick");
  const esc = t => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  sel.innerHTML = models.map(m =>
    `<option value="${esc(m.path)}">${esc(m.name)} · ${size(m.size)}</option>`)
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

async function browse(kind, target) {
  let path, paths, reason;
  try {
    ({ path, paths, reason } = await api("/pick?kind=" + kind, {})); // POST, like the other actions
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
$("choose-file").onclick = () => browse("files", "source");
$("choose-model").onclick = () => browse("file", "model");
$("choose-dir").onclick = () => browse("folder", "out-dir");
$("change-file").onclick = () => browse("files", "source");

$("reset").onclick = () => {
  ["source", "model", "out-dir", "basename"].forEach(id => ($(id).value = ""));
  auto = { out_dir: "", basename: "" };
  extras = [];
  renderBatchNote();
  formError(null);
  paint();
};

$("start").onclick = async () => {
  const body = form();
  try {
    const { existing } = await api("/collisions", body);
    if (existing.length) {
      if (!confirm(`Files with this output name already exist. Replace them?\n\n${existing.join("\n")}`)) return;
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
  if (!confirm(t("job.cancelConfirm"))) return;
  try { await api("/cancel", {}); } catch (err) { formError(err.detail); }
  await refresh();
};
$("job-again").onclick = () => { pinned = true; render(lastState); };

const STAGE_KEYS = ["queued", "starting", "converting", "transcribing", "saving",
                    "completed", "cancelling", "cancelled", "failed"];

function renderJob(job) {
  const live = job.status === "running" || job.status === "cancelling";
  const pct = Math.round(job.percent);

  $("job-meta").innerHTML = [job.source.split("/").pop(), clock(job.duration), job.language,
    job.model.split("/").pop()].map(x => `<span>${x}</span>`).join("<i>·</i>");
  $("job-count").innerHTML = `${pct}<sup>%</sup>`;
  $("job-stage").textContent = STAGE_KEYS.includes(job.stage) ? t("job." + job.stage) : job.stage;
  $("job-progress").value = pct;
  $("job-tape").firstElementChild.style.width = pct + "%";
  $("job-tape").classList.toggle("idle", live && pct === 0);
  $("job-tape").classList.toggle("done", job.status === "completed");

  const secs = (live ? Date.now() / 1000 : job.ended_at || 0) - job.started_at;
  $("job-elapsed").textContent = clock(secs);
  $("job-clock-label").textContent = live ? t("job.elapsed") : t("job.total");

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
      e.target.textContent = t("job.copied");
      setTimeout(() => (e.target.textContent = t("job.copy")), 1400);
    };
  }
}

let lastState = null;

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
      if (!confirm(t("job.discardConfirm"))) return;
      await api("/resume/" + discardId, null, "DELETE");
    } else return;
    await refresh();
  } catch (err) { formError(err.detail); }
});

function render(s) {
  lastState = s;
  const missing = Object.entries(s.environment).filter(([, v]) => !v.ok).map(([k]) => k);
  $("env").className = missing.length ? "pill bad" : "pill";
  $("env").textContent = missing.length ? t("env.missing", { names: missing.join(", ") }) : t("env.ready");

  if (!bootstrapped) {
    bootstrapped = true;
    $("extra").value = s.default_extra_args;
    fillModels(s.models, s.settings.default_model_path);
    if (s.settings.default_language) $("language").value = s.settings.default_language;
    paint();
  }

  // Stay on the compose screen once asked for it, even as queued jobs come and
  // go — otherwise queueing more files while one runs is impossible. Starting or
  // resuming something clears the pin and jumps back to the job screen.
  const job = s.job;
  const onJob = !!job && !pinned;
  show($("screen-job"), onJob);
  show($("screen-new"), !onJob);
  if (onJob) renderJob(job);
  renderQueue(s.queue || []);
  renderResumable(s.resumable || []);

  show($("history-box"), s.history.length > 0);
  $("history").innerHTML = s.history.map(r => `
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

// --- views -------------------------------------------------------------------

const VIEWS = ["transcribe", "library", "settings"];

function currentView() {
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  return VIEWS.includes(name) ? name : "transcribe";
}

function routeChanged() {
  const view = currentView();
  for (const name of VIEWS) show($("view-" + name), name === view);
  for (const link of document.querySelectorAll("nav a")) {
    link.classList.toggle("here", link.dataset.view === view);
    link.setAttribute("aria-current", link.dataset.view === view ? "page" : "false");
  }
  // Each view loads its own data when it becomes visible, so switching to it is
  // never stale and hidden views cost nothing.
  if (view === "library" && typeof openLibrary === "function") openLibrary();
  if (view === "settings" && typeof openSettings === "function") openSettings();
}

window.addEventListener("hashchange", routeChanged);
routeChanged();

applyTranslations();
const refresh = () => api("/state").then(render).catch(offline);
refresh();
setInterval(refresh, 1000); // polling a loopback server is free; no SSE needed
