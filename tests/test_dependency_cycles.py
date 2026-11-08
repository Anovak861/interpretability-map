import tempfile
import unittest
from pathlib import Path

from interp_map import db


class DependencyCycleTests(unittest.TestCase):
    def test_cycle_broken_toward_more_foundational_claims_and_chains_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = db.connect(Path(tmp) / "t.db")
            db.init(c)
            ids = {s: db.upsert_claim(c, s, s) for s in ("broad", "mid", "narrow", "x", "y")}
            docs = [db.upsert_document(c, title=f"d{i}", url=f"https://e.org/{i}", status="extracted") for i in range(3)]
            for i, d in enumerate(docs):   # broad: 3 documents, mid: 2, narrow: 1
                c.execute("INSERT INTO claim_evidence (claim_id, document_id, relation) VALUES (?, ?, 'supports')", (ids["broad"], d))
                if i < 2:
                    c.execute("INSERT INTO claim_evidence (claim_id, document_id, relation) VALUES (?, ?, 'supports')", (ids["mid"], d))
                if i < 1:
                    c.execute("INSERT INTO claim_evidence (claim_id, document_id, relation) VALUES (?, ?, 'supports')", (ids["narrow"], d))
            edges = [("narrow", "mid"), ("mid", "broad"), ("broad", "narrow"), ("x", "y")]  # a 3-cycle plus a plain chain
            c.executemany("INSERT INTO claim_dependencies (claim_id, depends_on_id) VALUES (?, ?)",
                          [(ids[a], ids[b]) for a, b in edges])
            removed = db.break_dependency_cycles(c)
            left = {(a, b) for a, b in c.execute("SELECT claim_id, depends_on_id FROM claim_dependencies")}
            self.assertEqual(removed, 1)
            self.assertNotIn((ids["broad"], ids["narrow"]), left)   # broad no longer depends on narrow
            self.assertIn((ids["narrow"], ids["mid"]), left)
            self.assertIn((ids["mid"], ids["broad"]), left)
            self.assertIn((ids["x"], ids["y"]), left)               # unrelated chain untouched
            c.close()


if __name__ == "__main__":
    unittest.main()
