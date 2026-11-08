"use strict";
/* Curation UI: editing, merging and deleting claims and concepts, correcting how a document
   bears on claims, and the Review page. Loaded before app.js; uses its helpers at call time. */

const CLAIM_TYPES = ["empirical", "theoretical", "methodological", "hypothesis"];
const CONCEPT_KINDS = ["topic", "method", "phenomenon", "hypothesis", "tool", "model_component", "benchmark"];
const STRENGTHS = ["", "weak", "moderate", "strong"];
const RELATION_HELP = {
  proposes: "first states it (with strength set, also its first test)",
  assumes: "relies on it without testing it",
  supports: "tests it; result agrees",
  refutes: "tests it; result disagrees",
  mixed: "tests it; mixed result",
  replicates: "re-runs a prior test; agrees",
  fails_to_replicate: "re-runs a prior test; disagrees",
  extends: "builds on it to a broader or new setting",
  critiques: "argues against it without new experiments",
};

/* ---------------------------------------------------------------- vocabulary cache */

let vocabCache = {};
async function vocab(kind) {
  if (!vocabCache[kind]) {
    vocabCache[kind] = kind === "claim"
      ? (await api("/api/claims")).map((c) => ({ slug: c.slug, label: c.statement, meta: STATUS_LABEL[c.status] }))
      : (await api("/api/concepts")).nodes.map((n) => ({ slug: n.slug, label: n.name, meta: n.kind }));
  }
  return vocabCache[kind];
}
const invalidateVocab = () => { vocabCache = {}; };

/* ---------------------------------------------------------------- modal */

function modal({ title, body, confirmLabel = "Save", danger = false, wide = false, onConfirm, onMount }) {
  const el = document.createElement("div");
  el.className = "modal-backdrop";
  el.innerHTML = `
    <div class="modal${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <h3>${esc(title)}</h3>
      <div class="modal-body">${body}</div>
      <div class="modal-error" role="alert"></div>
      <div class="modal-actions">
        <button class="btn" data-cancel type="button">Cancel</button>
        ${onConfirm ? `<button class="btn ${danger ? "danger-solid" : "primary"}" data-confirm type="button">${esc(confirmLabel)}</button>` : ""}
      </div>
    </div>`;
  document.body.appendChild(el);
  const close = () => { el.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape" && !e.target.closest(".picker-wrap")) close(); };
  document.addEventListener("keydown", onKey);
  el.addEventListener("mousedown", (e) => { if (e.target === el) close(); });
  $("[data-cancel]", el).addEventListener("click", close);
  const confirmBtn = $("[data-confirm]", el);
  if (confirmBtn) confirmBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    $(".modal-error", el).textContent = "";
    try {
      await onConfirm(el);
      close();
    } catch (err) {
      $(".modal-error", el).textContent = err.message;
      confirmBtn.disabled = false;
    }
  });
  if (onMount) onMount(el);
  const first = $("input, textarea, select", el);
  if (first) first.focus();
  return close;
}

/* ---------------------------------------------------------------- picker */

/* Type-ahead over claims or concepts. `allowCreate` adds a "create new" option. */
function picker(input, { kind, exclude = () => new Set(), onPick, allowCreate = false }) {
  const wrap = document.createElement("div");
  wrap.className = "picker-wrap";
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  const list = document.createElement("div");
  list.className = "picker-list";
  wrap.appendChild(list);
  let items = [], sel = 0;

  const draw = async () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { list.classList.remove("open"); return; }
    const words = q.split(/\s+/);
    const skip = exclude();
    items = (await vocab(kind))
      .filter((v) => !skip.has(v.slug) && words.every((w) => (v.label + " " + v.slug).toLowerCase().includes(w)))
      .slice(0, 8);
    if (allowCreate) items.push({ create: true, label: input.value.trim() });
    sel = 0;
    list.innerHTML = items.length ? items.map((v, i) => `
      <div class="picker-item${i === sel ? " sel" : ""}" data-i="${i}">
        ${v.create ? `<span class="create">+ New ${kind}:</span> ${esc(v.label)}`
          : `<span>${esc(v.label.length > 110 ? v.label.slice(0, 108) + "…" : v.label)}</span><span class="meta">${esc(v.meta || "")}</span>`}
      </div>`).join("") : `<div class="picker-empty">No matching ${kind}s</div>`;
    list.classList.add("open");
  };
  const choose = async (i) => {
    const v = items[i];
    if (!v) return;
    list.classList.remove("open");
    input.value = "";
    await onPick(v);
  };
  input.addEventListener("input", draw);
  input.addEventListener("keydown", (e) => {
    if (!list.classList.contains("open")) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = Math.max(0, Math.min(items.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
      $$(".picker-item", list).forEach((x, i) => x.classList.toggle("sel", i === sel));
    } else if (e.key === "Enter") { e.preventDefault(); choose(sel); }
    else if (e.key === "Escape") { e.stopPropagation(); list.classList.remove("open"); }
  });
  list.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".picker-item");
    if (item) { e.preventDefault(); choose(+item.dataset.i); }
  });
  input.addEventListener("blur", () => setTimeout(() => list.classList.remove("open"), 120));
}

