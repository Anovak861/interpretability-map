"use strict";

/* ================================================================ basics */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;
const extractionLabel = (provenance) => {
  const match = /^codex-cli:([^: ]+):reasoning=([^ ]+)/.exec(provenance || "");
  if (!match) return provenance?.startsWith("codex-cli:") ? "Processed by Codex" : provenance;
  if (match[1] === "gpt-6.1-sol" && match[2] === "unspecified") return "6.1 sol no reasoning";
  const model = match[1] === "gpt-6.1-sol" ? "6.1 sol" : match[1];
  return `${model} ${match[2] === "none" ? "no reasoning" : `${match[2]} reasoning`}`;
};
const extractionTooltip = (provenance) => provenance?.includes("reasoning=unspecified")
  ? `${provenance}. Historical label: no explicit reasoning setting was supplied; this does not mean zero reasoning tokens.`
  : provenance;
const app = $("#app");

const STATUSES = ["replicated", "supported", "single_source", "untested", "contested", "refuted"];
const STATUS_LABEL = {
  replicated: "Replicated", supported: "Supported", single_source: "Single source",
  untested: "Untested", contested: "Contested", refuted: "Refuted",
};
const STATUS_HELP = {
  replicated: "Positive evidence from at least two independent groups (no shared authors).",
  supported: "Several positive results, all from one group.",
  single_source: "One positive result so far.",
  untested: "Proposed or assumed, but never directly tested.",
  contested: "Positive and negative evidence both exist, or only mixed results.",
  refuted: "Only negative evidence.",
};
const RELATION_ORDER = ["proposes", "supports", "replicates", "refutes", "fails_to_replicate", "mixed",
  "extends", "critiques", "assumes"];
const RELATION_LABEL = { fails_to_replicate: "fails to replicate" };
const NOTE_KINDS = ["note", "idea", "critique", "question", "next_step", "dead_end"];
const NOTE_LABEL = { next_step: "next step", dead_end: "dead end" };
const SOURCE_LABEL = {
  arxiv: "arXiv", transformer_circuits: "Transformer Circuits", lesswrong: "LessWrong",
  alignmentforum: "Alignment Forum", twitter: "X / Twitter", web: "Web", manual: "Manual",
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 1800);
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const year = (d) => (d || "").slice(0, 4);
const fmtDate = (d) => (d ? d.slice(0, 10) : "");
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const authorsShort = (a, n = 3) => !a || !a.length ? "" : a.length <= n ? a.join(", ") : `${a.slice(0, n).join(", ")} +${a.length - n}`;

/* ================================================================ components */

const pill = (s) => `<span class="pill st-${s}" title="${esc(STATUS_HELP[s])}">${STATUS_LABEL[s] || s}</span>`;
const rel = (r) => `<span class="rel rel-${r}">${esc(RELATION_LABEL[r] || r)}</span>`;
const src = (s) => `<span class="src src-${s}">${esc(SOURCE_LABEL[s] || s)}</span>`;
const noteKind = (k) => `<span class="nk nk-${k}">${esc(NOTE_LABEL[k] || k)}</span>`;
const conceptChip = (c, primary) =>
  `<a class="chip${primary ? " primary" : ""}" href="#/concept/${enc(c.slug || c)}">${esc(c.name || c)}</a>`;

function mixbar(statuses, big = false) {
  const total = STATUSES.reduce((n, s) => n + (statuses[s] || 0), 0);
  if (!total) return `<div class="mixbar${big ? " big" : ""}"></div>`;
  const parts = STATUSES.filter((s) => statuses[s]).map((s) =>
    `<span class="bg-${s}" style="width:${(statuses[s] / total) * 100}%" title="${STATUS_LABEL[s]}: ${statuses[s]}"></span>`);
  return `<div class="mixbar${big ? " big" : ""}" role="img" aria-label="${STATUSES.filter((s) => statuses[s]).map((s) => `${STATUS_LABEL[s]} ${statuses[s]}`).join(", ")}">${parts.join("")}</div>`;
}

function legend(statuses) {
  return `<div class="legend">${STATUSES.map((s) =>
    `<span title="${esc(STATUS_HELP[s])}"><i class="bg-${s}"></i>${STATUS_LABEL[s]}<b>${statuses ? statuses[s] || 0 : ""}</b></span>`).join("")}</div>`;
}

function evidenceLine(c) {
  const bits = [];
  if (c.positive) bits.push(`<span class="st-replicated">+${c.positive}</span>`);
  if (c.negative) bits.push(`<span class="st-refuted">−${c.negative}</span>`);
  if (c.mixed) bits.push(`<span class="st-contested">~${c.mixed}</span>`);
  if (!bits.length) bits.push(`<span class="faint">no tests</span>`);
  const groups = c.groups > 1 ? ` · ${c.groups} groups` : "";
  return `${bits.join(" ")}<span class="faint">${groups}</span>`;
}

function notesPanel(targetType, slug, notes) {
  const list = notes.length
    ? notes.map((n) => `
      <div class="note">
        <div class="head">${noteKind(n.kind)}<span>${esc(n.author)} · ${fmtDate(n.created_at)}</span>
          <button class="del" data-del-note="${n.id}" title="Delete note">Delete</button></div>
        <div class="body">${esc(n.body)}</div>
      </div>`).join("")
    : `<div class="empty">No notes yet.</div>`;
  return `
    <div class="card">
      <h2>Notes <span class="count">${notes.length || ""}</span></h2>
      ${list}
      <form class="note-form" data-note-form data-target-type="${targetType}" data-slug="${esc(slug)}">
        <textarea name="body" placeholder="Add a note, idea, critique or next step…" aria-label="Note"></textarea>
        <div class="row">
          <select name="kind" aria-label="Note type">${NOTE_KINDS.map((k) => `<option value="${k}">${NOTE_LABEL[k] || k}</option>`).join("")}</select>
          <button class="btn primary" type="submit">Save note</button>
        </div>
      </form>
    </div>`;
}

