/* Claim hierarchy views: the Foundations page, and the hierarchy cards on a claim page.
   Loaded before app.js, which supplies $, $$, api, esc, enc, pill, plural and the router. */

const LEVEL_LABEL = { paradigm: "Paradigm", thesis: "Broad claim", finding: "Finding" };
const STANCE_ORDER = ["supports", "contradicts", "qualifies", "applies"];
const STANCE_LABEL = { supports: "Supports", contradicts: "Contradicts", qualifies: "Qualifies", applies: "Relies on it without testing" };
const levelTag = (l) => (l ? `<span class="level level-${l}">${LEVEL_LABEL[l]}</span>` : "");

function stanceBar(st) {
  const total = STANCE_ORDER.reduce((n, k) => n + (st[k] || 0), 0);
  if (!total) return `<div class="stance-bar empty-bar" title="No findings filed yet"></div>`;
  return `<div class="stance-bar" title="${STANCE_ORDER.map((k) => `${st[k] || 0} ${k}`).join(" · ")}">${STANCE_ORDER.map((k) =>
    st[k] ? `<i class="stance-${k}" style="width:${(100 * st[k]) / total}%"></i>` : "").join("")}</div>`;
}
const tested = (c) => (c.stances.supports || 0) + (c.stances.contradicts || 0) + (c.stances.qualifies || 0);
const underTested = (c) => (c.stances.applies || 0) + c.assumed_by > 0 && (c.stances.applies || 0) > tested(c);

const foundationFilters = { paradigm: "", concept: "", q: "", flag: "", sort: "load", shown: 60 };
const FOUNDATION_FLAGS = [
  ["contested", "Contested", (c) => c.status === "contested" || c.status === "refuted"],
  ["undertested", "Relied on more than tested", underTested],
  ["empty", "No findings yet", (c) => !c.findings],
  ["ungrounded", "Rests on no paradigm", (c) => !c.rests_on.length],
];

