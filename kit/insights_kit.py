#!/usr/bin/env python3
"""Projects Insights Kit

既存の GitHub Project (Organization / 個人) に、次のものを後付けします。既存 Issue の中身は変更しません。

  - 三点見積 (PERT): 楽観・最可能・悲観 (人日) を入れると「見積 (人日)」= (楽観 + 4×最可能 + 悲観) / 6 を自動計算
  - 進め方 (cadence): 2週間 / 1週間スプリント、ウォーターフォールの工程 (Phase)、工程 × スプリント、なし
  - フィーバーチャート: 進捗率とバッファ消費率を毎日計算し、Project のドラフト Issue と SVG に記録

設定と履歴は Project 内のドラフト Issue「📈 Fever: 進捗率」の本文に保存するため、リポジトリへの書き込みは不要です。
通常は GitHub Actions (.github/workflows/run.yml を呼ぶワークフロー) から実行します。
手元で動かす場合は GH_TOKEN (または .env の PROJECT_TOKEN) を設定して:

  python kit/insights_kit.py setup --project-url URL --planned-end-date YYYY-MM-DD [--start-date ...] [--buffer auto|N]
                                   [--cadence phase+sprint-2w] [--estimate-field 名前] [--no-japan-holidays]
  python kit/insights_kit.py daily --project-url URL [--date YYYY-MM-DD]
  python kit/insights_kit.py check-token
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOTENV = ROOT / ".env"
STATE_METRIC = "Progress %"  # 設定と履歴はこのトラッカーの本文に保存
CONFIG_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
HISTORY_RE = re.compile(r"<!-- insights-kit:history\s*(\[.*?\])\s*-->", re.S)
MAX_HISTORY = 400

JST = dt.timezone(dt.timedelta(hours=9))
HOLIDAY_API = "https://holidays-jp.github.io/api/v1/date.json"
PROJECT_URL_RE = re.compile(r"github\.com/(orgs|users)/([^/]+)/projects/(\d+)")

CADENCES = {
    "sprint-2w": "2週間スプリント",
    "sprint-1w": "1週間スプリント",
    "phase": "工程 (ウォーターフォール)",
    "phase+sprint-2w": "工程 × 2週間スプリント",
    "none": "なし",
}
PHASES = [("要件定義", "BLUE"), ("基本設計", "PURPLE"), ("詳細設計", "PINK"),
          ("実装", "ORANGE"), ("テスト", "YELLOW"), ("リリース", "GREEN")]
METRICS = [("Progress %", "BLUE", "進捗率"), ("Buffer %", "ORANGE", "バッファ消費率")]
ZONES = [("Green", "GREEN", "順調"), ("Yellow", "YELLOW", "注意・対策準備"), ("Red", "RED", "要対策")]
ZONE_EMOJI = {"Green": "🟢", "Yellow": "🟡", "Red": "🔴"}

DEFAULT_FIELDS = {
    "status": "Status",
    "optimistic": "楽観 (人日)",
    "most_likely": "最可能 (人日)",
    "pessimistic": "悲観 (人日)",
    "estimate": "見積 (人日)",
    "sprint": "Sprint",
    "phase": "Phase",
    "fever_value": "Fever Value",
    "fever_metric": "Fever Metric",
    "fever_zone": "Fever Zone",
}


# =========================================================================== utils
def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def die(msg: str) -> None:
    print(f"::error::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"[error] {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"[warn] {msg}")


def load_dotenv() -> None:
    if DOTENV.exists():
        for raw in DOTENV.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.removeprefix("export ").split("=", 1)
            k, v = k.strip(), v.strip().strip("\"'")
            if k and v and k not in os.environ:
                os.environ[k] = v
    if os.environ.get("PROJECT_TOKEN") and not os.environ.get("GH_TOKEN"):
        os.environ["GH_TOKEN"] = os.environ["PROJECT_TOKEN"]


def gh(*args: str, json_out: bool = False, check: bool = True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        if check:
            die(f"gh {' '.join(args[:3])} が失敗しました:\n{r.stderr.strip()}")
        return None
    out = r.stdout.strip()
    return json.loads(out) if json_out else out


def gql(query: str, check: bool = True, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            args += ["-F", f"{k}={v}"] if isinstance(v, (int, bool)) else ["-f", f"{k}={v}"]
    res = gh(*args, json_out=True, check=check)
    return res["data"] if res else None


def q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def summary(md: str) -> None:
    """Actions のジョブサマリー (なければ標準出力) に書く。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    else:
        print(md)


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()