/* ================================================================ router */

const routes = {
  "": renderOverview,
  concepts: renderConcepts,
  concept: renderConcept,
  claims: renderClaims,
  claim: renderClaim,
  documents: renderDocuments,
  document: renderDocument,
  lineage: renderLineage,
  notes: renderNotes,
  review: renderReview, // curate.js
  reading: renderReading, // graphs.js
};
const navFor = { "": "overview", concept: "concepts", claim: "claims", document: "documents", reading: "lineage" };
let cyInstances = [];

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const [route, ...rest] = path.split("/");
  return { route: route || "", slug: rest.length ? decodeURIComponent(rest.join("/")) : null,
           params: new URLSearchParams(query) };
}

async function render({ keepScroll = false } = {}) {
  const { route, slug, params } = parseHash();
  const fn = routes[route] || renderOverview;
  const nav = navFor[route] ?? route;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === nav));
  cyInstances.forEach((cy) => cy.destroy());
  cyInstances = [];
  const scroll = window.scrollY;
  try {
    await fn(slug, params);
  } catch (e) {
    app.className = "";
    app.innerHTML = `<div class="err">Couldn't load this page: ${esc(e.message)}</div>`;
  }
  window.scrollTo(0, keepScroll ? scroll : 0);
}
window.addEventListener("hashchange", () => render());

/* ================================================================ overview */

async function renderOverview() {
  const d = await api("/api/overview");
  app.className = "";
  const c = d.counts;
  const totalClaims = STATUSES.reduce((n, s) => n + (d.status_mix[s] || 0), 0);
  const solid = (d.status_mix.replicated || 0) + (d.status_mix.supported || 0);
  const run = d.last_run;
  const runLine = run
    ? `Last update ${fmtDate(run.started_at)}: ${(run.extraction || {}).extracted || 0} documents extracted${run.stopped ? ` · <span class="st-contested">stopped early</span>` : ""}`
    : "No automated update has run yet.";

  const concepts = d.concepts.slice().sort((a, b) => b.claims - a.claims);
  app.innerHTML = `
    <div class="page-head">
      <div><h1>Field overview</h1>
        <p>How well-supported the mapped claims are, where the foundations are weak, and what's been added recently. ${runLine}</p></div>
    </div>
    <div class="stats">
      <a class="stat" href="#/documents"><div class="n">${c.documents}</div><div class="l">Documents</div></a>
      <a class="stat" href="#/claims"><div class="n">${c.claims}</div><div class="l">Claims</div></a>
      <a class="stat" href="#/concepts"><div class="n">${c.concepts}</div><div class="l">Concepts</div></a>
      <a class="stat" href="#/notes"><div class="n">${c.annotations}</div><div class="l">Notes</div></a>
      <a class="stat" href="#/documents?review=extracted"><div class="n">${c.unreviewed}</div><div class="l">Awaiting review</div></a>
      <div class="stat" title="Works cited by ingested documents but not ingested themselves"><div class="n">${c.stubs}</div><div class="l">Cited, not yet ingested</div></div>
    </div>

    <div class="card">
      <h2>Claim status across the field <span class="hint">${totalClaims ? Math.round((solid / totalClaims) * 100) : 0}% replicated or supported</span></h2>
      ${mixbar(d.status_mix, true)}
      ${legend(d.status_mix)}
    </div>

    <div class="grid cols-main-side section-gap">
      <div class="card">
        <h2>Health by concept <span class="hint">claims include sub-concepts</span></h2>
        <table class="list">
          <thead><tr><th>Concept</th><th class="num hide-sm">Docs</th><th class="num">Claims</th><th>Status mix</th><th class="num hide-sm">Solid</th><th class="num" title="Claims that other work relies on but are weakly tested">Fragile</th></tr></thead>
          <tbody>${concepts.map((k) => `
            <tr class="clickable" data-href="#/concept/${enc(k.slug)}">
              <td><a href="#/concept/${enc(k.slug)}">${esc(k.name)}</a></td>
              <td class="num hide-sm">${k.documents}</td>
              <td class="num">${k.claims}</td>
              <td style="width:28%">${mixbar(k.statuses)}</td>
              <td class="num hide-sm">${k.solid_share == null ? "–" : Math.round(k.solid_share * 100) + "%"}</td>
              <td class="num">${k.fragile ? `<span class="tag-flag">${k.fragile}</span>` : `<span class="faint">0</span>`}</td>
            </tr>`).join("")}</tbody>
        </table>
      </div>

      <div class="grid">
        <div class="card">
          <h2>Fragile foundations <span class="count">${d.fragile.length}</span></h2>
          <p class="small muted" style="margin:-6px 0 8px">Claims other work leans on that are untested, single-source or contested.</p>
          ${d.fragile.length ? d.fragile.map((f) => `
            <div class="note">
              <div class="head">${pill(f.status)}<span>relied on by ${f.assumed_by} docs · ${f.dependents} claims</span></div>
              <div class="target"><a href="#/claim/${enc(f.slug)}" class="statement" style="font-size:14px;color:var(--text)">${esc(f.statement)}</a></div>
            </div>`).join("") : `<div class="empty">None right now.</div>`}
        </div>
        <div class="card">
          <h2>Recent notes</h2>
          ${d.recent_notes.length ? d.recent_notes.map(noteWithTarget).join("") : `<div class="empty">No notes yet.</div>`}
        </div>
        <div class="card">
          <h2>Recently added</h2>
          <ul class="link-list">${d.recent_documents.map((x) => `
            <li><span class="date">${year(x.published_date)}</span>
              <span class="lbl"><a href="#/document/${enc(x.slug)}">${esc(x.title)}</a></span></li>`).join("")}</ul>
        </div>
      </div>
    </div>`;
}

