import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts import run_until_usage_limit as runner
from interp_map import db, extract, fetchers, pipeline
from interp_map.models import DocumentMeta, Extraction


class SustainedIngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "drafts").mkdir()
        self.paths = [patch.object(runner, "RUN_DIR", self.root),
                      patch.object(runner, "STATE_PATH", self.root / "state.json")]
        for p in self.paths:
            p.start()
        self.conn = db.connect(self.root / "test.db")
        db.init(self.conn)
        self.state = {"waves": 0, "extracted_ids": []}

    def tearDown(self):
        self.conn.close()
        for p in self.paths:
            p.stop()
        self.tmp.cleanup()

    def candidate(self, title="Expected research title"):
        self.conn.execute("INSERT INTO candidates(url,title,source) VALUES (?,?,?)",
                          ("https://example.org/research", title, "citation"))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM candidates").fetchone()

    def test_historical_discovery_skips_legacy_id_and_completes_mixed_batch(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>http://arxiv.org/abs/cs/0407047v3</id><title>Legacy paper</title></entry>
          <entry><id>malformed</id><title>Malformed entry</title></entry>
          <entry><id>http://arxiv.org/abs/2507.21509v2</id><title>Persona Vectors</title>
          <summary>Character traits</summary><published>2025-07-29T00:00:00Z</published></entry>
        </feed>'''
        response = httpx.Response(200, text=feed, request=httpx.Request("GET", "https://export.arxiv.org/api/query"))
        discoverers = {name: lambda *_: 0 for name in pipeline.DISCOVERERS}
        with patch.object(pipeline, "DISCOVERERS", discoverers), \
                patch.object(pipeline, "load_config", return_value={"discovery": {"citation_stubs": {}}}), \
                patch.object(fetchers, "polite_get", return_value=response), patch.object(runner, "log") as log:
            runner.discover(self.conn, self.state, wide=True)
        rows = self.conn.execute("SELECT title,url FROM candidates").fetchall()
        self.assertEqual([(r["title"], r["url"]) for r in rows],
                         [("Persona Vectors", "https://arxiv.org/abs/2507.21509")])
        self.assertEqual(self.state["historical_cursor"], 1)
        self.assertEqual(json.loads((self.root / "state.json").read_text())["historical_cursor"], 1)
        messages = [call.args[0] for call in log.call_args_list]
        self.assertTrue(any("skipped" in m and "cs/0407047v3" in m for m in messages))
        self.assertTrue(any("skipped" in m and "'malformed'" in m for m in messages))

    def test_historical_discovery_fetch_or_parse_failure_does_not_advance(self):
        discoverers = {name: lambda *_: 0 for name in pipeline.DISCOVERERS}
        for status, body in [(503, "unavailable"), (200, "<broken")]:
            with self.subTest(status=status):
                response = httpx.Response(status, text=body, request=httpx.Request("GET", "https://export.arxiv.org/api/query"))
                with patch.object(pipeline, "DISCOVERERS", discoverers), \
                        patch.object(pipeline, "load_config", return_value={"discovery": {"citation_stubs": {}}}), \
                        patch.object(fetchers, "polite_get", return_value=response), patch.object(runner, "log") as log:
                    runner.discover(self.conn, self.state, wide=True)
                self.assertEqual(self.state["historical_cursor"], 0)
                self.assertTrue(any("historical discovery failed:" in call.args[0] for call in log.call_args_list))

    def test_non_canon_mismatched_document_is_never_stored(self):
        row = self.candidate()
        wrong = fetchers.Fetched(DocumentMeta(title="Unrelated research", url=row["url"]),
                                 "Expected research title is cited here. " + "wrong body " * 400)
        with patch.object(runner, "ranked_rows", return_value=[row]), patch.object(fetchers, "fetch", return_value=wrong):
            runner.fetch_candidates(self.conn, self.state, 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM candidates").fetchone()[0], "failed")

    def test_duplicate_existing_title_does_not_fetch_or_add_document(self):
        row = self.candidate()
        doc_id = db.upsert_document(self.conn, title=row["title"], url="https://canonical.example/paper", status="extracted")
        self.conn.commit()
        with patch.object(runner, "ranked_rows", return_value=[row]), patch.object(fetchers, "fetch") as fetch:
            runner.fetch_candidates(self.conn, self.state, 1)
            fetch.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT document_id FROM candidates").fetchone()[0], doc_id)

    def test_ingested_canon_document_keeps_manual_priority_over_low_auto_rank(self):
        ids = []
        for n, title in enumerate(["Conceptual persona foundation", "Unselected tangential post", "Empirical work"]):
            ids.append(pipeline.store_fetched(self.conn, fetchers.Fetched(
                DocumentMeta(title=title, url=f"https://example.org/{n}", source="web"), "text " * 600)))
        self.conn.execute("INSERT INTO candidates(url,title,source,status,document_id) VALUES (?,?,?,?,?)",
                          ("https://example.org/0", "Conceptual persona foundation", "canon", "ingested", ids[0]))
        self.conn.commit()
        ranks = {str(ids[0]): {"relevance": 1, "importance": 1},
                 str(ids[1]): {"relevance": 1, "importance": 1},
                 str(ids[2]): {"relevance": 3, "importance": 4}}
        self.state["ranked_documents"] = ranks
        self.assertEqual([r["id"] for r in runner.ranked_rows(self.conn, self.state, "documents")], [ids[0], ids[2]])
        # Preserve model scores for transparency; the explicit selection governs ordering.
        self.assertEqual(ranks[str(ids[0])]["relevance"], 1)
        self.state["skipped_documents"] = {str(ids[0]): "Provider restriction"}
        self.assertEqual([r["id"] for r in runner.ranked_rows(self.conn, self.state, "documents")], [ids[2]])

    def test_ranking_prompt_includes_persona_conceptual_foundations(self):
        row = self.candidate("Role-play conceptual foundation")
        result = json.dumps({"items": [{"id": row["id"], "relevance": 3, "importance": 5, "reason": "conceptual foundation"}]})
        with patch.object(runner, "logged_call", return_value=result) as call:
            runner.rank(self.conn, self.state, "candidates")
        prompt = call.call_args.args[0]
        self.assertIn("persona and character research", prompt)
        self.assertIn("eligible even without new experiments", prompt)

    def test_quota_stop_prevents_next_worker_or_audit_and_preserves_queue(self):
        for n in range(2):
            pipeline.store_fetched(self.conn, fetchers.Fetched(
                DocumentMeta(title=f"Research {n}", url=f"https://example.org/{n}"), "text " * 600))
        rows = pipeline.pending_documents(self.conn)
        with patch.object(extract, "via_codex_cli", side_effect=extract.UsageLimitError("quota exhausted")) as run, \
                patch.object(runner.quality, "audit_extraction") as audit:
            stop = runner.extract_wave(self.conn, self.state, rows, 1)
        self.assertEqual(stop["type"], "UsageLimitError")
        self.assertEqual(run.call_count, 1)
        audit.assert_not_called()
        self.assertEqual(len(pipeline.pending_documents(self.conn)), 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM extraction_failures").fetchone()[0], 0)
        self.assertEqual(self.state["active_documents"], [])

    def test_stale_draft_from_reused_document_id_is_not_audited(self):
        f = fetchers.Fetched(DocumentMeta(title="Current paper", url="https://example.org/current"), "text " * 600)
        doc_id = pipeline.store_fetched(self.conn, f)
        stale = Extraction(document=DocumentMeta(title="Old paper", url="https://example.org/old"), summary="Unrelated draft")
        (self.root / "drafts" / f"{doc_id}.json").write_text(stale.model_dump_json(), encoding="utf-8")
        with patch.object(extract, "via_codex_cli", side_effect=extract.UsageLimitError("stop")) as run, \
                patch.object(runner.quality, "audit_extraction") as audit:
            runner.extract_wave(self.conn, self.state, pipeline.pending_documents(self.conn), 1)
        run.assert_called_once()
        audit.assert_not_called()

    def test_provider_content_restriction_marks_skip_without_failure_retry(self):
        f = fetchers.Fetched(DocumentMeta(title="Restricted source", url="https://example.org/restricted"), "text " * 600)
        doc_id = pipeline.store_fetched(self.conn, f)
        with patch.object(extract, "via_codex_cli", side_effect=extract.ContentRestrictionError("provider restriction")) as run, \
                patch.object(runner.quality, "audit_extraction") as audit, \
                patch.object(runner.quality, "merge_wave_concepts", return_value=[]), \
                patch.object(runner, "dedup_documents"):
            stop = runner.extract_wave(self.conn, self.state, pipeline.pending_documents(self.conn), 1)
        self.assertIsNone(stop)
        self.assertIn("Provider content restriction", self.state["skipped_documents"][str(doc_id)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM extraction_failures").fetchone()[0], 0)
        run.assert_called_once()
        audit.assert_not_called()

    def test_both_quota_windows_can_stop_run(self):
        for window in ("primary", "secondary", "individualLimit"):
            self.assertTrue(runner.quota_exhausted({"rateLimitsByLimitId": {
                "codex": {window: {"usedPercent": 100}}}}))
        self.assertFalse(runner.quota_exhausted({"error": "temporarily unavailable"}))

    def test_stop_marker_pauses_before_any_ranking_or_provider_work(self):
        (self.root / "STOP").write_text("maintenance", encoding="utf-8")
        auth = SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        with patch.object(runner.sys, "argv", ["runner"]), \
                patch.object(runner.subprocess, "run", return_value=auth), \
                patch.object(extract, "find_codex", return_value="unused"), \
                patch.object(db, "connect", return_value=self.conn), \
                patch.object(pipeline, "_Lock"), \
                patch.object(runner, "rate_limits", return_value={}), \
                patch.object(runner, "rank") as rank:
            runner.main()
        rank.assert_not_called()
        self.assertEqual(json.loads((self.root / "state.json").read_text())["status"], "paused_by_operator")


class WebPdfTests(unittest.TestCase):
    def response(self, url, content, content_type):
        return httpx.Response(200, content=content, headers={"content-type": content_type},
                              request=httpx.Request("GET", url))

    def reader(self, text, title="Expected research title"):
        return SimpleNamespace(metadata={"/Title": title}, pages=[SimpleNamespace(extract_text=lambda: text)])

    def landing(self):
        return self.response("https://example.org/proceedings/paper.html",
                             b'<meta name="citation_title" content="Expected research title">'
                             b'<meta name="citation_pdf_url" content="paper.pdf">Abstract', "text/html")

    def test_redirected_landing_resolves_relative_pdf_and_uses_final_identity(self):
        pdf = self.response("https://example.org/proceedings/paper.pdf", b"%PDF-fake", "application/octet-stream")
        with patch.object(fetchers._client, "get", side_effect=[self.landing(), pdf]) as get, \
                patch("pypdf.PdfReader", return_value=self.reader("Expected research title\nFull text")):
            result = fetchers.fetch_web("https://example.org/redirect")
        self.assertEqual(get.call_args_list[1].args[0], "https://example.org/proceedings/paper.pdf")
        self.assertEqual(result.meta.url, "https://example.org/proceedings/paper.html")
        self.assertEqual(result.meta.doc_type, "paper")
        self.assertIn("Full text", result.text)

    def test_advertised_pdf_returning_error_html_cannot_fall_back_to_abstract(self):
        html = self.response("https://example.org/error", b"Login needed", "text/html")
        with patch.object(fetchers._client, "get", side_effect=[self.landing(), html]):
            with self.assertRaisesRegex(ValueError, "non-PDF"):
                fetchers.fetch_web("https://example.org/paper")

    def test_wrong_linked_pdf_is_rejected(self):
        pdf = self.response("https://example.org/paper.pdf", b"%PDF-fake", "application/pdf")
        with patch.object(fetchers._client, "get", side_effect=[self.landing(), pdf]), \
                patch("pypdf.PdfReader", return_value=self.reader("An entirely different paper\n" * 200)):
            with self.assertRaisesRegex(ValueError, "does not match"):
                fetchers.fetch_web("https://example.org/paper")

    def test_direct_pdf_with_generic_content_type_is_not_parsed_as_html(self):
        pdf = self.response("https://example.org/final.pdf", b"%PDF-fake", "application/octet-stream")
        with patch.object(fetchers._client, "get", return_value=pdf), \
                patch("pypdf.PdfReader", return_value=self.reader("Actual PDF text")):
            result = fetchers.fetch_web("https://example.org/redirect")
        self.assertEqual(result.text, "Actual PDF text")
        self.assertEqual(result.meta.url, "https://example.org/final.pdf")


if __name__ == "__main__":
    unittest.main()
