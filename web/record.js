"use strict";
// Recording: one button, two dropdowns folded away behind it, and a plain
// account of what is missing when the machine cannot do what is being asked.
//
// Devices are fetched once when the app opens and when asked again — never on
// the one-second poll, because listing them spawns ffmpeg. Everything else here
// is drawn from the state the whole page already polls.

const NONE = "";

let recDevices = [];
let recFound = null;   // the last device listing, so a language switch can redraw it
// Whether a recording is running. app.js reads it to decide what owns the screen,
// because recording outranks everything else that could be there.
let recIsLive = false;
let adopted = null;    // the saved recording that has already entered the flow
let warning = null;    // what to say about it after its recording state is gone

const longClock = (s) => {
  if (s == null || !isFinite(s)) return "0:00";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
};

function recOptions(select, devices, chosen, preferLoopback) {
  const esc = (text) => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  // The system-audio source is ours rather than the machine's, so its name is a
  // translated string here instead of whatever the backend called it.
  select.innerHTML = `<option value="">${esc(t("rec.nothing"))}</option>` + devices
    .map(d => `<option value="${esc(d.id)}">${esc(d.builtin ? t("rec.systemAudio") : d.name)}</option>`)
    .join("");
  if (chosen && devices.some(d => d.id === chosen)) {
    select.value = chosen;
    return;
  }
  // Nothing remembered: guess the obvious one. A loopback device exists to carry
  // the computer's own audio and is never the microphone, so the two guesses
  // cannot land on the same device.
  //
  // For the voice, the machine's own default input before anything else. Taking
  // the first device in the list instead is what once put a Bluetooth headset on
  // somebody's voice channel and recorded nothing at all from it.
  const guess = (!preferLoopback && devices.find(d => d.default && !d.loopback))
    || devices.find(d => (preferLoopback ? d.loopback : !d.loopback));
  select.value = guess ? guess.id : NONE;
}

async function loadDevices(button) {
  if (button) button.disabled = true;
  try {
    const found = await api("/record/devices");
    recDevices = found.devices || [];
    recFound = found;
    recOptions($("rec-voice"), recDevices, found.voice, false);
    recOptions($("rec-computer"), recDevices, found.computer, true);
    $("rec-plan").dataset.folder = found.folder || "";
    $("rec-plan").dataset.labels = JSON.stringify(found.labels || []);
    recAdvice(found);
    recPlan();
  } catch (err) {
    recError(err.detail);
  } finally {
    if (button) button.disabled = false;
  }
}

function recAdvice(found) {
  const which = (found.advice || [])[0];
  show($("rec-advice"), !!which);
  show($("rec-advice-how"), which === "needLoopback");
  if (!which) return;
  $("rec-advice-title").textContent = t("rec." + which + "Title");
  $("rec-advice-what").textContent = t("rec." + which + "What");
  $("rec-advice-steps").textContent = t("rec.loopbackSteps");
  if (which === "noDevices" && (found.log || []).length) {
    $("rec-log").textContent = found.log.join("\n");
    show($("rec-log-box"), true);
  }
}

function recPlan() {
  const voice = $("rec-voice").value, computer = $("rec-computer").value;
  const folder = $("rec-plan").dataset.folder || "";
  let labels = [];
  try { labels = JSON.parse($("rec-plan").dataset.labels || "[]"); } catch { labels = []; }
  $("rec-start").disabled = !(voice || computer);
  const where = `<i>…/${tail(folder).replace(/&/g, "&amp;").replace(/</g, "&lt;")}/</i>`;
  if (voice && computer) {
    $("rec-plan").innerHTML = `${where}<b> ${t("rec.planBoth",
      { voice: labels[0] || "", computer: labels[1] || "" })}</b>`;
  } else if (voice || computer) {
    $("rec-plan").innerHTML = `${where}<b> ${t("rec.planOne")}</b>`;
  } else {
    $("rec-plan").innerHTML = `<i>${t("rec.planNothing")}</i>`;
  }
}

for (const id of ["rec-voice", "rec-computer"]) $(id).addEventListener("change", recPlan);
$("rec-refresh").onclick = (e) => loadDevices(e.currentTarget);

function recError(detail) {
  show($("rec-error"), !!detail);
  show($("rec-allow"), !!(detail && detail.pane));
  if (!detail) return;
  $("rec-error-msg").textContent = detail.message;
  $("rec-error-code").textContent = (detail.code || "").replace(/_/g, " ");
  // The pane, opened rather than described. Somebody who has just been told a
  // recording captured nothing should not then have to go and find the switch.
  if (detail.pane) {
    $("rec-allow").onclick = () => api("/privacy/" + detail.pane, {}).catch(() => {});
  }
  if (detail.details) {
    $("rec-log").textContent = detail.details;
    show($("rec-log-box"), true);
  }
  $("rec-error").focus();
}

$("rec-start").onclick = async (e) => {
  e.currentTarget.disabled = true;
  recError(null);
  try {
    await api("/record/start", { voice: $("rec-voice").value, computer: $("rec-computer").value });
    await refresh();
  } catch (err) {
    recError(err.detail);
  } finally {
    recPlan();
  }
};

async function stopRecording(keep) {
  if (!keep && !await ask(t("rec.throwConfirm"))) return;
  try {
    await api("/record/stop?keep=" + (keep ? "true" : "false"), {});
    await refresh();
  } catch (err) {
    recError(err.detail);
  }
}
$("rec-stop").onclick = () => stopRecording(true);
$("rec-throw").onclick = () => stopRecording(false);

for (const id of ["rec-again", "rec-dismiss"]) {
  $(id).onclick = async () => {
    recError(null);
    warning = null;
    renderWarning();
    try { await api("/record/dismiss", {}); } catch { /* nothing to clear */ }
    await refresh();
  };
}

