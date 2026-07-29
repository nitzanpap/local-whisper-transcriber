"use strict";
// The Settings view: the defaults every new job inherits, and the watched folders.

const SETTING_FIELDS = {
  "set-model": "default_model_path",
  "set-language": "default_language",
  "set-extra": "default_extra_args",
  "set-vad": "vad_model_path",
  "set-ffmpeg": "ffmpeg_path",
  "set-ffprobe": "ffprobe_path",
  "set-whisper": "whisper_cli_path",
};

async function openSettings() {
  let conf;
  try {
    conf = await api("/settings");
  } catch (err) {
    return formError(err.detail);
  }
  for (const [id, key] of Object.entries(SETTING_FIELDS)) $(id).value = conf[key] || "";
  $("set-watch").value = (conf.watch_folders || []).join("\n");
}

$("save-settings").onclick = async () => {
  const body = { watch_folders: $("set-watch").value.split("\n").map(s => s.trim()).filter(Boolean) };
  for (const [id, key] of Object.entries(SETTING_FIELDS)) body[key] = $(id).value.trim();
  try {
    await api("/settings", body, "PUT");
  } catch (err) {
    return formError(err.detail);
  }
  show($("settings-saved"), true);
  setTimeout(() => show($("settings-saved"), false), 1800);
  bootstrapped = false;  // let the Transcribe view pick the new defaults up
};

// Browse buttons for the model and VAD paths.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-browse]");
  if (!btn) return;
  try {
    const { path, reason } = await api("/pick?kind=" + btn.dataset.browse, {}, "POST");
    if (reason) return formError({ message: reason });
    if (path) $(btn.dataset.into).value = path;
  } catch (err) {
    formError(err.detail);
  }
});

$("add-watch").onclick = async () => {
  try {
    const { path, reason } = await api("/pick?kind=folder", {}, "POST");
    if (reason) return formError({ message: reason });
    if (!path) return;
    const current = $("set-watch").value.split("\n").map(s => s.trim()).filter(Boolean);
    if (!current.includes(path)) current.push(path);
    $("set-watch").value = current.join("\n");
  } catch (err) {
    formError(err.detail);
  }
};

$("queue-folder").onclick = async () => {
  const note = $("folder-result");
  try {
    const { path, reason } = await api("/pick?kind=folder", {}, "POST");
    if (reason) return formError({ message: reason });
    if (!path) return;
    note.textContent = "Looking…";
    show(note, true);
    const result = await api("/queue-folder", { path });
    note.textContent = result.queued
      ? `Queued ${result.queued}: ${result.names.join(", ")}` + (result.skipped.length
        ? ` — skipped ${result.skipped.length} (${result.skipped.join("; ")})` : "")
      : `Nothing to queue.` + (result.skipped.length ? ` Skipped: ${result.skipped.join("; ")}` : "");
  } catch (err) {
    show(note, false);
    formError(err.detail);
  }
};

$("clear-history").onclick = async () => {
  if (!confirm("Clear the list of past transcriptions?\n\nThe transcript files themselves are not touched.")) return;
  try {
    await api("/history", null, "DELETE");
  } catch (err) {
    return formError(err.detail);
  }
  if (typeof entries !== "undefined") entries = [];
};

if (currentView() === "settings") openSettings();  // deep link straight to #/settings
