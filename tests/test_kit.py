import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "insights-kit"))
import insights_kit as k  # noqa: E402


def item(status=None, state="OPEN"):
    return {"values": {"Status": status} if status else {}, "content": {"state": state}}


class CfdTest(unittest.TestCase):
    ORDER = ["Todo", "In Progress", "In Review", "Done"]

    def test_status_counts(self):
        work = [item("Todo"), item("Todo"), item("Done"), item(None, "CLOSED"), item(None), item("Blocked"),
                item("Todo", "CLOSED")]  # Close 済みは Status が Todo でも完了 (進捗率と同じ数え方)
        self.assertEqual(k.status_counts(work, "Status", ["Done"]),
                         {"Todo": 2, "Done": 3, k.NO_STATUS: 1, "Blocked": 1})

    def test_bands_bottom_up(self):
        hist = [{"date": "2026-11-27", "status": {"Todo": 3, "Done": 1, "Blocked": 1, k.NO_STATUS: 1}}]
        self.assertEqual(k.cfd_bands(hist, self.ORDER, ["Done"]),
                         ["Done", "In Review", "In Progress", "Todo", "Blocked", k.NO_STATUS])

    def test_svg_is_valid_and_stacks_to_total(self):
        hist = [{"date": f"2026-11-{d}", "status": {"Todo": 5 - i, "In Progress": 1, "Done": i}}
                for i, d in enumerate(["24", "25", "26", "27"])]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfd.svg"
            k.svg_cfd(path, hist, self.ORDER, ["Done"], "CFD <test>")
            root = ET.parse(path).getroot()  # XML として壊れていないこと
            polys = root.findall("{http://www.w3.org/2000/svg}polygon")
            self.assertEqual(len(polys), 4)  # Done / In Review / In Progress / Todo
            text = path.read_text(encoding="utf-8")
            self.assertIn("Done (3)", text)
            self.assertIn("&lt;test&gt;", text)

    def test_svg_single_day_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = Path(tmp) / "one.svg"
            k.svg_cfd(one, [{"date": "2026-11-28", "status": {"Todo": 2}}], self.ORDER, ["Done"], "t")
            bars = [r for r in ET.parse(one).getroot().findall("{http://www.w3.org/2000/svg}rect")
                    if r.find("{http://www.w3.org/2000/svg}title") is not None]
            self.assertEqual(len(bars), 4)  # 1 日分は帯ごとの棒で描く
            empty = Path(tmp) / "empty.svg"
            k.svg_cfd(empty, [{"date": "2026-11-28", "progress": 1}], self.ORDER, ["Done"], "t")
            self.assertIn("まだデータがありません", empty.read_text(encoding="utf-8"))

    def test_cfd_table_in_flow_order(self):
        hist = [{"date": "2026-11-28", "status": {"Todo": 2, "Done": 1}}]
        md = k.cfd_md(hist, self.ORDER, ["Done"])
        self.assertIn("| 日付 | Todo | In Progress | In Review | Done | 合計 |", md)
        self.assertIn("| 2026-11-28 | 2 | 0 | 0 | 1 | 3 |", md)
        self.assertEqual(k.cfd_md([{"date": "2026-11-28"}], self.ORDER, ["Done"]), "")


class StateBodyTest(unittest.TestCase):
    ROW = {"elapsed_days": 120, "done": 123.45, "total": 456.78, "unit": "人日", "progress": 27.0, "buffer": 45.6,
           "buffer_days": 12.3, "zone": "Yellow",
           "status": {"Todo": 120, "In Progress": 15, "In Review": 8, "Done": 300, k.NO_STATUS: 3}}

    def history(self, n):
        return [dict(self.ROW, date=f"d{i:04d}") for i in range(n)]

    def test_long_history_is_trimmed_from_oldest(self):
        visible = "x" * 8000
        body = k.state_body(visible, {"a": 1}, self.history(k.MAX_HISTORY))
        self.assertLessEqual(len(body), k.BODY_LIMIT)
        _, kept = k.parse_state(body, strict=True)
        self.assertEqual(kept[-1]["date"], f"d{k.MAX_HISTORY - 1:04d}")  # 最新は残る
        self.assertGreater(len(kept), 100)

    def test_short_history_is_kept_whole(self):
        body = k.state_body("v", {"a": 1}, self.history(40))
        cfg, kept = k.parse_state(body, strict=True)
        self.assertEqual((cfg, len(kept)), ({"a": 1}, 40))


