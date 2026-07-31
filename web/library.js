"use strict";
// The library: every finished transcript, searchable, read alongside its audio.
// It is not a place you go to — it sits under the two ways in and is what the app
// shows when nothing is happening, because it is what the app is for.

let entries = [];
let openEntry = null;
let cueEls = [];
let activeCue = -1;

async function openLibrary() {
  try {
    ({ entries } = await api("/transcripts"));
  } catch (err) {
    return formError(err.detail);
  }
  renderEntries();
}

// Everything interpolated below comes from disk (file names, transcript text), so
// it is escaped — including attribute values, which quotes would otherwise break out of.
function esc(t) {
  return String(t == null ? "" : t)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderEntries() {
  $("entries").innerHTML = entries.length ? entries.map(e => `
    <button class="entry" data-entry="${esc(e.id)}">
      <span class="entry-name">${esc(e.name)}</span>
      <span class="meta">${esc(clock(e.duration))}<i>·</i>${esc(e.language)}<i>·</i>${esc(when(e.ended_at))}${
        e.has_media ? "" : `<i>·</i>${t("lib.moved")}`}</span>
    </button>`).join("")
    : `<p class="hint">${t("lib.empty")}</p>`;
}

// --- one transcript ----------------------------------------------------------

// A Hebrew transcript should read like a Hebrew page: the timestamp column on
// the right, the text beginning at the right edge.
const RTL_LANGUAGES = new Set(["he", "ar", "fa", "ur", "yi", "iw"]);

async function showEntry(id, seekMs) {
  let detail;
  try {
    detail = await api("/transcripts/" + id);
  } catch (err) {
    return formError(err.detail);
  }
  openEntry = detail;
  $("reader-name").textContent = detail.name;
  $("reader-meta").innerHTML = [clock(detail.duration), detail.language, when(detail.ended_at)]
    .map(x => `<span>${esc(x)}</span>`).join("<i>·</i>");

  const player = $("player");
  show($("player-note"), !detail.has_media);
  if (detail.has_media) {
    player.src = "/api/media/" + id;
    player.hidden = false;
  } else {
    player.removeAttribute("src");
    player.hidden = true;
    $("player-note").textContent = t("lib.noMedia");
  }

  // Cues when an .srt survives; otherwise the plain text, which cannot be seeked.
  cueEls = [];
  activeCue = -1;
  const rtl = RTL_LANGUAGES.has((detail.language || "").toLowerCase());
  $("cues").dir = rtl ? "rtl" : "ltr";
  $("reader-name").dir = "auto";  // the file name has its own language
  if (detail.cues.length) {
    $("cues").innerHTML = detail.cues.map((c, i) =>
      `<p class="cue" data-cue="${i}" data-at="${c.start}" role="button" tabindex="0"
          title="${t("lib.jumpTo", { at: stampOf(c.start) })}"><span class="at">${stampOf(c.start)}</span>${esc(c.text)}</p>`).join("");
    cueEls = [...$("cues").querySelectorAll(".cue")];
  } else {
    $("cues").innerHTML = `<pre class="sheet plain">${esc(detail.text)}</pre>`;
  }

  renderFacts(detail);
  // The reader is the whole screen, whether it was reached by finishing a
  // transcription or by picking one out of the list.
  show($("reader"), true);
  show($("resting"), false);
  show($("screen-start"), false);
  show($("screen-job"), false);
  if (seekMs) seekTo(seekMs);
  return true;
}

// What the run cost. Nobody needs it to read a transcript, which is why it is
// folded away — but "why did that take 40 minutes" deserves an answer.
function renderFacts(d) {
  const unknown = t("fact.unknown");
  const secs = n => (n == null ? unknown : clock(n));
  const speed = d.duration && d.work_seconds
    ? t("fact.speedValue", { n: (d.duration / d.work_seconds).toFixed(1) }) : unknown;
  const rows = [
    ["fact.took", secs(d.work_seconds)],
    ["fact.audio", secs(d.duration)],
    ["fact.speed", speed],
    ["fact.cpu", d.cpu_seconds == null ? unknown : clock(d.cpu_seconds)],
    ["fact.memory", d.peak_memory_mb == null ? unknown : `${d.peak_memory_mb} MB`],
    ["fact.model", (d.model || "").split("/").pop() || unknown],
    ["fact.language", d.language || unknown],
    ["fact.silence", d.vad_model == null ? unknown : (d.vad_model ? t("fact.yes") : t("fact.no"))],
    // A word list grows without limit and nobody reads it here; the first few say
    // whether one was in force, which is the only question this panel answers.
    ["fact.vocabulary", d.vocabulary == null ? unknown : shorten(d.vocabulary) || "—"],
    ["fact.args", (d.extra_args || []).join(" ") || "—"],
    ["fact.when", when(d.ended_at)],
  ];
  $("reader-facts").innerHTML = rows
    .map(([key, value]) => `<dt>${esc(t(key))}</dt><dd>${esc(value)}</dd>`).join("");
}

// Enough to recognise, never enough to fill the panel.
function shorten(text, limit = 48) {
  const said = String(text || "").trim();
  return said.length <= limit ? said : said.slice(0, limit).replace(/[,\s]+\S*$/, "") + "…";
}

function stampOf(ms) {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

// Scroll the cue list itself rather than asking the browser to scrollIntoView:
// a smooth scroll across 30,000px of transcript gets interrupted and silently
// does nothing, which left the highlighted line off screen.
function centre(el) {
  const box = $("cues");
  box.scrollTop = el.offsetTop - box.offsetTop - box.clientHeight / 2 + el.offsetHeight / 2;
}

function seekTo(ms) {
  const player = $("player");
  if (!player.hidden) {
    player.currentTime = ms / 1000;
    player.play().catch(() => {});  // autoplay may be refused; the seek still lands
  }
  const el = cueEls.find(c => Number(c.dataset.at) >= ms) || cueEls[cueEls.length - 1];
  if (el) centre(el);
}

// Following stops while you are reading somewhere else, and resumes a few
// seconds after you stop scrolling.
let scrolledAt = 0;
$("cues").addEventListener("scroll", (e) => {
  if (e.isTrusted) scrolledAt = Date.now();
});

// Highlight the cue being spoken. Only touches the DOM when the cue changes.
$("player").addEventListener("timeupdate", () => {
  if (!cueEls.length) return;
  const now = $("player").currentTime * 1000;
  let i = activeCue;
  if (i < 0 || Number(cueEls[i].dataset.at) > now) i = 0;
  while (i + 1 < cueEls.length && Number(cueEls[i + 1].dataset.at) <= now) i++;
  if (i === activeCue) return;
  if (activeCue >= 0) cueEls[activeCue].classList.remove("now");
  cueEls[i].classList.add("now");
  activeCue = i;
  if (Date.now() - scrolledAt > 5000) centre(cueEls[i]);
});

$("reader-close").onclick = () => {
  $("player").pause();
  show($("reader"), false);
  show($("hits-box"), $("hits").children.length > 0);
  // Closing the transcript is also how the flow ends, so the finished job is let
  // go of here — otherwise the next poll would open the reader on it again.
  if (typeof leaveFlow === "function") leaveFlow();
  show($("resting"), true);
};

$("reader-copy").onclick = async (e) => {
  const text = openEntry.cues.length
    ? openEntry.cues.map(c => c.text).join("\n") : openEntry.text;
  await navigator.clipboard.writeText(text);
  flashCopied(e.currentTarget);
};

// Opening a transcript is a fresh answer to "where did it go", so the last one goes.
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-entry]")) show($("reader-saved"), false);
});

