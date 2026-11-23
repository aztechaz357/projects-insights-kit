import re
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SetupHtmlTest(unittest.TestCase):
    def test_embedded_template_matches_workflow(self):
        html = (ROOT / "setup.html").read_text(encoding="utf-8")
        m = re.search(r'<script type="text/plain" id="workflow-template">(.*?)</script>', html, re.S)
        yml = (ROOT / ".github" / "workflows" / "insights-kit.yml").read_text(encoding="utf-8")
        self.assertEqual(m.group(1).splitlines(), yml.splitlines(), "setup.html の雛形と insights-kit.yml がずれています")

    def test_prefill_url_fits(self):
        yml = (ROOT / ".github" / "workflows" / "insights-kit.yml").read_text(encoding="utf-8")
        self.assertLess(len(urllib.parse.quote(yml, safe="")) + 200, 5000)


if __name__ == "__main__":
    unittest.main()
