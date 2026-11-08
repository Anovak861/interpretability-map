"""Build a large synthetic map in a separate database, to see how the graph views behave at
scale before the real map gets there. It never touches data/interp_map.db.

    .venv/Scripts/python scripts/make_synthetic.py data/synthetic.db [documents]

Then view it with:  INTERP_MAP_DB=data/synthetic.db .venv/Scripts/python -m interp_map.web.server
"""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from interp_map import db  # noqa: E402

random.seed(7)
out = Path(sys.argv[1])
if out.resolve() == db.ROOT.joinpath("data", "interp_map.db").resolve():
    sys.exit("refusing to overwrite the real database; pass another path")
out.unlink(missing_ok=True)
conn = db.connect(out)
db.init(conn)

AREAS = {
    "Sparse dictionary learning": ["SAE architectures", "Transcoders", "Crosscoders", "Feature splitting", "Feature absorption",
                                   "Dead latents", "SAE scaling laws", "Dictionary evaluation", "Matryoshka SAEs"],
    "Circuit discovery": ["Activation patching", "Path patching", "Attribution patching", "ACDC", "Attribution graphs",
                          "Edge pruning", "Circuit faithfulness", "Self-repair"],
    "Probing": ["Linear probes", "Nonlinear probes", "Control tasks", "Probe selectivity", "Concept erasure", "Sparse probing"],
    "Steering and control": ["Activation addition", "Representation engineering", "Directional ablation",
                             "Refusal directions", "Steering vector generalization", "Persona vectors"],
    "Superposition theory": ["Toy models", "Feature geometry", "Computation in superposition", "Interference",
                             "Polytope structure", "Privileged bases"],
    "Attention mechanisms": ["Induction heads", "QK/OV circuits", "Copy suppression", "Attention sinks",
                             "Positional mechanisms", "Head composition"],
    "Training dynamics": ["Phase changes", "Grokking", "Developmental interpretability", "Loss landscape",
                          "Learning coefficient", "Circuit formation"],
    "Automated interpretability": ["Neuron explanations", "Explanation scoring", "Agentic interpretability",
                                   "Auto-labelling features", "Simulation scoring"],
    "Model internals for safety": ["Deception detection", "Lie probes", "Sleeper agents", "Monitoring",
                                   "Eliciting latent knowledge", "Backdoor detection"],
    "Representation geometry": ["Linear representation hypothesis", "Multi-dimensional features", "Manifolds",
                                "Causal inner product", "Feature directions"],
    "Factual recall": ["Knowledge neurons", "ROME", "MEMIT", "Entity tracking", "Attribute extraction"],
    "Vision interpretability": ["Curve detectors", "Feature visualization", "Multimodal neurons", "CLIP features"],
}
WEIGHTS = [10, 9, 6, 7, 5, 5, 4, 4, 5, 4, 3, 2]

cid = {}
def concept(name, kind="topic", parents=()):
    slug = db.slugify(name)
    cid[slug] = db.upsert_concept(conn, slug, name, f"Synthetic concept: {name}.", kind)
    for p in parents:
        conn.execute("INSERT OR IGNORE INTO concept_relations (child_id, parent_id) VALUES (?, ?)", (cid[slug], cid[p]))
    return slug

root = concept("Mechanistic interpretability")
area_concepts = {}
for area, subs in AREAS.items():
    a = concept(area, "topic", [root])
    area_concepts[a] = [a]
    for s in subs:
        k = concept(s, random.choice(["method", "phenomenon", "hypothesis"]), [a])
        area_concepts[a].append(k)
        # a few sub-sub-concepts
        if random.random() < 0.35:
            area_concepts[a].append(concept(f"{s}: variants", "method", [k]))
areas = list(area_concepts)
# cross-area parents (concepts that genuinely straddle two areas)
for a in areas:
    for k in random.sample(area_concepts[a][1:], k=min(2, len(area_concepts[a]) - 1)):
        if random.random() < 0.4:
            other = random.choice([x for x in areas if x != a])
            conn.execute("INSERT OR IGNORE INTO concept_relations (child_id, parent_id) VALUES (?, ?)", (cid[k], cid[other]))

first = ["Ana", "Ben", "Chen", "Dara", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jun", "Kai", "Lena", "Max", "Nia", "Oren",
         "Pia", "Quin", "Rui", "Sam", "Tara", "Uma", "Vik", "Wen", "Xia", "Yan", "Zoe"]