class RecipeTest(unittest.TestCase):
    def test_time_charts_have_no_group_by(self):
        cfg = {"fields": k.DEFAULT_FIELDS, "cadence": "phase+sprint-2w", "focus_issues": ["acme/portal#1"]}
        md = k.insights_recipe(cfg, ["Todo", "In Progress", "Done"])
        rows = [r.split(" | ") for r in md.splitlines() if r.startswith("| ") and "---" not in r][1:]
        self.assertTrue(rows)
        for r in rows:
            if r[3] == "Time":
                self.assertEqual(r[4], "(選べない)", r)
        self.assertIn('`fever-metric:"Progress %"`', md)
        self.assertIn("Parent issue", md)


class MermaidTest(unittest.TestCase):
    ZONES = {"yellow": [0.10, 0.70], "red": [0.30, 0.90]}

    def test_fever_lines_follow_progress(self):
        hist = [{"date": "2026-11-26", "progress": 50.0, "buffer": 40.0},
                {"date": "2026-11-27", "progress": 100.0, "buffer": 130.0}]
        md = k.fever_mermaid(hist, self.ZONES)
        self.assertTrue(md.startswith("```mermaid\n"))
        self.assertIn('x-axis ["11-26", "11-27"]', md)
        self.assertIn("y-axis \"バッファ消費率 (%)\" 0 --> 140", md)  # 最大値 + 10 を 10 単位で切り上げ
        self.assertIn("line [60, 90]", md)   # 赤: 30% → 90% を進捗率で補間
        self.assertIn("line [40, 70]", md)   # 黄
        self.assertIn("line [100, 100]", md)
        self.assertIn("line [40, 130]", md)  # 実際の消費率
        self.assertEqual(k.fever_mermaid([], self.ZONES), "")

    def test_cfd_lines_are_cumulative_from_done(self):
        hist = [{"date": "2026-11-27", "status": {"Todo": 4, "In Progress": 2, "Done": 3}}]
        md = k.cfd_mermaid(hist, ["Todo", "In Progress", "Done"], ["Done"])
        lines = [l.strip() for l in md.splitlines() if l.strip().startswith("line [")]
        self.assertEqual(lines, ["line [3]", "line [5]", "line [9]"])  # Done → +In Progress → +Todo
        self.assertEqual(k.cfd_mermaid([{"date": "2026-11-27"}], ["Todo"], ["Done"]), "")


