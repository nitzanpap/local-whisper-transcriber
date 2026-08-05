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
  //
  // For the computer, our own tap before any other loopback. A machine with Teams
  // or Zoom installed lists their drivers too, and those carry audio only while
  // those apps route into them — so guessing one of those recorded a channel of
  // digital silence and said nothing was wrong.
  const guess = (!preferLoopback && devices.find(d => d.default && !d.loopback))
    || (preferLoopback && devices.find(d => d.loopback && d.builtin))
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

// --- checking it works, before it matters ------------------------------------

const SIDE_ICON = { true: "✓", false: "✕" };

async function runCheck(button) {
  const said = button.textContent;
  button.disabled = true;
  button.textContent = t("rec.checking");
  // The result is drawn inside What to record, so open it: a verdict nobody can see
  // is not a verdict, and this is also how somebody learns where the control lives.
  $("rec-sources").open = true;
  show($("rec-check-result"), false);
  recError(null);
  try {
    const found = await api("/record/check",
      { voice: $("rec-voice").value, computer: $("rec-computer").value });
    renderCheck(found.sides);
  } catch (err) {
    recError(err.detail);
  } finally {
    button.disabled = false;
    button.textContent = said;
    await refresh();   // a passing check takes the offer off the first screen
  }
}
$("rec-check").onclick = (e) => runCheck(e.currentTarget);
$("first-go").onclick = (e) => runCheck(e.currentTarget);

// Not now is for now, not for ever: it stays gone until the app is opened again,
// because somebody who has not answered the question still has not answered it.
let firstDismissed = false;
$("first-later").onclick = () => { firstDismissed = true; show($("first-check"), false); };

function renderCheck(sides) {
  const rows = Object.entries(sides);
  const bad = rows.filter(([, r]) => !r.heard);
  $("rec-check-title").textContent = bad.length ? t("rec.checkBad") : t("rec.checkGood");
  $("rec-check-sides").innerHTML = rows.map(([side, r]) =>
    `<p><b>${SIDE_ICON[r.heard]}</b> ${t("rec.side." + side)} — ${
      r.heard ? t("rec.checkHeard") : t("rec.why." + r.why)}</p>`).join("");
  // One pane, for the first thing that is actually a permission.
  const permission = bad.find(([side, r]) => r.why === "refused" || r.why === "nothing");
  show($("rec-check-fix"), !!permission);
  if (permission) {
    const pane = permission[0] === "voice" ? "microphone" : "audio";
    $("rec-check-fix").onclick = () => api("/privacy/" + pane, {}).catch(() => {});
  }
  show($("rec-check-result"), true);
}

$("rec-check-close").onclick = () => show($("rec-check-result"), false);