def parse_date(s: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s.strip())
    except ValueError:
        die(f"{name} は YYYY-MM-DD 形式で入力してください: '{s}'")


# ==================================================================== calendar
class Calendar:
    def __init__(self, cfg: dict):
        sch = cfg["schedule"]
        self.holidays: set[str] = set(sch.get("extra_holidays", []))
        if sch.get("japan_holidays", True):
            try:
                with urllib.request.urlopen(HOLIDAY_API, timeout=15) as r:
                    self.holidays |= set(json.load(r).keys())
            except Exception as e:  # noqa: BLE001
                warn(f"祝日データを取得できませんでした ({e})。土日のみ除外して計算します")

    def is_bday(self, d: dt.date) -> bool:
        return d.weekday() < 5 and d.isoformat() not in self.holidays

    def count(self, start: dt.date, end: dt.date) -> int:
        """start..end (両端含む) の営業日数。"""
        n, d = 0, start
        while d <= end:
            n += self.is_bday(d)
            d += dt.timedelta(days=1)
        return n

    def elapsed(self, start: dt.date, today: dt.date) -> int:
        """start から today の前日までの営業日数 (= 今朝時点で消化した日数)。"""
        return self.count(start, today - dt.timedelta(days=1)) if today > start else 0

    def add(self, start: dt.date, bdays: float) -> dt.date:
        d, left = start, math.ceil(bdays)
        while left > 0:
            d += dt.timedelta(days=1)
            left -= self.is_bday(d)
        return d


# ==================================================================== metrics
def pert(o, m, p):
    """三点見積。最可能が無ければ None、楽観/悲観が無ければ最可能で代用。"""
    if m is None:
        return None
    o = m if o is None else o
    p = m if p is None else p
    return round((o + 4 * m + p) / 6, 2)


def fever_zone(p: float, b: float, zones: dict) -> str:
    def line(z):
        return z[0] + (z[1] - z[0]) * p

    if b >= line(zones["red"]):
        return "Red"
    if b >= line(zones["yellow"]):
        return "Yellow"
    return "Green"


def compute_buffer(cfg: dict, plan_days: int, total_est: float, var_sum: float) -> tuple[float, str]:
    sch = cfg["schedule"]
    if sch["buffer"] != "auto":
        return float(sch["buffer"]), "手動指定"
    auto = cfg["buffer_auto"]
    if var_sum > 0 and total_est > 0:
        # 各タスクの標準偏差 σ=(悲観-楽観)/6 の二乗和平方根 × k を、工数比で営業日に換算
        days = plan_days * auto["k"] * math.sqrt(var_sum) / total_est
        return max(round(days, 1), 1.0), f"PERT (k={auto['k']} × √Σσ²)"
    return max(round(plan_days * auto["fallback_ratio"], 1), 1.0), f"楽観/悲観が未入力のため計画日数の {int(auto['fallback_ratio'] * 100)}%"


# ==================================================================== GitHub
FIELDS_QUERY = """
query($id: ID!) { node(id: $id) { ... on ProjectV2 {
  fields(first: 100) { nodes {
    __typename
    ... on ProjectV2FieldCommon { id name dataType }
    ... on ProjectV2SingleSelectField { options { id name } }
    ... on ProjectV2IterationField { configuration {
      iterations { id title startDate duration } completedIterations { id title startDate duration } } }
  } } } } }"""

ITEMS_QUERY = """
query($id: ID!, $after: String) { node(id: $id) { ... on ProjectV2 {
  items(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id type isArchived
      content {
        __typename
        ... on Issue { number title state url }
        ... on PullRequest { number title state url }
        ... on DraftIssue { id title body }
      }
      fieldValues(first: 50) { nodes {
        __typename
        ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
        ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
        ... on ProjectV2ItemFieldIterationValue { title field { ... on ProjectV2FieldCommon { name } } }
      } } } } } } }"""