/* ---------------------------------------------------------------- chip editor */

/* Editable list of claims or concepts. With `roles`, a star marks primary concepts. */
function chipEditor(container, { kind, items, roles = false, placeholder, allowCreate = false }) {
  let state = items.map((x) => ({ ...x }));
  container.innerHTML = `<div class="chips chip-edit"></div><input class="field-input" placeholder="${esc(placeholder)}">`;
  const chips = $(".chip-edit", container);
  const draw = () => {
    chips.innerHTML = state.length ? state.map((x, i) => `
      <span class="chip${roles && x.role === "primary" ? " primary" : ""}" title="${esc(x.label)}">
        ${roles ? `<button type="button" class="star" data-star="${i}" title="Toggle primary">${x.role === "primary" ? "★" : "☆"}</button>` : ""}
        <span>${esc(x.label.length > 70 ? x.label.slice(0, 68) + "…" : x.label)}</span>
        <button type="button" class="x" data-x="${i}" aria-label="Remove">×</button>
      </span>`).join("") : `<span class="faint small">None</span>`;
  };
  chips.addEventListener("click", (e) => {
    const x = e.target.closest("[data-x]"), star = e.target.closest("[data-star]");
    if (x) state.splice(+x.dataset.x, 1);
    if (star) { const it = state[+star.dataset.star]; it.role = it.role === "primary" ? "secondary" : "primary"; }
    draw();
  });
  picker($("input", container), {
    kind, allowCreate,
    exclude: () => new Set(state.map((x) => x.slug)),
    onPick: async (v) => {
      if (v.create) {
        const created = kind === "concept"
          ? await api("/api/concepts", { method: "POST", body: { name: v.label, kind: "topic" } })
          : await api("/api/claims", { method: "POST", body: { statement: v.label } });
        invalidateVocab();
        toast(`Created ${kind}`);
        v = { slug: created.slug, label: v.label };
      }
      state.push({ slug: v.slug, label: v.label, role: "secondary" });
      draw();
    },
  });
  draw();
  return { get: () => state };
}

/* ---------------------------------------------------------------- merge / delete dialogs */

function mergeDialog(kind, item) {
  let target = null;
  const noun = kind === "claim" ? "claim" : "concept";
  modal({
    title: `Merge this ${noun} into another`,
    confirmLabel: "Merge",
    body: `
      <p class="small muted">Use this when two ${noun}s are the same idea. Everything attached to
        <b>${esc(item.label)}</b> (${kind === "claim" ? "evidence, dependencies" : "documents, claims, sub-concepts"} and notes)
        moves to the ${noun} you pick. Future extractions that use this label will attach to that ${noun} too.</p>
      <label class="field"><span>Merge into</span><input id="merge-target" class="field-input" placeholder="Search ${noun}s…"></label>
      <div id="merge-chosen" class="merge-chosen faint small">Nothing chosen yet.</div>
      <div id="merge-suggest"></div>`,
    onMount: async (el) => {
      const show = (v) => {
        target = v;
        $("#merge-chosen", el).innerHTML = `Into: <b>${esc(v.label)}</b> <span class="slug">${esc(v.slug)}</span>`;
        $("#merge-chosen", el).classList.remove("faint");
      };
      picker($("#merge-target", el), { kind, exclude: () => new Set([item.slug]), onPick: show });
      const sims = await api(`/api/${kind}s/${enc(item.slug)}/similar`).catch(() => []);
      if (sims.length) {
        $("#merge-suggest", el).innerHTML = `<div class="small muted" style="margin:12px 0 6px">Most similar:</div>` +
          sims.map((s, i) => `<button type="button" class="suggest" data-i="${i}">${esc(s.label)} <span class="faint">${Math.round(s.score * 100)}% overlap</span></button>`).join("");
        $$(".suggest", el).forEach((b) => b.addEventListener("click", () => show(sims[+b.dataset.i])));
      }
    },
    onConfirm: async () => {
      if (!target) throw new Error(`Pick the ${noun} to merge into.`);
      await api(`/api/${kind}s/${enc(item.slug)}/merge`, { method: "POST", body: { into: target.slug } });
      invalidateVocab();
      toast("Merged");
      location.hash = `#/${kind}/${enc(target.slug)}`;
    },
  });
}

