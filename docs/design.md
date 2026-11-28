# Projects Insights Kit 設計書

このキットの構造と処理の流れをまとめます。図は Mermaid で書いているので、GitHub 上でそのまま表示されます。

- 対象: キットを保守する人、社内でレビューする人
- 対象のコード: [.github/workflows/insights-kit.yml](../.github/workflows/insights-kit.yml) と [.github/insights-kit/insights_kit.py](../.github/insights-kit/insights_kit.py)
- 使い方は [README](../README.md) を見てください

## 目次

- [1. 設計の方針](#1-設計の方針)
- [2. 全体の構成](#2-全体の構成)
  - [2.1. 何をどこで見るか](#21-何をどこで見るか)
- [3. クラス図](#3-クラス図)
  - [3.1. 主な定数](#31-主な定数)
- [4. データの保存形式](#4-データの保存形式)
- [5. シーケンス図](#5-シーケンス図)
  - [5.1. セットアップ (`mode: setup`)](#51-セットアップ-mode-setup)
  - [5.2. 毎朝の更新 (`mode: daily`)](#52-毎朝の更新-mode-daily)
  - [5.3. 図を見る](#53-図を見る)
- [6. アクティビティ図](#6-アクティビティ図)
  - [6.1. ワークフローのジョブ](#61-ワークフローのジョブ)
  - [6.2. 毎朝の計算 (`run_daily`)](#62-毎朝の計算-run_daily)
  - [6.3. ゾーンの判定 (`fever_zone`)](#63-ゾーンの判定-fever_zone)
- [7. 配布と保守](#7-配布と保守)

## 1. 設計の方針

| 方針 | 実現方法 |
|---|---|
| コピーするだけで動く | 必要なのは 2 ファイルだけ。Python 標準ライブラリと、Actions のランナーに入っている `gh` CLI だけを使う |
| 外部に情報を出さない | 通信先は GitHub API だけ。アクションは GitHub 公式の `actions/checkout` と `actions/upload-artifact` だけ。日本の祝日もコード内で計算する。テスト (`tests/test_setup_html.py`) で確認している |
| リポジトリに書き込まない | ワークフローの権限は `contents: read`。設定と履歴は Project 内のドラフト Issue の本文に保存する |
| 既存の Issue を変えない | 書き込むのは Project のフィールドの値 (見積など) と、キットが作るドラフト Issue だけ |
| 入力を安全に扱う | ワークフローの入力値は環境変数で渡す (スクリプトインジェクション対策) |

## 2. 全体の構成

```mermaid
flowchart LR
    subgraph Repo["利用者のリポジトリ"]
        WF[".github/workflows/<br/>insights-kit.yml"]
        KIT[".github/insights-kit/<br/>insights_kit.py"]
        SEC[("Secret<br/>PROJECT_TOKEN")]
    end

    subgraph Actions["GitHub Actions (ランナー)"]
        JOB["ジョブ run<br/>(平日 09:00 JST / 手動)"]
        OUT[("out/<br/>fever-chart.svg<br/>cfd.svg<br/>fever-history.json")]
    end

    subgraph GitHub["GitHub (保存先)"]
        API["GitHub GraphQL API"]
        PRJ["Project<br/>(Issue・フィールド・Insights)"]
        DRAFT["ドラフト Issue<br/>📈 Fever: 進捗率 / バッファ消費率<br/>(設定 JSON・履歴)"]
        ART["Artifacts<br/>(SVG・zip、30 日)"]
        SUM["実行結果の Summary"]
    end

    USER(("利用者"))

    WF --> JOB
    KIT --> JOB
    SEC --> JOB
    JOB -- "gh api graphql" --> API
    API --> PRJ
    API --> DRAFT
    JOB --> OUT
    OUT -- "upload-artifact" --> ART
    JOB --> SUM
    USER -- "Insights を見る" --> PRJ
    USER -- "📈 図を開く" --> SUM
    SUM -. リンク .-> ART
```

### 2.1. 何をどこで見るか

| 図・情報 | 作る場所 | 見る場所 |
|---|---|---|
| バーンアップ・バーンダウン | Project の Insights (利用者がチャートを保存) | Project の Insights |
| フィーバーの推移 (日付が横軸) | トラッカーのドラフトに書いた `Fever Value` / `Fever Zone` を Insights で集計 | Project の Insights |
| フィーバーチャート (進捗率が横軸) | `svg_fever_chart` → Artifacts | Summary の「📈 図を開く」 |
| 累積フロー図 (CFD) | `svg_cfd` → Artifacts | Summary の「📈 図を開く」 |
| 簡易版の折れ線 | `fever_mermaid` / `cfd_mermaid` (Mermaid) | Summary の下の方 |
| 数字の表 | `tracker_body` / `breakdown_md` / `cfd_md` | ドラフト「📈 Fever: 進捗率」の本文と Summary |

## 3. クラス図

`insights_kit.py` のクラスは `Project` と `Calendar` の 2 つだけです。そのほかの処理は関数で、データは辞書 (dict) で受け渡します。
下の図では、関数をまとめて «module» として、主な辞書の形を «dict» として示します。

```mermaid
classDiagram
    direction TB

    class Project {
        +owner: str
        +number: int
        +id: str
        +resolve(url)$ dict
        +fields() dict
        +items() list
        +create_number(name)
        +create_select(name, options)
        +create_iteration(name, start, weeks, count) bool
        +set_number(item_id, field, value)
        +set_option(item_id, field, option)
        +create_draft(title, body) dict
        +edit_draft(draft_id, body)
    }

    class Calendar {
        +holidays: set
        +japan: bool
        +is_bday(d) bool
        +count(start, end) int
        +elapsed(start, today) int
        +add(start, bdays) date
    }

    class Commands {
        <<module>>
        +cmd_check_token(a)
        +cmd_setup(a)
        +cmd_daily(a)
        +run_daily(proj, cfg, history, today, out)
    }

    class Trackers {
        <<module>>
        +find_trackers(items, metric_field) dict
        +tracker_metric(item, metric_field) str
        +ensure_trackers(proj, names, fields, trackers)
        +parse_state(body, strict) tuple
        +state_body(visible, cfg, history) str
    }

    class Metrics {
        <<module>>
        +pert(o, m, p) float
        +compute_buffer(cfg, plan_days, total, var_sum) tuple
        +fever_zone(p, b, zones) str
        +status_counts(work, status_field, done_status) dict
        +japan_holidays(year) set
    }

    class Reports {
        <<module>>
        +tracker_body(...) str
        +breakdown_md(cfg, work, items, est, is_done) str
        +cfd_md(history, order, done) str
        +insights_recipe(cfg, statuses, fields) str
        +chart_link(url, row, fields, status_field) str
        +fever_mermaid(history, zones) str
        +cfd_mermaid(history, order, done) str
        +svg_fever_chart(path, history, zones, title)
        +svg_cfd(path, history, order, done, title)
        +summary(md)
    }

    class GitHubCLI {
        <<module>>
        +gh(args) str
        +gql(query, variables) dict
    }

    class Config {
        <<dict>>
        project: dict
        schedule: dict
        cadence: str
        fields: dict
        focus_issues: list
        done_status: list
        fever_zones: dict
        buffer_auto: dict
    }

    class HistoryRow {
        <<dict>>
        date: str
        elapsed_days: int
        done: float
        total: float
        unit: str
        progress: float
        buffer: float
        buffer_days: float
        zone: str
        status: dict
    }

    class Item {
        <<dict>>
        id: str
        type: ISSUE / DRAFT_ISSUE / PULL_REQUEST
        content: dict
        values: dict
    }

    Commands --> Project : 使う
    Commands --> Calendar : 使う
    Commands --> Trackers
    Commands --> Metrics
    Commands --> Reports
    Project --> GitHubCLI : GraphQL
    Trackers --> Project : ドラフトを作る
    Calendar --> Metrics : japan_holidays
    Project ..> Item : items() が返す
    Trackers ..> Config : 本文から読む・書く
    Trackers ..> HistoryRow : 本文から読む・書く
    Commands ..> HistoryRow : 毎日 1 行追加
    Reports ..> HistoryRow : 図と表にする
```

### 3.1. 主な定数

| 定数 | 意味 |
|---|---|
| `DEFAULT_FIELDS` | キットが使う Project のフィールド名 (Status、楽観・最可能・悲観、見積、Fever Value / Metric / Zone、Phase、Sprint) |
| `METRICS` | トラッカーの指標。`Progress %` (進捗率) と `Buffer %` (バッファ消費率) |
| `ZONES` | フィーバーのゾーン。Green / Yellow / Red |
| `STATE_METRIC` | 設定と履歴を保存するトラッカー (`Progress %`) |
| `MAX_HISTORY` / `BODY_LIMIT` | 履歴の保存件数 (260 営業日) と、ドラフト本文の文字数の上限 (60,000) |

## 4. データの保存形式

設定と履歴は、ドラフト「📈 Fever: 進捗率 (Progress %)」の本文に、人が読む部分と一緒に保存します。

```mermaid
flowchart TB
    subgraph Body["ドラフト「📈 Fever: 進捗率」の本文"]
        V["人が読む部分<br/>今日の値・推移の表・担当者別・注目 Issue 別・CFD の表"]
        C["⚙️ 設定<br/>JSON のコードブロック: schedule, cadence, fields など<br/>(利用者が編集してよい)"]
        H["履歴<br/>HTML コメント insights-kit:history の中に JSON の配列<br/>(画面には出ない)"]
    end
    V --- C --- H
```

- 読み込み: `parse_state` が正規表現 `CONFIG_RE` / `HISTORY_RE` で取り出す
- 書き込み: `state_body` が組み立てる。本文が `BODY_LIMIT` を超える場合は、履歴を古い方から削る
- もう 1 つのトラッカー「📈 Fever: バッファ消費率 (Buffer %)」の本文は、人が読む部分だけ

各トラッカーには、Project のフィールドにも値を書きます。Insights はこの値を集計します。

| フィールド | 進捗率のトラッカー | バッファ消費率のトラッカー |
|---|---|---|
| `Fever Metric` | `Progress %` | `Buffer %` |
| `Fever Value` | 進捗率 (%) | バッファ消費率 (%) |
| `Fever Zone` | 今日のゾーン | 今日のゾーン |

## 5. シーケンス図

### 5.1. セットアップ (`mode: setup`)

```mermaid
sequenceDiagram
    autonumber
    actor U as 利用者
    participant A as GitHub Actions
    participant K as insights_kit.py
    participant G as GitHub API
    participant P as Project

    U->>A: Run workflow (mode: setup, 計画完了日など)
    A->>K: check-token
    K->>G: GET /user
    G-->>K: OK (classic なら scopes も確認)
    A->>K: setup --project-url ...
    K->>G: Project.resolve (URL から Project を取得)
    K->>G: fields()
    loop 足りないフィールドごと
        K->>G: createProjectV2Field (数値・単一選択・Iteration)
        G->>P: フィールド追加
    end
    K->>G: items() で既存のトラッカーを探す
    Note over K: 既存の設定 JSON と履歴があれば引き継ぐ
    K->>G: fields() を読み直す (チャートのリンクに ID が要る)
    K->>A: Summary に「Insights のチャート設定」とリンクの表
    K->>K: run_daily (5.2. と同じ)
    A->>A: SVG を Artifacts に置き、Summary の先頭に「📈 図を開く」
```

### 5.2. 毎朝の更新 (`mode: daily`)

```mermaid
sequenceDiagram
    autonumber
    participant S as スケジュール (平日 09:00 JST)
    participant A as GitHub Actions
    participant K as insights_kit.py
    participant G as GitHub API
    participant D as トラッカー (ドラフト)
    participant F as Artifacts

    S->>A: ワークフロー起動
    A->>A: Check (PROJECT_URL と Secret の確認)
    A->>K: check-token
    A->>K: daily
    K->>G: items()
    G-->>K: Issue・ドラフト・フィールドの値
    K->>K: find_trackers → parse_state (設定と履歴)
    K->>G: fields()
    loop Issue ごと
        K->>K: pert(楽観, 最可能, 悲観)
        opt 見積が変わった
            K->>G: set_number(見積)
        end
    end
    K->>K: 進捗率・バッファ消費率・ゾーン・Status 別件数を計算し、履歴に追加
    K->>K: svg_fever_chart / svg_cfd → out/
    opt トラッカーが足りない
        K->>G: create_draft + set_option(Fever Metric)
    end
    loop トラッカーごと
        K->>G: set_number(Fever Value) / set_option(Fever Zone)
        K->>D: edit_draft(本文: 表・設定・履歴)
    end
    K->>A: Summary (表・簡易版の図) を一時ファイルに書く
    A->>F: upload-artifact (zip と、zip にしない SVG 2 枚)
    F-->>A: artifact-url
    A->>A: Summary = 「📈 図を開く」のリンク + キットの Summary
```

### 5.3. 図を見る

```mermaid
sequenceDiagram
    actor U as 利用者
    participant P as Project の Insights
    participant S as 実行結果の Summary
    participant F as Artifacts

    U->>P: Insights を開く
    P-->>U: バーンアップ・バーンダウン・フィーバーの推移
    U->>S: Actions → Insights Kit → 最新の実行
    S-->>U: 「📈 図を開く」・表・簡易版の図
    U->>F: 「フィーバーチャート」「累積フロー図 (CFD)」をクリック
    F-->>U: SVG をブラウザに表示 (image/svg+xml・inline)
```

## 6. アクティビティ図

### 6.1. ワークフローのジョブ

```mermaid
flowchart TD
    start([開始: スケジュール or 手動]) --> chk{PROJECT_URL が<br/>YOUR-ORG のまま?}
    chk -- はい --> skip([通知を出して終了])
    chk -- いいえ --> tok{Secret<br/>PROJECT_TOKEN がある?}
    tok -- いいえ --> err([エラーで終了])
    tok -- はい --> co[checkout<br/>.github/insights-kit だけ・認証情報を残さない]
    co --> ct[check-token]
    ct --> mode{mode}
    mode -- setup --> end1{planned_end_date<br/>がある?}
    end1 -- いいえ --> err
    end1 -- はい --> setup[insights_kit.py setup]
    mode -- daily --> daily[insights_kit.py daily]
    setup --> up[upload-artifact<br/>zip: fever-chart]
    daily --> up
    up --> svg[upload-artifact<br/>fever-chart.svg / cfd.svg を zip にせず]
    svg --> sum[Summary:<br/>「📈 図を開く」+ キットの Summary]
    sum --> done([終了])
```

### 6.2. 毎朝の計算 (`run_daily`)

```mermaid
flowchart TD
    s([開始]) --> load[items と fields を読む]
    load --> filter[集計対象を選ぶ<br/>トラッカー・PR・中身が見えない Issue は除く]
    filter --> pert[PERT で見積を計算<br/>変わったものだけ Project に書く]
    pert --> hasEst{見積が入った<br/>項目がある?}
    hasEst -- はい --> byEst["完了・全体を見積 (人日) で合計"]
    hasEst -- いいえ --> byCount[完了・全体を件数で数える]
    byEst --> buf
    byCount --> buf
    buf{バッファの指定} -- auto --> pertBuf{楽観・悲観が<br/>入っている?}
    pertBuf -- はい --> k["計画営業日 × k × √Σσ² / 全体見積"]
    pertBuf -- いいえ --> half[計画営業日 × 50%]
    buf -- 数値 --> manual[指定の営業日]
    k --> started
    half --> started
    manual --> started
    started{今日が<br/>開始日以降?}
    started -- いいえ --> svgs
    started -- はい --> calc["進捗率 p = 完了 / 全体<br/>バッファ消費率 b = max(0, 経過営業日 − p × 計画営業日) / バッファ<br/>ゾーン = fever_zone(p, b)<br/>Status 別件数"]
    calc --> hist[履歴に今日の行を追加<br/>同じ日付は置き換え・最大 260 件]
    hist --> svgs[SVG と履歴 JSON を out/ に書く]
    svgs --> trk{トラッカーが<br/>2 つある?}
    trk -- いいえ --> mk[足りないトラッカーを作る]
    trk -- はい --> upd
    mk --> upd[トラッカーの Fever Value・Zone と本文を更新<br/>変わった値だけ書く]
    upd --> row{今日の行がある?}
    row -- いいえ --> sum0[Summary: 見積の更新のみ]
    row -- はい --> sum1[Summary: 今日の値・簡易版の図・表]
    sum0 --> e([終了])
    sum1 --> e
```

### 6.3. ゾーンの判定 (`fever_zone`)

ゾーンの境界線は、進捗率 p に応じて直線で動きます (既定: 黄 10%→70%、赤 30%→90%)。

```mermaid
flowchart LR
    in([p: 進捗率, b: バッファ消費率]) --> r{"b ≥ 0.30 + (0.90 − 0.30) × p ?"}
    r -- はい --> red([Red 🔴 要対策])
    r -- いいえ --> y{"b ≥ 0.10 + (0.70 − 0.10) × p ?"}
    y -- はい --> yellow([Yellow 🟡 注意・対策準備])
    y -- いいえ --> green([Green 🟢 順調])
```

## 7. 配布と保守

```mermaid
flowchart LR
    src["insights-kit.yml<br/>insights_kit.py"] -- "tools/sync_setup_html.py" --> html["setup.html<br/>(2 ファイルを埋め込み、コピーボタンで配布)"]
    src --> test["tests/<br/>test_kit.py・test_setup_html.py"]
    html --> test
    test --> sandbox["試験用の Project で<br/>setup・daily を実行して確認"]
```

| テスト | 確認すること |
|---|---|
| `test_setup_html.py` | setup.html の埋め込みが元のファイルと一致する。GitHub 公式以外のアクションや別リポジトリを使っていない。通信のライブラリを使わず、URL が GitHub だけ。日本の祝日の計算 |
| `test_kit.py` | CFD の集計と帯の順番、SVG が壊れていない、本文の文字数の上限、Insights のチャート設定表とリンク、Mermaid の図の値 |
