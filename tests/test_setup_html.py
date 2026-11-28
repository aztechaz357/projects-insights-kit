import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import sync_setup_html  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"


class SetupHtmlTest(unittest.TestCase):
    def test_embedded_files_match(self):
        html = (ROOT / "setup.html").read_text(encoding="utf-8")
        for tag_id, path in sync_setup_html.EMBEDS.items():
            self.assertEqual(sync_setup_html.embedded(html, tag_id).splitlines(),
                             path.read_text(encoding="utf-8").splitlines(),
                             f"setup.html の {tag_id} と {path.name} がずれています (python tools/sync_setup_html.py を実行)")


class SelfContainedTest(unittest.TestCase):
    """コピーして使うので、外部のコードやサービスに依存しないことを確認する。"""

    def test_no_external_workflow_or_repository(self):
        for wf in WORKFLOWS.glob("*.yml"):
            text = wf.read_text(encoding="utf-8")
            for m in re.finditer(r"^\s*-?\s*uses:\s*(\S+)", text, re.M):
                self.assertRegex(m.group(1), r"^actions/(checkout|upload-artifact)@",
                                 f"{wf.name}: GitHub 公式以外のアクション/ワークフロー {m.group(1)}")
            self.assertNotIn("repository:", text, f"{wf.name}: 別リポジトリを取得しています")
            self.assertNotIn("workflow_call", text)

    def test_kit_talks_only_to_github(self):
        src = (ROOT / ".github" / "insights-kit" / "insights_kit.py").read_text(encoding="utf-8")
        # urllib.parse (URL の組み立てだけで通信しない) は可
        for bad in ("urllib.request", "urlopen", "http.client", "requests", "socket", "curl", "wget"):
            self.assertNotIn(bad, src, f"insights_kit.py が {bad} を使っています")
        hosts = {h for h in re.findall(r"https?://([^/\s\"'`]+)", src)}
        self.assertLessEqual(hosts, {"github.com", "www.w3.org"}, hosts)

    def test_japan_holidays(self):
        sys.path.insert(0, str(ROOT / ".github" / "insights-kit"))
        import insights_kit
        got = sorted(d.strftime("%m%d") for d in insights_kit.japan_holidays(2026))
        self.assertEqual(got, "0101 0112 0211 0223 0320 0429 0503 0504 0505 0506 0720 0811 0921 0922 0923 1012 1103 1123".split())


if __name__ == "__main__":
    unittest.main()
