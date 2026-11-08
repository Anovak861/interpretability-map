import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interp_map import db, extract, quality
from interp_map.fetchers import Fetched
from interp_map.models import DocumentMeta, Extraction


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = patch.object(quality, "QUALITY_AUDITS_DIR", self.root / "logs")
        self.logs.start()
        self.conn = db.connect(self.root / "test.db")
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self.logs.stop()
        self.tmp.cleanup()

    def concept(self, slug, name="Same name"):
        return db.upsert_concept(self.conn, slug, name, "A concept definition", "method")

    def decision(self, confidence=.99, verdict="equivalent", a="new", b="old"):
        return {"a": a, "b": b, "verdict": verdict, "confidence": confidence,
                "rationale": "Both definitions refer to the same method."}

    def review(self, decisions):
        return patch.object(extract, "run_codex_cli", return_value=json.dumps({"decisions": decisions}))

    def test_audit_uses_source_and_authoritative_metadata_and_saves_metrics(self):
        f = Fetched(DocumentMeta(title="Authoritative", url="https://example.org"), "FULL SOURCE")
        draft = Extraction(document=f.meta, summary="Draft summary")
        response = draft.model_copy(deep=True)
        response.document.title = "Hallucinated metadata"
        response.summary = "Corrected summary"

        def run(prompt, schema, **kwargs):
            self.assertIn("FULL SOURCE", prompt)
            self.assertIn("Draft summary", prompt)
            self.assertIn("canonical-claim", prompt)
            self.assertEqual(kwargs["model"], "gpt-6.1-sol")
            self.assertEqual(kwargs["reasoning_effort"], "high")
            kwargs["telemetry"]["usage"] = {"output_tokens": 20}
            return response.model_dump_json()

        with patch.object(extract, "run_codex_cli", side_effect=run):
            result = quality.audit_extraction(f, {"claims": [{"slug": "canonical-claim"}]}, draft)
        self.assertEqual(result.document.title, "Authoritative")
        self.assertEqual(result.summary, "Corrected summary")
        self.assertEqual(draft.summary, "Draft summary")
        self.assertEqual(result.provenance, "codex-cli:gpt-6.1-sol:reasoning=high + source-audited")
        record = json.loads(next((self.root / "logs").glob("*.json")).read_text())
        self.assertEqual(record["telemetry"]["usage"]["output_tokens"], 20)
        self.assertTrue(record["validated"])

    def test_audit_failure_propagates_and_records_before(self):
        f = Fetched(DocumentMeta(title="Doc"), "Source")
        ex = Extraction(document=f.meta, summary="Draft")
        for error in (extract.UsageLimitError("limit"), extract.NotLoggedInError("login"), ValueError("invalid")):
            with patch.object(extract, "run_codex_cli", side_effect=error):
                with self.assertRaises(type(error)):
                    quality.audit_extraction(f, {}, ex)
        records = [json.loads(p.read_text()) for p in (self.root / "logs").glob("*.json")]
        self.assertEqual(len(records), 3)
        self.assertTrue(all("before" in r and "error" in r and "after" not in r for r in records))

    def test_incomplete_source_cannot_pass_audit(self):
        f = Fetched(DocumentMeta(title="Doc"), "Text ends in the middle of a proof")
        ex = Extraction(document=f.meta, summary="INCOMPLETE_SOURCE: proof and results missing")
        with patch.object(extract, "run_codex_cli", return_value=ex.model_dump_json()):
            with self.assertRaises(quality.IncompleteSourceError):
                quality.audit_extraction(f, {}, ex)
        record = json.loads(next((self.root / "logs").glob("*.json")).read_text())
        self.assertNotIn("validated", record)
        self.assertIn("IncompleteSourceError", record["error"])

    def test_exact_names_are_candidates_and_existing_concept_is_retained(self):
        self.concept("old")
        self.concept("new")
        with patch.object(db, "possible_vocab_duplicates", return_value=[]), self.review([self.decision()]):
            result = quality.merge_wave_concepts(self.conn, ["new"])
        self.assertEqual(result[0]["status"], "merged")
        self.assertEqual(db.resolve_slug(self.conn, "concept", "new"), "old")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edit_log WHERE action='codex_merge_review'").fetchone()[0], 1)

    def test_invalid_audit_json_cannot_be_returned_for_import(self):
        f = Fetched(DocumentMeta(title="Doc"), "Source")
        ex = Extraction(document=f.meta, summary="Draft")
        with patch.object(extract, "run_codex_cli", return_value='{"summary": "missing document"}'):
            with self.assertRaises(ValueError):
                quality.audit_extraction(f, {}, ex)
        record = json.loads(next((self.root / "logs").glob("*.json")).read_text())
        self.assertIn("raw_output", record)
        self.assertNotIn("after", record)

    def test_merge_quota_error_propagates_without_changes(self):
        self.concept("old")
        self.concept("new")
        with patch.object(extract, "run_codex_cli", side_effect=extract.UsageLimitError("limit")):
            with self.assertRaises(extract.UsageLimitError):
                quality.merge_wave_concepts(self.conn, ["new"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM concepts").fetchone()[0], 2)

    def test_ambiguous_low_confidence_unsolicited_and_conflicting_pairs_untouched(self):
        self.concept("old")
        self.concept("new")
        for decisions in ([self.decision(.97)], [self.decision(verdict="uncertain")],
                          [self.decision(a="not-a-candidate")],
                          [self.decision(), self.decision(verdict="distinct")]):
            with self.review(decisions):
                result = quality.merge_wave_concepts(self.conn, ["new"])
            self.assertTrue(all(r["status"] == "left_unchanged" for r in result))
            self.assertEqual(self.conn.execute("SELECT count(*) FROM concepts").fetchone()[0], 2)

    def test_merge_cannot_create_indirect_cycle(self):
        old = self.concept("old")
        new = self.concept("new")
        middle = self.concept("middle", "Different")
        self.conn.executemany("INSERT INTO concept_relations(child_id,parent_id) VALUES (?,?)",
                              [(new, middle), (middle, old)])
        self.conn.commit()
        with self.review([self.decision()]):
            result = quality.merge_wave_concepts(self.conn, ["new"])
        self.assertEqual(result[0]["status"], "cycle_blocked")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM concepts").fetchone()[0], 3)

    def test_no_wave_candidates_needs_no_provider(self):
        self.concept("old")
        with patch.object(extract, "run_codex_cli") as run:
            self.assertEqual(quality.merge_wave_concepts(self.conn, []), [])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