function noteWithTarget(n) {
  return `
    <div class="note">
      <div class="head">${noteKind(n.kind)}<span>${fmtDate(n.created_at)}</span></div>
      <div class="target"><a href="#/${n.target_type}/${enc(n.target_slug)}">${esc((n.target_label || "").slice(0, 110))}</a></div>
      <div class="body">${esc(n.body)}</div>
    </div>`;
}

/* ================================================================ concepts */

let conceptView = "outline";

async function renderConcepts() {
  const d = await api("/api/concepts");
  app.className = conceptView === "graph" ? "wide" : "";
  app.innerHTML = `
    <div class="page-head">
      <div><h1>Concepts</h1>
        <p>Concepts form a graph rather than a tree: a concept can build on several foundations. Health counts include each concept's sub-concepts.</p></div>
      <div class="actions"><div class="seg" role="tablist">
        <button data-view="outline" class="${conceptView === "outline" ? "on" : ""}">Outline</button>
        <button data-view="graph" class="${conceptView === "graph" ? "on" : ""}">Graph</button>
      </div></div>
    </div>
    <div id="concept-body"></div>`;
  $$(".seg button", app).forEach((b) => b.addEventListener("click", () => { conceptView = b.dataset.view; renderConcepts(); }));
  if (conceptView === "graph") conceptGraph(d);
  else conceptOutline(d);
}

function conceptOutline(d) {
  const bySlug = Object.fromEntries(d.nodes.map((n) => [n.slug, n]));
  const children = {}, parents = {};
  d.edges.forEach((e) => {
    (children[e.parent] ||= []).push(e.child);
    (parents[e.child] ||= []).push(e.parent);
  });
  const weight = (s) => (bySlug[s].claims || 0) * 100 + (bySlug[s].documents || 0);
  const roots = d.nodes.filter((n) => !parents[n.slug]).map((n) => n.slug).sort((a, b) => weight(b) - weight(a));
  const placed = new Set();

  const row = (slug, depth, via = null) => {
    const n = bySlug[slug];
    const kids = (children[slug] || []).slice().sort((a, b) => weight(b) - weight(a));
    const first = !placed.has(slug);
    placed.add(slug);
    const ps = parents[slug] || [];
    const others = ps.filter((p) => p !== via);
    const otherParents = via && others.length ? `<span class="also">also under ${others.map((p) => esc(bySlug[p].name)).join(", ")}</span>` : "";
    const head = `
      <div class="node">
        <div>${kids.length && first ? `<span class="twisty" data-twisty>▾</span>` : `<span class="twisty"></span>`}<a href="#/concept/${enc(slug)}" class="${depth === 0 ? "indent-0" : ""}">${esc(n.name)}</a><span class="kind">${esc(n.kind)}</span>${first ? otherParents : `<span class="also">↑ shown in full elsewhere</span>`}</div>
        <div class="num faint small hide-sm" style="text-align:right">${n.documents ? plural(n.documents, "doc") : ""}</div>
        <div class="hide-sm">${n.claims ? mixbar(n.statuses) : ""}</div>
        <div class="num small" style="text-align:right">${n.claims || ""}</div>
      </div>`;
    const sub = first && kids.length ? `<ul>${kids.map((k) => row(k, depth + 1, slug)).join("")}</ul>` : "";
    return `<li>${head}${sub}</li>`;
  };

  $("#concept-body").innerHTML = `
    <div class="card">
      <div class="outline-head"><div>Concept</div><div class="hide-sm" style="text-align:right">Docs</div><div class="hide-sm">Status mix</div><div style="text-align:right">Claims</div></div>
      <ul class="outline">${roots.map((r) => row(r, 0)).join("")}</ul>
      ${legend()}
    </div>`;
  $$("[data-twisty]").forEach((t) => t.addEventListener("click", () => {
    const ul = t.closest("li").querySelector(":scope > ul");
    const open = ul.style.display !== "none";
    ul.style.display = open ? "none" : "";
    t.textContent = open ? "▸" : "▾";
  }));
}

function mixColor(share) {
  // amber (weak) -> green (solid); grey when there are no claims
  if (share == null) return cssVar("--surface-2");
  const a = hexToRgb(cssVar("--st-single_source")), b = hexToRgb(cssVar("--st-replicated"));
  const m = (i) => Math.round(a[i] + (b[i] - a[i]) * share);
  return `rgb(${m(0)},${m(1)},${m(2)})`;
}
function hexToRgb(h) {
  const v = h.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16));
}