function deleteDialog(kind, item, consequences) {
  modal({
    title: `Delete this ${kind}?`,
    confirmLabel: "Delete",
    danger: true,
    body: `<p><b>${esc(item.label)}</b></p>
      <p class="small muted">${consequences} Future extractions that use this label will skip it.
        If it's really the same idea as another ${kind}, merge it instead so nothing is lost.</p>`,
    onConfirm: async () => {
      await api(`/api/${kind}s/${enc(item.slug)}`, { method: "DELETE" });
      invalidateVocab();
      toast("Deleted");
      location.hash = kind === "claim" ? "#/claims" : "#/concepts";
    },
  });
}

/* "Possible duplicates" card: one-click merges in either direction, or dismiss. */
async function fillDuplicates(slot, kind, item) {
  const sims = (await api(`/api/${kind}s/${enc(item.slug)}/similar`).catch(() => [])).filter((s) => s.score >= 0.3);
  if (!sims.length) { slot.remove(); return; }
  slot.innerHTML = `
    <h2>Possible duplicates <span class="count">${sims.length}</span></h2>
    <p class="small muted" style="margin:-6px 0 8px">Labels with a lot of word overlap. Merge if they're the same idea.</p>
    ${sims.map((s, i) => `
      <div class="note">
        <div class="target"><a href="#/${kind}/${enc(s.slug)}">${esc(s.label)}</a></div>
        <div class="head" style="margin-top:6px">
          <span>${Math.round(s.score * 100)}% overlap</span>
          <span class="dupe-actions">
            <button class="btn sm" data-keep-here="${i}" title="Merge that one into this one">Merge into this</button>
            <button class="btn sm ghost" data-dismiss="${i}">Different</button>
          </span>
        </div>
      </div>`).join("")}`;
  slot.addEventListener("click", async (e) => {
    const keep = e.target.closest("[data-keep-here]"), dis = e.target.closest("[data-dismiss]");
    if (keep) {
      const s = sims[+keep.dataset.keepHere];
      await api(`/api/${kind}s/${enc(s.slug)}/merge`, { method: "POST", body: { into: item.slug } });
      invalidateVocab();
      toast("Merged into this one");
      render({ keepScroll: true });
    } else if (dis) {
      const s = sims[+dis.dataset.dismiss];
      await api("/api/duplicates/dismiss", { method: "POST", body: { kind, a: item.slug, b: s.slug } });
      dis.closest(".note").remove();
    }
  });
}

const curateButtons = () => `
  <span class="actions-inline">
    <button class="btn sm" data-act="edit">Edit</button>
    <button class="btn sm" data-act="merge">Merge…</button>
    <button class="btn sm danger" data-act="delete">Delete</button>
  </span>`;

/* ---------------------------------------------------------------- claim page */

