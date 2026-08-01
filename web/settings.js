"use strict";
// The Settings view. Three questions at the top — which language, how accurate,
// where the files go — and one door for everything else.
//
// Two controls were taken off the screen rather than moved, and both still work
// from the settings file. Skipping silence, because its answer was always yes: the
// app uses the silence model whenever there is one, and clearing that path under
// Expert is the real off switch. And the maximum recording length, which §5a names
// as a question only somebody debugging would ask.
//
// Removing a field means removing it from SETTING_FIELDS too. A key that is not
// sent is left exactly as it was, because the server merges what it is given —
// whereas a field that is gone but still listed would send a blank and wipe it.

const SETTING_FIELDS = {
  "set-model": "default_model_path",
  "set-language": "default_language",
  "set-extra": "default_extra_args",
  "set-vad": "vad_model_path",
  "set-vocab": "vocabulary",
  "set-output": "output_folder",
  "set-rec-folder": "recording_folder",
  "set-rec-label-voice": "record_label_voice",
  "set-rec-label-computer": "record_label_computer",
  "set-ffmpeg": "ffmpeg_path",
  "set-ffprobe": "ffprobe_path",
  "set-whisper": "whisper_cli_path",
};

// How a model file names itself, and what that means to someone who did not
// choose it. Anything unrecognised keeps its own name.
// Order matters: the first match wins, so anything more specific comes first.
// `large-v3-turbo` used to read as "best" purely because "large" is in its name —
// it is a faster, less accurate cut of large-v3, and calling it the best model on
// the machine sent people to the slowest choice for the worst reason.
const QUALITY = [
  [/large.*turbo/, "goodFast"],
  [/large/, "best"],
  [/medium/, "good"],
  [/small/, "quick"],
  [/base|tiny/, "roughest"],
];

const VAD_DOWNLOAD =
  "curl -L -o ~/whisper-models/ggml-silero-v5.1.2.bin " +
  "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin";

let knownModels = [];

function qualityLabel(model) {
  const name = (model.name || "").toLowerCase();
  const found = QUALITY.find(([pattern]) => pattern.test(name));
  // Grade and size only: three scripts in one line clips and reads badly, and
  // the file name is right there under Expert for anyone who wants it.
  const grade = found ? t("quality." + found[1]) : model.name;
  return found ? `${grade} · ${size(model.size)}` : `${model.name} · ${size(model.size)}`;
}

async function openSettings() {
  let conf, state;
  try {
    [conf, state] = await Promise.all([api("/settings"), api("/state")]);
  } catch (err) {
    return formError(err.detail);
  }
  for (const [id, key] of Object.entries(SETTING_FIELDS)) $(id).value = conf[key] || "";
  $("set-watch").value = (conf.source_folders || []).join("\n");
  $("set-output-mode").value = conf.output_folder ? "folder" : "beside";
  show($("output-folder-row"), !!conf.output_folder);

  knownModels = state.models || [];
  const chosen = conf.default_model_path || (knownModels[0] || {}).path || "";
  $("set-quality").innerHTML = knownModels
    .map(m => `<option value="${escAttr(m.path)}">${escAttr(qualityLabel(m))}</option>`)
    .join("") || `<option value="">${escAttr(t("set.noModels"))}</option>`;
  $("set-quality").value = chosen;
  $("model-help").textContent = knownModels.length
    ? t("set.modelFound")
    : t("set.modelMissing");

  // A model ships with the app, so this is normally ready without anybody doing
  // anything. The download line is kept for a build that somehow has none.
  $("vad-help").textContent = conf.vad_ready ? t("set.silenceReady") : VAD_DOWNLOAD;
  $("set-rec-auto").value = conf.record_auto_transcribe === true ? "on" : "off";

  $("set-reading-size").value = display("reading_size", "1.02rem");
  $("set-reading-face").value = display("reading_face", "var(--display)");
}

