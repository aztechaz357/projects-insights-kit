#!/usr/bin/env python3
"""Projects Insights Kit

既存の GitHub Project (Organization / 個人) に、次のものを後付けします。既存 Issue の中身は変更しません。

  - 三点見積 (PERT): 楽観・最可能・悲観 (人日) を入れると「見積 (人日)」= (楽観 + 4×最可能 + 悲観) / 6 を自動計算
  - 進め方 (cadence): 2週間 / 1週間スプリント、ウォーターフォールの工程 (Phase)、工程 × スプリント、なし
  - フィーバーチャート: 進捗率とバッファ消費率を毎日計算し、Project のドラフト Issue と SVG に記録

設定と履歴は Project 内のドラフト Issue「📈 Fever: 進捗率」の本文に保存するため、リポジトリへの書き込みは不要です。
通信先は GitHub API (gh CLI 経由) だけです。外部サービスへの送信や外部コードの取得は行いません
(日本の祝日もこのファイルの中で計算します)。標準ライブラリと gh CLI だけで動きます。

通常は GitHub Actions (.github/workflows/insights-kit.yml) から実行します。
手元で動かす場合は GH_TOKEN (または カレントディレクトリの .env の PROJECT_TOKEN) を設定して:

  python .github/insights-kit/insights_kit.py setup --project-url URL --planned-end-date YYYY-MM-DD [--start-date ...] [--buffer auto|N]
                                   [--cadence phase+sprint-2w] [--estimate-field 名前] [--no-japan-holidays]
                                   [--focus-issues "acme/portal#12, acme/portal#30"]
  python .github/insights-kit/insights_kit.py daily --project-url URL [--date YYYY-MM-DD]
  python .github/insights-kit/insights_kit.py check-token
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import urllib.parse
import subprocess
import sys
from pathlib import Path

DOTENV = Path.cwd() / ".env"
STATE_METRIC = "Progress %"  # 設定と履歴はこのトラッカーの本文に保存
CONFIG_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
HISTORY_RE = re.compile(r"<!-- insights-kit:history\s*(\[.*?\])\s*-->", re.S)
MAX_HISTORY = 260  # 約 1 年分の営業日
BODY_LIMIT = 60000  # ドラフト本文の上限は 65,536 文字。余裕を持たせる

JST = dt.timezone(dt.timedelta(hours=9))
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
def _nth_monday(year: int, month: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(7 - first.weekday()) % 7 + 7 * (n - 1))


def japan_holidays(year: int) -> set[dt.date]:
    """日本の祝日 (国民の祝日に関する法律、2020 年以降の規定)。外部 API を使わずに計算する。

    春分・秋分は 1980〜2099 年の近似式。会社独自の休日は設定の extra_holidays に入れる。
    """
    if year < 1980 or year > 2099:
        return set()
    D = lambda m, d: dt.date(year, m, d)  # noqa: E731
    y = year - 1980
    spring = int(20.8431 + 0.242194 * y - y // 4)
    autumn = int(23.2488 + 0.242194 * y - y // 4)
    hs = {D(1, 1), _nth_monday(year, 1, 2), D(2, 11), D(3, spring), D(4, 29), D(5, 3), D(5, 4), D(5, 5),
          _nth_monday(year, 9, 3), D(9, autumn), D(11, 3), D(11, 23)}
    if year >= 2020:
        hs.add(D(2, 23))  # 天皇誕生日
    # 海の日・山の日・スポーツの日 (2020/2021 は東京五輪の特例)
    special = {2020: [D(7, 23), D(7, 24), D(8, 10)], 2021: [D(7, 22), D(7, 23), D(8, 8)]}
    hs.update(special.get(year) or [_nth_monday(year, 7, 3), D(8, 11), _nth_monday(year, 10, 2)])
    # 振替休日: 祝日が日曜なら、その後の最初の祝日でない日
    for h in sorted(hs):
        if h.weekday() == 6:
            d = h + dt.timedelta(days=1)
            while d in hs:
                d += dt.timedelta(days=1)
            hs.add(d)
    # 国民の休日: 前後を祝日に挟まれた平日
    for h in sorted(hs):
        d = h + dt.timedelta(days=1)
        if d not in hs and d + dt.timedelta(days=1) in hs and d.weekday() != 6:
            hs.add(d)
    return hs


class Calendar:
    def __init__(self, cfg: dict):
        sch = cfg["schedule"]
        self.holidays: set[str] = set(sch.get("extra_holidays", []))
        self.japan = bool(sch.get("japan_holidays", True))
        self._jp_years: set[int] = set()

    def is_bday(self, d: dt.date) -> bool:
        if self.japan and d.year not in self._jp_years:
            self._jp_years.add(d.year)
            self.holidays |= {h.isoformat() for h in japan_holidays(d.year)}
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
    ... on ProjectV2FieldCommon { id databaseId name dataType }
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
        ... on Issue { number title state url repository { nameWithOwner } assignees(first: 10) { nodes { login } }
                       parent { number repository { nameWithOwner } } }
        ... on PullRequest { number title state url }
        ... on DraftIssue { id title body assignees(first: 10) { nodes { login } } }
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
            e = {"id": f["id"], "db": f.get("databaseId"), "type": f.get("dataType")}
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

    def create_draft(self, title: str, body: str) -> dict:
        """ドラフト Issue を作り、items() と同じ形の dict を返す (作成直後は items() に出ないことがあるため)。"""
        data = gql("mutation($p: ID!, $t: String!, $b: String!) { addProjectV2DraftIssue(input: {projectId: $p, title: $t, body: $b}) "
                   "{ projectItem { id content { ... on DraftIssue { id } } } } }", p=self.id, t=title, b=body)
        item = data["addProjectV2DraftIssue"]["projectItem"]
        return {"id": item["id"], "type": "DRAFT_ISSUE", "values": {},
                "content": {"id": item["content"]["id"], "title": title, "body": body}}

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
        "focus_issues": parse_focus_issues(a.focus_issues) if a.focus_issues.strip() else old.get("focus_issues", []),
        "done_status": old.get("done_status", ["Done"]),
        "fever_zones": old.get("fever_zones", {"yellow": [0.10, 0.70], "red": [0.30, 0.90]}),
        "buffer_auto": old.get("buffer_auto", {"k": 2.0, "fallback_ratio": 0.5}),
    }
    summary(f"## ✅ セットアップ完了: [{info['title']}]({info['url']})\n\n"
            f"- 期間: {start} 〜 {planned_end} (バッファ: {buffer if buffer == 'auto' else f'{buffer} 営業日'})\n"
            f"- 進め方: {CADENCES[a.cadence]}\n"
            f"- 追加したフィールド: {', '.join(created) or 'なし'}\n"
            f"- 既存を利用したフィールド: {', '.join(kept) or 'なし'}\n"
            f"- 設定の保存先: Project 内のドラフト「📈 Fever: 進捗率」の本文 (JSON を編集すると次回から反映)\n")
    fields = proj.fields()  # setup で作ったフィールドも含めて読み直す (チャートのリンクに ID が要る)
    statuses = list(fields[names["status"]].get("options", {}))
    if len(statuses) < 3:
        warn(f"Status の選択肢が {statuses} だけのため、累積フロー図 (CFD) で作業中の滞留が見えません。"
             "Project の Status に「In Progress」「In Review」などを追加してください")
    summary(insights_recipe(cfg, statuses, fields))
    run_daily(proj, cfg, history, today_jst(), Path(a.out))


TRACKER_TITLE_RE = re.compile(r"^📈 Fever: .*\((Progress %|Buffer %)\)$")


def tracker_metric(it: dict, metric_field: str) -> str | None:
    """トラッカーのドラフトなら指標名を返す。作成直後は Fever Metric の値が API に反映されていないことがあるため、
    タイトルでも判定する (判定できないと重複して作ってしまう)。"""
    if it["type"] != "DRAFT_ISSUE":
        return None
    v = it["values"].get(metric_field)
    if v in {m[0] for m in METRICS}:
        return v
    m = TRACKER_TITLE_RE.match(it["content"].get("title") or "")
    return m.group(1) if m else None


def find_trackers(items: list[dict], metric_field: str) -> dict:
    out = {}
    for it in items:
        metric = tracker_metric(it, metric_field)
        if metric and metric not in out:  # 同じ指標が複数あれば最初の 1 件を使う
            out[metric] = it
    return out


def ensure_trackers(proj: Project, names: dict, fields: dict, trackers: dict) -> None:
    """足りないトラッカーを作り、trackers に追加する (作り直した一覧を読み直さない)。"""
    for metric, _, label in METRICS:
        if metric not in trackers:
            it = proj.create_draft(f"📈 Fever: {label} ({metric})", "Projects Insights Kit が自動更新します")
            proj.set_option(it["id"], fields[names["fever_metric"]], metric)
            it["values"][names["fever_metric"]] = metric
            trackers[metric] = it
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


def insights_recipe(cfg: dict, statuses: list[str] | None = None, fields: dict | None = None) -> str:
    """Insights のチャート設定表 (Markdown)。試験用 Project の実際の画面で確認した組み合わせだけを載せる:
    X-axis が Time のチャートは Group by を選べず、系列は Open / Completed (Status が Done) に固定される。"""
    f = cfg["fields"]
    est, metric = f["estimate"], f["fever_metric"]
    excl = f"no:{slug(metric)}"
    auto = "(選べない)"
    rows = [
        ("バーンダウン (全体)", f"`{excl} -status:Done`", "Line", "Time", auto, f"Sum of {est}"),
        ("バーンアップ", f"`{excl}`", "Stacked area", "Time", auto, f"Sum of {est}"),
        ("進捗率の推移", f"`{slug(metric)}:{q('Progress %')}`", "Line", "Time", auto, f"Sum of {f['fever_value']}"),
        ("バッファ消費率の推移", f"`{slug(metric)}:{q('Buffer %')}`", "Line", "Time", auto, f"Sum of {f['fever_value']}"),
        ("フィーバー (現在のゾーン)", f"`has:{slug(metric)}`", "Bar", f["fever_zone"], "None", "Count of items"),
        ("Status 別の件数 (今)", f"`{excl} is:issue`", "Column", "Status", "None", "Count of items"),
    ]
    if "sprint" in cfg["cadence"]:
        rows.insert(1, ("バーンダウン (今のスプリント)", f"`{excl} -status:Done {slug(f['sprint'])}:@current`", "Line", "Time", auto, f"Sum of {est}"))
    if "phase" in cfg["cadence"]:
        rows.append(("工程別の残作業", f"`{excl} -status:Done`", "Stacked column", f["phase"], "Status", f"Sum of {est}"))
    rows += [
        ("担当者別の残作業", f"`{excl} -status:Done`", "Stacked column", "Assignees", "Status", f"Sum of {est}"),
        ("自分のバーンダウン (見る人ごと)", f"`{excl} -status:Done assignee:@me`", "Line", "Time", auto, f"Sum of {est}"),
        ("注目 Issue 別の残作業", f"`{excl} -status:Done`", "Stacked column", "Parent issue", "Status", f"Sum of {est}"),
    ]
    for ref in cfg.get("focus_issues") or ["OWNER/REPO#番号"]:
        rows.append((f"注目 {ref} のバーンダウン", f"`{excl} -status:Done parent-issue:{ref}`", "Line", "Time", auto, f"Sum of {est}"))
    url = (cfg.get("project") or {}).get("url")
    if fields and url:
        links = [chart_link(url, r, fields, f["status"]) for r in rows]
        lines = ["## 📊 Insights のチャート", "",
                 "**作り方**: 「開く」のリンクを開く → 左の **Burn up** の横の **⋯** → **Save changes to new chart** → "
                 "グラフ名の横の鉛筆で名前を付ける。手で作るときは、下の表のとおりに **New chart → Configure** で設定します。", "",
                 "| チャート | 作成 | Filter (グラフ上の入力欄) | Layout | X-axis | Group by | Y-axis |",
                 "|---|---|---|---|---|---|---|"]
        lines += [f"| {r[0]} | {f'[開く]({lk})' if lk else '-'} | {' | '.join(r[1:])} |" for r, lk in zip(rows, links)]
    else:
        lines = ["## 📊 Insights の設定 (Project → Insights → New chart → Configure)", "",
                 "| チャート | Filter (グラフ上の入力欄) | Layout | X-axis | Group by | Y-axis |", "|---|---|---|---|---|---|"]
        lines += [f"| {' | '.join(r)} |" for r in rows]
    lines += ["",
              "- X-axis を **Time** にしたチャートは Group by を選べず、線 (帯) は **Open** と **Completed** (Status が Done) の 2 つに自動で分かれます",
              "- Y-axis で `Sum of …` を選ぶと、Count of items の代わりにフィールドの合計になります",
              "",
              "### 累積フロー図 (CFD)",
              "Insights では Status ごとの帯を時系列に積み上げられないため、CFD はキットが描きます。",
              "毎朝 Status ごとの件数を記録し、実行結果の **Artifacts** (`cfd.svg`) と「📈 Fever: 進捗率」の本文の表に出します。",
              "- 帯の **縦の幅** = その Status にいる件数 (仕掛かり量)。作業中の帯が太り続けていたら滞留しています",
              "- 帯の **横の幅** ≒ その Status を通り抜けるのにかかる時間 (リードタイム)",
              "- 一番下の完了の帯の **増え方** = 完了のペース。全体の高さが増え続けていたらスコープが増えています",
              "- 帯の並びは Project の Status の選択肢の順です。Settings → Status で流れの順 "
              "(例: Todo → In Progress → In Review → Done) に並べてください"]
    if statuses:
        lines += [f"- 今の Status の順: {' → '.join(statuses)}"]
        if len(statuses) < 3:
            lines += ["- ⚠️ 作業中を表す Status がありません。「In Progress」「In Review」などを追加すると、滞留が見えるようになります"]
    lines += ["", "### 担当者ごと・注目 Issue ごとに見る",
              "- `assignee:@me` は **グラフを見ている人自身** に置き換わるので、1 枚で各自が自分の分を見られます。"
              "特定の人だけを見るときは `assignee:ユーザー名` (カンマ区切りで複数可) にします",
              "- 「注目 Issue 別の残作業」は、X-axis の **Parent issue** で親 Issue ごとに 1 本の棒になります (親がないものは No Parent issue)",
              "- `parent-issue:OWNER/REPO#番号` は、その Issue の **サブ Issue** (直下の子) のうち、この Project に入っているものだけに絞ります",
              "- 担当者別・注目 Issue 別の進捗の表は、毎朝の実行結果の Summary と「📈 Fever: 進捗率」の本文にも出ます"
              " (こちらは孫以下のサブ Issue も含めて集計します)"]
    if not cfg.get("focus_issues"):
        lines += ["- 注目 Issue を設定すると、上の表に Issue ごとの行が入ります (mode: setup の focus_issues、または設定 JSON の `focus_issues`)"]
    return "\n".join(lines)


CHART_LAYOUTS = {"Line": "line", "Stacked area": "stacked-area", "Bar": "bar", "Column": "column",
                 "Stacked column": "stacked-column", "Stacked bar": "stacked-bar"}


def chart_link(project_url: str, row: tuple, fields: dict, status_field: str) -> str | None:
    """設定済みのチャートを開く URL。Insights は設定を URL のクエリに持つので、既定の Burn up に設定を付けて開き、
    「Save changes to new chart」で保存してもらう (試験用 Project の画面で確認した形式)。
    使うフィールドが Project に無い・プレースホルダーの行は None。"""
    _, flt, layout, x, group, y = row
    flt = flt.strip("`")
    if "OWNER/REPO" in flt or layout not in CHART_LAYOUTS:
        return None

    def db(name):
        return (fields.get(name) or {}).get("db")

    params = []
    if x == "Time":
        # Time のときも UI は Group by に Status の ID を入れる (無いと「設定が無効」になる)
        if not db(status_field):
            return None
        params += [("xAxis[dataSource][column]", "time"), ("xAxis[groupBy]", str(db(status_field)))]
    else:
        if not db(x) or (group not in ("None", "") and not db(group)):
            return None
        params += [("xAxis[dataSource][column]", str(db(x))),
                   ("xAxis[groupBy]", "" if group in ("None", "") else str(db(group)))]
    if y.startswith("Sum of "):
        if not db(y[len("Sum of "):]):
            return None
        params += [("yAxis[aggregate][operation]", "sum"), ("yAxis[aggregate][columns]", str(db(y[len("Sum of "):])))]
    params += [("filterQuery", flt), ("layout", CHART_LAYOUTS[layout])]
    return f"{project_url.rstrip('/')}/insights?{urllib.parse.urlencode(params)}"


def parse_focus_issues(text: str) -> list[str]:
    """「acme/portal#12, https://github.com/acme/portal/issues/30」→ ["acme/portal#12", "acme/portal#30"]"""
    out = []
    for tok in re.split(r"[,\s、]+", text.strip()):
        if not tok:
            continue
        m = (re.fullmatch(r"(?:https?://github\.com/)?([\w.-]+/[\w.-]+)(?:#|/issues/)(\d+)/?", tok))
        if not m:
            die(f"注目 Issue は owner/repo#番号 か Issue の URL で入力してください: '{tok}'")
        ref = f"{m.group(1)}#{m.group(2)}"
        if ref not in out:
            out.append(ref)
    return out