function wireClaimCuration(c) {
  const item = { slug: c.slug, label: c.statement };
  $("[data-act=edit]").addEventListener("click", () => {
    let concepts, deps;
    modal({
      title: "Edit claim",
      wide: true,
      body: `
        <label class="field"><span>Statement</span>
          <textarea name="statement" class="field-input" rows="3">${esc(c.statement)}</textarea>
          <span class="help">Phrase it so it stands on its own, independent of any one paper.</span></label>
        <div class="field-row">
          <label class="field"><span>Type</span><select name="claim_type" class="field-input">
            ${CLAIM_TYPES.map((t) => `<option ${t === c.claim_type ? "selected" : ""}>${t}</option>`).join("")}</select></label>
          <label class="field" style="flex:1"><span>Scope</span>
            <input name="scope" class="field-input" value="${esc(c.scope || "")}" placeholder="e.g. small attention-only transformers"></label>
        </div>
        <div class="field"><span>Concepts</span><div id="ed-concepts"></div></div>
        <div class="field"><span>Depends on (claims this one presupposes)</span><div id="ed-deps"></div></div>`,
      onMount: (el) => {
        concepts = chipEditor($("#ed-concepts", el), { kind: "concept", placeholder: "Add a concept…", allowCreate: true,
          items: c.concept_details.map((k) => ({ slug: k.slug, label: k.name })) });
        deps = chipEditor($("#ed-deps", el), { kind: "claim", placeholder: "Add a claim this depends on…",
          items: c.depends_on_claims.map((d) => ({ slug: d.slug, label: d.statement })) });
      },
      onConfirm: async (el) => {
        await api(`/api/claims/${enc(c.slug)}`, { method: "PUT", body: {
          statement: $("[name=statement]", el).value, claim_type: $("[name=claim_type]", el).value,
          scope: $("[name=scope]", el).value, concepts: concepts.get().map((x) => x.slug),
          depends_on: deps.get().map((x) => x.slug) } });
        invalidateVocab();
        toast("Claim saved");
        render({ keepScroll: true });
      },
    });
  });
  $("[data-act=merge]").addEventListener("click", () => mergeDialog("claim", item));
  $("[data-act=delete]").addEventListener("click", () => deleteDialog("claim", item,
    `Its ${plural(c.evidence.length, "evidence link")}, its dependencies and its notes will be removed.`));
  fillDuplicates($("#dupes-slot"), "claim", item);
}

/* ---------------------------------------------------------------- concept page */

function wireConceptCuration(k) {
  const item = { slug: k.slug, label: k.name };
  $("[data-act=edit]").addEventListener("click", () => {
    let parents;
    modal({
      title: "Edit concept",
      wide: true,
      body: `
        <div class="field-row">
          <label class="field" style="flex:1"><span>Name</span><input name="name" class="field-input" value="${esc(k.name)}"></label>
          <label class="field"><span>Kind</span><select name="kind" class="field-input">
            ${CONCEPT_KINDS.map((t) => `<option ${t === k.kind ? "selected" : ""}>${t}</option>`).join("")}</select></label>
        </div>
        <label class="field"><span>Description</span>
          <textarea name="description" class="field-input" rows="3">${esc(k.description || "")}</textarea></label>
        <div class="field"><span>Builds on (parent concepts)</span><div id="ed-parents"></div>
          <span class="help">A concept can have several parents. Loops are refused.</span></div>`,
      onMount: (el) => {
        parents = chipEditor($("#ed-parents", el), { kind: "concept", placeholder: "Add a parent concept…", allowCreate: true,
          items: k.parents.map((p) => ({ slug: p.slug, label: p.name })) });
      },
      onConfirm: async (el) => {
        await api(`/api/concepts/${enc(k.slug)}`, { method: "PUT", body: {
          name: $("[name=name]", el).value, kind: $("[name=kind]", el).value,
          description: $("[name=description]", el).value, parents: parents.get().map((x) => x.slug) } });
        invalidateVocab();
        toast("Concept saved");
        render({ keepScroll: true });
      },
    });
  });
  $("[data-act=merge]").addEventListener("click", () => mergeDialog("concept", item));
  $("[data-act=delete]").addEventListener("click", () => deleteDialog("concept", item,
    `It will be untagged from ${plural(k.documents.filter((d) => d.direct).length, "document")} and its claims, and its ` +
    `${plural(k.children.length, "sub-concept")} will lose it as a parent.`));
  fillDuplicates($("#dupes-slot"), "concept", item);
}

/* ---------------------------------------------------------------- document page */

function relationSelect(name, value) {
  return `<select name="${name}" class="field-input">${RELATION_ORDER.map((r) =>
    `<option value="${r}" ${r === value ? "selected" : ""} title="${esc(RELATION_HELP[r])}">${RELATION_LABEL[r] || r}</option>`).join("")}</select>`;
}
function strengthSelect(name, value) {
  return `<select name="${name}" class="field-input">${STRENGTHS.map((s) =>
    `<option value="${s}" ${s === (value || "") ? "selected" : ""}>${s || "no strength"}</option>`).join("")}</select>`;
}

