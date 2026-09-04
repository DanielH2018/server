"""The single-page GUI artifact_server.py serves at `/`.

One raw string, no Python logic. It is a module rather than a `.html` file so that the pod
needs only the ConfigMap keys it already mounts — a separate asset would need its own key,
its own mount and its own read at request time, for a page that never changes between
deploys.

Split out of artifact_server.py so the server module reads as routing and indexing rather
than as 160 lines of CSS and JavaScript. Nothing imports back the other way.
"""

GUI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Homelab Artifacts</title>
<style>
:root{--base:#1e1e2e;--mantle:#181825;--crust:#11111b;--surface0:#313244;--surface1:#45475a;
--text:#cdd6f4;--subtext0:#a6adc8;--overlay0:#6c7086;--blue:#89b4fa;--green:#a6e3a1;
--yellow:#f9e2af;--mauve:#cba6f7;--red:#f38ba8}
*{box-sizing:border-box}
body{margin:0;background:var(--base);color:var(--text);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;background:var(--mantle);border-bottom:1px solid var(--surface1);
padding:16px 24px;z-index:5}
h1{margin:0 0 12px;font-size:18px;letter-spacing:.3px}
h1 span{color:var(--overlay0);font-weight:400;font-size:13px;margin-left:8px}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=search]{flex:1 1 320px;min-width:220px;background:var(--crust);color:var(--text);
border:1px solid var(--surface1);border-radius:6px;padding:9px 12px;font-size:14px}
input[type=search]:focus{outline:none;border-color:var(--blue)}
select,label.chk{background:var(--crust);color:var(--subtext0);border:1px solid var(--surface1);
border-radius:6px;padding:8px 10px;font-size:13px}
label.chk{display:flex;align-items:center;gap:6px;cursor:pointer}
main{padding:20px 24px 60px;max-width:1100px;margin:0 auto}
.card{display:block;background:var(--surface0);border:1px solid var(--surface1);border-left:3px solid var(--blue);
border-radius:8px;padding:14px 16px;margin-bottom:10px;text-decoration:none;color:inherit}
.card:hover{border-color:var(--blue);background:#3a3b52}
.card h2{margin:0 0 4px;font-size:15px;font-weight:600;color:var(--blue)}
.card p{margin:0 0 8px;color:var(--subtext0);font-size:13px}
.meta{color:var(--overlay0);font-size:12px;display:flex;gap:14px;flex-wrap:wrap}
.chip{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600;
color:var(--crust)}
.chip-host{background:var(--mauve)}.chip-done{background:var(--green)}
.chip-active{background:var(--yellow)}.chip-planned{background:var(--overlay0)}
.chip-cat{background:var(--blue)}.chip-svc{background:var(--surface1);color:var(--text)}
.chip-tag{background:transparent;color:var(--subtext0);border:1px solid var(--surface1)}
/* Derived, not declared: hollow rather than filled, so a guess never reads as a statement. */
.chip-derived{background:transparent;border:1px dashed var(--overlay0);color:var(--subtext0)}
.tagrow{margin-top:8px}
mark{background:var(--yellow);color:var(--crust);border-radius:2px}
.empty{color:var(--overlay0);padding:40px 0;text-align:center}
footer{color:var(--overlay0);font-size:12px;padding:0 24px 30px;max-width:1100px;margin:0 auto}
</style></head>
<body>
<header>
  <h1>Homelab Artifacts <span id="count"></span></h1>
  <div class="controls">
    <input type="search" id="q" placeholder="Search titles, summaries and body text..." autofocus>
    <select id="host"><option value="">All hosts</option></select>
    <select id="category"><option value="">All categories</option></select>
    <select id="status"><option value="">Any status</option></select>
    <select id="sort">
      <option value="mtime">Newest first</option>
      <option value="title">Title A-Z</option>
      <option value="host">Host</option>
    </select>
    <label class="chk"><input type="checkbox" id="dupes"> show .md duplicates</label>
  </div>
</header>
<main><div id="list"></div></main>
<footer id="foot"></footer>
<script>
let DATA = {artifacts: [], hosts: [], generated: ""};
const $ = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
function fmtSize(n) {
  return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB"
                                           : (n / 1048576).toFixed(1) + " MB";
}
// Every whitespace-separated term must appear somewhere in the haystack (AND, not phrase),
// so "drift box" finds the daniel-box drift audit without caring about word order.
// Metadata joins the haystack rather than getting its own syntax: typing "longhorn backup"
// finds the artifact whether those words are its tags, its category, or just its prose.
function matches(a, terms) {
  if (!terms.length) return true;
  const hay = [a.title, a.summary, a.name, a.host, a.text, a.category, a.status,
               (a.services || []).join(" "), (a.tags || []).join(" ")]
    .join(" ").toLowerCase();
  return terms.every(t => hay.includes(t));
}
function highlight(text, terms) {
  let out = esc(text);
  for (const t of terms) {
    if (!t) continue;
    out = out.replace(new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi"),
                      "<mark>$1</mark>");
  }
  return out;
}
function render() {
  const terms = $("q").value.toLowerCase().split(/\s+/).filter(Boolean);
  const host = $("host").value;
  const category = $("category").value;
  const status = $("status").value;
  const sort = $("sort").value;
  const dupes = $("dupes").checked;
  let items = DATA.artifacts.filter(a =>
    (!host || a.host === host) && (!category || a.category === category) &&
    (!status || a.status === status) &&
    (dupes || !a.companion_html) && matches(a, terms));
  items.sort((x, y) => sort === "title" ? x.title.localeCompare(y.title)
           : sort === "host" ? (x.host + x.title).localeCompare(y.host + y.title)
           : y.mtime.localeCompare(x.mtime));
  $("count").textContent = items.length + " of " + DATA.artifacts.length + " artifacts";
  $("list").innerHTML = items.length ? items.map(a => {
    const slices = a.slices ? ["done", "active", "planned"].filter(k => a.slices[k])
      .map(k => `<span class="chip chip-${k}">${a.slices[k]} ${k}</span>`).join(" ") : "";
    // A derived value is marked, never rendered as if the document had stated it — a guessed
    // category that looks identical to a declared one makes the heuristic authoritative.
    const der = (f) => (a.source || {})[f] === "derived" ? " chip-derived" : "";
    const derTitle = (f) => (a.source || {})[f] === "derived"
      ? ' title="inferred from the document, not declared by it"' : "";
    const cat = a.category
      ? `<span class="chip chip-cat${der("category")}"${derTitle("category")}>${esc(a.category)}</span>` : "";
    const stat = a.status
      ? `<span class="chip chip-${a.status}${der("status")}"${derTitle("status")}>${esc(a.status)}</span>` : "";
    const svcs = (a.services || []).map(s =>
      `<span class="chip chip-svc${der("services")}"${derTitle("services")}>${esc(s)}</span>`).join(" ");
    const tags = (a.tags || []).map(t => `<span class="chip chip-tag">${esc(t)}</span>`).join(" ");
    return `<a class="card" href="${esc(a.url)}" target="_blank" rel="noopener">
      <h2>${highlight(a.title, terms)}</h2>
      ${a.summary ? `<p>${highlight(a.summary, terms)}</p>` : ""}
      <div class="meta">
        <span class="chip chip-host">${esc(a.host)}</span>
        ${cat}${stat}
        <span>${esc(a.name)}</span>
        <span>${fmtDate(a.updated || a.mtime)}</span>
        <span>${fmtSize(a.size)}</span>
        ${slices}
      </div>
      ${svcs || tags ? `<div class="meta tagrow">${svcs} ${tags}</div>` : ""}</a>`;
  }).join("") : `<div class="empty">No artifact matches that search.</div>`;
}
async function load() {
  const res = await fetch("/api/index.json", {cache: "no-store"});
  DATA = await res.json();
  const fill = (id, label, values) => {
    $(id).innerHTML = `<option value="">${label}</option>` +
      (values || []).map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  };
  fill("host", "All hosts", DATA.hosts);
  fill("category", "All categories", DATA.categories);
  fill("status", "Any status", DATA.statuses);
  $("foot").textContent = "Indexed " + fmtDate(DATA.generated) +
    " - artifacts are pruned 7 days after their last update.";
  render();
}
["q", "host", "category", "status", "sort", "dupes"].forEach(
  id => $(id).addEventListener("input", render));
load();
</script>
</body></html>
"""
