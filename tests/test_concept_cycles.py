import json
import tempfile
import unittest
from pathlib import Path

from interp_map import db
from interp_map.models import ConceptRef, DocumentMeta, Extraction


class IngestionConceptCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def edge(self, child, parent):
        self.conn.execute("INSERT INTO concept_relations(child_id,parent_id) VALUES (?,?)",
                          (db.ensure_concept(self.conn, child), db.ensure_concept(self.conn, parent)))

    def edges(self):
        return set(tuple(r) for r in self.conn.execute(
            "SELECT c.slug,p.slug FROM concept_relations r JOIN concepts c ON c.id=r.child_id "
            "JOIN concepts p ON p.id=r.parent_id"))

    def ingest(self, concepts):
        return db.ingest_extraction(self.conn, Extraction(
            document=DocumentMeta(title="Audited source", url="https://example.org/source"),
            summary="Retain the extraction", concepts=concepts,
            provenance="codex-cli:gpt-6.1-sol:reasoning=high + source-audited"))

    def test_reciprocal_parent_is_rejected_but_extraction_and_valid_edges_survive(self):
        self.edge("representation-engineering", "activation-steering")
        doc_id = self.ingest([ConceptRef(slug="activation-steering", name="Activation steering",
                                       parents=["representation-engineering", "interventions"])])
        self.assertEqual(self.edges(), {("representation-engineering", "activation-steering"),
                                       ("activation-steering", "interventions")})
        self.assertEqual(self.conn.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()[0], "extracted")
        row = self.conn.execute("SELECT * FROM edit_log WHERE action='reject_concept_parent'").fetchone()
        detail = json.loads(row["detail"])
        self.assertEqual(row["target_slug"], "activation-steering")
        self.assertEqual(detail["parent"], "representation-engineering")
        self.assertEqual(detail["document_id"], doc_id)
        self.assertIn("gpt-6.1-sol:reasoning=high", detail["provenance"])

    def test_indirect_cycle_with_edges_added_in_same_extraction_is_rejected(self):
        self.ingest([ConceptRef(slug="a", name="A", parents=["b"]),
                     ConceptRef(slug="b", name="B", parents=["c"]),
                     ConceptRef(slug="c", name="C", parents=["a"])])
        self.assertEqual(self.edges(), {("a", "b"), ("b", "c")})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM document_concepts").fetchone()[0], 3)

    def test_alias_resolved_self_parent_is_rejected_and_logged(self):
        db.ensure_concept(self.conn, "canonical")
        self.conn.execute("INSERT INTO vocab_aliases(kind,old_slug,new_slug) VALUES ('concept','alias','canonical')")
        self.ingest([ConceptRef(slug="canonical", name="Canonical", parents=["alias"])])
        self.assertEqual(self.edges(), set())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edit_log WHERE action='reject_concept_parent'").fetchone()[0], 1)

    def test_legacy_cycle_does_not_loop_or_block_unrelated_safe_edge(self):
        self.edge("a", "b")
        self.edge("b", "a")
        self.ingest([ConceptRef(slug="new", name="New", parents=["a"])])
        self.assertEqual(self.edges(), {("a", "b"), ("b", "a"), ("new", "a")})


if __name__ == "__main__":
    unittest.main()