class Project:
    def __init__(self, info: dict):
        self.owner, self.number, self.id = info["owner"], info["number"], info["id"]

    @staticmethod
    def resolve(url: str) -> dict:
        m = PROJECT_URL_RE.search(url)
        if not m:
            die("Project の URL は https://github.com/orgs/<org>/projects/<番号> (個人は /users/) の形で入力してください")
        kind, owner, number = m.group(1), m.group(2), int(m.group(3))
        # gh project view は所有者の種別判定で read:org 等を要求するため、URL から判別して GraphQL を直接呼ぶ
        root = "organization" if kind == "orgs" else "user"
        data = gql(f"query($login: String!, $n: Int!) {{ {root}(login: $login) {{ projectV2(number: $n) {{ id title url }} }} }}",
                   check=False, login=owner, n=number)
        p = ((data or {}).get(root) or {}).get("projectV2")
        if not p:
            die(f"Project {url} を開けませんでした。URL と、トークンの Projects 権限 (Resource owner) を確認してください")
        return {"url": p["url"], "owner": owner, "owner_type": "Organization" if kind == "orgs" else "User",
                "number": number, "id": p["id"], "title": p["title"]}

    def fields(self) -> dict:
        out = {}
        for f in gql(FIELDS_QUERY, id=self.id)["node"]["fields"]["nodes"]:
            if "name" not in f:
                continue
            e = {"id": f["id"], "type": f.get("dataType")}
            if "options" in f:
                e["options"] = {o["name"]: o["id"] for o in f["options"]}
            if "configuration" in f:
                c = f["configuration"]
                e["iterations"] = {i["title"]: i for i in c["iterations"] + c["completedIterations"]}
            out[f["name"]] = e
        return out

    def items(self) -> list[dict]:
        out, after = [], None
        while True:
            data = gql(ITEMS_QUERY, id=self.id, after=after)["node"]["items"]
            for n in data["nodes"]:
                if n.get("isArchived"):
                    continue
                vals = {}
                for v in n["fieldValues"]["nodes"]:
                    if v.get("field"):
                        vals[v["field"]["name"]] = v.get("number", v.get("name", v.get("title")))
                out.append({"id": n["id"], "type": n["type"], "content": n.get("content") or {}, "values": vals})
            if not data["pageInfo"]["hasNextPage"]:
                return out
            after = data["pageInfo"]["endCursor"]

    def create_number(self, name: str) -> None:
        gql(f"mutation {{ createProjectV2Field(input: {{projectId: {q(self.id)}, dataType: NUMBER, name: {q(name)}}}) "
            f"{{ projectV2Field {{ ... on ProjectV2Field {{ id }} }} }} }}")

    def create_select(self, name: str, options: list[tuple]) -> None:
        opts = ", ".join(f"{{name: {q(o[0])}, color: {o[1]}, description: {q(o[2] if len(o) > 2 else '')}}}" for o in options)
        gql(f"mutation {{ createProjectV2Field(input: {{projectId: {q(self.id)}, dataType: SINGLE_SELECT, name: {q(name)}, "
            f"singleSelectOptions: [{opts}]}}) {{ projectV2Field {{ ... on ProjectV2SingleSelectField {{ id }} }} }} }}")

    def create_iteration(self, name: str, start: dt.date, weeks: int, count: int) -> bool:
        dur = 7 * weeks
        its = ", ".join(f"{{title: {q(f'Sprint {i + 1}')}, startDate: {q((start + dt.timedelta(days=dur * i)).isoformat())}, duration: {dur}}}"
                        for i in range(count))
        res = gql(f"mutation {{ createProjectV2Field(input: {{projectId: {q(self.id)}, dataType: ITERATION, name: {q(name)}, "
                  f"iterationConfiguration: {{startDate: {q(start.isoformat())}, duration: {dur}, iterations: [{its}]}}}}) "
                  f"{{ projectV2Field {{ ... on ProjectV2IterationField {{ id }} }} }} }}", check=False)
        return res is not None

    def _set_value(self, item_id: str, field: dict, value_literal: str) -> None:
        gql(f"mutation {{ updateProjectV2ItemFieldValue(input: {{projectId: {q(self.id)}, itemId: {q(item_id)}, "
            f"fieldId: {q(field['id'])}, value: {{{value_literal}}}}}) {{ projectV2Item {{ id }} }} }}")

    def set_number(self, item_id: str, field: dict, value: float) -> None:
        self._set_value(item_id, field, f"number: {float(value)!r}")

    def set_option(self, item_id: str, field: dict, option: str) -> None:
        self._set_value(item_id, field, f"singleSelectOptionId: {q(field['options'][option])}")

    def create_draft(self, title: str, body: str) -> str:
        data = gql("mutation($p: ID!, $t: String!, $b: String!) { addProjectV2DraftIssue(input: {projectId: $p, title: $t, body: $b}) "
                   "{ projectItem { id } } }", p=self.id, t=title, b=body)
        return data["addProjectV2DraftIssue"]["projectItem"]["id"]

    def edit_draft(self, draft_id: str, body: str) -> None:
        gql("mutation($d: ID!, $b: String!) { updateProjectV2DraftIssue(input: {draftIssueId: $d, body: $b}) { draftIssue { id } } }",
            d=draft_id, b=body)