async function renderFoundations() {
  const d = await api("/api/foundations");
  app.className = "";
  const f = foundationFilters;
  const paradigms = d.claims.filter((c) => c.level === "paradigm").sort((a, b) => b.dependents - a.dependents);
  const theses = d.claims.filter((c) => c.level === "thesis");
  const onParadigm = (slug) => theses.filter((t) => t.rests_on.includes(slug));
  const conceptCount = {};
  theses.forEach((t) => t.concepts.forEach((k) => (conceptCount[k] = (conceptCount[k] || 0) + 1)));
  const conceptOpts = Object.keys(conceptCount).filter((k) => conceptCount[k] >= 4)
    .sort((a, b) => (d.concept_names[a] || a).localeCompare(d.concept_names[b] || b));

  app.innerHTML = `
    <div class="page-head">
      <div><h1>Foundations</h1>
      <p>What the field takes for granted, the broad claims built on that, and how well each one has been tested.
        ${paradigms.length} paradigms, ${theses.length} broad claims and ${d.findings.toLocaleString()} single-experiment findings
        (${d.unplaced_findings.toLocaleString()} of them filed under nothing).</p></div>
    </div>
    <h2 class="section-h">Paradigms <span class="hint">assumptions most work builds on; select one to see what rests on it</span></h2>
    <div class="paradigm-grid" id="paradigms"></div>
    <h2 class="section-h">Broad claims <span class="hint">the bar shows what the findings under each claim do</span></h2>
    <div class="stance-legend">${STANCE_ORDER.map((k) => `<span><i class="dot stance-${k}"></i>${STANCE_LABEL[k].toLowerCase()}</span>`).join("")}</div>
    <div class="toolbar">
      <input type="search" id="fq" placeholder="Filter broad claims…" value="${esc(f.q)}">
      <select id="fconcept" aria-label="Concept"><option value="">All concepts</option>
        ${conceptOpts.map((k) => `<option value="${esc(k)}" ${k === f.concept ? "selected" : ""}>${esc(d.concept_names[k] || k)} (${conceptCount[k]})</option>`).join("")}</select>
      <select id="fsort" aria-label="Sort">
        ${[["load", "Most relied on"], ["findings", "Most findings"], ["against", "Most contradicted"]].map(([v, l]) =>
          `<option value="${v}" ${f.sort === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    </div>
    <div class="toolbar" id="fflags"></div>
    <div id="thesis-count" class="small muted" style="margin-bottom:10px"></div>
    <div class="thesis-grid" id="theses"></div>
    <div id="more" style="margin-top:14px"></div>`;

  const draw = () => {
    $("#paradigms").innerHTML = paradigms.map((p) => {
      const on = onParadigm(p.slug);
      const shaky = on.filter((t) => t.status === "contested" || t.status === "refuted").length;
      return `<button type="button" class="paradigm ${f.paradigm === p.slug ? "on" : ""} ${f.paradigm && f.paradigm !== p.slug ? "dim" : ""}"
          data-paradigm="${esc(p.slug)}" aria-pressed="${f.paradigm === p.slug}">
        <span class="statement">${esc(p.statement)}</span>
        <span class="meta">${pill(p.status)}<span>${plural(on.length, "broad claim")} rest on this${shaky ? ` · ${shaky} contested` : ""}</span>
          <a href="#/claim/${enc(p.slug)}" class="open">Open</a></span></button>`;
    }).join("");
    $("#fflags").innerHTML = FOUNDATION_FLAGS.map(([k, l, test]) =>
      `<button type="button" class="filter-chip ${f.flag === k ? "on" : ""}" data-flag="${k}">${l} <span class="n">${theses.filter(test).length}</span></button>`).join("");
    const q = f.q.toLowerCase();
    const flag = FOUNDATION_FLAGS.find(([k]) => k === f.flag);
    const rows = theses.filter((t) => (!f.paradigm || t.rests_on.includes(f.paradigm)) && (!f.concept || t.concepts.includes(f.concept)) &&
      (!flag || flag[2](t)) && (!q || t.statement.toLowerCase().includes(q)));
    const sorters = {
      load: (a, b) => b.load - a.load || b.findings - a.findings,
      findings: (a, b) => b.findings - a.findings || b.load - a.load,
      against: (a, b) => (b.stances.contradicts || 0) - (a.stances.contradicts || 0) || b.findings - a.findings,
    };
    rows.sort(sorters[f.sort]);
    $("#thesis-count").textContent = `${rows.length} of ${theses.length} broad claims`;
    $("#theses").innerHTML = rows.slice(0, f.shown).map((t) => `
      <a class="thesis" href="#/claim/${enc(t.slug)}">
        <span class="statement">${esc(t.statement)}</span>
        ${stanceBar(t.stances)}
        <span class="meta">${pill(t.status)}
          <span>${t.findings ? STANCE_ORDER.filter((k) => t.stances[k]).map((k) => `${t.stances[k]} ${k === "applies" ? "rely" : k.replace(/s$/, "")}`).join(" · ") : "no findings yet"}</span>
          ${t.load ? `<span title="Documents that assume it plus claims that depend on it">relied on ${t.load}×</span>` : ""}
          ${underTested(t) ? `<span class="tag-flag">under-tested</span>` : ""}</span>
      </a>`).join("") || `<div class="empty">No broad claims match these filters.</div>`;
    $("#more").innerHTML = rows.length > f.shown ? `<button type="button" class="btn" id="show-more">Show ${Math.min(60, rows.length - f.shown)} more</button>` : "";
  };
  draw();
  app.addEventListener("click", (e) => {
    if (!$("#theses")) return;
    const p = e.target.closest("[data-paradigm]"), fl = e.target.closest("[data-flag]");
    if (e.target.closest("a.open")) return;
    if (p) { f.paradigm = f.paradigm === p.dataset.paradigm ? "" : p.dataset.paradigm; f.shown = 60; draw(); }
    else if (fl) { f.flag = f.flag === fl.dataset.flag ? "" : fl.dataset.flag; f.shown = 60; draw(); }
    else if (e.target.id === "show-more") { f.shown += 60; draw(); }
  });
  $("#fq").addEventListener("input", (e) => { f.q = e.target.value; f.shown = 60; draw(); });
  $("#fconcept").addEventListener("change", (e) => { f.concept = e.target.value; f.shown = 60; draw(); });
  $("#fsort").addEventListener("change", (e) => { f.sort = e.target.value; draw(); });
}

/* Cards for a claim page: where a finding is filed, or the findings filed under a broader claim. */
function hierarchyCards(c) {
  const line = (x) => `<li><span style="width:118px;flex-shrink:0">${pill(x.status)}</span>
    <span class="lbl"><a href="#/claim/${enc(x.slug)}" class="statement" style="font-size:14px">${esc(x.statement)}</a></span></li>`;
  let out = "";
  if (c.filed_under.length) {
    out += `<div class="card"><h2>Filed under</h2><ul class="link-list">${c.filed_under.map((p) => `
      <li><span class="stance-tag stance-${p.stance}" style="width:118px;flex-shrink:0">${esc(p.stance)}</span>
        <span class="lbl">${levelTag(p.level)} <a href="#/claim/${enc(p.slug)}" class="statement" style="font-size:14px">${esc(p.statement)}</a></span></li>`).join("")}</ul></div>`;
  } else if (c.level === "finding") {
    out += `<div class="card"><h2>Filed under</h2><div class="empty">Nothing yet: no broad claim in the map covers this finding.</div></div>`;
  }
  if (c.level === "paradigm" || c.level === "thesis") {
    const st = {};
    c.filed_findings.forEach((x) => (st[x.stance] = (st[x.stance] || 0) + 1));
    out += `<div class="card"><h2>Findings filed under this claim <span class="count">${c.filed_findings.length}</span></h2>
      ${c.filed_findings.length ? stanceBar(st) + STANCE_ORDER.filter((k) => st[k]).map((k) => `
        <h3 class="stance-h"><i class="dot stance-${k}"></i>${STANCE_LABEL[k]} <span class="count">${st[k]}</span></h3>
        <ul class="link-list">${c.filed_findings.filter((x) => x.stance === k).map(line).join("")}</ul>`).join("")
      : `<div class="empty">None yet. Evidence from new papers will be filed here.</div>`}</div>`;
  }
  return out;
}