function escAttr(text) {
  return String(text).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

// Choosing a quality is choosing a model file; the Expert field follows along.
$("set-quality").addEventListener("change", () => {
  $("set-model").value = $("set-quality").value;
});

// --- reading preferences live in the browser, applied at once ---------------

function display(key, fallback) {
  return localStorage.getItem("lwt." + key) || fallback;
}

function applyDisplay() {
  document.documentElement.style.setProperty("--reading-size", display("reading_size", "1.02rem"));
  document.documentElement.style.setProperty("--reading-face", display("reading_face", "var(--display)"));
}

for (const id of ["set-reading-size", "set-reading-face"]) {
  $(id).addEventListener("change", () => {
    localStorage.setItem("lwt." + id.replace("set-reading-", "reading_"), $(id).value);
    applyDisplay();
  });
}
applyDisplay();

// --- saving -----------------------------------------------------------------

$("save-settings").onclick = async () => {
  const body = { source_folders: $("set-watch").value.split("\n").map(s => s.trim()).filter(Boolean) };
  for (const [id, key] of Object.entries(SETTING_FIELDS)) body[key] = $(id).value.trim();
  // "Next to each recording" means no folder at all, not the last one typed.
  if ($("set-output-mode").value === "beside") body.output_folder = "";
  body.record_auto_transcribe = $("set-rec-auto").value === "on";
  try {
    await api("/settings", body, "PUT");
  } catch (err) {
    return formError(err.detail);
  }
  show($("settings-saved"), true);
  setTimeout(() => show($("settings-saved"), false), 1800);
  bootstrapped = false;  // let the Transcribe view pick the new defaults up
};

// --- backup -----------------------------------------------------------------

$("export-settings").onclick = async (e) => {
  try {
    // A native Save panel, so the file lands where you put it and the app can
    // name the place afterwards. A browser download would land silently in
    // whatever folder the browser happens to use.
    const { path, reason } = await pickPath("save", e.currentTarget);
    if (reason) return formError({ message: reason });
    if (!path) return note("backup-result", t("set.exportCancelled"));
    const saved = await api("/settings/export", {
      path,
      display: { reading_size: display("reading_size", ""), reading_face: display("reading_face", ""),
                 language: display("ui_language", "") },
    });
    note("backup-result", t("set.exported", { path: saved.path }));
  } catch (err) {
    formError(err.detail);
  }
};

$("import-settings").onclick = async (e) => {
  try {
    const { path, reason } = await pickPath("file", e.currentTarget);
    if (reason) return formError({ message: reason });
    if (!path) return;
    const loaded = await api("/settings/import", { path });
    for (const [key, value] of Object.entries(loaded.display || {})) {
      if (value) localStorage.setItem("lwt." + key, value);
    }
    applyDisplay();
    if (loaded.display && loaded.display.language) setLanguage(loaded.display.language);
    await openSettings();
    note("backup-result", t("set.imported", { path: loaded.path }));
  } catch (err) {
    formError(err.detail);
  }
};

function note(id, text) {
  $(id).textContent = text;
  show($(id), true);
  setTimeout(() => show($(id), false), 4000);
}

// --- folders and paths ------------------------------------------------------

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-browse]");
  if (!btn) return;
  try {
    const { path, reason } = await pickPath(btn.dataset.browse, btn);
    if (reason) return formError({ message: reason });
    if (path) $(btn.dataset.into).value = path;
  } catch (err) {
    formError(err.detail);
  }
});

$("add-watch").onclick = async (e) => {
  try {
    const { path, reason } = await pickPath("folder", e.currentTarget);
    if (reason) return formError({ message: reason });
    if (!path) return;
    const current = $("set-watch").value.split("\n").map(s => s.trim()).filter(Boolean);
    if (!current.includes(path)) current.push(path);
    $("set-watch").value = current.join("\n");
  } catch (err) {
    formError(err.detail);
  }
};

$("queue-folder").onclick = async (e) => {
  try {
    const { path, reason } = await pickPath("folder", e.currentTarget);
    if (reason) return formError({ message: reason });
    if (!path) return;
    note("folder-result", t("set.looking"));
    const result = await api("/queue-folder", { path });
    note("folder-result", result.queued
      ? t("set.queuedN", { n: result.queued, names: result.names.join(", ") })
      : t("set.queuedNone") + (result.skipped.length ? " " + result.skipped.join("; ") : ""));
  } catch (err) {
    show($("folder-result"), false);
    formError(err.detail);
  }
};

$("set-output-mode").addEventListener("change", () => {
  show($("output-folder-row"), $("set-output-mode").value === "folder");
});

$("check-now").onclick = async () => {
  note("folder-result", t("set.looking"));
  try {
    const found = await api("/pending");
    note("folder-result", found.count
      ? t("pending.what", { n: found.count, names: found.names.slice(0, 8).join(", ") })
      : t("pending.none"));
    if (found.count) { pendingDismissed = false; lookForNewRecordings(); }
  } catch (err) {
    show($("folder-result"), false);
    formError(err.detail);
  }
};

$("clear-history").onclick = async () => {
  if (!await ask(t("set.clearConfirm"))) return;
  try {
    await api("/history", null, "DELETE");
  } catch (err) {
    return formError(err.detail);
  }
  if (typeof entries !== "undefined") entries = [];
};

if (currentView() === "settings") openSettings();  // deep link straight to #/settings