async function renderConcept(slug) {
  const k = await api(`/api/concepts/${enc(slug)}`);
  app.className = "";
  const h = k.health || {};
  let showAll = true;
  const claimsTable = () => {
    const rows = k.claims.filter((c) => showAll || c.direct)
      .sort((a, b) => STATUSES.indexOf(a.status) - STATUSES.indexOf(b.status) || b.load - a.load);
    return rows.length ? `<table class="list stack-sm"><tbody>${rows.map((c) => `
      <tr class="clickable" data-href="#/claim/${enc(c.slug)}">
        <td style="width:120px">${pill(c.status)}</td>
        <td class="claim-cell"><div class="statement" style="font-size:14px">${esc(c.statement)}</div>
          ${c.direct ? "" : `<div class="slug">via sub-concept: ${esc(c.concepts.join(", "))}</div>`}</td>
        <td class="num small">${evidenceLine(c)}${c.fragile ? `<div class="tag-flag">fragile</div>` : ""}</td>
      </tr>`).join("")}</tbody></table>` : `<div class="empty">No claims ${showAll ? "" : "directly "}tagged with this concept.</div>`;
  };
  app.innerHTML = `
    <div class="eyebrow">Concept · ${esc(k.kind)}</div>
    <h1 class="detail-title">${esc(k.name)}</h1>
    ${k.description ? `<p class="prose" style="max-width:70ch;margin:0 0 14px">${esc(k.description)}</p>` : ""}
    <div class="meta-row" style="margin-bottom:20px">
      ${k.parents.length ? `<span>Builds on</span><span class="chips">${k.parents.map((p) => conceptChip(p)).join("")}</span>` : `<span class="faint">Root concept</span>`}
      ${curateButtons()}
    </div>
    <div class="grid cols-main-side">
      <div class="grid">
        <div class="card">
          <h2>Health</h2>
          <div class="kv">
            <div><div class="n">${h.claims || 0}</div><div class="l">Claims</div></div>
            <div><div class="n">${h.documents || 0}</div><div class="l">Documents</div></div>
            <div><div class="n">${h.solid_share == null ? "–" : Math.round(h.solid_share * 100) + "%"}</div><div class="l">Replicated or supported</div></div>
            <div><div class="n">${h.fragile || 0}</div><div class="l">Fragile foundations</div></div>
          </div>
          <div style="margin-top:14px">${mixbar(h.statuses || {}, true)}</div>
          ${legend(h.statuses || {})}
        </div>
        <div class="card">
          <h2>Claims <span class="count">${k.claims.length}</span>
            <span class="hint"><label><input type="checkbox" id="incl" checked> include sub-concepts</label></span></h2>
          <div id="claims-table">${claimsTable()}</div>
        </div>
        <div class="card">
          <h2>Documents <span class="count">${k.documents.length}</span></h2>
          <ul class="link-list">${k.documents.slice().reverse().map((x) => `
            <li><span class="date">${year(x.published_date)}</span>
              <span class="lbl"><a href="#/document/${enc(x.slug)}">${esc(x.title)}</a>
              <div class="small muted">${esc(authorsShort(x.authors))}</div></span>
              <span class="tagline">${x.primary ? `<span class="chip primary">primary</span>` : x.direct ? "" : `<span class="faint small">sub-concept</span>`}</span></li>`).join("") || `<li class="empty">None yet.</li>`}</ul>
        </div>
      </div>
      <div class="grid">
        <div class="card">
          <h2>Sub-concepts <span class="count">${k.children.length}</span></h2>
          ${k.children.length ? `<div class="chips">${k.children.map((c) => conceptChip(c)).join("")}</div>` : `<div class="empty">None.</div>`}
        </div>
        <div class="card" id="dupes-slot"></div>
        ${notesPanel("concept", k.slug, k.notes)}
      </div>
    </div>`;
  $("#incl").addEventListener("change", (e) => { showAll = e.target.checked; $("#claims-table").innerHTML = claimsTable(); });
  wireConceptCuration(k);
}

/* ================================================================ claims */

const claimFilters = { statuses: new Set(), concept: "", q: "", fragile: false, sort: "load" };