function wireDocumentCuration(d) {
  const card = $("#doc-claims");
  const editBtn = $("#edit-claims");
  editBtn.addEventListener("click", () => {
    const editing = card.classList.toggle("editing");
    editBtn.textContent = editing ? "Done" : "Edit";
    if (!editing) return render({ keepScroll: true });
    $$(".claim-row", card).forEach((row) => {
      const c = d.claims.find((x) => String(x.evidence_id) === row.dataset.ev);
      row.innerHTML = `
        <div class="ev-edit">
          <div class="statement" style="font-size:14px">${esc(c.statement)}</div>
          <div class="field-row">
            ${relationSelect("relation", c.relation)}
            ${strengthSelect("strength", c.strength)}
          </div>
          <textarea name="experiment_summary" class="field-input" rows="2" placeholder="What was done to test it (if tested)">${esc(c.experiment_summary || "")}</textarea>
          <div class="field-row">
            <span class="help" data-help>${esc(RELATION_HELP[c.relation])}</span>
            <span style="margin-left:auto;display:flex;gap:6px">
              <button class="btn sm danger" data-remove type="button">Remove from document</button>
              <button class="btn sm primary" data-save type="button">Save</button>
            </span>
          </div>
        </div>`;
      $("[name=relation]", row).addEventListener("change", (e) => { $("[data-help]", row).textContent = RELATION_HELP[e.target.value]; });
      $("[data-save]", row).addEventListener("click", async () => {
        try {
          await api(`/api/evidence/${c.evidence_id}`, { method: "PUT", body: {
            relation: $("[name=relation]", row).value, strength: $("[name=strength]", row).value || null,
            experiment_summary: $("[name=experiment_summary]", row).value } });
          toast("Saved");
          row.classList.add("saved");
        } catch (err) { toast(err.message); }
      });
      $("[data-remove]", row).addEventListener("click", async () => {
        if (!confirm("Remove this claim from the document? The claim itself stays in the map.")) return;
        await api(`/api/evidence/${c.evidence_id}`, { method: "DELETE" });
        row.remove();
        toast("Removed");
      });
    });
    // add a claim this document bears on: existing, or a new one
    const add = document.createElement("div");
    add.className = "add-evidence";
    add.innerHTML = `
      <h4>Add a claim this document bears on</h4>
      <input id="add-claim" class="field-input" placeholder="Search existing claims, or type a new claim statement…">
      <div id="add-chosen" class="small faint" style="margin:6px 0">No claim chosen.</div>
      <div class="field-row">${relationSelect("relation", "supports")}${strengthSelect("strength", "")}</div>
      <textarea name="experiment_summary" class="field-input" rows="2" placeholder="What was done to test it (if tested)"></textarea>
      <div class="field-row"><button class="btn sm primary" id="add-save" type="button" style="margin-left:auto">Add claim</button></div>`;
    card.appendChild(add);
    let chosen = null;
    picker($("#add-claim", add), { kind: "claim", allowCreate: true,
      exclude: () => new Set(d.claims.map((x) => x.slug)),
      onPick: (v) => {
        chosen = v;
        $("#add-chosen", add).innerHTML = v.create ? `New claim: <b>${esc(v.label)}</b>` : `Claim: <b>${esc(v.label)}</b>`;
        $("#add-chosen", add).classList.remove("faint");
      } });
    $("#add-save", add).addEventListener("click", async () => {
      if (!chosen) return toast("Choose or type a claim first");
      try {
        let slug = chosen.slug;
        if (chosen.create) slug = (await api("/api/claims", { method: "POST", body: { statement: chosen.label } })).slug;
        await api(`/api/documents/${enc(d.slug)}/evidence`, { method: "POST", body: {
          claim: slug, relation: $("[name=relation]", add).value, strength: $("[name=strength]", add).value || null,
          experiment_summary: $("[name=experiment_summary]", add).value } });
        invalidateVocab();
        toast("Claim added");
        render({ keepScroll: true });
      } catch (err) { toast(err.message); }
    });
  });

  $("#edit-concepts").addEventListener("click", () => {
    let ed;
    modal({
      title: "Edit document concepts",
      body: `<p class="small muted">★ marks the document's primary concepts (1–3 is typical).</p><div id="ed-doc-concepts"></div>`,
      onMount: (el) => {
        ed = chipEditor($("#ed-doc-concepts", el), { kind: "concept", roles: true, allowCreate: true,
          placeholder: "Add a concept…", items: d.concepts.map((k) => ({ slug: k.slug, label: k.name, role: k.role })) });
      },
      onConfirm: async () => {
        await api(`/api/documents/${enc(d.slug)}/concepts`, { method: "PUT",
          body: ed.get().map((x) => ({ slug: x.slug, role: x.role || "secondary" })) });
        invalidateVocab();
        toast("Concepts saved");
        render({ keepScroll: true });
      },
    });
  });
}

