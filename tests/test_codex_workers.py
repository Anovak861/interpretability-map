import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
from interp_map import db, extract, pipeline
from interp_map.fetchers import Fetched
from interp_map.models import Extraction, DocumentMeta


def fetched(title="Test paper"):
    return Fetched(DocumentMeta(title=title, url="https://example.org/" + title.replace(" ", "-")),
                   "A synthetic paper about activation patching.", [])


def result(f):
    return json.dumps({
        "document": {"title": "Model restatement", "url": "https://wrong.example"},
        "summary": "Synthetic summary",
        "concepts": [{"slug": "activation-patching", "name": "Activation patching",
                      "parents": ["causal-analysis"], "role": "primary"}],
        "claims": [{"slug": "patching-localizes", "statement": "Patching localizes behavior",
                    "relation": "supports", "concepts": ["activation-patching"],
                    "strength": "moderate", "experiment_summary": "Synthetic experiment"}],
        "references": [{"title": "Earlier work", "url": "https://example.org/prior",
                        "link_type": "explicit_citation"}],
    })


class CodexWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = patch.object(extract, "CODEX_RUNS_DIR", Path(self.tmp.name))
        self.logs.start()

    def tearDown(self):
        self.logs.stop()
        self.tmp.cleanup()

    def test_model_settings_are_explicit_and_invalid_none_is_rejected(self):
        with patch.dict(extract.os.environ, {"CODEX_MODEL": "gpt-6-astra", "CODEX_REASONING_EFFORT": "medium"}):
            self.assertEqual(extract.codex_settings(), ("gpt-6-astra", "medium"))
        for model in ("gpt-6.1-sol", "gpt-6-astra"):
            with self.assertRaises(ValueError):
                extract.codex_settings(model, "none")

    @patch.object(extract, "find_codex", return_value="codex.exe")
    def test_unset_reasoning_control_is_not_explicit_none(self, _):
        def run(command, **kwargs):
            self.assertFalse(any("model_reasoning_effort" in arg for arg in command))
            Path(command[command.index("--output-last-message") + 1]).write_text('{"items": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(extract.subprocess, "run", side_effect=run):
            extract.run_codex_cli("test", pipeline.TRIAGE_SCHEMA, model="gpt-6.1-sol", reasoning_effort="default")
    def test_strict_schema_preserves_nullable_fields_and_original(self):
        original = Extraction.model_json_schema()
        schema = extract.strict_schema(original)
        def check(node):
            if isinstance(node, dict):
                self.assertNotIn("default", node)
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)
        check(schema)
        self.assertIn({"type": "null"}, schema["$defs"]["DocumentMeta"]["properties"]["url"]["anyOf"])
        self.assertNotIn("additionalProperties", original)

    @patch.object(extract, "find_codex", return_value="codex.exe")
    def test_worker_uses_stdin_isolated_output_and_read_only(self, _):
        paths = []
        def run(command, **kwargs):
            paths.append(kwargs["cwd"])
            self.assertEqual(command[-1], "-")
            self.assertIn("read-only", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("paper text", kwargs["input"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("CODEX_API_KEY", kwargs["env"])
            schema_path = Path(command[command.index("--output-schema") + 1])
            self.assertFalse(json.loads(schema_path.read_text())["additionalProperties"])
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text('{"items": []}', encoding="utf-8")
            self.assertEqual(command[command.index("--model") + 1], "gpt-6-astra")
            self.assertIn('model_reasoning_effort="high"', command)
            return subprocess.CompletedProcess(command, 0, json.dumps({"type":"turn.completed",
                "usage":{"input_tokens":123,"cached_input_tokens":40,"output_tokens":12}}), "")
        with patch.dict(extract.os.environ, {"OPENAI_API_KEY": "unused", "CODEX_API_KEY": "unused"}), \
                patch.object(extract.subprocess, "run", side_effect=run):
            for _ in range(2):
                telemetry = {}
                self.assertEqual(extract.run_codex_cli("paper text", pipeline.TRIAGE_SCHEMA,
                    model="gpt-6-astra", reasoning_effort="high", telemetry=telemetry), '{"items": []}')
                self.assertEqual(telemetry["usage"]["input_tokens"], 123)
                self.assertEqual(telemetry["usage"]["cached_input_tokens"], 40)
                self.assertEqual(telemetry["reasoning_effort"], "high")
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(not p.exists() for p in paths))

    @patch.object(extract, "find_codex", return_value="codex.exe")
    def test_errors_and_empty_output(self, _):
        for message, expected in [("401 Unauthorized", extract.NotLoggedInError),
                                  ("usage_limit_reached", extract.UsageLimitError),
                                  ("429 rate limit", extract.UsageLimitError),
                                  ("bad option", RuntimeError)]:
            with self.subTest(message=message), patch.object(extract.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 1, "", message)):
                with self.assertRaises(expected):
                    extract.run_codex_cli("test", pipeline.TRIAGE_SCHEMA)
        with patch.object(extract.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaisesRegex(ValueError, "no final"):
                extract.run_codex_cli("test", pipeline.TRIAGE_SCHEMA)

    def test_validation_retry_metadata_and_provenance(self):
        f = fetched()
        with patch.dict(extract.os.environ, {"CODEX_MODEL": "test-model"}), \
                patch.object(extract, "run_codex_cli", side_effect=["not JSON", result(f)]) as worker:
            ex = extract.via_codex_cli(f, {})
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(ex.document, f.meta)
        self.assertEqual(ex.provenance, "codex-cli:test-model:reasoning=high")
        self.assertIn("invalid", worker.call_args.args[0])

    def test_unknown_backend_does_not_fall_through_to_api(self):
        with self.assertRaises(ValueError):
            extract.extraction_backend("typo")
        self.assertIs(extract.extraction_backend("claude_cli"), extract.via_claude_cli)
        self.assertIs(extract.extraction_backend("api"), extract.via_api)

    def test_invalid_output_exhausts_retry_without_import(self):
        with patch.object(extract, "run_codex_cli", return_value='{"summary": "missing document"}') as worker:
            with self.assertRaisesRegex(RuntimeError, "validation twice"):
                extract.via_codex_cli(fetched(), {})
        self.assertEqual(worker.call_count, 2)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        db.init(self.conn)
        self.outputs = patch.object(extract, "EXTRACTIONS_DIR", Path(self.tmp.name) / "extractions")
        self.outputs.start()
        self.logs = patch.object(extract, "CODEX_RUNS_DIR", Path(self.tmp.name) / "runs")
        self.logs.start()

    def tearDown(self):
        self.outputs.stop()
        self.logs.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_merged_citation_title_cannot_recreate_duplicate(self):
        canonical = pipeline.store_fetched(self.conn, fetched("Canonical paper"))
        variant = db.upsert_document(self.conn, title="Canonical paper (Author et al., 2024)")
        self.conn.commit()
        slugs = dict(self.conn.execute("SELECT id,slug FROM documents"))
        db.merge(self.conn, "document", slugs[variant], slugs[canonical])
        reused = db.upsert_document(self.conn, title="Canonical paper (Author et al., 2024)")
        self.assertEqual(reused, canonical)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 1)

    def test_codex_waves_import_concepts_claims_and_lineage(self):
        for title in ("First", "Second"):
            pipeline.store_fetched(self.conn, fetched(title))
        snapshots = []
        def worker(prompt, schema, **kwargs):
            snapshots.append(prompt)
            return result(fetched())
        with patch.object(extract, "run_codex_cli", side_effect=worker), \
                patch.object(extract, "run_claude_cli", side_effect=AssertionError("must use Codex")):
            stats = pipeline.extract_pending(self.conn, workers=2, wave_size=1, backend="codex_cli")
        self.assertEqual(stats, {"extracted": 2, "failed": 0, "stopped": None, "waves": 2})
        vocab = json.loads(snapshots[1].split("<existing_vocabulary>\n", 1)[1].split("\n</existing_vocabulary>", 1)[0])
        self.assertIn("activation-patching", {c["slug"] for c in vocab["concepts"]})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM claim_evidence").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM document_links").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM concept_relations").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents WHERE status='extracted'").fetchone()[0], 2)
        self.assertEqual(len(list(extract.EXTRACTIONS_DIR.glob("*.json"))), 2)

    def test_priority_ids_exclude_other_documents_and_deduplicate(self):
        ids = [pipeline.store_fetched(self.conn, fetched(title)) for title in ("First", "Second", "Other")]
        seen = []
        def worker(f, vocab):
            seen.append(f.meta.title)
            return extract.parse_extraction(result(f), f, "codex-cli:test")
        with patch.object(extract, "via_codex_cli", side_effect=worker):
            stats = pipeline.extract_pending(self.conn, workers=1, wave_size=1, backend="codex_cli",
                                             document_ids=[ids[1], ids[0], ids[1]])
        self.assertEqual(seen, ["Second", "First"])
        self.assertEqual(stats["extracted"], 2)
        self.assertEqual([d["id"] for d in pipeline.pending_documents(self.conn)], [ids[2]])

    def test_usage_limit_keeps_documents_pending_without_failures(self):
        for i in range(8):
            pipeline.store_fetched(self.conn, fetched(str(i)))
        with patch.object(extract, "via_codex_cli", side_effect=extract.UsageLimitError("quota")):
            stats = pipeline.extract_pending(self.conn, workers=1, wave_size=8, backend="codex_cli")
        self.assertIn("UsageLimitError", stats["stopped"])
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(pipeline.pending_documents(self.conn)), 8)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM extraction_failures").fetchone()[0], 0)

    def test_codex_update_triage_routes_without_claude(self):
        pipeline.add_candidate(self.conn, url="https://example.org/new", title="New paper", source="arxiv")
        self.conn.commit()
        candidate_id = self.conn.execute("SELECT id FROM candidates").fetchone()[0]
        cfg = {"triage": {"batch_size": 40}, "update": {"backend": "codex_cli"}}
        with patch.object(extract, "run_codex_cli", return_value=json.dumps({"items": [
                {"id": candidate_id, "relevance": 3, "note": "core method"}]})) as worker, \
                patch.object(extract, "run_claude_cli", side_effect=AssertionError("must use Codex")):
            self.assertEqual(pipeline.triage(self.conn, cfg), 1)
        worker.assert_called_once()
        self.assertEqual(self.conn.execute("SELECT relevance FROM candidates").fetchone()[0], 3)

    def test_single_document_uses_codex_backend(self):
        f = fetched()
        doc_id = pipeline.store_fetched(self.conn, f)
        with patch.object(extract, "run_codex_cli", return_value=result(f)):
            cli.run_extraction(self.conn, doc_id, f, "codex_cli")
        row = self.conn.execute("SELECT status,provenance FROM documents WHERE id=?", (doc_id,)).fetchone()
        self.assertEqual(row["status"], "extracted")
        self.assertTrue(row["provenance"].startswith("codex-cli:"))


if __name__ == "__main__":
    unittest.main()