class ChartLinkTest(unittest.TestCase):
    URL = "https://github.com/users/acme/projects/7"
    FIELDS = {"Status": {"db": 11}, "Assignees": {"db": 12}, "Parent issue": {"db": 13},
              "見積 (人日)": {"db": 21}, "Fever Value": {"db": 22}, "Fever Zone": {"db": 23}, "Fever Metric": {"db": 24}}

    def params(self, row):
        import urllib.parse
        link = k.chart_link(self.URL, row, self.FIELDS, "Status")
        self.assertTrue(link.startswith(self.URL + "/insights?"), link)
        return dict(urllib.parse.parse_qsl(link.split("?", 1)[1], keep_blank_values=True))

    def test_time_chart_uses_status_as_group(self):
        p = self.params(("バーンアップ", "`no:fever-metric`", "Stacked area", "Time", "(選べない)", "Sum of 見積 (人日)"))
        self.assertEqual(p, {"xAxis[dataSource][column]": "time", "xAxis[groupBy]": "11",
                             "yAxis[aggregate][operation]": "sum", "yAxis[aggregate][columns]": "21",
                             "filterQuery": "no:fever-metric", "layout": "stacked-area"})

    def test_field_chart_with_and_without_group(self):
        p = self.params(("担当者別", "`-status:Done`", "Stacked column", "Assignees", "Status", "Sum of 見積 (人日)"))
        self.assertEqual((p["xAxis[dataSource][column]"], p["xAxis[groupBy]"], p["layout"]), ("12", "11", "stacked-column"))
        p = self.params(("ゾーン", "`has:fever-metric`", "Bar", "Fever Zone", "None", "Count of items"))
        self.assertEqual(p["xAxis[groupBy]"], "")  # 空でも付けないと X-axis が反映されない
        self.assertNotIn("yAxis[aggregate][operation]", p)

    def test_filter_with_quotes_and_percent(self):
        p = self.params(("進捗率", '`fever-metric:"Progress %"`', "Line", "Time", "(選べない)", "Sum of Fever Value"))
        self.assertEqual(p["filterQuery"], 'fever-metric:"Progress %"')

    def test_no_link_for_placeholder_or_missing_field(self):
        self.assertIsNone(k.chart_link(self.URL, ("注目", "`parent-issue:OWNER/REPO#番号`", "Line", "Time", "-", "Sum of 見積 (人日)"),
                                       self.FIELDS, "Status"))
        self.assertIsNone(k.chart_link(self.URL, ("工程", "`x`", "Stacked column", "Phase", "Status", "Count of items"),
                                       self.FIELDS, "Status"))

    def test_recipe_has_links_when_fields_given(self):
        cfg = {"fields": k.DEFAULT_FIELDS, "cadence": "sprint-2w", "focus_issues": [], "project": {"url": self.URL}}
        fields = dict(self.FIELDS, Sprint={"db": 31})
        md = k.insights_recipe(cfg, ["Todo", "Done"], fields)
        self.assertIn("Save changes to new chart", md)
        self.assertGreaterEqual(md.count("[開く](" + self.URL + "/insights?"), 9)


class ItemTableTest(unittest.TestCase):
    """開始日 11/2 (月)〜完了予定日 11/13 (金) = 10 営業日。今日 11/9 (月) の朝は 5 営業日経過 → 計画 50%。"""
    F = dict(k.DEFAULT_FIELDS)
    TODAY = k.dt.date(2026, 11, 9)

    @staticmethod
    def issue(n, parent=None, est=None, status=None, dates=True):
        vals = {}
        if est is not None:
            vals["見積 (人日)"] = est
        if status:
            vals["Status"] = status
        if dates:
            vals.update({"開始日": "2026-11-02", "完了予定日": "2026-11-13", "Action": "レビュー | 前倒し"})
        return {"id": f"I{n}", "type": "ISSUE", "values": vals,
                "content": {"number": n, "repository": {"nameWithOwner": "o/r"}, "title": f"課題{n}", "state": "OPEN",
                            "url": f"https://github.com/o/r/issues/{n}", "assignees": {"nodes": [{"login": "taro"}]},
                            "parent": {"number": parent, "repository": {"nameWithOwner": "o/r"}} if parent else None}}

    def rows(self, items, history):
        cal = k.Calendar({"schedule": {"japan_holidays": False}})
        return k.item_rows(items, items, self.F, cal, self.TODAY, history,
                           lambda i: i["values"].get("見積 (人日)"), lambda i: i["values"].get("Status") == "Done")

    def test_plan_and_actual(self):
        # #1 ─┬ #2 (3 人日・完了)
        #     └ #3 ── #4 (2 人日)     #3 は途中の親なので、#1 の進捗は末端の #2 と #4 で計算 → 3 / 5 = 60%
        items = [self.issue(1), self.issue(2, 1, 3, "Done"), self.issue(3, 1, 5), self.issue(4, 3, 2)]
        history = [{"date": "2026-11-02", "items": {"o/r#1": 10.0}}, {"date": "2026-11-06", "items": {"o/r#1": 45.0}}]
        rows, actuals = self.rows(items, history)
        self.assertEqual([r["ref"] for r in rows], ["o/r#1", "o/r#3"])  # サブ Issue を持つものだけがアイテム
        r = rows[0]
        self.assertEqual((r["plan"], r["actual"], r["diff"]), (50.0, 60.0, 10.0))
        self.assertEqual((r["d_plan"], r["d_actual"], r["d_need"]), (10.0, 15.0, 0.0))   # 次の朝の計画 60% − 実績 60%
        self.assertEqual((r["w_plan"], r["w_actual"], r["w_need"]), (50.0, 50.0, 40.0))  # 5 営業日後の計画 100% − 60%
        self.assertEqual(actuals, {"o/r#1": 60.0, "o/r#3": 0.0})

    def test_markdown(self):
        items = [self.issue(1), self.issue(2, 1, 3, "Done"), self.issue(5, dates=False), self.issue(6, 5, 1)]
        rows, _ = self.rows(items, [])
        fields = {n: {} for n in self.F.values()}
        md = k.items_md(rows, self.F, fields)
        self.assertIn("| 1 | [#1](https://github.com/o/r/issues/1) 課題1 | @taro | 2026-11-02 | 2026-11-13 | 50.0% | 100.0% | 🟢 +50.0 |", md)
        self.assertIn("レビュー \\| 前倒し", md)  # 表を壊さない
        self.assertIn("日付が未入力", md)       # #5 は日付なし
        self.assertNotIn("⚠️", md)
        self.assertIn("⚠️", k.items_md(rows, self.F, {}))  # フィールドが無ければ setup の再実行を案内

    def test_csv(self):
        import csv as csvmod
        items = [self.issue(1), self.issue(2, 1, 3, "Done")]
        items[0]["values"]["Action"] = "=HYPERLINK(\"x\")"  # Excel の数式として実行させない
        rows, _ = self.rows(items, [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "item-progress-2026-11-09.csv"
            k.items_csv(path, rows, self.TODAY)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))  # Excel 用に BOM 付き
            with open(path, encoding="utf-8-sig", newline="") as fp:
                data = list(csvmod.DictReader(fp))
        self.assertEqual(list(data[0]), k.ITEM_CSV_COLUMNS)
        self.assertEqual((data[0]["日付"], data[0]["アイテム"], data[0]["累計 計画 (%)"], data[0]["累計 実績 (%)"]),
                         ("2026-11-09", "o/r#1", "50.0", "100.0"))
        self.assertEqual(data[0]["Action"], "'=HYPERLINK(\"x\")")