// The one that takes it away. The picker takes a moment to open, so the button says
// so — a dead-looking button is how somebody ends up opening two save panels.
$("reader-save").onclick = async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  show($("reader-saved"), false);
  try {
    const { path } = await api("/transcripts/" + openEntry.id + "/save", {});
    $("reader-saved").textContent = path ? t("job.savedTo", { path }) : t("job.saveCancelled");
    show($("reader-saved"), true);
  } catch (err) {
    formError(err.detail);
  } finally {
    button.disabled = false;
  }
};

$("reader-reveal").onclick = () => {
  const dir = (openEntry.txt || openEntry.srt || "").replace(/\/[^/]*$/, "");
  if (dir) api("/reveal", { path: dir }).catch(() => {});
};

// --- search ------------------------------------------------------------------

let searchTimer = null;

$("q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 250);  // typing shouldn't grep on every keystroke
});

async function runSearch() {
  const q = $("q").value.trim();
  if (q.length < 2) {
    show($("hits-box"), false);
    $("search-status").textContent = "";
    return;
  }
  let hits = [];
  try {
    ({ hits } = await api("/search?q=" + encodeURIComponent(q)));
  } catch (err) {
    return formError(err.detail);
  }
  $("search-status").textContent = hits.length
    ? t(hits.length === 1 ? "lib.hits" : "lib.hitsPlural", { n: hits.length })
    : t("lib.noHits");
  show($("hits-box"), hits.length > 0);
  $("hits").innerHTML = hits.map(h => `
    <button class="entry" data-entry="${esc(h.id)}" data-at="${Number(h.start)}">
      <span class="meta"><b>${esc(h.name)}</b><i>·</i>${stampOf(h.start)}</span>
      <span class="hit-text">${esc(h.text)}</span>
    </button>`).join("");
}

// --- one delegated click handler for entries, hits and cues ------------------

document.addEventListener("click", (e) => {
  const entry = e.target.closest("[data-entry]");
  if (entry) return showEntry(entry.dataset.entry, Number(entry.dataset.at || 0));
  const cue = e.target.closest(".cue");
  if (cue) seekTo(Number(cue.dataset.at));
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const cue = e.target.closest && e.target.closest(".cue");
  if (!cue) return;
  e.preventDefault();
  seekTo(Number(cue.dataset.at));
});
