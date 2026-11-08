import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from interp_map import db, pipeline


class TransformerCircuitsDiscoveryTests(unittest.TestCase):
    def test_index_heading_excludes_description_and_byline(self):
        html = '''<a class="note" href="2022/solu/index.html">
          <h3>Softmax <em>Linear</em> Units</h3>
          <div class="byline">Elhage et al., 2022</div>
          <div class="description">An alternative activation function increases interpretability.</div>
        </a><a href="2025/plain/index.html">Plain title</a>
        <a href="https://unrelated.example/">Not a TC article</a>'''
        response = httpx.Response(200, text=html, request=httpx.Request("GET", "https://transformer-circuits.pub/"))
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "test.db")
            try:
                db.init(conn)
                with patch.object(pipeline._client, "get", return_value=response):
                    self.assertEqual(pipeline.discover_transformer_circuits(conn, {}), 2)
                titles = {row[0]: row[1] for row in conn.execute("SELECT url,title FROM candidates")}
                self.assertEqual(titles["https://transformer-circuits.pub/2022/solu"], "Softmax Linear Units")
                self.assertEqual(titles["https://transformer-circuits.pub/2025/plain"], "Plain title")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