class CreateTest(unittest.TestCase):
    def args(self, **kw):
        import argparse
        base = dict(template_url="https://github.com/orgs/acme/projects/2", title="新プロジェクト", start_date="2026-12-01",
                    planned_end_date="2027-03-31", buffer="auto", cadence="sprint-2w", estimate_field="",
                    no_japan_holidays=False, focus_issues="", out="out")
        base.update(kw)
        return argparse.Namespace(**base)

    def test_bad_input_stops_before_copying(self):
        # 入力の誤りで Project だけができてしまわないよう、GitHub に触る前に止まること
        orig = k.Project.resolve
        k.Project.resolve = staticmethod(lambda *a, **kw: self.fail("コピーの前に止まっていない"))
        try:
            for kw in ({"planned_end_date": "2026-11-01"}, {"buffer": "abc"}, {"cadence": "x"},
                       {"focus_issues": "not-an-issue"}, {"title": "  "}):
                with self.subTest(kw=kw), self.assertRaises(SystemExit):
                    k.cmd_create(self.args(**kw))
        finally:
            k.Project.resolve = orig

    def test_workflow_edit_url(self):
        import os
        from unittest import mock
        env = {"GITHUB_REPOSITORY": "acme/portal", "GITHUB_REF_NAME": "main", "GITHUB_SERVER_URL": "https://github.com"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(k.workflow_edit_url(),
                             "https://github.com/acme/portal/edit/main/.github/workflows/insights-kit.yml")
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "", "GITHUB_REF_NAME": ""}):
            self.assertIsNone(k.workflow_edit_url())

    def test_workflow_offers_create_and_template(self):
        wf = (ROOT / ".github" / "workflows" / "insights-kit.yml").read_text(encoding="utf-8")
        self.assertIn("options: [daily, setup, create, template]", wf)
        self.assertIn('TEMPLATE_URL: ""', wf)  # 配布時は空 (setup.html で書き込む)


if __name__ == "__main__":
    unittest.main()