/* ---------------------------------------------------------------- review page */

async function renderReview() {
  const r = await api("/api/review");
  app.className = "";
  const actionText = (e) => {
    const d = e.detail ? JSON.parse(e.detail) : {};
    const link = (slug) => `<a href="#/${e.target_type}/${enc(slug)}">${esc(slug)}</a>`;
    switch (e.action) {
      case "merge": return `merged ${e.target_type} <span class="slug">${esc(d.from)}</span> into ${link(e.target_slug)}`;
      case "delete": return `deleted ${e.target_type} <span class="slug">${esc(e.target_slug)}</span>`;
      case "create": return `created ${e.target_type} ${link(e.target_slug)}`;
      case "edit": return `edited ${e.target_type} ${link(e.target_slug)}`;
      case "edit_evidence": return `changed how a document bears on ${link(e.target_slug)} (${esc(d.before?.[0])} → ${esc(d.after?.[0])})`;
      case "delete_evidence": return `removed a document's ${esc(d.relation)} link to ${link(e.target_slug)}`;
      case "add_evidence": return `linked ${esc(d.document)} to ${link(e.target_slug)} (${esc(d.relation)})`;
      case "edit_concepts": return `edited the concepts of ${link(e.target_slug)}`;
      case "not_duplicates": return `marked ${link(e.target_slug)} and <span class="slug">${esc(d.other)}</span> as different`;
      default: return `${esc(e.action)} ${link(e.target_slug)}`;
    }
  };
  const sideHtml = (kind, s) => `
    <div class="dupe-side">
      ${kind === "claim" ? pill(s.status) : `<span class="faint small">${esc(s.kind)}</span>`}
      <a href="#/${kind}/${enc(s.slug)}" class="${kind === "claim" ? "statement" : ""}" style="color:var(--text);font-weight:${kind === "claim" ? 400 : 600}">${esc(s.label)}</a>
      ${s.detail ? `<div class="small muted">${esc(s.detail)}</div>` : ""}
      <div class="small faint">${esc(s.slug)} · ${kind === "claim" ? plural(s.uses, "evidence link") : plural(s.uses, "document")}</div>
    </div>`;

  app.innerHTML = `
    <div class="page-head">
      <div><h1>Review</h1><p>Check new extractions, clean up duplicate labels, and see what's been changed by hand.
        Edits here are protected: re-importing an extraction won't undo them, and merged or deleted labels stay merged or deleted.</p></div>
    </div>
    <div class="card" id="consolidation-slot" style="margin-bottom:16px"><div class="loading">Loading consolidation plan…</div></div>
    <div class="grid cols-main-side">
      <div class="grid">
        <div class="card">
          <h2>Possible duplicate labels <span class="count">${r.duplicates_total}</span>
            <span class="hint">${r.duplicates_total > r.duplicates.length ? `showing the ${r.duplicates.length} strongest` : "usually from extractions that ran in parallel"}</span></h2>
          ${r.duplicates.length ? r.duplicates.map((p, i) => `
            <div class="dupe-pair" data-i="${i}">
              <div class="dupe-grid">${sideHtml(p.kind, p.a)}<div class="dupe-vs">${Math.round(p.score * 100)}%</div>${sideHtml(p.kind, p.b)}</div>
              <div class="dupe-actions">
                <button class="btn sm" data-keep="a">Keep left, merge right into it</button>
                <button class="btn sm" data-keep="b">Keep right</button>
                <button class="btn sm ghost" data-different>Different ideas</button>
              </div>
            </div>`).join("") : `<div class="empty">No likely duplicates right now.</div>`}
        </div>
        <div class="card">
          <h2>Awaiting review <span class="count">${r.queue.length}</span></h2>
          ${r.queue.length ? `<ul class="link-list">${r.queue.map((d) => `
            <li><span class="date">${year(d.published_date)}</span>
              <span class="lbl"><a href="#/document/${enc(d.slug)}">${esc(d.title)}</a>
                <div class="small muted">${src(d.source)} · ${plural(d.claims, "claim")}${d.curated ? ` · <span class="st-supported">edited</span>` : ""}</div></span>
              <span class="tagline"><button class="btn sm" data-reviewed="${esc(d.slug)}">Mark reviewed</button></span></li>`).join("")}</ul>`
            : `<div class="empty">Everything has been reviewed.</div>`}
        </div>
      </div>
      <div class="card">
        <h2>Edit history</h2>
        ${r.edits.length ? r.edits.map((e) => `
          <div class="note"><div class="head"><span>${esc(e.user || "")} · ${esc((e.at || "").slice(0, 16))}</span></div>
            <div class="small">${actionText(e)}</div></div>`).join("") : `<div class="empty">No edits yet.</div>`}
      </div>
    </div>`;

  fillConsolidation($("#consolidation-slot"));
  $$(".dupe-pair").forEach((el) => {
    const p = r.duplicates[+el.dataset.i];
    el.addEventListener("click", async (e) => {
      const keep = e.target.closest("[data-keep]"), diff = e.target.closest("[data-different]");
      try {
        if (keep) {
          const [stay, go] = keep.dataset.keep === "a" ? [p.a, p.b] : [p.b, p.a];
          await api(`/api/${p.kind}s/${enc(go.slug)}/merge`, { method: "POST", body: { into: stay.slug } });
          toast("Merged");
        } else if (diff) {
          await api("/api/duplicates/dismiss", { method: "POST", body: { kind: p.kind, a: p.a.slug, b: p.b.slug } });
          toast("Marked as different");
        } else return;
        invalidateVocab();
        render({ keepScroll: true });
      } catch (err) { toast(err.message); }
    });
  });
  $$("[data-reviewed]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/documents/${enc(b.dataset.reviewed)}/review`, { method: "POST", body: { reviewed: true } });
    toast("Marked reviewed");
    render({ keepScroll: true });
  }));
}