def issue_ref(content: dict) -> str | None:
    repo = (content.get("repository") or {}).get("nameWithOwner")
    return f"{repo}#{content['number']}" if repo and content.get("number") else None


def breakdown_md(cfg: dict, work: list[dict], all_items: list[dict], est, is_done) -> str:
    """担当者別・注目 Issue 別の進捗表 (Markdown)。見積が 1 件も無いグループは件数で計算する。"""
    def stats(group: list[dict]) -> str:
        n, d = len(group), sum(1 for i in group if is_done(i))
        e_all = [i for i in group if est(i)]
        rest = round(sum(est(i) for i in e_all if not is_done(i)), 1)
        if e_all:
            total = sum(est(i) for i in e_all)
            p = sum(est(i) for i in e_all if is_done(i)) / total * 100
        else:
            p = d / n * 100 if n else 0.0
        return f"{n} | {d} | {rest} | {p:.0f}%"

    head = "| {} | 件数 | 完了 | 残り見積 (人日) | 進捗 | Insights の Filter に足す |\n|---|---|---|---|---|---|"
    lines = ["### 👤 担当者別", "", head.format("担当者")]
    by_user: dict[str, list] = {}
    for it in work:
        logins = [a["login"] for a in ((it["content"].get("assignees") or {}).get("nodes") or [])]
        for login in logins or [""]:
            by_user.setdefault(login, []).append(it)
    for login in sorted(by_user, key=lambda k: (k == "", k.lower())):
        name, flt = (f"@{login}", f"`assignee:{login}`") if login else ("(未割り当て)", "`no:assignee`")
        lines.append(f"| {name} | {stats(by_user[login])} | {flt} |")

    focus = cfg.get("focus_issues") or []
    if focus:
        parent_of, titles = {}, {}
        for it in all_items:
            c = it["content"]
            ref = issue_ref(c)
            if ref:
                titles[ref.lower()] = c.get("title", "")
                if c.get("parent"):
                    parent_of[ref.lower()] = issue_ref(c["parent"]).lower()

        def under(ref: str, root: str) -> bool:
            seen = set()
            while ref in parent_of and ref not in seen:
                seen.add(ref)
                ref = parent_of[ref]
                if ref == root:
                    return True
            return False

        lines += ["", "### 🎯 注目 Issue 別 (サブ Issue を孫以下まで集計)", "", head.format("注目 Issue")]
        for root in focus:
            group = [i for i in work if (r := issue_ref(i["content"])) and under(r.lower(), root.lower())]
            title = titles.get(root.lower(), "").replace("|", "\\|")
            label = f"{root} {title}".strip()
            if group:
                lines.append(f"| {label} | {stats(group)} | `parent-issue:{root}` |")
            else:
                lines.append(f"| {label} | 0 | - | - | - | サブ Issue がこの Project にありません |")
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
        if it["id"] in tracker_ids or tracker_metric(it, f["fever_metric"]) or it["type"] not in ("ISSUE", "DRAFT_ISSUE"):
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
               "zone": fever_zone(p, b, cfg["fever_zones"]), "status": status_counts(work, f["status"], cfg["done_status"])}
        history = sorted([h for h in history if h["date"] != row["date"]] + [row], key=lambda h: h["date"])[-MAX_HISTORY:]
    status_order = list(fields[f["status"]].get("options", {}))
    out.mkdir(parents=True, exist_ok=True)
    save(out / "fever-history.json", history)
    svg_fever_chart(out / "fever-chart.svg", history, cfg["fever_zones"], f"フィーバーチャート — {cfg['project']['title']}")
    svg_cfd(out / "cfd.svg", history, status_order, cfg["done_status"], f"累積フロー図 (CFD) — {cfg['project']['title']}")

    # --- 3) トラッカー (ドラフト Issue) を更新。設定と履歴は「進捗率」側に保存
    if len(trackers) < len(METRICS):
        ensure_trackers(proj, f, fields, trackers)  # 作成したものは trackers に入る
    breakdown = breakdown_md(cfg, work, items, est, is_done)
    cfd_table = cfd_md(history, status_order, cfg["done_status"])
    visible = (tracker_body(cfg, history, plan_days, buffer_days, buffer_how, deadline, missing)
               + "\n\n" + breakdown + ("\n\n" + cfd_table if cfd_table else ""))
    stored_cfg = {k: v for k, v in cfg.items() if k != "project"}
    for metric, it in trackers.items():
        if metric == STATE_METRIC:
            body = state_body(visible, stored_cfg, history)
        else:
            body = f"{visible}\n\n設定と履歴は「📈 Fever: 進捗率」の本文にあります。"
        if it["values"].get(f["fever_metric"]) != metric:
            proj.set_option(it["id"], fields[f["fever_metric"]], metric)
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
            + "\n\n".join(x for x in (fever_mermaid(history, cfg["fever_zones"]),
                                       cfd_mermaid(history, status_order, cfg["done_status"])) if x) + "\n\n"
            "本来の形のフィーバーチャート (横軸が進捗率の `fever-chart.svg`) と累積フロー図 (`cfd.svg`) は、"
            "この実行結果の Artifacts からダウンロードできます。\n\n"
            + breakdown + ("\n\n" + cfd_table if cfd_table else "") + "\n")
    print(f"progress={row['progress']}% buffer={row['buffer']}% zone={zone} ({row['done']}/{row['total']} {unit}), estimates updated={updated}")