async function renderClaims() {
  const [claims, concepts] = await Promise.all([api("/api/claims"), api("/api/concepts")]);
  app.className = "";
  const children = {};
  concepts.edges.forEach((e) => (children[e.parent] ||= []).push(e.child));
  const descendants = (slug) => {
    const out = new Set([slug]), stack = [slug];
    while (stack.length) (children[stack.pop()] || []).forEach((c) => { if (!out.has(c)) { out.add(c); stack.push(c); } });
    return out;
  };
  const counts = {};
  claims.forEach((c) => (counts[c.status] = (counts[c.status] || 0) + 1));
  const conceptOpts = concepts.nodes.filter((n) => n.claims).sort((a, b) => a.name.localeCompare(b.name));

  app.innerHTML = `
    <div class="page-head">
      <div><h1>Claims</h1><p>Each claim stands apart from any single document. Its status comes from the evidence attached to it.</p></div>
    </div>
    <div class="toolbar">
      <input type="search" id="cq" placeholder="Filter claims…" value="${esc(claimFilters.q)}">
      <select id="cconcept" aria-label="Concept"><option value="">All concepts</option>
        ${conceptOpts.map((n) => `<option value="${esc(n.slug)}" ${n.slug === claimFilters.concept ? "selected" : ""}>${esc(n.name)}</option>`).join("")}</select>
      <select id="csort" aria-label="Sort">
        ${[["load", "Most relied on"], ["evidence", "Most evidence"], ["status", "Status"], ["alpha", "A–Z"]].map(([v, l]) =>
          `<option value="${v}" ${claimFilters.sort === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      <label class="filter-chip ${claimFilters.fragile ? "on" : ""}" style="color:var(--st-contested)"><input type="checkbox" id="cfragile" ${claimFilters.fragile ? "checked" : ""} hidden>Fragile foundations only</label>
    </div>
    <div class="toolbar">${STATUSES.map((s) => `
      <button class="filter-chip st-${s} ${claimFilters.statuses.has(s) ? "on" : ""}" data-status="${s}" title="${esc(STATUS_HELP[s])}">
        ${STATUS_LABEL[s]} <span class="n">${counts[s] || 0}</span></button>`).join("")}
    </div>
    <div class="card" id="claims-list"></div>`;

  const draw = () => {
    const f = claimFilters;
    const within = f.concept ? descendants(f.concept) : null;
    const q = f.q.toLowerCase();
    let rows = claims.filter((c) =>
      (!f.statuses.size || f.statuses.has(c.status)) && (!f.fragile || c.fragile) &&
      (!within || c.concepts.some((k) => within.has(k))) &&
      (!q || c.statement.toLowerCase().includes(q) || c.slug.includes(q)));
    const ev = (c) => c.positive + c.negative + c.mixed;
    const sorters = {
      load: (a, b) => b.load - a.load || ev(b) - ev(a),
      evidence: (a, b) => ev(b) - ev(a) || b.load - a.load,
      status: (a, b) => STATUSES.indexOf(a.status) - STATUSES.indexOf(b.status) || b.load - a.load,
      alpha: (a, b) => a.statement.localeCompare(b.statement),
    };
    rows.sort(sorters[f.sort]);
    $("#claims-list").innerHTML = rows.length ? `
      <table class="list stack-sm">
        <thead><tr><th>Status</th><th>Claim</th><th class="num">Evidence</th><th class="num hide-sm" title="Documents that assume it + claims that depend on it">Relied on</th></tr></thead>
        <tbody>${rows.map((c) => `
          <tr class="clickable" data-href="#/claim/${enc(c.slug)}">
            <td style="width:120px">${pill(c.status)}</td>
            <td class="claim-cell"><div class="statement">${esc(c.statement)}</div><div class="slug">${esc(c.slug)} · ${esc(c.claim_type)}</div></td>
            <td class="num small">${evidenceLine(c)}</td>
            <td class="num hide-sm">${c.load ? `${c.load}${c.fragile ? ` <span class="tag-flag">fragile</span>` : ""}` : `<span class="faint">0</span>`}</td>
          </tr>`).join("")}</tbody>
      </table>` : `<div class="empty">No claims match these filters.</div>`;
  };
  draw();
  $("#cq").addEventListener("input", (e) => { claimFilters.q = e.target.value; draw(); });
  $("#cconcept").addEventListener("change", (e) => { claimFilters.concept = e.target.value; draw(); });
  $("#csort").addEventListener("change", (e) => { claimFilters.sort = e.target.value; draw(); });
  $("#cfragile").addEventListener("change", (e) => {
    claimFilters.fragile = e.target.checked; e.target.parentElement.classList.toggle("on", e.target.checked); draw();
  });
  $$("[data-status]").forEach((b) => b.addEventListener("click", () => {
    const s = b.dataset.status;
    claimFilters.statuses.has(s) ? claimFilters.statuses.delete(s) : claimFilters.statuses.add(s);
    b.classList.toggle("on");
    draw();
  }));
}

async function renderClaim(slug) {
  const c = await api(`/api/claims/${enc(slug)}`);
  app.className = "";
  const ev = c.evidence.slice().sort((a, b) => (a.published_date || "").localeCompare(b.published_date || ""));
  const relColor = (r) => ({
    supports: "--rel-positive", replicates: "--rel-positive", refutes: "--rel-negative", fails_to_replicate: "--rel-negative",
    mixed: "--rel-mixed", assumes: "--rel-assumes", proposes: "--rel-proposes", extends: "--rel-extends", critiques: "--rel-critiques",
  }[r] || "--border-strong");
  const claimLink = (x) => `<li><span style="width:118px;flex-shrink:0">${pill(x.status)}</span>
    <span class="lbl"><a href="#/claim/${enc(x.slug)}" class="statement" style="font-size:14px">${esc(x.statement)}</a></span></li>`;

  app.innerHTML = `
    <div class="eyebrow">Claim · ${esc(c.claim_type)}</div>
    <h1 class="claim-title">${esc(c.statement)}</h1>
    <div class="meta-row" style="margin-bottom:20px">
      ${pill(c.status)}
      <span>${esc(STATUS_HELP[c.status])}</span>
      ${c.status_override ? `<span class="faint">(set by hand; derived status is ${STATUS_LABEL[c.derived_status]})</span>` : ""}
      ${c.fragile ? `<span class="tag-flag">Fragile foundation</span>` : ""}
      ${curateButtons()}
    </div>
    <div class="grid cols-main-side">
      <div class="grid">
        <div class="card">
          <div class="kv">
            <div><div class="n st-replicated">${c.positive}</div><div class="l">Supporting</div></div>
            <div><div class="n st-refuted">${c.negative}</div><div class="l">Refuting</div></div>
            <div><div class="n st-contested">${c.mixed}</div><div class="l">Mixed</div></div>
            <div><div class="n">${c.groups}</div><div class="l">Independent groups</div></div>
            <div><div class="n">${c.assumed_by}</div><div class="l">Assumed by</div></div>
            <div><div class="n">${c.dependents}</div><div class="l">Dependent claims (incl. indirect)</div></div>
          </div>
          ${c.scope ? `<p class="small muted" style="margin:12px 0 0"><b>Scope:</b> ${esc(c.scope)}</p>` : ""}
          ${c.critiqued_by ? `<p class="small" style="margin:8px 0 0;color:var(--rel-critiques)">Critiqued by ${plural(c.critiqued_by, "document")} without new experiments.</p>` : ""}
        </div>
        <div class="card">
          <h2>Evidence over time <span class="count">${ev.length}</span></h2>
          <ol class="timeline">${ev.map((e) => `
            <li style="--rel-c: var(${relColor(e.relation)})">
              <div class="when">${fmtDate(e.published_date) || "undated"} · ${src(e.source)}${e.strength ? ` · ${esc(e.strength)} evidence` : ""}</div>
              <div class="what">${rel(e.relation)}<a href="#/document/${enc(e.slug)}">${esc(e.title)}</a></div>
              <div class="small muted">${esc(authorsShort(e.authors, 4))}</div>
              ${e.experiment_summary ? `<div class="exp">${esc(e.experiment_summary)}</div>` : ""}
              ${e.models_studied.length ? `<div class="models chips">${e.models_studied.map((m) => `<span class="chip">${esc(m)}</span>`).join("")}</div>` : ""}
            </li>`).join("")}</ol>
        </div>
      </div>
      <div class="grid">
        <div class="card">
          <h2>Concepts</h2>
          <div class="chips">${c.concept_details.map((k) => conceptChip(k)).join("") || `<span class="empty">None</span>`}</div>
        </div>
        <div class="card">
          <h2>Depends on <span class="count">${c.depends_on_claims.length}</span></h2>
          ${c.depends_on_claims.length ? `<ul class="link-list">${c.depends_on_claims.map(claimLink).join("")}</ul>` : `<div class="empty">Nothing; this claim stands on its own.</div>`}
        </div>
        <div class="card">
          <h2>Relied on by <span class="count">${c.dependent_claims.length}</span></h2>
          ${c.dependent_claims.length ? `<ul class="link-list">${c.dependent_claims.map(claimLink).join("")}</ul>` : `<div class="empty">No claims depend on this one.</div>`}
        </div>
        <div class="card" id="dupes-slot"></div>
        ${notesPanel("claim", c.slug, c.notes)}
        <div class="card">
          <h2>Status override</h2>
          <p class="small muted" style="margin:-6px 0 10px">Derived from evidence: ${pill(c.derived_status)}. Override it when you know better, and say why.</p>
          <form class="override" id="override">
            <select name="status"><option value="">Use derived status</option>
              ${STATUSES.map((s) => `<option value="${s}" ${c.status_override === s ? "selected" : ""}>${STATUS_LABEL[s]}</option>`).join("")}</select>
            <input name="note" placeholder="Reason" value="${esc(c.status_note || "")}">
            <button class="btn" type="submit">Save</button>
          </form>
        </div>
      </div>
    </div>`;
  $("#override").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await api(`/api/claims/${enc(c.slug)}/status`, { method: "POST", body: { status: f.get("status") || null, note: f.get("note") || null } });
    toast("Status updated");
    render({ keepScroll: true });
  });
  wireClaimCuration(c);
}

/* ================================================================ documents */

const docFilters = { q: "", source: "", type: "", review: "", sort: "newest", author: "" };

async function renderDocuments(_, params) {
  const docs = await api("/api/documents");
  app.className = "";
  if (params.has("review")) docFilters.review = params.get("review");
  docFilters.author = params.get("author") || "";
  const sources = [...new Set(docs.map((d) => d.source))];
  const types = [...new Set(docs.map((d) => d.doc_type))];

  app.innerHTML = `
    <div class="page-head">
      <div><h1>Documents</h1><p>Papers, posts and essays that have been ingested and analyzed.${docFilters.author ? ` Showing work by <b>${esc(docFilters.author)}</b> · <a href="#/documents">clear</a>` : ""}</p></div>
    </div>
    <div class="toolbar">
      <input type="search" id="dq" placeholder="Filter by title or author…" value="${esc(docFilters.q)}">
      <select id="dsource" aria-label="Source"><option value="">All sources</option>${sources.map((s) => `<option value="${s}" ${docFilters.source === s ? "selected" : ""}>${SOURCE_LABEL[s] || s}</option>`).join("")}</select>
      <select id="dtype" aria-label="Type"><option value="">All types</option>${types.map((t) => `<option value="${t}" ${docFilters.type === t ? "selected" : ""}>${t.replace("_", " ")}</option>`).join("")}</select>
      <div class="seg" id="dreview">${[["", "All"], ["extracted", "Unreviewed"], ["reviewed", "Reviewed"]].map(([v, l]) =>
        `<button data-v="${v}" class="${docFilters.review === v ? "on" : ""}">${l}</button>`).join("")}</div>
      <select id="dsort" aria-label="Sort">${[["newest", "Newest"], ["oldest", "Oldest"], ["cited", "Most cited"], ["claims", "Most claims"]].map(([v, l]) =>
        `<option value="${v}" ${docFilters.sort === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    </div>
    <div class="doc-list" id="docs"></div>`;

  const draw = () => {
    const f = docFilters, q = f.q.toLowerCase();
    let rows = docs.filter((d) =>
      (!f.source || d.source === f.source) && (!f.type || d.doc_type === f.type) &&
      (!f.review || d.status === f.review) && (!f.author || d.authors.includes(f.author)) &&
      (!q || d.title.toLowerCase().includes(q) || d.authors.join(" ").toLowerCase().includes(q)));
    const by = {
      newest: (a, b) => (b.published_date || "").localeCompare(a.published_date || ""),
      oldest: (a, b) => (a.published_date || "").localeCompare(b.published_date || ""),
      cited: (a, b) => b.cited_by - a.cited_by, claims: (a, b) => b.claims - a.claims,
    };
    rows.sort(by[f.sort]);
    $("#docs").innerHTML = rows.length ? rows.map((d) => `
      <div class="card doc-card" data-href="#/document/${enc(d.slug)}">
        <a class="title" href="#/document/${enc(d.slug)}">${esc(d.title)}</a>
        <div class="byline">${esc(authorsShort(d.authors, 4))}</div>
        <div class="foot">
          ${src(d.source)}<span>${fmtDate(d.published_date)}</span>
          <span>${plural(d.claims, "claim")}</span>
          ${d.provenance?.startsWith("codex-cli:") ? `<span class="chip" title="${esc(extractionTooltip(d.provenance))}">${esc(extractionLabel(d.provenance))}</span>` : ""}
          ${d.cited_by ? `<span>cited by ${d.cited_by}</span>` : ""}
          ${d.status === "reviewed" ? `<span class="st-replicated">✓ reviewed</span>` : d.status === "fetched" ? `<span class="faint">awaiting extraction</span>` : ""}
          <span class="chips">${d.primary_concepts.map((k) => `<span class="chip primary">${esc(k.replace(/-/g, " "))}</span>`).join("")}</span>
          ${d.status !== "fetched" ? `<a class="lineage-link" href="#/lineage?focus=${enc(d.slug)}" title="Show this document in the lineage graph">Show in lineage →</a>` : ""}
        </div>
      </div>`).join("") : `<div class="empty">No documents match.</div>`;
  };
  draw();
  $("#dq").addEventListener("input", (e) => { docFilters.q = e.target.value; draw(); });
  $("#dsource").addEventListener("change", (e) => { docFilters.source = e.target.value; draw(); });
  $("#dtype").addEventListener("change", (e) => { docFilters.type = e.target.value; draw(); });
  $("#dsort").addEventListener("change", (e) => { docFilters.sort = e.target.value; draw(); });
  $$("#dreview button").forEach((b) => b.addEventListener("click", () => {
    docFilters.review = b.dataset.v;
    $$("#dreview button").forEach((x) => x.classList.toggle("on", x === b));
    draw();
  }));
}

async function renderDocument(slug) {
  const d = await api(`/api/documents/${enc(slug)}`);
  app.className = "";
  const grouped = RELATION_ORDER.map((r) => [r, d.claims.filter((c) => c.relation === r)]).filter(([, cs]) => cs.length);
  const summary = d.summary ? sectionHeadings(DOMPurify.sanitize(marked.parse(d.summary))) : `<p class="faint">No summary yet${d.status === "fetched" ? ": this document is waiting for extraction" : ""}.</p>`;
  const linkItem = (x) => `
    <li><span class="date">${year(x.published_date)}</span>
      <span class="lbl">${x.status === "stub"
        ? `${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>` : esc(x.title)} <span class="faint small">· not ingested</span>`
        : `<a href="#/document/${enc(x.slug)}">${esc(x.title)}</a>`}
      ${x.link_type === "inferred_influence" ? `<div class="small faint">inferred influence${x.confidence ? ` (${Math.round(x.confidence * 100)}%)` : ""}${x.rationale ? `: ${esc(x.rationale)}` : ""}</div>` : ""}
      ${x.later_link ? `<div class="small faint">later link added in a page update</div>` : ""}</span></li>`;
  const buildsOn = d.builds_on.filter((x) => !x.later_link);
  const ingestedFirst = (a, b) => (a.status === "stub") - (b.status === "stub") || (a.published_date || "").localeCompare(b.published_date || "");

  app.innerHTML = `
    <div class="eyebrow">${esc(d.doc_type.replace("_", " "))} · ${src(d.source)}</div>
    <h1 class="detail-title">${esc(d.title)}</h1>
    <div class="byline-lg">${d.authors.map((a) => `<a href="#/documents?author=${enc(a)}" style="color:inherit">${esc(a)}</a>`).join(", ")}</div>
    <div class="meta-row" style="margin-bottom:22px">
      <span>${fmtDate(d.published_date)}</span>
      ${d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">Open original ↗</a>` : ""}
      <a href="#/lineage?focus=${enc(d.slug)}">Show in lineage →</a>
      <a href="#/reading/${enc(d.slug)}">Reading list →</a>
      ${["extracted", "reviewed"].includes(d.status) ? `<button class="btn ${d.status === "reviewed" ? "ok" : ""}" id="review">${d.status === "reviewed" ? "✓ Reviewed" : "Mark extraction reviewed"}</button>` : `<span class="faint">${esc(d.status)}</span>`}
      ${d.curated ? `<span class="small st-supported" title="Re-importing this document's extraction won't overwrite your edits">Edited by hand</span>` : ""}
    </div>
    <div class="grid cols-main-side">
      <div class="grid">
        <div class="card"><div class="prose">${summary}</div>
          ${d.provenance ? `<div class="small faint" style="margin-top:8px">Summary and claims by ${esc(d.provenance)}</div>` : ""}</div>
        <div class="card" id="doc-claims">
          <h2>Claims this document bears on <span class="count">${d.claims.length}</span>
            <span class="hint"><button class="btn sm" id="edit-claims">Edit</button></span></h2>
          ${grouped.map(([r, cs]) => cs.map((c) => `
            <div class="claim-row" data-ev="${c.evidence_id}">
              <div>${rel(r)}${c.strength ? `<div class="small faint" style="margin-top:4px">${esc(c.strength)}</div>` : ""}</div>
              <div><a href="#/claim/${enc(c.slug)}" class="statement" style="color:var(--text)">${esc(c.statement)}</a></div>
              <div class="side">${pill(c.status)}</div>
              ${c.experiment_summary ? `<div class="exp">${esc(c.experiment_summary)}${c.location ? ` <span class="faint">(${esc(c.location)})</span>` : ""}</div>` : ""}
            </div>`).join("")).join("") || `<div class="empty">No claims extracted yet.</div>`}
        </div>
      </div>
      <div class="grid">
        <div class="card"><h2>Concepts <span class="hint"><button class="btn sm" id="edit-concepts">Edit</button></span></h2>
          <div class="chips">${d.concepts.map((k) => conceptChip(k, k.role === "primary")).join("") || `<span class="empty">None yet</span>`}</div></div>
        <div class="card"><h2>Builds on <span class="count">${buildsOn.length}</span></h2>
          ${buildsOn.length ? `<ul class="link-list">${buildsOn.sort(ingestedFirst).slice(0, 25).map(linkItem).join("")}</ul>
            ${buildsOn.length > 25 ? `<div class="small faint">+${buildsOn.length - 25} more</div>` : ""}` : `<div class="empty">No known citations.</div>`}</div>
        <div class="card"><h2>Built on by <span class="count">${d.built_on_by.filter((x) => !x.later_link).length}</span></h2>
          ${d.built_on_by.filter((x) => !x.later_link).length ? `<ul class="link-list">${d.built_on_by.filter((x) => !x.later_link).map(linkItem).join("")}</ul>` : `<div class="empty">Nothing in the map cites this yet.</div>`}</div>
        ${notesPanel("document", d.slug, d.notes)}
      </div>
    </div>`;
  const btn = $("#review");
  if (btn) btn.addEventListener("click", async () => {
    const reviewed = d.status !== "reviewed";
    await api(`/api/documents/${enc(d.slug)}/review`, { method: "POST", body: { reviewed } });
    toast(reviewed ? "Marked reviewed" : "Marked unreviewed");
    render({ keepScroll: true });
  });
  wireDocumentCuration(d);
}

// Summaries mark sections as a paragraph of bold text ("**Central idea**"); render those
// as headings while leaving bold words inside sentences alone.
function sectionHeadings(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  tpl.content.querySelectorAll("p").forEach((p) => {
    const only = p.children.length === 1 && p.firstElementChild.tagName === "STRONG" &&
      p.textContent.trim() === p.firstElementChild.textContent.trim();
    if (only) {
      const h = document.createElement("h3");
      h.className = "sec";
      h.textContent = p.textContent.trim().replace(/[:.]$/, "");
      p.replaceWith(h);
    }
  });
  return tpl.innerHTML;
}

/* lineage and concept graph views live in graphs.js */

/* ================================================================ notes */

let noteKindFilter = "";

async function renderNotes() {
  const notes = await api("/api/notes");
  app.className = "";
  const counts = {};
  notes.forEach((n) => (counts[n.kind] = (counts[n.kind] || 0) + 1));
  const rows = notes.filter((n) => !noteKindFilter || n.kind === noteKindFilter);
  app.innerHTML = `
    <div class="page-head"><div><h1>Notes</h1><p>Your notes, ideas, critiques and next steps across the map. Add new ones from any document, claim or concept page.</p></div></div>
    <div class="toolbar">
      <button class="filter-chip ${noteKindFilter ? "" : "on"}" data-kind="">All <span class="n">${notes.length}</span></button>
      ${NOTE_KINDS.filter((k) => counts[k]).map((k) => `<button class="filter-chip ${noteKindFilter === k ? "on" : ""}" data-kind="${k}">${NOTE_LABEL[k] || k} <span class="n">${counts[k]}</span></button>`).join("")}
    </div>
    <div class="card">${rows.length ? rows.map((n) => `
      <div class="note">
        <div class="head">${noteKind(n.kind)}<span>${esc(n.target_type)} · ${fmtDate(n.created_at)}</span>
          <button class="del" data-del-note="${n.id}">Delete</button></div>
        <div class="target"><a href="#/${n.target_type}/${enc(n.target_slug)}">${esc((n.target_label || "").slice(0, 140))}</a></div>
        <div class="body">${esc(n.body)}</div>
      </div>`).join("") : `<div class="empty">No notes yet. Open any document, claim or concept to add one.</div>`}</div>`;
  $$("[data-kind]").forEach((b) => b.addEventListener("click", () => { noteKindFilter = b.dataset.kind; renderNotes(); }));
}

/* ================================================================ global handlers */

document.addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del-note]");
  if (del) {
    e.preventDefault();
    if (!confirm("Delete this note?")) return;
    await api(`/api/notes/${del.dataset.delNote}`, { method: "DELETE" });
    toast("Note deleted");
    return render({ keepScroll: true });
  }
  const row = e.target.closest("tr[data-href], .doc-card[data-href]");
  if (row && !e.target.closest("a, button, input, label")) location.hash = row.dataset.href;
});

document.addEventListener("submit", async (e) => {
  const form = e.target.closest("[data-note-form]");
  if (!form) return;
  e.preventDefault();
  const body = form.body.value.trim();
  if (!body) return form.body.focus();
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    await api("/api/notes", { method: "POST", body: {
      target_type: form.dataset.targetType, slug: form.dataset.slug, body, kind: form.kind.value } });
    toast("Note saved");
    render({ keepScroll: true });
  } catch (err) {
    toast(`Couldn't save: ${err.message}`);
    btn.disabled = false;
  }
});

/* ---------------------------------------------------------------- search */

const searchInput = $("#search"), searchBox = $("#search-results");
let searchTimer, searchSel = -1;

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) { searchBox.classList.remove("open"); return; }
  searchTimer = setTimeout(async () => {
    const r = await api(`/api/search?q=${enc(q)}`);
    const group = (title, items, href, meta) => items.length
      ? `<h6>${title}</h6>${items.map((i) => `<a href="${href(i)}"><span>${esc(i.label.length > 90 ? i.label.slice(0, 88) + "…" : i.label)}</span><span class="meta">${esc(meta(i))}</span></a>`).join("")}` : "";
    const html =
      group("Concepts", r.concepts, (i) => `#/concept/${enc(i.slug)}`, (i) => i.meta) +
      group("Claims", r.claims, (i) => `#/claim/${enc(i.slug)}`, (i) => i.meta) +
      group("Documents", r.documents, (i) => `#/document/${enc(i.slug)}`, (i) => SOURCE_LABEL[i.meta] || i.meta) +
      group("Authors", r.authors, (i) => `#/documents?author=${enc(i.label)}`, (i) => plural(i.meta, "doc"));
    searchBox.innerHTML = html || `<div class="empty" style="padding:10px">No matches for “${esc(q)}”.</div>`;
    searchSel = -1;
    searchBox.classList.add("open");
  }, 160);
});
searchInput.addEventListener("keydown", (e) => {
  const items = $$("a", searchBox);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    searchSel = Math.max(0, Math.min(items.length - 1, searchSel + (e.key === "ArrowDown" ? 1 : -1)));
    items.forEach((a, i) => a.classList.toggle("sel", i === searchSel));
  } else if (e.key === "Enter" && items.length) {
    location.hash = (items[searchSel] || items[0]).getAttribute("href");
    closeSearch();
  } else if (e.key === "Escape") closeSearch();
});
searchBox.addEventListener("click", (e) => { if (e.target.closest("a")) closeSearch(); });
document.addEventListener("click", (e) => { if (!e.target.closest(".search")) searchBox.classList.remove("open"); });
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !e.target.closest("input, textarea, select")) { e.preventDefault(); searchInput.focus(); }
});
function closeSearch() { searchBox.classList.remove("open"); searchInput.value = ""; searchInput.blur(); }

/* ---------------------------------------------------------------- theme */

const THEME_KEY = "interp-map-theme";
try { const saved = localStorage.getItem(THEME_KEY); if (saved) document.documentElement.dataset.theme = saved; } catch {}
$("#theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, document.documentElement.dataset.theme); } catch {}
  render({ keepScroll: true }); // graphs read colours from CSS variables
});

try { if (window.cytoscape && window.cytoscapeDagre) cytoscape.use(cytoscapeDagre); } catch { /* already registered */ }
try { if (window.cytoscape && window.cytoscapeFcose) cytoscape.use(cytoscapeFcose); } catch { /* already registered */ }
render();
