import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interp_map import extract, quality
from interp_map.fetchers import Fetched
from interp_map.models import ClaimRef, DocumentMeta, Extraction, ReferenceRef


def vocabulary(prompt):
    return json.loads(prompt.split("<existing_vocabulary>\n", 1)[1].split("\n</existing_vocabulary>", 1)[0])


class PromptPackingTests(unittest.TestCase):
    def setUp(self):
        self.f = Fetched(DocumentMeta(title="Persona steering", abstract="Character drift and alignment"),
                         "FULL SOURCE START\nPersona steering controls character drift.\nFULL SOURCE END")
        self.vocab = {"concepts": [{"slug": "persona", "name": "Persona", "description": "Keep this whole concept"}],
                      "claims": [{"slug": f"other-{i}", "statement": "Unrelated computer vision texture detection", "scope": "z" * 600}
                                 for i in range(20)] + [{"slug": "relevant", "statement": "Persona steering controls character drift", "scope": "q" * 600}]}
        self.base = extract._transport_chars(extract.build_codex_prompt(self.f, {**self.vocab, "claims": []}))

    def test_relevance_full_source_concepts_and_no_input_mutation(self):
        original = copy.deepcopy(self.vocab)
        metadata = {}
        with patch.object(extract, "CODEX_PROMPT_MAX_CHARS", self.base + 1500):
            prompt = extract.build_codex_prompt(self.f, self.vocab, packing_metadata=metadata)
            again = extract.build_codex_prompt(self.f, self.vocab)
        packed = vocabulary(prompt)
        self.assertIn("relevant", [c["slug"] for c in packed["claims"]])
        self.assertEqual(packed["concepts"], original["concepts"])
        self.assertIn(self.f.text, prompt)
        self.assertEqual(self.vocab, original)
        self.assertEqual(prompt, again)
        self.assertGreater(metadata["omitted_claims"], 0)
        self.assertLessEqual(metadata["transport_chars"], self.base + 1500)
        for claim in packed["claims"]:
            self.assertIn(claim, original["claims"])

    def test_mandatory_canonical_scope_and_transitive_dependency_survive(self):
        self.vocab["claims"][0]["depends_on"] = ["other-1"]
        with patch.object(extract, "CODEX_PROMPT_MAX_CHARS", self.base + 1550):
            prompt = extract.build_codex_prompt(self.f, self.vocab, mandatory_claim_slugs=["other-0"])
        packed = {c["slug"]: c for c in vocabulary(prompt)["claims"]}
        for slug in ["other-0", "other-1"]:
            self.assertEqual(packed[slug]["scope"], "z" * 600)

    def test_full_reference_records_preserved_beyond_old_200_link_limit(self):
        self.f.references = [ReferenceRef(title=f"Reference {i}", url=f"https://example.org/{i}", rationale="Keep metadata") for i in range(205)]
        prompt = extract.build_codex_prompt(self.f, {"concepts": [], "claims": []})
        self.assertIn("Reference 204", prompt)
        self.assertIn("Keep metadata", prompt)

    def test_unfittable_source_or_mandatory_context_raises_before_provider(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(extract, "CODEX_RUNS_DIR", Path(tmp)), \
                patch.object(extract, "CODEX_PROMPT_MAX_CHARS", self.base - 1), \
                patch.object(extract, "run_codex_cli") as run:
            with self.assertRaises(extract.PromptBudgetError):
                extract.via_codex_cli(self.f, self.vocab)
            run.assert_not_called()
        with patch.object(extract, "CODEX_PROMPT_MAX_CHARS", self.base + 100):
            with self.assertRaises(extract.PromptBudgetError):
                extract.build_codex_prompt(self.f, self.vocab, mandatory_claim_slugs=["other-0"])

    def test_audit_budgets_preamble_draft_verification_and_mandatory_claims(self):
        draft = Extraction(document=self.f.meta, summary="Draft", claims=[ClaimRef(
            slug="other-0", statement="Draft restatement", relation="assumes", depends_on=["other-1"])])
        with tempfile.TemporaryDirectory() as tmp, patch.object(quality, "QUALITY_AUDITS_DIR", Path(tmp)), \
                patch.object(extract, "CODEX_PROMPT_MAX_CHARS", self.base + 6000):
            def run(prompt, schema, **kwargs):
                self.assertLessEqual(extract._transport_chars(prompt), self.base + 6000)
                self.assertIn("VERIFIED SOURCE", prompt)
                self.assertIn(self.f.text, prompt)
                packed = {c["slug"]: c for c in vocabulary(prompt)["claims"]}
                self.assertEqual(packed["other-0"]["scope"], "z" * 600)
                self.assertEqual(packed["other-1"]["scope"], "z" * 600)
                return draft.model_dump_json()
            with patch.object(extract, "run_codex_cli", side_effect=run):
                quality.audit_extraction(self.f, self.vocab, draft, source_verification="VERIFIED SOURCE")
            record = json.loads(next(Path(tmp).glob("*.json")).read_text())
            self.assertEqual(record["prompt_packing"]["mandatory_slugs"], ["other-0", "other-1"])

    def test_provider_content_restriction_is_typed_not_retried(self):
        response = subprocess.CompletedProcess([], 1, "", "Invalid prompt: we've limited access to this content for safety reasons")
        with patch.object(extract, "find_codex", return_value="unused"), \
                patch.object(extract.subprocess, "run", return_value=response):
            with self.assertRaises(extract.ContentRestrictionError):
                extract.run_codex_cli("test", Extraction.model_json_schema())
        with tempfile.TemporaryDirectory() as tmp, patch.object(extract, "CODEX_RUNS_DIR", Path(tmp)), \
                patch.object(extract, "run_codex_cli", side_effect=extract.ContentRestrictionError("blocked")) as run:
            with self.assertRaises(extract.ContentRestrictionError):
                extract.via_codex_cli(self.f, self.vocab)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