function recError(detail) {
  show($("rec-error"), !!detail);
  show($("rec-allow"), !!(detail && detail.pane));
  if (!detail) return;
  $("rec-error-msg").textContent = detail.message;
  const code = (detail.code || "").replace(/_/g, " ");
  $("rec-error-code").textContent = code;
  show($("rec-error-more"), !!code);
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

function renderRecording(rec, orphans, settings) {
  renderOrphans(orphans || []);
  // Offered until it has been answered, and only when there is something to answer
  // with: a machine with no inputs at all has a different problem, said elsewhere.
  show($("first-check"), !firstDismissed && recDevices.length > 0
       && !(settings || {}).capture_checked);
  const live = !!rec && RECORDING_LIVE.includes(rec.status);
  recIsLive = live;
  show($("rec-live"), live);
  // The needles run on their own faster clock while there is something to show,
  // and not at all otherwise.
  needlesFor(!!rec && rec.status === "recording");
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
    // Which needles exist, and what to call them. The widths are not set here —
    // this runs once a second, and once a second is what made the meter look
    // broken. See watchNeedles.
    const asked = rec.labels || [];
    ["voice", "computer"].forEach((side, n) => {
      const row = $("rec-needle-" + side);
      show(row, side === "voice" || !!rec.stereo);
      row.querySelector(".who").textContent = asked[n] || "";
      // Dimmed rather than hidden: a side that was asked for and is silent is
      // information, and taking its row away would leave nothing to notice.
      row.classList.toggle("gone", (rec.not_arriving || []).includes(side));
    });
    // A side that is producing nothing, while it is still worth knowing. It goes
    // away by itself when audio starts arriving, because then it is no longer true.
    const dead = rec.not_arriving || [];
    show($("rec-nothing"), dead.length > 0);
    if (dead.length) {
      $("rec-nothing-what").textContent = dead.map(side => t("rec.notArriving." + side)).join("\n\n");
      const pane = dead[0] === "voice" ? "microphone" : "audio";
      $("rec-nothing-fix").onclick = () => api("/privacy/" + pane, {}).catch(() => {});
    }
    // A side that was arriving and stopped. Only worth saying about a side that is
    // not already being reported as producing nothing at all, or the same silence
    // gets two notices with two different explanations for it.
    const stalled = (rec.stalled || []).filter(side => !dead.includes(side));
    show($("rec-stalled"), stalled.length > 0);
    if (stalled.length) {
      $("rec-stalled-what").textContent =
        stalled.map(side => t("rec.stalled." + side)).join("\n\n");
    }
    // A side that is arriving but arriving broken. Different from both of the
    // above and worse than either: the meter moves, the file grows, and what is
    // being written is silence standing in for audio that never came. Two hours
    // of a client meeting were lost to this with nothing on screen at all.
    const losing = (rec.losing || []).filter(side => !dead.includes(side));
    show($("rec-losing"), losing.length > 0);
    if (losing.length) {
      $("rec-losing-what").textContent = losing.map(side =>
        t("rec.losing." + side,
          { pct: Math.round(((rec.padding || {})[side] || {}).fraction * 100) })).join("\n\n");
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
      side === "voice" ? t("rec.quietVoice") : t("rec.quietComputer"))
      // A side too close to its own background is a different sentence from one
      // that heard nothing, and it belongs on the same screen: this is the last
      // moment anybody could move the microphone before the next meeting.
      .concat((rec.noisy || []).map(side =>
        t(side === "voice" ? "rec.noisyVoice" : "rec.noisyComputer",
          { db: Math.round(rec.snr[side]) }))),
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

// The needles, on their own clock.
//
// Everything else on this page is repainted once a second, which is right for a
// clock and a size and wrong for a level: a bar that moves once a second does not
// read as a slow meter, it reads as a broken one. So the levels come from an
// endpoint that carries nothing else, fifteen times a second, and touch two style
// properties rather than redrawing the screen.
//
// Fifteen because the sources cannot beat it: peak is measured over 50 ms windows
// on the microphone and reported ten times a second by the helper. Asking faster
// would return the same number twice.
const NEEDLE_MS = 66;

// Peak dB: about -60 when a room is quiet, -20 or so for a voice, 0 at the top of
// the scale. Below -60 there is nothing worth showing a bar for.
const needleWidth = (db) => Math.max(0, Math.min(100, ((db + 60) / 60) * 100));

// Fast up, slow down — how every meter that has ever felt right behaves. Rising
// instantly is what makes it feel connected to the sound; falling instantly makes
// it flicker, because speech is full of tiny gaps that nobody hears as silence.
const FALL = 6;
const shown = { voice: 0, computer: 0 };

let needleTimer = null;
async function watchNeedles() {
  let m;
  try {
    m = await api("/record/meters");
  } catch {
    return;  // the once-a-second poll is what reports a dead backend; not this
  }
  if (!m.recording) {
    // Let them fall to nothing rather than snapping, so stopping looks like
    // stopping rather than like the page breaking.
    for (const side of ["voice", "computer"]) shown[side] = 0;
    paintNeedles();
    clearInterval(needleTimer);
    needleTimer = null;
    return;
  }
  for (const side of ["voice", "computer"]) {
    const target = needleWidth(m.peak?.[side] ?? -120);
    shown[side] = target > shown[side] ? target : Math.max(target, shown[side] - FALL);
  }
  paintNeedles();
}

function paintNeedles() {
  for (const side of ["voice", "computer"]) {
    const bar = $("rec-needle-" + side)?.querySelector("i");
    if (bar) bar.style.width = shown[side] + "%";
  }
}

// Started when a recording starts and stopped when it ends, so nothing polls
// while there is nothing to show.
function needlesFor(recording) {
  if (recording && needleTimer === null) {
    needleTimer = setInterval(watchNeedles, NEEDLE_MS);
  } else if (!recording && needleTimer !== null) {
    clearInterval(needleTimer);
    needleTimer = null;
    for (const side of ["voice", "computer"]) shown[side] = 0;
    paintNeedles();
  }
}