# ==================================================================== setup
def cmd_setup(a) -> None:
    if a.cadence not in CADENCES:
        die(f"cadence は {', '.join(CADENCES)} のいずれか")
    start = parse_date(a.start_date, "開始日") if a.start_date else today_jst()
    planned_end = parse_date(a.planned_end_date, "計画完了日")
    if planned_end <= start:
        die("計画完了日は開始日より後にしてください")
    buffer = (a.buffer or "auto").strip().lower()
    if buffer != "auto":
        try:
            buffer = float(buffer)
            assert buffer > 0
        except (ValueError, AssertionError):
            die("バッファは auto か 正の数 (営業日) で入力してください")

    info = Project.resolve(a.project_url)
    proj = Project(info)
    print(f"Project: {info['title']} ({info['url']})")

    names = dict(DEFAULT_FIELDS)
    if a.estimate_field:
        names["estimate"] = a.estimate_field.strip()
    fields = proj.fields()
    created, kept = [], []

    def ensure_number(key):
        n = names[key]
        if n in fields:
            if fields[n]["type"] != "NUMBER":
                die(f"既存フィールド「{n}」が数値型ではありません ({fields[n]['type']})")
            kept.append(n)
        else:
            proj.create_number(n)
            created.append(n)

    def ensure_select(key, options):
        n = names[key]
        if n in fields:
            missing = [o[0] for o in options if o[0] not in fields[n].get("options", {})]
            if missing:
                warn(f"既存フィールド「{n}」に選択肢 {missing} がありません。Project の設定で追加してください")
            kept.append(n)
        else:
            proj.create_select(n, options)
            created.append(n)

    if names["status"] not in fields:
        die(f"Status フィールド「{names['status']}」が見つかりません")
    for key in ("optimistic", "most_likely", "pessimistic", "estimate", "fever_value"):
        ensure_number(key)
    ensure_select("fever_metric", METRICS)
    ensure_select("fever_zone", ZONES)
    if "phase" in a.cadence:
        ensure_select("phase", [(p, c, "") for p, c in PHASES])

    # バッファ込みの納期までスプリントを用意 (auto は暫定で計画期間の 50%)
    if "sprint" in a.cadence:
        weeks = 1 if a.cadence.endswith("1w") else 2
        span = (planned_end - start).days * (1.5 if buffer == "auto" else 1) + (0 if buffer == "auto" else buffer * 7 / 5)
        count = max(1, math.ceil(span / (7 * weeks)) + 1)
        if names["sprint"] in fields:
            kept.append(names["sprint"])
        elif proj.create_iteration(names["sprint"], start, weeks, count):
            created.append(f"{names['sprint']} ({weeks}週間 × {count})")
        else:
            warn(f"Iteration フィールド「{names['sprint']}」を API で作成できませんでした。Project の Settings から手動で作成してください")

    # 既存の設定 (手で調整した値) と履歴を引き継ぐ
    trackers = find_trackers(proj.items(), names["fever_metric"])
    old, history = {}, []
    if STATE_METRIC in trackers:
        old, history = parse_state(trackers[STATE_METRIC]["content"].get("body") or "", strict=False)
    cfg = {
        "project": info,
        "schedule": {"start_date": start.isoformat(), "planned_end_date": planned_end.isoformat(), "buffer": buffer,
                     "japan_holidays": not a.no_japan_holidays,
                     "extra_holidays": old.get("schedule", {}).get("extra_holidays", [])},
        "cadence": a.cadence,
        "fields": names,
        "done_status": old.get("done_status", ["Done"]),
        "fever_zones": old.get("fever_zones", {"yellow": [0.10, 0.70], "red": [0.30, 0.90]}),
        "buffer_auto": old.get("buffer_auto", {"k": 2.0, "fallback_ratio": 0.5}),
    }
    ensure_trackers(proj, names, proj.fields(), trackers)
    summary(f"## ✅ セットアップ完了: [{info['title']}]({info['url']})\n\n"
            f"- 期間: {start} 〜 {planned_end} (バッファ: {buffer if buffer == 'auto' else f'{buffer} 営業日'})\n"
            f"- 進め方: {CADENCES[a.cadence]}\n"
            f"- 追加したフィールド: {', '.join(created) or 'なし'}\n"
            f"- 既存を利用したフィールド: {', '.join(kept) or 'なし'}\n"
            f"- 設定の保存先: Project 内のドラフト「📈 Fever: 進捗率」の本文 (JSON を編集すると次回から反映)\n")
    summary(insights_recipe(cfg))
    run_daily(proj, cfg, history, today_jst(), Path(a.out))