last = ["Abe", "Brook", "Cruz", "Dahl", "Ekko", "Fenn", "Gray", "Holt", "Ito", "Jade", "Kerr", "Lund", "Moss", "Nakai",
        "Ortiz", "Park", "Quill", "Ross", "Sato", "Tran", "Ueda", "Vance", "Wolfe", "Young"]
groups = [[f"{random.choice(first)} {random.choice(last)}" for _ in range(random.randint(2, 6))] for _ in range(60)]

docs, claims_by_area = [], {a: [] for a in areas}
start = date(2019, 1, 1)
N = int(sys.argv[2]) if len(sys.argv) > 2 else 350
for i in range(N):
    # more documents in later years, like the real field
    t = random.random() ** 0.6
    d = start + timedelta(days=int(t * (date(2026, 10, 1) - start).days))
    area = random.choices(areas, WEIGHTS)[0]
    words = random.sample(area_concepts[area][1:], k=min(2, len(area_concepts[area]) - 1))
    title = f"{random.choice(['On', 'Towards', 'Scaling', 'Revisiting', 'Understanding', 'Measuring'])} " \
            f"{words[0].split(':')[0].lower()} {random.choice(['in language models', 'at scale', 'in transformers', 'and its limits', 'in practice'])}"
    doc_id = db.upsert_document(conn, title=f"{title} ({i})", doc_type=random.choice(["paper"] * 4 + ["blog_post"]),
                                source=random.choice(["arxiv"] * 5 + ["lesswrong", "transformer_circuits", "alignmentforum"]),
                                url=f"https://example.org/synthetic/{i}", published_date=d.isoformat(),
                                summary="Synthetic document for layout testing.", status="extracted")
    db.set_authors(conn, doc_id, random.choice(groups))
    conn.execute("INSERT OR REPLACE INTO document_concepts VALUES (?, ?, 'primary')", (doc_id, cid[words[0]]))
    for k in words[1:]:
        conn.execute("INSERT OR REPLACE INTO document_concepts VALUES (?, ?, 'secondary')", (doc_id, cid[k]))
    if random.random() < 0.3:  # cross-area secondary concept
        other = random.choice(area_concepts[random.choice(areas)])
        conn.execute("INSERT OR IGNORE INTO document_concepts VALUES (?, ?, 'secondary')", (doc_id, cid[other]))
    docs.append((doc_id, d, area))

docs.sort(key=lambda x: x[1])
indeg = {d: 0 for d, _, _ in docs}
for idx, (doc_id, d, area) in enumerate(docs):
    earlier = docs[:idx]
    if earlier:
        # preferential attachment, biased to the same area
        w = [(indeg[e] + 1) * (4 if a == area else 1) for e, _, a in earlier]
        for e in set(random.choices([e for e, _, _ in earlier], w, k=min(len(earlier), random.randint(2, 9)))):
            conn.execute("INSERT OR IGNORE INTO document_links VALUES (?, ?, 'explicit_citation', NULL, NULL, 'synthetic')", (doc_id, e))
            indeg[e] += 1
    # propose new claims, engage with earlier ones
    for j in range(random.randint(1, 3)):
        slug = f"claim-{doc_id}-{j}"
        c = db.upsert_claim(conn, slug, f"Synthetic claim {j} from document {doc_id} about {area.lower()}.")
        conn.execute("INSERT INTO claim_evidence (claim_id, document_id, relation, strength) VALUES (?, ?, 'proposes', 'moderate')", (c, doc_id))
        conn.execute("INSERT OR IGNORE INTO claim_concepts VALUES (?, ?)", (c, cid[area]))
        claims_by_area[area].append(c)
    pool = claims_by_area[area][:-3] or []
    for c in random.sample(pool, k=min(len(pool), random.randint(0, 4))):
        rel = random.choice(["assumes", "assumes", "supports", "supports", "extends", "refutes", "mixed", "replicates"])
        conn.execute("INSERT OR IGNORE INTO claim_evidence (claim_id, document_id, relation, strength) VALUES (?, ?, ?, 'moderate')", (c, doc_id, rel))
conn.commit()
print(f"{N} documents, {len(cid)} concepts,",
      conn.execute("select count(*) from document_links").fetchone()[0], "citations,",
      conn.execute("select count(*) from claims").fetchone()[0], "claims")