def _mermaid_num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _mermaid_chart(title: str, labels: list[str], y_title: str, ymax: float, series: list[tuple[str, str, list[float]]],
                   note: str) -> str:
    """Mermaid の xychart (折れ線)。series = [(凡例名, 色, 値)]。xychart には凡例が無いので note に色の説明を書く。"""
    palette = ", ".join(c for _, c, _ in series)
    lines = ["```mermaid",
             f'%%{{init: {{"themeVariables": {{"xyChart": {{"plotColorPalette": "{palette}"}}}}}}}}%%',
             "xychart-beta",
             f'    title "{title}"',
             "    x-axis [" + ", ".join(f'"{x}"' for x in labels) + "]",
             f'    y-axis "{y_title}" 0 --> {_mermaid_num(ymax)}']
    lines += ["    line [" + ", ".join(_mermaid_num(v) for v in vals) + "]" for _, _, vals in series]
    return "\n".join(lines + ["```", "", note])


def fever_mermaid(history: list[dict], zones: dict, days: int = 30) -> str:
    """フィーバーチャートを、日付ごとのバッファ消費率と、その日の進捗率での黄・赤の境界線として描く (Mermaid)。
    Actions の Summary などで画像なしに見られる。Project のドラフトの画面では Mermaid が描画されないので本文には入れない。"""
    rows = [h for h in history if "buffer" in h][-days:]
    if not rows:
        return ""

    def zl(z, p):
        return (z[0] + (z[1] - z[0]) * p / 100) * 100

    ymax = math.ceil(max([120] + [h["buffer"] + 10 for h in rows]) / 10) * 10
    series = [("赤ゾーンの境界", "#cf222e", [zl(zones["red"], h["progress"]) for h in rows]),
              ("黄ゾーンの境界", "#d4a72c", [zl(zones["yellow"], h["progress"]) for h in rows]),
              ("バッファ枯渇 (100%)", "#8c959f", [100.0] * len(rows)),
              ("バッファ消費率", "#0969da", [h["buffer"] for h in rows])]
    note = ("🔵 青線: バッファ消費率 / 🟡 黄線・🔴 赤線: その日の進捗率でのゾーンの境界 (青線がこれを超えると黄・赤) / "
            "灰線: バッファ枯渇 (納期遅延)")
    return _mermaid_chart("フィーバー: バッファ消費率の推移 (%)", [h["date"][5:] for h in rows], "バッファ消費率 (%)",
                          ymax, series, note)


