"""The scripts artifact_server.py adds to every served HTML artifact.

`STAR_JS` is served at `/api/star.js` and `OUTLINE_JS` at `/api/outline.js`. Each draws
inside a closed shadow root, so neither touches the artifact's own markup or CSS beyond
the ids the outline gives headings that lack one. Raw strings, no Python logic, for the
same reason `_gui_html.py` is a module.
"""

# Draws nothing until GET /api/star answers, so a copy opened elsewhere shows no button.
STAR_JS = r"""(() => {
  const me = document.currentScript;
  if (!me) return;
  const host = me.dataset.host, path = me.dataset.path;
  const url = "/api/star?host=" + encodeURIComponent(host) + "&path=" + encodeURIComponent(path);
  fetch(url).then(r => r.ok ? r.json() : null).then(state => {
    if (!state) return;
    let starred = state.starred;
    const box = document.createElement("div");
    // A closed shadow root keeps the artifact's CSS off the button and the button's off the page.
    const root = box.attachShadow({mode: "closed"});
    root.innerHTML = `<style>
:host{position:fixed;top:12px;right:12px;z-index:2147483647}
button,span{display:block;font:20px/1 system-ui,sans-serif;padding:6px 9px;border-radius:999px;
  border:1px solid #45475a;background:#1e1e2ee6;color:#a6adc8;box-shadow:0 1px 4px #0004}
button{cursor:pointer}button:hover,.on{color:#f9e2af}
button:focus-visible{outline:2px solid #f9e2af;outline-offset:2px}
small{font-size:13px;margin-left:6px;vertical-align:3px;color:#f9e2af}small.soon{color:#f38ba8}
@media print{:host{display:none}}
</style>`;
    const el = document.createElement(state.writable ? "button" : "span");
    const DAY_MS = 86400000, WARN_DAYS = 7;
    const due = state.prunes_at ? Date.parse(state.prunes_at) : NaN;
    const draw = () => {
      el.textContent = starred ? "★" : "☆";
      el.className = starred ? "on" : "";
      let pruned = "";
      if (!starred && !Number.isNaN(due)) {
        const days = Math.ceil((due - Date.now()) / DAY_MS);
        pruned = days > 0 ? ` It is pruned on ${new Date(due).toISOString().slice(0, 10)}.`
          : " It is pruned at the next sweep.";
        if (days <= WARN_DAYS) {
          const note = document.createElement("small");
          note.textContent = days > 1 ? `pruned in ${days} days`
            : days === 1 ? "pruned tomorrow" : "pruned at the next sweep";
          if (days <= 1) note.className = "soon";
          el.append(note);
        }
      }
      el.title = state.writable
        ? (starred ? "Starred: never pruned. Click to unstar." : "Star to keep it from being pruned." + pruned)
        : (starred ? "Starred: never pruned" : "Not starred." + pruned);
      if (state.writable) el.setAttribute("aria-pressed", String(starred));
    };
    if (state.writable) {
      el.type = "button";
      el.setAttribute("aria-label", "Star this artifact");
      el.addEventListener("click", async () => {
        el.disabled = true;
        try {
          const res = await fetch("/api/star", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({host, path, starred: !starred})});
          if (!res.ok) throw new Error((await res.text()).trim() || res.status);
          starred = (await res.json()).starred;
          draw();
        } catch (err) {
          el.title = "Could not change the star: " + err.message;
        } finally {
          el.disabled = false;
        }
      });
    }
    draw();
    root.append(el);
    document.body.append(box);
  }).catch(() => {});
})();
"""

