"use strict";
// The models screen. A model is the one technical thing this app cannot hide
// entirely — it is gigabytes and somebody has to decide which — so the job here is
// to make it a choice about transcripts rather than a choice about filenames.
//
// Two lists, in the order the questions come: what can I use now, and what else is
// there. Handy's screen is the shape being followed; the one thing deliberately
// done differently is that models stay ordinary files in a folder of the user's
// own, so anything downloaded here works with whisper.cpp elsewhere, and a model
// somebody already has is picked up wherever they keep it.

function modelCard(m, chosen) {
  const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  const bar = (label, value) => value == null ? "" :
    `<div class="score"><span>${t("models." + label)}</span>
       <i><b style="width:${Math.max(4, Math.min(100, value))}%"></b></i></div>`;
  const tags = [
    m.recommended ? `<em class="tag good">${t("models.recommended")}</em>` : "",
    m.path && m.path === chosen ? `<em class="tag on">${t("models.inUse")}</em>` : "",
    m.languages === "en" ? `<em class="tag">${t("models.englishOnly")}</em>` : "",
  ].join("");
  return `<article class="card${m.path && m.path === chosen ? " here" : ""}" data-model="${esc(m.id)}">
    <div class="card-head">
      <div><h3>${esc(m.name)}${tags}</h3><p>${esc(m.description)}</p></div>
      <div class="scores">${bar("accuracy", m.accuracy)}${bar("speed", m.speed)}</div>
    </div>
    <div class="card-foot">
      <span class="grey">${size(m.size_bytes)}</span>
      <span class="card-do"></span>
    </div>
  </article>`;
}

// Whatever this model's row should offer right now. Split out because it is the
// only part that changes on the once-a-second poll, and redrawing the whole list
// underneath somebody's cursor is how a click lands on the wrong thing.
function modelActions(m, chosen, running) {
  if (running && running.id === m.id) {
    const pct = running.percent;
    return `<span class="doing">${running.status === "checking" ? t("models.checking")
      : t("models.getting", { pct })}</span>
      <i class="tape thin"><b style="width:${pct}%"></b></i>
      <button class="link" data-cancel="1">${t("models.cancel")}</button>`;
  }
  if (m.have) {
    return (m.path === chosen ? "" :
      `<button class="link" data-use="${m.path}">${t("models.use")}</button>`) +
      `<button class="link warn" data-drop="${m.id}">${t("models.delete")}</button>`;
  }
  return `<button class="link" data-get="${m.id}">${t("models.get")}</button>`;
}

function renderModels(state) {
  const all = state.catalogue || [];
  const chosen = (state.settings || {}).default_model_path || "";
  const running = state.download;
  for (const [box, wanted] of [["models-have", true], ["models-available", false]]) {
    const mine = all.filter(m => m.have === wanted || (!wanted && running && running.id === m.id && !m.have));
    const target = $(box);
    // Rebuilt only when the set changes; otherwise just the row of buttons, so a
    // download ticking every second does not yank the list out from under a click.
    const signature = mine.map(m => m.id + (m.have ? "1" : "0")).join(",");
    if (target.dataset.shape !== signature) {
      target.innerHTML = mine.map(m => modelCard(m, chosen)).join("");
      target.dataset.shape = signature;
    }
    for (const m of mine) {
      const card = target.querySelector(`[data-model="${CSS.escape(m.id)}"]`);
      if (card) {
        card.querySelector(".card-do").innerHTML = modelActions(m, chosen, running);
        card.classList.toggle("here", !!m.path && m.path === chosen);
      }
    }
  }
  show($("models-none"), !all.some(m => m.have));
}

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-get],[data-drop],[data-cancel],[data-use]");
  if (!el) return;
  try {
    if (el.dataset.get) await api("/models/" + el.dataset.get + "/download", {});
    else if (el.dataset.cancel) await api("/models/cancel", {});
    else if (el.dataset.use) await api("/settings", { default_model_path: el.dataset.use }, "PUT");
    else if (el.dataset.drop) {
      if (!await ask(t("models.deleteConfirm"))) return;
      await api("/models/" + el.dataset.drop, null, "DELETE");
    }
    await refresh();
  } catch (err) { formError(err.detail); }
});

$("models-rescan").onclick = async () => {
  // The scan is cached, so a file somebody has just put in the folder by hand is
  // invisible until it is asked for again. This used to mean restarting the app.
  try { await api("/models/rescan", {}); await refresh(); }
  catch (err) { formError(err.detail); }
};
$("models-folder").onclick = () => api("/open-models-folder", {}).catch(() => {});
