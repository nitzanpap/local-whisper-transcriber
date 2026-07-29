"use strict";
// The Library view: every finished transcript, searchable, read alongside its audio.

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
  show($("entries-box"), $("reader").hidden);
  $("entries").innerHTML = entries.length ? entries.map(e => `
    <button class="entry" data-entry="${esc(e.id)}">
      <span class="entry-name">${esc(e.name)}</span>
      <span class="meta">${esc(clock(e.duration))}<i>·</i>${esc(e.language)}<i>·</i>${esc(when(e.ended_at))}${
        e.has_media ? "" : "<i>·</i>recording moved"}</span>
    </button>`).join("")
    : `<p class="hint">Nothing transcribed yet. The Transcribe view is where that starts.</p>`;
}

// --- one transcript ----------------------------------------------------------

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
    $("player-note").textContent = "The original recording is no longer where it was, so there is nothing to play.";
  }

  // Cues when an .srt survives; otherwise the plain text, which cannot be seeked.
  cueEls = [];
  activeCue = -1;
  if (detail.cues.length) {
    $("cues").innerHTML = detail.cues.map((c, i) =>
      `<p class="cue" data-cue="${i}" data-at="${c.start}" role="button" tabindex="0"
          title="Jump to ${stampOf(c.start)}"><span class="at">${stampOf(c.start)}</span>${esc(c.text)}</p>`).join("");
    cueEls = [...$("cues").querySelectorAll(".cue")];
  } else {
    $("cues").innerHTML = `<pre class="sheet plain">${esc(detail.text)}</pre>`;
  }

  show($("reader"), true);
  show($("entries-box"), false);
  show($("hits-box"), false);
  if (seekMs) seekTo(seekMs);
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
  show($("entries-box"), true);
  show($("hits-box"), $("hits").children.length > 0);
};

$("reader-copy").onclick = async (e) => {
  const text = openEntry.cues.length
    ? openEntry.cues.map(c => c.text).join("\n") : openEntry.text;
  await navigator.clipboard.writeText(text);
  e.target.textContent = "Copied";
  setTimeout(() => (e.target.textContent = "Copy transcript"), 1400);
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
    show($("entries-box"), $("reader").hidden);
    return;
  }
  let hits = [];
  try {
    ({ hits } = await api("/search?q=" + encodeURIComponent(q)));
  } catch (err) {
    return formError(err.detail);
  }
  $("search-status").textContent = hits.length
    ? `${hits.length} match${hits.length === 1 ? "" : "es"}`
    : "No transcript contains that.";
  show($("hits-box"), hits.length > 0);
  $("hits").innerHTML = hits.map(h => `
    <button class="entry" data-entry="${esc(h.id)}" data-at="${Number(h.start)}">
      <span class="meta"><b>${esc(h.name)}</b><i>·</i>${stampOf(h.start)}</span>
      <span class="hit-text">${esc(h.text)}</span>
    </button>`).join("");
}

if (currentView() === "library") openLibrary();  // deep link straight to #/library

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