# Lists the page's h2/h3 headings in a panel on the left, with each slice's status and a mark on
# each section changed since the reader last scrolled to it, and puts a copy-link button beside a
# hovered heading. A page with fewer than three headings, or with data-outline="off" on <html>
# or <body>, gets none of it.
OUTLINE_JS = r"""(() => {
  const MIN_HEADINGS = 3, PANEL_ROOM = 268, KEY = "artifact-outline";
  const SEEN_KEY = "artifact-seen:" + location.pathname;
  if ([document.documentElement, document.body].some(e => e.dataset.outline === "off")) return;
  const heads = [...document.querySelectorAll("h2, h3")].filter(h => h.textContent.trim());
  if (heads.length < MIN_HEADINGS) return;

  const label = h => {
    const copy = h.cloneNode(true);
    copy.querySelectorAll(".chip").forEach(c => c.remove());
    return copy.textContent.replace(/\s+/g, " ").trim();
  };
  const taken = new Set([...document.querySelectorAll("[id]")].map(e => e.id));
  heads.forEach(h => {
    if (h.id) return;
    const base = label(h).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "section";
    let id = base;
    for (let n = 2; taken.has(id); n++) id = base + "-" + n;
    taken.add(id);
    h.id = id;
  });

  // A section runs from its heading to the next h2/h3, so an edit under an h3 marks only the h3.
  const hash = s => {
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193);
    return (h >>> 0).toString(36);
  };
  const sectionHash = i => {
    const r = document.createRange();
    r.setStartBefore(heads[i]);
    if (heads[i + 1]) r.setEndBefore(heads[i + 1]);
    else r.setEnd(document.body, document.body.childNodes.length);
    return hash(r.toString().replace(/\s+/g, " ").trim());
  };
  const hashes = heads.map((_, i) => sectionHash(i));
  const read = (key, fallback) => {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  };
  const write = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} };
  // The first visit records a baseline and marks nothing; later visits mark what differs from
  // the last version of each section that was scrolled to.
  const seen = read(SEEN_KEY, null);
  const changed = heads.map((h, i) => seen !== null && seen[h.id] !== hashes[i]);
  const record = seen ?? Object.fromEntries(heads.map((h, i) => [h.id, hashes[i]]));
  if (seen === null) write(SEEN_KEY, record);

  const SLICE_LABELS = {planned: "Planned", active: "In progress", done: "Done"};
  const sliceStatus = h => {
    const box = h.closest("[data-status]");
    const status = box && box.querySelector("h2, h3") === h ? box.dataset.status : "";
    return status in SLICE_LABELS ? status : "";
  };

  const box = document.createElement("div");
  // A closed shadow root keeps the artifact's CSS off the panel and the panel's off the page.
  const root = box.attachShadow({mode: "closed"});
  root.innerHTML = `<style>
:host{position:fixed;top:12px;left:12px;z-index:2147483646;font:13px/1.4 system-ui,sans-serif}
button{font:inherit;padding:6px 12px;border-radius:999px;border:1px solid #45475a;
  background:#1e1e2ee6;color:#a6adc8;box-shadow:0 1px 4px #0004;cursor:pointer}
button:hover{color:#cdd6f4}
button:focus-visible,a:focus-visible{outline:2px solid #cba6f7;outline-offset:2px}
.count{color:#89b4fa}
nav{position:fixed;top:52px;left:12px;bottom:12px;width:232px;overflow-y:auto;padding:8px 0;
  border:1px solid #45475a;border-radius:8px;background:#181825f2;box-shadow:0 2px 8px #0006}
nav[hidden]{display:none}
ol{list-style:none;margin:0;padding:0}
a{display:flex;align-items:baseline;gap:6px;padding:4px 14px;color:#a6adc8;text-decoration:none}
a:hover{color:#cdd6f4;background:#313244}
a.h3{padding-left:28px;font-size:12px;color:#6c7086}
a.on{color:#cba6f7;font-weight:600}
.text{flex:1}
.slice,.new{flex:none;width:7px;height:7px;border-radius:50%;align-self:center}
.slice.planned{background:#6c7086}.slice.active{background:#f9e2af}.slice.done{background:#a6e3a1}
.new{background:#89b4fa}
@media print{:host{display:none}}
</style><button type="button" aria-controls="panel"></button>
<nav id="panel" aria-label="Contents"><ol></ol></nav>`;
  const toggle = root.querySelector("button"), panel = root.querySelector("nav");
  const list = root.querySelector("ol");
  const links = heads.map((h, i) => {
    const a = document.createElement("a");
    a.href = "#" + h.id;
    if (h.tagName === "H3") a.className = "h3";
    const status = sliceStatus(h);
    if (status) {
      const dot = document.createElement("span");
      dot.className = "slice " + status;
      dot.title = SLICE_LABELS[status];
      a.append(dot);
    }
    const text = document.createElement("span");
    text.className = "text";
    text.textContent = label(h);
    a.append(text);
    if (changed[i]) {
      const dot = document.createElement("span");
      dot.className = "new";
      dot.title = "Changed since you last read it";
      a.append(dot);
    }
    const li = document.createElement("li");
    li.append(a);
    list.append(li);
    return a;
  });
  const changes = changed.filter(Boolean).length;
  toggle.textContent = "Contents";
  if (changes) {
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = ` · ${changes} changed`;
    toggle.append(count);
  }

  const roomy = () => heads[0].getBoundingClientRect().left >= PANEL_ROOM;
  const show = open => {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.title = open ? "Hide the contents" : "Show the contents";
  };
  const fit = () => show(roomy() && read(KEY, "open") !== "closed");
  toggle.addEventListener("click", () => {
    const open = panel.hidden;
    if (roomy()) write(KEY, open ? "open" : "closed");
    show(open);
  });
  const jump = i => {
    scrollTo({top: heads[i].getBoundingClientRect().top + scrollY - 56});
    history.replaceState(null, "", "#" + heads[i].id);
  };
  list.addEventListener("click", e => {
    const i = links.indexOf(e.target.closest("a"));
    if (i < 0) return;
    e.preventDefault();
    jump(i);
    if (!roomy()) show(false);
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !panel.hidden && !roomy()) show(false);
  });

  let current = -1, queued = false;
  const mark = () => {
    queued = false;
    let i = 0;
    heads.forEach((h, k) => { if (h.getBoundingClientRect().top <= 96) i = k; });
    if (innerHeight + scrollY >= document.documentElement.scrollHeight - 2) i = heads.length - 1;
    if (i === current) return;
    if (current >= 0) links[current].classList.remove("on");
    links[i].classList.add("on");
    current = i;
    if (record[heads[i].id] !== hashes[i]) {
      record[heads[i].id] = hashes[i];
      write(SEEN_KEY, record);
    }
    const a = links[i];
    if (a.offsetTop < panel.scrollTop || a.offsetTop + a.offsetHeight > panel.scrollTop + panel.clientHeight)
      panel.scrollTop = a.offsetTop - panel.clientHeight / 2;
  };

  const pin = document.createElement("div");
  const pinRoot = pin.attachShadow({mode: "closed"});
  pinRoot.innerHTML = `<style>
:host{position:fixed;z-index:2147483645;display:none}
button{font:600 13px/1 system-ui,sans-serif;padding:4px 7px;border-radius:6px;cursor:pointer;
  border:1px solid #45475a;background:#313244;color:#a6adc8}
button:hover{color:#cdd6f4}
@media print{:host{display:none!important}}
</style><button type="button" aria-label="Copy a link to this section">#</button>`;
  const copy = pinRoot.querySelector("button");
  let pinned = -1, hideTimer = 0;
  const hidePin = () => { pin.style.display = "none"; pinned = -1; };
  const showPin = i => {
    clearTimeout(hideTimer);
    if (i === pinned) return;
    const r = document.createRange();
    r.selectNodeContents(heads[i]);
    const text = r.getBoundingClientRect(), line = heads[i].getBoundingClientRect();
    pin.style.left = Math.min(text.right + 8, innerWidth - 40) + "px";
    pin.style.top = line.top + (line.height - 24) / 2 + "px";
    pin.style.display = "block";
    copy.textContent = "#";
    copy.title = "Copy a link to this section";
    pinned = i;
  };
  document.addEventListener("mouseover", e => {
    const i = heads.indexOf(e.target.closest && e.target.closest("h2, h3"));
    if (i >= 0) showPin(i);
  });
  document.addEventListener("mouseout", e => {
    if (heads.includes(e.target.closest && e.target.closest("h2, h3")))
      hideTimer = setTimeout(hidePin, 400);
  });
  pin.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  pin.addEventListener("mouseleave", () => { hideTimer = setTimeout(hidePin, 400); });
  copy.addEventListener("click", async () => {
    const i = pinned;
    if (i < 0) return;
    const url = location.origin + location.pathname + "#" + heads[i].id;
    try {
      await navigator.clipboard.writeText(url);
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Copy failed";
      copy.title = url;
    }
    history.replaceState(null, "", "#" + heads[i].id);
  });

  addEventListener("scroll", () => {
    hidePin();
    if (!queued) { queued = true; requestAnimationFrame(mark); }
  }, {passive: true});
  addEventListener("resize", fit);

  document.body.append(box, pin);
  fit();
  mark();
})();
"""