def cfd_mermaid(history: list[dict], status_order: list[str], done_status: list[str], days: int = 30) -> str:
    """CFD を、積み上げの境界線 (下から累計した件数) の折れ線として描く (Mermaid には積み上げ面グラフが無いため)。"""
    rows = [h for h in history if h.get("status")][-days:]
    if not rows:
        return ""
    bands = cfd_bands(rows, status_order, done_status)
    series, acc = [], [0] * len(rows)
    for bi, band in enumerate(bands):
        acc = [acc[i] + rows[i]["status"].get(band, 0) for i in range(len(rows))]
        color = "#afb8c1" if band == NO_STATUS else CFD_COLORS[bi % len(CFD_COLORS)]
        series.append((band, color, list(acc)))
    ymax = max(1, math.ceil(max(acc) * 1.1))
    marks = {"#2da44e": "🟢", "#0969da": "🔵", "#8250df": "🟣", "#bf3989": "🔴", "#bc4c00": "🟠", "#afb8c1": "⚪"}
    note = ("線は下から積み上げた件数の上端です (線と線の間がその Status の件数)。 " +
            " / ".join(f"{marks.get(c, '■')} {b} まで" for b, c, _ in series))
    return _mermaid_chart("累積フロー図 (CFD・件数)", [h["date"][5:] for h in rows], "件数", ymax, series, note)


