import io
import unittest
from types import SimpleNamespace
from contextlib import redirect_stderr
from unittest.mock import patch

import httpx

from interp_map import fetchers


class ArxivCompletenessTests(unittest.TestCase):
    def test_svg_foreignobject_prose_is_preserved(self):
        html = ('<section><h3>Model Specification</h3><svg><path d="M 0 0"/>'
                '<foreignObject><div><p>Only genuine solutions.</p>'
                '<p>Never circumvent tests.</p></div></foreignObject></svg>'
                '<svg><text>Accuracy 90%</text></svg><script>unwanted()</script></section>')
        text = fetchers.html_to_text(html)
        self.assertIn("Only genuine solutions.", text)
        self.assertIn("Never circumvent tests.", text)
        self.assertIn("Accuracy 90%", text)
        self.assertNotIn("unwanted", text)
        self.assertNotIn("M 0 0", text)

    def test_truncated_html_falls_back_to_pdf(self):
        # Regression: LEACE's HTTP-200 rendering ended inside a proof/table.
        html = '<html><body><article class="ltx_document"><p>yields the equations:</p><table><td>'
        responses = [httpx.Response(200, text=html),
                     httpx.Response(200, content=b"%PDF-fake", headers={"content-type": "application/pdf"})]
        reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "Complete PDF including experiments")])
        # A fake module keeps this test independent of optional PDF installation.
        with patch.object(fetchers, "polite_get", side_effect=responses) as get, \
                patch.dict("sys.modules", {"pypdf": SimpleNamespace(PdfReader=lambda _: reader)}):
            self.assertEqual(fetchers._arxiv_full_text("2306.03819"), "Complete PDF including experiments")
        self.assertEqual(get.call_args_list[1].args[0], "https://arxiv.org/pdf/2306.03819")

    def test_closed_html_needs_bibliography_and_no_conversion_error(self):
        body = '<article class="ltx_document"><p>Research</p><section class="ltx_bibliography"><li class="ltx_bibitem">Reference</li></section></article>'
        complete = '<html><body>' + body + '</body></html>'
        self.assertTrue(fetchers._complete_arxiv_html(complete))
        self.assertFalse(fetchers._complete_arxiv_html(complete.replace('ltx_bibitem', 'other')))
        self.assertFalse(fetchers._complete_arxiv_html(complete.replace('Research', '<span class="ltx_ERROR">Bad conversion</span>')))
        self.assertFalse(fetchers._complete_arxiv_html(complete.replace('Research', 'LaTeXML conversion failed')))

    def test_complete_html_does_not_request_pdf(self):
        html = '<html><body><article class="ltx_document"><p>Full paper.</p><section class="ltx_bibliography"><li class="ltx_bibitem">Citation</li></section></article></body></html>'
        with patch.object(fetchers, "polite_get", return_value=httpx.Response(200, text=html)) as get:
            self.assertIn("Full paper.", fetchers._arxiv_full_text("2306.03819"))
            get.assert_called_once()


class SemanticScholarCooldownTests(unittest.TestCase):
    def test_429_skips_other_documents_for_fifteen_minutes_then_recovers(self):
        response = httpx.Response(200, json={"paperId": "s2-real-id", "references": [{
            "title": "Prior research", "externalIds": {"ArXiv": "2305.00586"},
            "url": "https://example.org/prior"}]})
        warning = io.StringIO()
        with patch.object(fetchers, "_s2_retry_at", 0), \
                patch.object(fetchers.time, "monotonic", return_value=100) as clock, \
                patch.object(fetchers.time, "sleep") as sleep, \
                patch.object(fetchers._client, "get", side_effect=[httpx.Response(429), response]) as get, \
                redirect_stderr(warning):
            self.assertEqual(fetchers.semantic_scholar_references("1111.11111"), ([], None))
            self.assertEqual(fetchers._s2_retry_at, 1000)
            clock.return_value = 999
            self.assertEqual(fetchers.semantic_scholar_references("2222.22222"), ([], None))
            self.assertEqual(get.call_count, 1)
            clock.return_value = 1000
            refs, paper_id = fetchers.semantic_scholar_references("2222.22222")
            self.assertEqual(get.call_count, 2)
            self.assertEqual(paper_id, "s2-real-id")
            self.assertEqual(refs[0].arxiv_id, "2305.00586")
            sleep.assert_not_called()
        self.assertIn("HTTP 429", warning.getvalue())
        self.assertIn("rate-limit cooldown", warning.getvalue())
        self.assertIn("backfill-refs", warning.getvalue())

    def test_non_rate_limit_failure_does_not_disable_following_documents(self):
        with patch.object(fetchers, "_s2_retry_at", 0), \
                patch.object(fetchers.time, "monotonic", return_value=100), \
                patch.object(fetchers._client, "get", side_effect=[
                    httpx.Response(404), httpx.Response(200, json={"paperId": "found", "references": []})]) as get, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(fetchers.semantic_scholar_references("missing"), ([], None))
            self.assertEqual(fetchers.semantic_scholar_references("known"), ([], "found"))
            self.assertEqual(get.call_count, 2)
            self.assertEqual(fetchers._s2_retry_at, 0)


if __name__ == "__main__":
    unittest.main()
