"""Render a pilot proposal (scripts/pilot_structure.py) as a standalone page.

    python scripts/pilot_view.py linear-representation-hypothesis   -> data/pilot/<slug>.html
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PILOT = ROOT / "data" / "pilot"

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ claim layers</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,500;8..60,600&display=swap" rel="stylesheet">
<style>
:root {
  /* layout: strata read bottom-up like a building: assumptions at the base, claims on them, experiments on top */
  --bg: #f4f5f7; --panel: #ffffff; --ink: #1c2330; --muted: #5d6779; --line: #d9dde5;
  --accent: #2f4fa8; --accent-soft: #e4e9f7;
  --supports: #2e7d5b; --contradicts: #b8433c; --qualifies: #b07a1a; --applies: #8a94a8;
  --display: "Source Serif 4", Georgia, serif; --body: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #12161d; --panel: #1b212b; --ink: #e6e9ef; --muted: #9aa4b6; --line: #2e3644;
  --accent: #8ea7f0; --accent-soft: #232d47;
  --supports: #5bbf93; --contradicts: #e2776f; --qualifies: #d9a94a; --applies: #6f7a8f; color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #12161d; --panel: #1b212b; --ink: #e6e9ef; --muted: #9aa4b6; --line: #2e3644;
  --accent: #8ea7f0; --accent-soft: #232d47;
  --supports: #5bbf93; --contradicts: #e2776f; --qualifies: #d9a94a; --applies: #6f7a8f; color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 var(--body); }
main { max-width: 1180px; margin: 0 auto; padding: 28px 20px 60px; display: grid; gap: 30px; }
h1 { font: 600 30px/1.15 var(--display); margin: 0 0 6px; text-wrap: balance; }
h2 { font: 600 20px/1.2 var(--display); margin: 0; }
p { margin: 0; } .lede { color: var(--muted); max-width: 68ch; }
.eyebrow { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
.stats { display: flex; flex-wrap: wrap; gap: 8px 22px; color: var(--muted); font-variant-numeric: tabular-nums; margin-top: 12px; }
.stats b { color: var(--ink); font-weight: 600; }
section { display: grid; gap: 14px; }
.head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; }
.head p { color: var(--muted); font-size: 14px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; }
button.chip { font: 500 13px var(--body); color: var(--ink); background: var(--panel); border: 1px solid var(--line);
  border-radius: 999px; padding: 5px 12px; cursor: pointer; }
button.chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--bg); }
button:focus-visible, .thesis:focus-visible, .paradigm:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.theses { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.thesis { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; display: grid; gap: 9px;
  align-content: start; cursor: pointer; text-align: left; font: inherit; color: inherit; }
.thesis.dim, .paradigm.dim { opacity: .28; }
.thesis.open { border-color: var(--accent); grid-column: 1 / -1; cursor: default; }
.thesis .st { font-weight: 500; }
.bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: var(--line); }
.bar i { display: block; } .supports { background: var(--supports); } .contradicts { background: var(--contradicts); }
.qualifies { background: var(--qualifies); } .applies { background: var(--applies); }
.meta { display: flex; flex-wrap: wrap; gap: 6px 10px; font-size: 12.5px; color: var(--muted); font-variant-numeric: tabular-nums; align-items: center; }
.tag { border-radius: 4px; padding: 1px 7px; font-weight: 600; font-size: 12px; border: 1px solid currentColor; }
.tag.shaky { color: var(--qualifies); } .tag.contested { color: var(--contradicts); } .tag.new { color: var(--accent); }
.p { background: var(--accent-soft); color: var(--accent); border-radius: 4px; padding: 1px 6px; font-weight: 600; font-size: 12px; }
.detail { display: grid; gap: 14px; border-top: 1px solid var(--line); padding-top: 12px; }
.detail h3 { font: 600 13px var(--body); margin: 0 0 4px; display: flex; gap: 8px; align-items: center; }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.detail ul { margin: 0; padding-left: 18px; display: grid; gap: 5px; max-width: 95ch; font-size: 14px; }
.detail .needs { color: var(--muted); font-size: 14px; }
.paradigms { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
.paradigm { background: var(--accent-soft); border: 1px solid var(--line); border-bottom: 4px solid var(--accent); border-radius: 8px;
  padding: 12px 14px; cursor: pointer; text-align: left; font: inherit; color: inherit; display: grid; gap: 8px; align-content: start; }
.paradigm.on { border-color: var(--accent); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13px; color: var(--muted); }
.legend span { display: inline-flex; gap: 6px; align-items: center; }
.hood { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; align-items: start; }
.col { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; display: grid; gap: 8px; }
.col.focus { border-color: var(--accent); }
.col h3 { font: 600 14px var(--body); margin: 0; } .col small { color: var(--muted); }
.col ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 7px; font-size: 14px; }
.col li span { color: var(--muted); font-size: 12.5px; display: block; }
details { color: var(--muted); font-size: 14px; } details ul { max-width: 95ch; display: grid; gap: 5px; }
</style>
</head>
<body>
<main>
<header>
  <p class="eyebrow">Pilot proposal, nothing applied to the map</p>
  <h1 id="title"></h1>
  <p class="lede" id="desc"></p>
  <div class="stats" id="stats"></div>
</header>

<section>
  <div class="head"><h2>Broad claims</h2><p>Each bar shows what the single-experiment findings under it do. Select a claim to read them.</p></div>
  <div class="legend">
    <span><i class="dot supports"></i>supports</span><span><i class="dot contradicts"></i>contradicts</span>
    <span><i class="dot qualifies"></i>qualifies</span><span><i class="dot applies"></i>relies on it without testing</span>
  </div>
  <div class="filters" id="filters"></div>
  <div class="theses" id="theses"></div>
</section>

<section>
  <div class="head"><h2>Paradigms underneath</h2><p>Assumptions the broad claims take for granted. Select one to see what rests on it.</p></div>
  <div class="paradigms" id="paradigms"></div>
</section>

<section>
  <div class="head"><h2>Concept neighbourhood</h2><p>What this idea is built on, what it contains, and what has been built on it.</p></div>
  <div class="hood" id="hood"></div>
</section>

<section>
  <details><summary id="loose-sum"></summary><ul id="loose"></ul></details>
  <details><summary id="dup-sum"></summary><ul id="dups"></ul></details>
</section>
</main>
<script>
const D = __DATA__;
const S = D.structure, C = D.claims, ORDER = ["supports", "contradicts", "qualifies", "applies"];
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.textContent = h; return e; };
const theses = S.theses.map(t => {
  const f = S.findings.filter(x => x.thesis === t.id);
  const n = Object.fromEntries(ORDER.map(k => [k, f.filter(x => x.stance === k).length]));
  const tests = n.supports + n.contradicts + n.qualifies;
  return {...t, f, n, tests, shaky: n.applies > tests, contested: n.contradicts > 0, isNew: !t.from};
});
let paradigm = null, filter = "all", open = null;

document.getElementById("title").textContent = D.focus.name;
document.getElementById("desc").textContent = D.focus.description || "";
const stats = document.getElementById("stats");
[[Object.keys(C).length, "claims today, in one flat list"], [S.paradigms.length, "paradigms"], [theses.length, "broad claims"],
 [S.findings.length, "single-experiment findings"], [theses.filter(t => t.shaky).length, "broad claims relied on more than tested"]]
  .forEach(([n, l]) => { const s = el("span"); s.append(el("b", "", n), " " + l); stats.append(s); });

const FILTERS = [["all", "All"], ["contested", "Has contradicting findings"], ["shaky", "Relied on more than tested"], ["new", "Newly written"]];
function drawFilters() {
  const box = document.getElementById("filters"); box.replaceChildren();
  FILTERS.forEach(([k, l]) => { const b = el("button", "chip", l); b.type = "button"; b.setAttribute("aria-pressed", filter === k);
    b.onclick = () => { filter = k; draw(); }; box.append(b); });
}
function drawTheses() {
  const box = document.getElementById("theses"); box.replaceChildren();
  theses.filter(t => filter === "all" || (filter === "new" ? t.isNew : t[filter])).forEach(t => {
    const isOpen = open === t.id;
    const card = el(isOpen ? "div" : "button", "thesis" + (isOpen ? " open" : "") + (paradigm && !(t.rests_on || []).includes(paradigm) ? " dim" : ""));
    if (!isOpen) { card.type = "button"; card.onclick = () => { open = t.id; draw(); }; }
    card.append(el("div", "st", t.statement));
    const bar = el("div", "bar"), total = t.f.length || 1;
    ORDER.forEach(k => { if (t.n[k]) { const i = el("i", k); i.style.width = (100 * t.n[k] / total) + "%"; bar.append(i); } });
    card.append(bar);
    const meta = el("div", "meta");
    meta.append(el("span", "", `${t.n.supports} support · ${t.n.contradicts} contradict · ${t.n.qualifies} qualify · ${t.n.applies} rely`));
    (t.rests_on || []).forEach(p => meta.append(el("span", "p", p)));
    if (t.contested) meta.append(el("span", "tag contested", "contested"));
    if (t.shaky) meta.append(el("span", "tag shaky", "under-tested"));
    if (t.isNew) meta.append(el("span", "tag new", "new wording"));
    card.append(meta);
    if (isOpen) {
      const d = el("div", "detail");
      const needs = (t.depends_on || []).map(id => theses.find(x => x.id === id)).filter(Boolean);
      if (needs.length) d.append(el("p", "needs", "Only makes sense if: " + needs.map(x => x.statement).join(" / ")));
      ORDER.forEach(k => { const fs = t.f.filter(x => x.stance === k); if (!fs.length) return;
        const g = el("div"), h = el("h3"); h.append(el("i", "dot " + k), k === "applies" ? "Relies on it without testing" : k[0].toUpperCase() + k.slice(1));
        const ul = el("ul"); fs.forEach(x => ul.append(el("li", "", C[x.id].statement))); g.append(h, ul); d.append(g); });
      if (!t.f.length) d.append(el("p", "needs", "No single-experiment findings were filed under this claim."));
      const close = el("button", "chip", "Close"); close.type = "button"; close.onclick = () => { open = null; draw(); };
      const row = el("div"); row.append(close); d.append(row); card.append(d);
    }
    box.append(card);
  });
}
function drawParadigms() {
  const box = document.getElementById("paradigms"); box.replaceChildren();
  S.paradigms.forEach(p => {
    const on = theses.filter(t => (t.rests_on || []).includes(p.id));
    const b = el("button", "paradigm" + (paradigm === p.id ? " on" : "") + (paradigm && paradigm !== p.id ? " dim" : "")); b.type = "button";
    b.setAttribute("aria-pressed", paradigm === p.id);
    const m = el("div", "meta"); m.append(el("span", "p", p.id), el("span", "", `${on.length} broad claims rest on this · ${on.filter(t => t.contested).length} of them contested`));
    b.append(el("div", "", p.statement), m);
    b.onclick = () => { paradigm = paradigm === p.id ? null : p.id; draw(); }; box.append(b);
  });
}
function drawHood() {
  const box = document.getElementById("hood"); box.replaceChildren();
  const by = g => D.neighbourhood.concepts.filter(x => x.group === g).map(x => ({...D.concepts[x.id], why: x.why})).sort((a, b) => b.claims - a.claims);
  [["Built on", "what it needs to make sense", by("foundation").concat(by("tool")), ""],
   ["Inside " + D.focus.name, "parts and special cases", by("part"), " focus"],
   ["Built on top of it", "extensions and challenges", by("extension"), ""],
   ["Beside it", "related ideas at a similar level", by("related"), ""]].forEach(([h, sub, items, cls]) => {
    const c = el("div", "col" + cls), hh = el("h3", "", h + " "); hh.append(el("small", "", sub)); c.append(hh);
    const ul = el("ul"); items.forEach(k => { const li = el("li", "", k.name); li.append(el("span", "", `${k.claims} claims · ${k.why}`)); ul.append(li); });
    c.append(ul); box.append(c);
  });
}
function draw() { drawFilters(); drawTheses(); drawParadigms(); }
const loose = S.findings.filter(f => !f.thesis);
document.getElementById("loose-sum").textContent = `${loose.length} findings fit no broad claim`;
loose.forEach(f => document.getElementById("loose").append(el("li", "", C[f.id].statement)));
document.getElementById("dup-sum").textContent = `${(S.duplicates || []).length} claims flagged as duplicates`;
(S.duplicates || []).forEach(x => document.getElementById("dups").append(el("li", "", C[x.id].statement + "  =  " + ((C[x.same_as] || {}).statement || x.same_as))));
draw(); drawHood();
</script>
</body>
</html>
"""


def main():
    slug = sys.argv[1]
    d = json.loads((PILOT / f"{slug}.json").read_text(encoding="utf-8"))
    for c in d["claims"].values():   # the page only needs statements
        for k in ("scope", "slug", "relations", "claim_type"):
            c.pop(k, None)
    data = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    (PILOT / f"{slug}.html").write_text(PAGE.replace("__TITLE__", d["focus"]["name"]).replace("__DATA__", data), encoding="utf-8")
    print(PILOT / f"{slug}.html")


if __name__ == "__main__":
    main()