def state_body(visible: str, stored_cfg: dict, history: list[dict]) -> str:
    """「📈 Fever: 進捗率」の本文 (表示 + 設定 JSON + 履歴)。
    ドラフト本文の上限 (65,536 文字) を超えないよう、超える分は古い履歴から削る (直近 30 日分は必ず残す)。"""
    kept = history
    while True:
        body = (f"{visible}\n\n### ⚙️ 設定 (編集できます。次回の実行から反映)\n\n```json\n"
                f"{json.dumps(stored_cfg, ensure_ascii=False, indent=2)}\n```\n\n"
                f"<!-- insights-kit:history\n{json.dumps(kept, ensure_ascii=False, separators=(',', ':'))}\n-->")
        if len(body) <= BODY_LIMIT or len(kept) <= 30:
            return body
        kept = kept[max(1, len(kept) // 10):]


def status_counts(work: list[dict], status_field: str, done_status: list[str]) -> dict:
    """CFD 用に、作業項目を Status ごとに数える。
    進捗率の計算と合わせるため、Close 済みの Issue は Status にかかわらず完了 (done_status の先頭) として数える。"""
    counts: dict[str, int] = {}
    for it in work:
        s = it["values"].get(status_field) or NO_STATUS
        if it["content"].get("state") in ("CLOSED", "MERGED") and s not in done_status and done_status:
            s = done_status[0]
        counts[s] = counts.get(s, 0) + 1
    return counts


def cfd_md(history: list[dict], status_order: list[str], done_status: list[str], days: int = 10) -> str:
    """CFD の元データ (直近の日付 × Status の件数) の表。"""
    rows = [h for h in history if h.get("status")][-days:]
    if not rows:
        return ""
    cols = list(reversed(cfd_bands(rows, status_order, done_status)))  # 表は流れの順 (Todo → … → Done)
    lines = [f"### 📊 累積フロー図 (CFD) の元データ (直近 {len(rows)} 日・件数)", "",
             "図は実行結果の Artifacts の `cfd.svg` にあります。", "",
             "| 日付 | " + " | ".join(c.replace("|", "\\|") for c in cols) + " | 合計 |",
             "|---|" + "---:|" * (len(cols) + 1)]
    for h in rows:
        st = h["status"]
        lines.append(f"| {h['date']} | " + " | ".join(str(st.get(c, 0)) for c in cols) + f" | {sum(st.values())} |")
    return "\n".join(lines)


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


NO_STATUS = "No Status"
CFD_COLORS = ["#2da44e", "#0969da", "#8250df", "#bf3989", "#bc4c00", "#4d2d00", "#1b7c83", "#6639ba", "#9a6700"]


def cfd_bands(history: list[dict], status_order: list[str], done_status: list[str]) -> list[str]:
    """CFD の帯の並び (下から上)。完了 → 流れの後ろの Status → … → 最初の Status → No Status → 記録にだけある Status。"""
    seen = {k for h in history for k in (h.get("status") or {})}
    done = [s for s in status_order if s in done_status] + [s for s in done_status if s not in status_order and s in seen]
    rest = [s for s in reversed(status_order) if s not in done]
    extra = sorted(s for s in seen if s not in done and s not in rest and s != NO_STATUS)
    return done + rest + extra + ([NO_STATUS] if NO_STATUS in seen else [])


def svg_cfd(path: Path, history: list[dict], status_order: list[str], done_status: list[str], title: str) -> None:
    """累積フロー図 (Status ごとの件数を日付順に積み上げた面グラフ)。"""
    rows = [h for h in history if h.get("status")]
    bands = cfd_bands(rows, status_order, done_status)
    W, H, L, R, T, B = 720, 480, 60, 170, 50, 60
    pw, ph = W - L - R, H - T - B
    ymax = max([1] + [sum(h["status"].values()) for h in rows])
    ymax = int(math.ceil(ymax * 1.1))
    n = len(rows)

    def x(i):
        return L + (pw * i / (n - 1) if n > 1 else pw / 2)

    def y(v):
        return T + ph * (1 - v / ymax)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<text x="{L}" y="30" font-size="16" font-weight="bold" fill="#24292f">{_esc(title)}</text>']
    step_y = max(1, int(math.ceil(ymax / 8)))
    for k in range(0, ymax + 1, step_y):
        out.append(f'<line x1="{L}" x2="{L + pw}" y1="{y(k):.1f}" y2="{y(k):.1f}" stroke="#00000014"/>')
        out.append(f'<text x="{L - 8}" y="{y(k) + 4:.1f}" text-anchor="end" fill="#57606a">{k}</text>')
    if not rows:
        out.append(f'<text x="{L + pw / 2}" y="{T + ph / 2}" text-anchor="middle" fill="#57606a">まだデータがありません</text>')
    else:
        base = [0.0] * n
        for bi, band in enumerate(bands):
            top = [base[i] + rows[i]["status"].get(band, 0) for i in range(n)]
            color = "#afb8c1" if band == NO_STATUS else CFD_COLORS[bi % len(CFD_COLORS)]
            if n == 1:  # 1 日分だけのときは棒で描く
                out.append(f'<rect x="{x(0) - 20:.1f}" y="{y(top[0]):.1f}" width="40" height="{y(base[0]) - y(top[0]):.1f}" '
                           f'fill="{color}" fill-opacity="0.85"><title>{_esc(band)}</title></rect>')
            else:
                upper = " ".join(f"{x(i):.1f},{y(top[i]):.1f}" for i in range(n))
                lower = " ".join(f"{x(i):.1f},{y(base[i]):.1f}" for i in reversed(range(n)))
                out.append(f'<polygon points="{upper} {lower}" fill="{color}" fill-opacity="0.85" stroke="#fff" stroke-width="0.5">'
                           f'<title>{_esc(band)}</title></polygon>')
            ly = T + 14 + 20 * (len(bands) - 1 - bi)  # 凡例は帯と同じ上下の並び
            out.append(f'<rect x="{L + pw + 16}" y="{ly - 10}" width="12" height="12" fill="{color}"/>')
            out.append(f'<text x="{L + pw + 34}" y="{ly}" fill="#24292f">{_esc(band)} ({rows[-1]["status"].get(band, 0)})</text>')
            base = top
        step = max(1, n // 8)
        for i, h in enumerate(rows):
            if i % step == 0 or i == n - 1:
                out.append(f'<text x="{x(i):.1f}" y="{T + ph + 18}" text-anchor="middle" fill="#57606a">{h["date"][5:]}</text>')
    out.append(f'<text x="18" y="{T + ph / 2}" transform="rotate(-90 18 {T + ph / 2})" text-anchor="middle" fill="#24292f">件数</text>')
    out.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="#8c959f"/>')
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
    s.add_argument("--focus-issues", default="",
                   help="注目 Issue (owner/repo#番号 または Issue の URL をカンマ区切り)。空なら前回の設定のまま")
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