def find_trackers(items: list[dict], metric_field: str) -> dict:
    names = {m[0] for m in METRICS}
    return {it["values"][metric_field]: it for it in items
            if it["type"] == "DRAFT_ISSUE" and it["values"].get(metric_field) in names}


def ensure_trackers(proj: Project, names: dict, fields: dict, trackers: dict) -> None:
    for metric, _, label in METRICS:
        if metric not in trackers:
            item_id = proj.create_draft(f"📈 Fever: {label} ({metric})", "Projects Insights Kit が自動更新します")
            proj.set_option(item_id, fields[names["fever_metric"]], metric)
            print(f"  + tracker draft: {metric}")


def parse_state(body: str, strict: bool) -> tuple[dict, list]:
    cfg, history = {}, []
    m = CONFIG_RE.search(body)
    if m:
        try:
            cfg = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            if strict:
                die(f"「📈 Fever: 進捗率」の設定 JSON が壊れています ({e})。カンマや引用符を確認してください")
    h = HISTORY_RE.search(body)
    if h:
        try:
            history = json.loads(h.group(1))
        except json.JSONDecodeError:
            warn("履歴を読み取れませんでした。今日から記録し直します")
    return cfg, history


def insights_recipe(cfg: dict) -> str:
    f = cfg["fields"]
    est, metric = f["estimate"], f["fever_metric"]
    excl = f"no:{slug(metric)}"
    rows = [
        ("バーンダウン (全体)", f"`{excl} -status:Done`", "Line", "Time", "None", f"Sum of {est}"),
        ("バーンアップ", f"`{excl}`", "Stacked area", "Time", "Status", f"Sum of {est}"),
        ("フィーバー (推移)", f"`has:{slug(metric)}`", "Line", "Time", metric, f"Sum of {f['fever_value']}"),
        ("フィーバー (現在のゾーン)", f"`has:{slug(metric)}`", "Bar", f["fever_zone"], "None", "Count of items"),
    ]
    if "sprint" in cfg["cadence"]:
        rows.insert(1, ("バーンダウン (今のスプリント)", f"`{excl} -status:Done {slug(f['sprint'])}:@current`", "Line", "Time", "None", f"Sum of {est}"))
    if "phase" in cfg["cadence"]:
        rows.append(("工程別の残作業", f"`{excl} -status:Done`", "Stacked column", f["phase"], "Status", f"Sum of {est}"))
    lines = ["## 📊 Insights の設定 (Project → Insights → New chart → Configure)", "",
             "| チャート | Filter (グラフ上の入力欄) | Layout | X-axis | Group by | Y-axis |", "|---|---|---|---|---|---|"]
    lines += [f"| {' | '.join(r)} |" for r in rows]
    lines += ["", "Time を選んだあと Group by が選べない場合は、Filter に `status:Done` を足した別チャートなどに分けてください。"]
    return "\n".join(lines)


def slug(name: str) -> str:
    """Project のフィルタで使うフィールド名表記 (スペースは - 、括弧などは引用符で囲む)。"""
    s = name.lower().replace(" ", "-")
    return s if re.fullmatch(r"[\w\-]+", s) else q(name)


# ==================================================================== daily
def cmd_daily(a) -> None:
    url = a.project_url or os.environ.get("PROJECT_URL", "")
    info = Project.resolve(url)
    proj = Project(info)
    trackers = find_trackers(proj.items(), DEFAULT_FIELDS["fever_metric"])
    if STATE_METRIC not in trackers:
        die("この Project はまだセットアップされていません。先に mode: setup で実行してください")
    cfg, history = parse_state(trackers[STATE_METRIC]["content"].get("body") or "", strict=True)
    if not cfg:
        die("「📈 Fever: 進捗率」の本文に設定 JSON がありません。mode: setup で再実行してください")
    cfg["project"] = info
    run_daily(proj, cfg, history, parse_date(a.date, "date") if a.date else today_jst(), Path(a.out))