/* ---------------------------------------------------------------- claim consolidation */

const consolidationView = { status: "pending", q: "", shown: 40 };
// groups most likely to need a human eye: folding an already widely shared claim, or very large
const needsLook = (g) => g.members.length >= 8 || g.members.some((m) => m.documents >= 3);

async function fillConsolidation(slot) {
  const r = await api("/api/consolidation");
  if (!r.plan) {
    slot.innerHTML = `<h2>Claim consolidation</h2><div class="empty">No plan yet. Run <code>cli.py consolidate</code> to propose
      folding paper-specific claims into broader ones.</div>`;
    return;
  }
  const v = consolidationView;
  const counts = { pending: 0, applied: 0, rejected: 0, skipped: 0, failed: 0 };
  r.groups.forEach((g) => (counts[g.status] = (counts[g.status] || 0) + 1));
  const q = v.q.toLowerCase();
  counts.flagged = r.groups.filter((g) => g.status === "pending" && needsLook(g)).length;
  const matches = r.groups.filter((g) => (v.status === "all" || (v.status === "flagged" ? g.status === "pending" && needsLook(g) : g.status === v.status)) &&
    (!q || [g.target_statement, g.concept, ...g.members.map((m) => m.statement)].join(" ").toLowerCase().includes(q)));
  const shown = matches.slice(0, v.shown);
  const folded = r.groups.filter((g) => g.status === "pending").reduce((n, g) => n + g.members.length, 0);

  slot.innerHTML = `
    <h2>Claim consolidation <span class="count">${counts.pending} groups to review</span>
      <span class="hint">${esc(r.plan)} · ${r.status === "ready" ? `${r.batches} batches` : `partial plan${r.stopped ? " (stopped early)" : ""}`}</span></h2>
    <p class="small muted" style="margin:-6px 0 10px">Each group folds narrow or duplicate claims into one broader claim so evidence from
      different papers lands in the same place. <b>Becomes evidence</b>: the claim's text is kept in the evidence description on the target.
      <b>Duplicate</b>: merged. Old labels keep resolving to the target. Pending groups would fold ${folded} claims.</p>
    <div class="toolbar">
      <div class="seg" id="cs-status">${[["pending", "Pending"], ["flagged", "Needs a look"], ["applied", "Applied"], ["rejected", "Rejected"], ["all", "All"]].map(([s, l]) =>
        `<button data-v="${s}" class="${v.status === s ? "on" : ""}" ${s === "flagged" ? 'title="Folds a claim already shared by 3+ documents, or has 8+ members"' : ""}>${l}${s !== "all" ? ` ${counts[s] || 0}` : ""}</button>`).join("")}</div>
      <input type="search" id="cs-q" placeholder="Filter by text or area…" value="${esc(v.q)}">
      ${["pending", "flagged"].includes(v.status) ? `<span class="rd-actions">
        <button class="btn sm" id="cs-all">Select shown</button>
        <button class="btn sm danger" id="cs-reject">Reject selected</button>
        <button class="btn sm primary" id="cs-apply">Apply selected</button></span>` : ""}
    </div>
    ${shown.length ? shown.map((g) => `
      <div class="cs-group ${g.status}" data-id="${g.id}">
        ${g.status === "pending" ? `<input type="checkbox" class="cs-pick" data-id="${g.id}" aria-label="Select group">` : `<span class="nk">${esc(g.status)}</span>`}
        <div>
          ${g.status === "pending" && needsLook(g) ? `<div class="tag-flag small" style="margin-bottom:3px">needs a look: ${g.members.length >= 8 ? `${g.members.length} members` : "folds a claim shared by 3+ documents"}</div>` : ""}
          <div class="cs-target">
            ${g.target_slug ? `<span class="chip">existing · ${plural(g.target_documents, "doc")}</span>
              <a href="#/claim/${enc(g.applied_target || g.target_slug)}" class="statement">${esc(g.target_statement)}</a>`
              : g.applied_target ? `<span class="chip primary">new claim</span> <a href="#/claim/${enc(g.applied_target)}" class="statement">${esc(g.target_statement)}</a>`
              : `<span class="chip primary">new claim · ${esc(g.new_claim_type || "")}</span> <span class="statement">${esc(g.target_statement)}</span>`}
          </div>
          <ul class="cs-members">${g.members.map((m) => `
            <li><span class="rel ${m.kind === "duplicate" ? "rel-replicates" : "rel-extends"}">${m.kind === "duplicate" ? "duplicate" : "becomes evidence"}</span>
              <span>${esc(m.statement)}</span> <span class="faint small">${plural(m.documents, "doc")}</span></li>`).join("")}</ul>
          <div class="small faint">${esc(g.concept)} · ${esc(g.rationale)}${g.error ? ` · <span class="st-refuted">${esc(g.error)}</span>` : ""}</div>
        </div>
      </div>`).join("") : `<div class="empty">Nothing here.</div>`}
    ${matches.length > shown.length ? `<button class="btn sm" id="cs-more" style="margin-top:10px">Show ${Math.min(40, matches.length - shown.length)} more of ${matches.length - shown.length}</button>` : ""}`;

  const rerender = () => fillConsolidation(slot);
  $$("#cs-status button", slot).forEach((b) => b.addEventListener("click", () => { v.status = b.dataset.v; v.shown = 40; rerender(); }));
  $("#cs-q", slot).addEventListener("change", (e) => { v.q = e.target.value; v.shown = 40; rerender(); });
  const more = $("#cs-more", slot);
  if (more) more.addEventListener("click", () => { v.shown += 40; rerender(); });
  const picked = () => $$(".cs-pick:checked", slot).map((x) => +x.dataset.id);
  const all = $("#cs-all", slot);
  if (all) all.addEventListener("click", () => $$(".cs-pick", slot).forEach((x) => (x.checked = true)));
  const act = async (verb) => {
    const ids = picked();
    if (!ids.length) return toast("Select groups first");
    if (verb === "apply" && !confirm(`Apply ${plural(ids.length, "group")}? Claims are folded into their targets (old labels keep resolving).`)) return;
    try {
      const out = await api(`/api/consolidation/${verb}`, { method: "POST", body: { ids } });
      toast(verb === "apply" ? `Applied ${out.applied}${out.failed && out.failed.length ? `, ${out.failed.length} failed` : ""}` : "Rejected");
      invalidateVocab();
      rerender();
    } catch (err) { toast(err.message); }
  };
  const apply = $("#cs-apply", slot), reject = $("#cs-reject", slot);
  if (apply) apply.addEventListener("click", () => act("apply"));
  if (reject) reject.addEventListener("click", () => act("reject"));
}