// --- what the poll paints ----------------------------------------------------

const RECORDING_LIVE = ["recording", "stopping", "saving"];

function renderRecording(rec, orphans) {
  renderOrphans(orphans || []);
  const live = !!rec && RECORDING_LIVE.includes(rec.status);
  recIsLive = live;
  show($("rec-live"), live);
  if (rec && rec.status === "saved") adopt(rec);
  renderWarning();

  if (rec && rec.status === "failed") recError(rec.error);

  if (rec && (rec.log || []).length) {
    $("rec-log").textContent = rec.log.join("\n");
    show($("rec-log-box"), true);
  }

  if (live) {
    $("rec-clock").textContent = longClock(rec.seconds);
    $("rec-size").textContent = size(rec.bytes || 0);
    $("rec-status").textContent = t("rec.status." + rec.status);
    $("rec-live-meta").innerHTML = [
      rec.stereo ? t("rec.twoChannels", { voice: rec.labels[0], computer: rec.labels[1] })
                 : t("rec.oneChannel"),
      t("rec.stopsAfter", { n: Math.round(rec.max_seconds / 60) }),
    ].map(x => `<span>${x}</span>`).join("<i>·</i>");
    // Driven by what the capture reports, never by a timer. The bar that used to
    // sweep on a 1.6s loop looked exactly like a level meter and moved whether or
    // not any audio existed, which is how a microphone recording digital zero went
    // unnoticed. A meter with nothing to show now shows nothing.
    const live = rec.live || {};
    const heard = Object.keys(live).length > 0;
    $("rec-tape").classList.toggle("idle", rec.status === "recording" && !heard);
    if (heard) {
      // LUFS runs from about -70 (silence) to 0 (as loud as it goes); speech sits
      // around -25, so the useful part of the scale is the top half.
      const width = (v) => Math.max(0, Math.min(100, ((v + 60) / 60) * 100));
      const loudest = Math.max(...Object.values(live));
      $("rec-tape").querySelector("i").style.width = width(loudest) + "%";
    }
    $("rec-stop").disabled = rec.status !== "recording";
    $("rec-throw").disabled = rec.status !== "recording";
  }

}

// A recording that has been written out is a file like any other, so it goes
// through the same door: it becomes the source, and the flow asks whether to
// transcribe it. Unless the setting already answered, in which case a job is
// running and the flow is a beat further on. Either way the recording is let go
// of here, because a notice saying it was saved would stand in front of the very
// screen that says the same thing better.
function adopt(rec) {
  if (adopted === rec.id) return;
  adopted = rec.id;
  // Kept because the recording state it came from is about to be cleared, and
  // because a side that heard nothing is worth saying at the last moment anybody
  // could still go back and record it again.
  warning = {
    at: longClock(rec.seconds),
    folder: rec.path.replace(/\/[^/]*$/, ""),
    quiet: (rec.quiet || []).map(side =>
      side === "voice" ? t("rec.quietVoice") : t("rec.quietComputer")),
  };
  // Whether the finished job still sitting in the state gets the screen. It must
  // not: the transcript of the last thing is not what somebody who just stopped
  // recording is waiting to see. If this recording was queued, that job is the
  // new one and the flow follows it.
  pin();
  if (!rec.job_id) {
    $("source").value = rec.path;
    inspect();
  }
  api("/record/dismiss", {}).catch(() => {});
}

function renderWarning() {
  show($("rec-done"), !!warning && warning.quiet.length > 0);
  if (!warning || !warning.quiet.length) return;
  $("rec-done-title").textContent = t("rec.savedTitle", { at: warning.at });
  $("rec-done-what").textContent = warning.quiet.join("\n\n");
  $("rec-open").onclick = () => api("/reveal", { path: warning.folder }).catch(() => {});
}

function renderOrphans(rows) {
  show($("rec-orphans"), rows.length > 0);
  if (!rows.length) return;
  $("rec-orphan-list").innerHTML = rows.map(r => `
    <p>${t("rec.orphanWhat", { at: longClock(r.seconds), size: size(r.bytes) })}
       <button class="link" data-keep-rec="${r.id}">${t("rec.orphanKeep")}</button>
       <button class="link" data-drop-rec="${r.id}">${t("job.discard")}</button></p>`).join("");
}

document.addEventListener("click", async (e) => {
  const keep = e.target.dataset && e.target.dataset.keepRec;
  const drop = e.target.dataset && e.target.dataset.dropRec;
  if (!keep && !drop) return;
  try {
    if (keep) await api("/record/keep/" + keep, {});
    else {
      if (!await ask(t("rec.orphanDropConfirm"))) return;
      await api("/record/keep/" + drop, null, "DELETE");
    }
    await refresh();
  } catch (err) {
    recError(err.detail);
  }
});

// Everything on this screen that script wrote has to be written again in the new
// language. The dropdowns are mostly device names, which are not ours to
// translate — but the system-audio entry is ours, so they are rebuilt too.
function redrawRecord() {
  if (!recFound) return;
  for (const [id, preferLoopback] of [["rec-voice", false], ["rec-computer", true]]) {
    // Whatever is selected now, not what was remembered when the view loaded, and
    // put back by hand afterwards: "Nothing" is a deliberate choice that
    // recOptions would otherwise treat as nothing chosen yet and guess over.
    const was = $(id).value;
    recOptions($(id), recDevices, was, preferLoopback);
    $(id).value = was;
  }
  recAdvice(recFound);
  recPlan();
}

// Recording is one of the two things the first screen offers, so the devices are
// listed as the app opens rather than when a tab is chosen. There is no tab.
loadDevices();