def run_daily(proj: Project, cfg: dict, history: list, today: dt.date, out: Path) -> None:
    f = cfg["fields"]
    fields = proj.fields()
    cal = Calendar(cfg)

    # --- 1) PERT で見積を更新
    items = proj.items()
    trackers = find_trackers(items, f["fever_metric"])
    tracker_ids = {it["id"] for it in trackers.values()}
    work, updated = [], 0
    for it in items:
        v = it["values"]
        if it["id"] in tracker_ids or it["type"] not in ("ISSUE", "DRAFT_ISSUE"):
            continue  # トラッカーと PR は集計しない
        if it["type"] == "ISSUE" and not it["content"].get("url"):
            continue  # 権限が無く中身が見えない Issue
        e = pert(v.get(f["optimistic"]), v.get(f["most_likely"]), v.get(f["pessimistic"]))
        if e is not None and v.get(f["estimate"]) != e:
            proj.set_number(it["id"], fields[f["estimate"]], e)
            v[f["estimate"]] = e
            updated += 1
        work.append(it)

    # --- 2) 集計
    def est(it):
        return it["values"].get(f["estimate"])

    def is_done(it):
        return it["values"].get(f["status"]) in cfg["done_status"] or it["content"].get("state") in ("CLOSED", "MERGED")

    with_est = [i for i in work if est(i)]
    missing = len(work) - len(with_est)
    if with_est:
        total = sum(est(i) for i in with_est)
        done = sum(est(i) for i in with_est if is_done(i))
        unit = "人日"
    else:
        total, done, unit = len(work), sum(1 for i in work if is_done(i)), "件"
    var_sum = sum(((i["values"][f["pessimistic"]] - i["values"][f["optimistic"]]) / 6) ** 2
                  for i in work if i["values"].get(f["pessimistic"]) is not None and i["values"].get(f["optimistic"]) is not None)

    sch = cfg["schedule"]
    start, planned_end = dt.date.fromisoformat(sch["start_date"]), dt.date.fromisoformat(sch["planned_end_date"])
    plan_days = cal.count(start, planned_end)
    buffer_days, buffer_how = compute_buffer(cfg, plan_days, total if unit == "人日" else 0, var_sum)
    deadline = cal.add(planned_end, buffer_days)

    row = None
    if today >= start:
        elapsed = cal.elapsed(start, today)
        p = done / total if total else 0.0
        b = max(0.0, elapsed - p * plan_days) / buffer_days
        row = {"date": today.isoformat(), "elapsed_days": elapsed, "done": round(done, 2), "total": round(total, 2),
               "unit": unit, "progress": round(p * 100, 1), "buffer": round(b * 100, 1), "buffer_days": buffer_days,
               "zone": fever_zone(p, b, cfg["fever_zones"])}
        history = sorted([h for h in history if h["date"] != row["date"]] + [row], key=lambda h: h["date"])[-MAX_HISTORY:]
    out.mkdir(parents=True, exist_ok=True)
    save(out / "fever-history.json", history)
    svg_fever_chart(out / "fever-chart.svg", history, cfg["fever_zones"], f"フィーバーチャート — {cfg['project']['title']}")

    # --- 3) トラッカー (ドラフト Issue) を更新。設定と履歴は「進捗率」側に保存
    if len(trackers) < len(METRICS):
        ensure_trackers(proj, f, fields, trackers)
        trackers = find_trackers(proj.items(), f["fever_metric"])
    visible = tracker_body(cfg, history, plan_days, buffer_days, buffer_how, deadline, missing)
    stored_cfg = {k: v for k, v in cfg.items() if k != "project"}
    for metric, it in trackers.items():
        if metric == STATE_METRIC:
            body = (f"{visible}\n\n### ⚙️ 設定 (編集できます。次回の実行から反映)\n\n```json\n"
                    f"{json.dumps(stored_cfg, ensure_ascii=False, indent=2)}\n```\n\n"
                    f"<!-- insights-kit:history\n{json.dumps(history, ensure_ascii=False, separators=(',', ':'))}\n-->")
        else:
            body = f"{visible}\n\n設定と履歴は「📈 Fever: 進捗率」の本文にあります。"
        if row:
            value = row["progress"] if metric == "Progress %" else row["buffer"]
            if it["values"].get(f["fever_value"]) != value:
                proj.set_number(it["id"], fields[f["fever_value"]], value)
            if it["values"].get(f["fever_zone"]) != row["zone"]:
                proj.set_option(it["id"], fields[f["fever_zone"]], row["zone"])
        if it["content"].get("body") != body:
            proj.edit_draft(it["content"]["id"], body)

    if not row:
        summary(f"開始日 {start} 前のため、見積の更新と設定の保存のみ行いました (見積更新 {updated} 件)")
        return
    zone = row["zone"]
    summary(f"## {ZONE_EMOJI[zone]} {today} — 進捗 {row['progress']}% / バッファ消費 {row['buffer']}%\n\n"
            f"| 項目 | 値 |\n|---|---|\n"
            f"| 完了 / 全体 | {row['done']} / {row['total']} {unit} |\n"
            f"| 経過 / 計画 | {row['elapsed_days']} / {plan_days} 営業日 |\n"
            f"| バッファ | {buffer_days} 営業日 ({buffer_how}) |\n"
            f"| 納期 (バッファ込み) | {deadline} |\n"
            f"| PERT で見積を更新 | {updated} 件 |\n"
            f"| 見積が未入力 | {missing} 件 |\n\n"
            "フィーバーチャートの図 (SVG) は、この実行結果の Artifacts からダウンロードできます。\n")
    print(f"progress={row['progress']}% buffer={row['buffer']}% zone={zone} ({row['done']}/{row['total']} {unit}), estimates updated={updated}")


