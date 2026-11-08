import tempfile
import unittest
from pathlib import Path

from interp_map import db


class RelevantVocabularyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.init(self.conn)
        c = self.conn
        self.cited = db.upsert_document(c, title="Cited SAE paper", url="https://example.org/cited", status="extracted")
        self.doc = db.upsert_document(c, title="Feature absorption in sparse autoencoders", url="https://example.org/new",
                                      status="fetched", abstract="We study absorption of parent features by SAE latents.")
        c.execute("INSERT INTO document_links VALUES (?, ?, 'explicit_citation', NULL, NULL, 'test')", (self.doc, self.cited))
        on_cited = db.upsert_claim(c, "cited-paper-claim", "Dictionary width changes which latents appear.")
        c.execute("INSERT INTO claim_evidence (claim_id, document_id, relation) VALUES (?, ?, 'proposes')", (on_cited, self.cited))
        db.upsert_claim(c, "absorption", "SAE latents absorb features of more general parent latents.")
        for i in range(400):
            db.upsert_claim(c, f"noise-{i}", f"Unrelated vision texture result number {i} about convolutional filters.")
        db.upsert_concept(c, "sparse-autoencoders", "Sparse autoencoders", "Dictionary learning on activations", "method")
        db.upsert_concept(c, "curve-detectors", "Curve detectors", "Vision neurons", "phenomenon")
        c.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def vocab(self, **kw):
        return db.relevant_vocabulary(self.conn, self.doc, "Feature absorption in sparse autoencoders",
                                      "We study absorption of parent features by SAE latents.", "", **kw)

    def test_cited_and_lexical_claims_included_and_capped(self):
        v = self.vocab(max_claims=30, hub_claims=5)
        slugs = {c["slug"] for c in v["claims"]}
        self.assertIn("cited-paper-claim", slugs)      # attached to a cited document
        self.assertIn("absorption", slugs)             # matches the title and abstract
        self.assertLessEqual(len(v["claims"]), 30)
        self.assertNotEqual(len(v["claims"]), 402)

    def test_every_concept_slug_is_still_listed(self):
        v = self.vocab(max_concepts=1)
        listed = {c["slug"] for c in v["concepts"]} | set(v["other_concept_slugs"])
        self.assertEqual(listed, {"sparse-autoencoders", "curve-detectors"})
        self.assertEqual(v["concepts"][0]["slug"], "sparse-autoencoders")


if __name__ == "__main__":
    unittest.main()
