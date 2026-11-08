import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interp_map import consolidate, db


class ConsolidateApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = c = db.connect(Path(self.tmp.name) / "t.db")
        db.init(c)
        self.d1 = db.upsert_document(c, title="Paper one", url="https://example.org/1", status="extracted")
        self.d2 = db.upsert_document(c, title="Paper two", url="https://example.org/2", status="extracted")
        self.broad = db.upsert_claim(c, "steering-works", "Adding feature directions steers behaviour.")
        self.narrow = db.upsert_claim(c, "steer-french", "Steering one SAE feature in Gemma induces French output.")
        self.dupe = db.upsert_claim(c, "steering-works-2", "Feature-direction addition steers model behaviour.")
        c.execute("INSERT INTO claim_evidence (claim_id, document_id, relation, strength, experiment_summary) "
                  "VALUES (?, ?, 'proposes', 'moderate', 'Table 3')", (self.narrow, self.d1))
        c.execute("INSERT INTO claim_evidence (claim_id, document_id, relation) VALUES (?, ?, 'assumes')", (self.dupe, self.d2))
        c.execute("INSERT INTO claim_evidence (claim_id, document_id, relation) VALUES (?, ?, 'proposes')", (self.broad, self.d2))
        c.commit()
        self.plan = Path(self.tmp.name) / "plan.json"
        self.plan.write_text(json.dumps({"groups": [
            {"id": 0, "status": "pending", "target_slug": "steering-works", "target_statement": "", "new_claim_type": None,
             "members": [{"slug": "steer-french", "kind": "instance"}, {"slug": "steering-works-2", "kind": "duplicate"}]},
            {"id": 1, "status": "pending", "target_slug": None, "target_statement": "A new broader claim.",
             "new_claim_type": "empirical", "members": [{"slug": "missing-a", "kind": "instance"}]},
        ]}), encoding="utf-8")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_instance_becomes_evidence_and_aliases_resolve(self):
        result = consolidate.apply_groups(self.conn, self.plan, [0])
        self.assertEqual(result["applied"], 1)
        rows = self.conn.execute("SELECT document_id, relation, experiment_summary FROM claim_evidence WHERE claim_id = ?",
                                 (self.broad,)).fetchall()
        by_doc = {(r[0], r[1]): r[2] for r in rows}
        # the narrow claim's tested result now supports the broad claim, keeping its text
        self.assertIn((self.d1, "supports"), by_doc)
        self.assertIn("Steering one SAE feature in Gemma", by_doc[(self.d1, "supports")])
        self.assertIn("Table 3", by_doc[(self.d1, "supports")])
        # duplicate merged; both old slugs resolve to the target for future extractions
        self.assertIn((self.d2, "assumes"), by_doc)
        for old in ("steer-french", "steering-works-2"):
            self.assertIsNone(self.conn.execute("SELECT 1 FROM claims WHERE slug = ?", (old,)).fetchone())
            self.assertEqual(db.resolve_slug(self.conn, "claim", old), "steering-works")
        self.assertEqual(json.loads(self.plan.read_text())["groups"][0]["status"], "applied")

    def test_reject_and_skip_vanished_members(self):
        consolidate.reject_groups(self.plan, [0])
        self.assertEqual(consolidate.apply_groups(self.conn, self.plan, [0])["applied"], 0)  # rejected stays rejected
        out = consolidate.apply_groups(self.conn, self.plan, [1])  # its only member no longer exists
        self.assertEqual(out["applied"], 0)
        self.assertEqual(json.loads(self.plan.read_text())["groups"][1]["status"], "skipped")
        # no empty claim was created for the proposed target
        self.assertIsNone(self.conn.execute("SELECT 1 FROM claims WHERE statement = 'A new broader claim.'").fetchone())


class ConsolidateValidateTests(unittest.TestCase):
    def test_short_ids_map_back_and_bad_ids_are_dropped(self):
        batch = {"concept": "Steering", "hubs": [{"slug": "hub", "documents": 9, "statement": "Broad claim."}],
                 "claims": [{"slug": f"n{i}", "documents": 1, "statement": f"Narrow {i}."} for i in range(1, 5)]}
        result = consolidate.BatchResult(groups=[
            consolidate.Group(target="h1", members=[consolidate.Member(id="c1", kind="instance"),
                                                    consolidate.Member(id="C2", kind="duplicate"),
                                                    consolidate.Member(id="c99", kind="instance"),   # invented
                                                    consolidate.Member(id="h1", kind="instance")],   # target itself
                              why="same idea"),
            consolidate.Group(target="c3", members=[consolidate.Member(id="c1", kind="instance")], why="reused"),
            consolidate.Group(target=None, new_statement="New broad.", members=[consolidate.Member(id="c4", kind="instance")],
                              why="only one member"),
        ])
        groups = consolidate.validate(batch, result)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["target_slug"], "hub")
        self.assertEqual([(m["slug"], m["kind"]) for m in groups[0]["members"]], [("n1", "instance"), ("n2", "duplicate")])
        prompt = consolidate._prompt(batch)
        self.assertIn("c1 | 1 | Narrow 1.", prompt)
        self.assertIn("h1 | 9 | Broad claim.", prompt)
        self.assertNotIn("n1 |", prompt)  # slugs stay out of the prompt


if __name__ == "__main__":
    unittest.main()