def tracker_body(cfg, history, plan_days, buffer_days, buffer_how, deadline, missing) -> str:
    sch = cfg["schedule"]
    lines = [
        "Projects Insights Kit が毎営業日自動更新します (表は上書きされます)。",
        "",
        f"- 期間: {sch['start_date']} 〜 {sch['planned_end_date']} (計画 {plan_days} 営業日) / 納期 (バッファ込み): {deadline}",
        f"- プロジェクトバッファ: {buffer_days} 営業日 ({buffer_how})",
        f"- 見積が未入力の項目: {missing} 件",
        "",
    ]
    if history:
        lines += ["| 日付 | 完了 / 全体 | 進捗率 | バッファ消費率 | ゾーン |", "|---|---:|---:|---:|:---:|"]
        for h in history[-30:]:
            lines.append(f"| {h['date']} | {h['done']} / {h['total']} {h['unit']} | {h['progress']}% | {h['buffer']}% | {ZONE_EMOJI[h['zone']]} |")
    else:
        lines.append("(開始日から記録を始めます)")
    return "\n".join(lines)


# ==================================================================== SVG
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg_fever_chart(path: Path, history: list[dict], zones: dict, title: str) -> None:
    W, H, L, R, T, B = 640, 480, 70, 30, 50, 60
    pw, ph = W - L - R, H - T - B
    ymax = max([120] + [h["buffer"] + 10 for h in history])

    def x(p):
        return L + pw * min(p, 100) / 100

    def y(b):
        return T + ph * (1 - b / ymax)

    def zl(z, p):
        return (z[0] + (z[1] - z[0]) * p / 100) * 100

    yl, rl = zones["yellow"], zones["red"]
    polys = [
        ("#dafbe1", [(0, 0), (100, 0), (100, zl(yl, 100)), (0, zl(yl, 0))]),
        ("#fff8c5", [(0, zl(yl, 0)), (100, zl(yl, 100)), (100, zl(rl, 100)), (0, zl(rl, 0))]),
        ("#ffebe9", [(0, zl(rl, 0)), (100, zl(rl, 100)), (100, ymax), (0, ymax)]),
    ]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{L}" y="30" font-size="16" font-weight="bold" fill="#24292f">{_esc(title)}</text>']
    for color, pts in polys:
        coords = " ".join(f"{x(px):.1f},{y(py):.1f}" for px, py in pts)
        out.append(f'<polygon points="{coords}" fill="{color}"/>')
    for k in range(0, 101, 20):
        out.append(f'<text x="{x(k):.1f}" y="{T + ph + 18}" text-anchor="middle" fill="#57606a">{k}%</text>')
    for k in range(0, int(ymax) + 1, 20):
        out.append(f'<line x1="{L}" x2="{L + pw}" y1="{y(k):.1f}" y2="{y(k):.1f}" stroke="#00000014"/>')
        out.append(f'<text x="{L - 8}" y="{y(k) + 4:.1f}" text-anchor="end" fill="#57606a">{k}%</text>')
    out.append(f'<line x1="{L}" x2="{L + pw}" y1="{y(100):.1f}" y2="{y(100):.1f}" stroke="#cf222e" stroke-dasharray="4 4"/>')
    out.append(f'<text x="{L + pw - 4}" y="{y(100) - 6:.1f}" text-anchor="end" fill="#cf222e">バッファ枯渇 (納期遅延)</text>')
    out.append(f'<text x="{L + pw / 2}" y="{H - 16}" text-anchor="middle" fill="#24292f">進捗率 (完了見積 / 全体見積)</text>')
    out.append(f'<text x="18" y="{T + ph / 2}" transform="rotate(-90 18 {T + ph / 2})" text-anchor="middle" fill="#24292f">バッファ消費率</text>')
    out.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="#8c959f"/>')
    if not history:
        out.append(f'<text x="{L + pw / 2}" y="{T + ph / 2}" text-anchor="middle" fill="#57606a">まだデータがありません</text>')
    else:
        pts = " ".join(f"{x(h['progress']):.1f},{y(h['buffer']):.1f}" for h in history)
        out.append(f'<polyline points="{pts}" fill="none" stroke="#24292f" stroke-width="2"/>')
        color = {"Green": "#1a7f37", "Yellow": "#9a6700", "Red": "#cf222e"}
        step = max(1, len(history) // 12)  # ラベルが多すぎないよう間引く
        for i, h in enumerate(history):
            last = i == len(history) - 1
            out.append(f'<circle cx="{x(h["progress"]):.1f}" cy="{y(h["buffer"]):.1f}" r="{6 if last else 4}" '
                       f'fill="{color[h["zone"]]}" stroke="#fff" stroke-width="1.5"><title>{h["date"]}</title></circle>')
            if last or i % step == 0:
                out.append(f'<text x="{x(h["progress"]) + 7:.1f}" y="{y(h["buffer"]) - 7:.1f}" fill="#24292f" font-size="11">{h["date"][5:]}</text>')
    out.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


# ==================================================================== token
def cmd_check_token(a) -> None:
    if not os.environ.get("GH_TOKEN"):
        die("トークンがありません。Actions では Secret PROJECT_TOKEN、手元では .env の PROJECT_TOKEN を設定してください")
    r = subprocess.run(["gh", "api", "-i", "user"], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        die("トークンで GitHub に認証できませんでした (期限切れ・削除済み・貼り間違い・Organization の承認待ちの可能性)")
    scopes = next((ln.split(":", 1)[1].strip() for ln in r.stdout.splitlines() if ln.lower().startswith("x-oauth-scopes:")), None)
    if scopes is None:
        print("fine-grained トークンです (Projects 権限は Project へのアクセスで確認します)")
    else:
        print(f"classic トークン: scopes={scopes}")
        if "project" not in scopes.replace(" ", "").split(","):
            die("classic トークンに project スコープがありません")
    print("OK")


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="既存 Project にフィールドとトラッカーを追加")
    s.add_argument("--project-url", required=True)
    s.add_argument("--start-date", default="")
    s.add_argument("--planned-end-date", required=True, help="バッファを含まない計画完了日")
    s.add_argument("--buffer", default="auto", help="auto (PERT から自動) または営業日数")
    s.add_argument("--cadence", default="sprint-2w", choices=list(CADENCES))
    s.add_argument("--estimate-field", default="", help="既存の見積 (数値) フィールド名。空なら「見積 (人日)」を作成")
    s.add_argument("--no-japan-holidays", action="store_true")
    s.add_argument("--out", default="insights-kit-out", help="SVG / 履歴 JSON の出力先")
    s.set_defaults(fn=cmd_setup)
    d = sub.add_parser("daily", help="見積の再計算とフィーバー指標の更新")
    d.add_argument("--project-url", default="", help="既定: 環境変数 PROJECT_URL")
    d.add_argument("--out", default="insights-kit-out", help="SVG / 履歴 JSON の出力先")
    d.add_argument("--date", default="", help="基準日 (既定: 今日 JST)")
    d.set_defaults(fn=cmd_daily)
    sub.add_parser("check-token", help="トークンの確認").set_defaults(fn=cmd_check_token)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
