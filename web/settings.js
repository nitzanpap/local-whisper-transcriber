"use strict";
// The Settings view: what every new transcription inherits, in plain words.
// Paths and flags live under Expert; the top of the screen is the four things
// that actually change the result.

const SETTING_FIELDS = {
  "set-model": "default_model_path",
  "set-language": "default_language",
  "set-extra": "default_extra_args",
  "set-vad": "vad_model_path",
  "set-vocab": "vocabulary",
  "set-ffmpeg": "ffmpeg_path",
  "set-ffprobe": "ffprobe_path",
  "set-whisper": "whisper_cli_path",
};

// How a model file names itself, and what that means to someone who did not
// choose it. Anything unrecognised keeps its own name.
const QUALITY = [
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
  $("set-watch").value = (conf.watch_folders || []).join("\n");

  knownModels = state.models || [];
  const chosen = conf.default_model_path || (knownModels[0] || {}).path || "";
  $("set-quality").innerHTML = knownModels
    .map(m => `<option value="${escAttr(m.path)}">${escAttr(qualityLabel(m))}</option>`)
    .join("") || `<option value="">${escAttr(t("set.noModels"))}</option>`;
  $("set-quality").value = chosen;
  $("model-help").textContent = knownModels.length
    ? t("set.modelFound")
    : t("set.modelMissing");

  $("set-silence").value = conf.vad_model_path ? "on" : "off";
  $("silence-hint").textContent = conf.vad_model_path
    ? t("set.silenceReady")
    : t("set.silenceMissing");
  $("vad-help").textContent = conf.vad_model_path ? "" : VAD_DOWNLOAD;

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

// Turning silence-skipping off must not lose the path, so it is remembered here
// and put back when switched on again.
let rememberedVad = "";
$("set-silence").addEventListener("change", () => {
  if ($("set-silence").value === "off") {
    rememberedVad = $("set-vad").value || rememberedVad;
    $("set-vad").value = "";
  } else if (!$("set-vad").value) {
    $("set-vad").value = rememberedVad;
    if (!rememberedVad) formError({ message: t("set.silenceNeedsModel"), details: VAD_DOWNLOAD });
  }
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

// --- backup -----------------------------------------------------------------

$("export-settings").onclick = async () => {
  let conf;
  try {
    conf = await api("/settings");
  } catch (err) {
    return formError(err.detail);
  }
  const payload = {
    kind: "local-whisper-transcriber-settings",
    saved_at: new Date().toISOString(),
    settings: conf,
    display: { reading_size: display("reading_size", ""), reading_face: display("reading_face", ""),
               language: display("ui_language", "") },
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)],
                                           { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "local-whisper-transcriber-settings.json";
  a.click();
  URL.revokeObjectURL(url);
  note("backup-result", t("set.exported"));
};

$("import-settings").onclick = () => $("import-file").click();

$("import-file").onchange = async () => {
  const file = $("import-file").files[0];
  if (!file) return;
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch {
    return formError({ message: t("set.importNotJson") });
  }
  if (payload.kind !== "local-whisper-transcriber-settings" || !payload.settings) {
    return formError({ message: t("set.importWrongFile") });
  }
  try {
    await api("/settings", payload.settings, "PUT");
  } catch (err) {
    return formError(err.detail);
  }
  for (const [key, value] of Object.entries(payload.display || {})) {
    if (value) localStorage.setItem("lwt." + key, value);
  }
  applyDisplay();
  if (payload.display && payload.display.language) setLanguage(payload.display.language);
  await openSettings();
  note("backup-result", t("set.imported"));
  $("import-file").value = "";
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
  try {
    const { path, reason } = await api("/pick?kind=folder", {}, "POST");
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

$("clear-history").onclick = async () => {
  if (!confirm(t("set.clearConfirm"))) return;
  try {
    await api("/history", null, "DELETE");
  } catch (err) {
    return formError(err.detail);
  }
  if (typeof entries !== "undefined") entries = [];
};

if (currentView() === "settings") openSettings();  // deep link straight to #/settings
