# Projects Insights Kit

**すでに Issue が入っている GitHub Project** に、次の4つを後付けします。
**既存のリポジトリに `.github/` 配下の 2 ファイルをコピーするだけ**で動き、操作はすべてブラウザで完結します。
リポジトリには何も書き込まず、既存 Issue のタイトル・本文・状態も変更しません。

> **外部に依存しません。** 実行されるのはコピーした 2 ファイルと GitHub 公式のアクション (`actions/checkout`, `actions/upload-artifact`) だけで、
> 作者のリポジトリや外部サービスのコードを取りに行くことはありません。通信先は GitHub API のみで、Project の情報が GitHub 以外に送られることはありません
> (日本の祝日も外部 API を使わず `insights_kit.py` の中で計算します)。コピーした時点のコードが社内のリポジトリで動くので、社内でレビューしてから使えます。

## コピーするファイル

| ファイル | 役割 |
|---|---|
| [.github/workflows/insights-kit.yml](.github/workflows/insights-kit.yml) | いつ・何を実行するか (`PROJECT_URL` を書き換える) |
| [.github/insights-kit/insights_kit.py](.github/insights-kit/insights_kit.py) | 処理の本体 (Python 標準ライブラリ + gh CLI のみ) |

`setup.html`・`tools/`・`tests/` はこのキットの配布・保守用で、コピー先には不要です。

| 後付けするもの | 中身 |
|---|---|
| 三点見積 (PERT) | 楽観・最可能・悲観 (人日) を入れると **見積 (人日) = (楽観 + 4×最可能 + 悲観) / 6** を毎朝自動計算 |
| 進め方 | 工程 × 2週間スプリント / 2週間スプリント / 1週間スプリント / 工程 / なし から選択 |
| フィーバーチャート | 進捗率とバッファ消費率を毎朝計算し、Insights で表示 |
| 累積フロー図 (CFD) | Status ごとの件数の推移を Insights で表示し、仕掛かりの滞留やスコープの増加を見る |

## セットアップ (所要 10 分)

> **かんたんセットアップ**: [setup.html](setup.html) を開き (ファイルの画面右上の **Download raw file** で保存 → ダブルクリック)、
> Project とリポジトリの URL を貼り付けると、下の手順 1 の 2 ファイルを (URL を書き換え済みで) コピーしてそのまま GitHub の作成画面に貼り付けられます。手順 2 のトークン作成・Secret 登録の画面へのリンクも出ます。
> サーバーや GitHub Pages は不要で、入力した値はどこにも送信しません。

### 1. 2 つのファイルを置く

**既存のリポジトリに入れる場合** (Project の Issue があるリポジトリなど、どこでも可)

1. リポジトリの **Add file → Create new file** で、ファイル名 `.github/insights-kit/insights_kit.py` に
   [insights_kit.py](.github/insights-kit/insights_kit.py) の中身をすべて貼り付けて **Commit changes**
2. 同じように `.github/workflows/insights-kit.yml` に [insights-kit.yml](.github/workflows/insights-kit.yml) の中身を貼り付け、
   `PROJECT_URL:` の行を対象 Project の URL に書き換えて (例: `https://github.com/orgs/acme/projects/5`) **Commit changes**
3. ブランチ保護がある場合は PR を作ってマージ

git で入れる場合は、このリポジトリの `.github/workflows/insights-kit.yml` と `.github/insights-kit/` をそのままコピーします。

**新しくリポジトリを作る場合**: このリポジトリの中身をコピーして社内に新しいリポジトリを作り、上の 2 の `PROJECT_URL` の書き換えだけ行います。

### 2. トークンを作って Secret に登録する

Projects は Actions 標準のトークンでは操作できないため、個人アクセストークンを1つ用意します。

**Organization の Project の場合 (fine-grained トークン)**

> 会社では、**専用アカウント (bot ユーザー) に対象 Project だけの Write 権限を付け、そのアカウントでトークンを作る**のがおすすめです。
> 管理者に権限の理由を聞かれたら、下の「[トークンの権限について (管理者向けの説明)](#トークンの権限について-管理者向けの説明)」を見せてください。

1. https://github.com/settings/personal-access-tokens/new を開く
2. **Resource owner** を会社の Organization にする。**Expiration** は社内ルールに合わせて短めに (例: 90 日)
3. **Repository access**: **Only select repositories** で、Project に入っている Issue のリポジトリだけを選ぶ
4. **Permissions** (これ以外は付けない)
   - Organization permissions → **Projects: Read and write**
   - Repository permissions → **Issues: Read-only**、**Pull requests: Read-only**
5. **Generate token**。Organization の設定によっては管理者の承認が必要です

**個人アカウントの Project の場合 (classic トークン)**: https://github.com/settings/tokens/new で `project` と `repo` にチェック。

手順 1 のリポジトリの **Settings → Secrets and variables → Actions → New repository secret** に
**Name: `PROJECT_TOKEN`** で登録します。

### 3. Setup を実行する

**Actions** タブ → 左の **Insights Kit** → **Run workflow** で、**mode を `setup`** にして次を入力します。

| 入力 | 例 | 説明 |
|---|---|---|
| 計画完了日 | `2027-03-31` | **バッファを含まない**完了予定日 (setup では必須) |
| 開始日 | `2027-01-06` | 空なら今日 |
| プロジェクトバッファ | `auto` | `auto` は三点見積から自動計算。`10` のように営業日数でも指定可 |
| 進め方 | `phase+sprint-2w` | 下の「進め方の選び方」参照 |
| 既存の見積フィールド名 | `見積日数` | 人日の数値フィールドが既にあれば名前を入れる。空なら「見積 (人日)」を新規作成 |
| 日本の祝日を除外 | ✅ | 営業日の計算から祝日を除く |

終わると、実行結果の画面 (Summary) に **この Project 用の Insights 設定表** が出ます。
期間や進め方を変えたいときは、同じように mode: setup で再実行します (既存のフィールドや履歴はそのまま)。

### 4. Project で見積を入れる

Project のテーブルに列が増えています。各 Issue に **楽観 / 最可能 / 悲観 (人日)** を入れてください。
翌朝 (すぐ反映したいときは mode: **daily** で Run workflow) に **見積 (人日)** が自動で入ります。

- 最可能だけ入れた場合は、見積 = 最可能
- 三点見積を使わず見積を直接入れても構いません (三点が空の Issue は上書きしません)

### 5. Insights でチャートを作る

Project 右上の **Insights → New chart** で作り、**Filter** はグラフ上の入力欄、それ以外は **Configure** で設定して **Save** します。
(見積フィールド名を変えた場合は、Setup の Summary に出る表を使ってください。)

| チャート | Filter | Layout | X-axis | Group by | Y-axis |
|---|---|---|---|---|---|
| バーンダウン (全体) | `no:fever-metric -status:Done` | Line | Time | None | Sum of 見積 (人日) |
| バーンダウン (今のスプリント) | `no:fever-metric -status:Done sprint:@current` | Line | Time | None | Sum of 見積 (人日) |
| バーンアップ | `no:fever-metric` | Stacked area | Time | Status | Sum of 見積 (人日) |
| 累積フロー図 (CFD) | `no:fever-metric is:issue` | Stacked area | Time | Status | Count of items |
| フィーバー (推移) | `has:fever-metric` | Line | Time | Fever Metric | Sum of Fever Value |
| フィーバー (現在のゾーン) | `has:fever-metric` | Bar | Fever Zone | None | Count of items |
| 工程別の残作業 | `no:fever-metric -status:Done` | Stacked column | Phase | Status | Sum of 見積 (人日) |

- `no:fever-metric` は、自動で作るドラフト Issue「📈 Fever: …」2 件を集計から外すためのものです
- Insights の推移グラフは **実際の変更時刻** で記録されるため、線は導入した日から伸びていきます
- Time を選ぶと Group by が選べない場合は、Filter に `status:Done` を足したチャートを別に作って並べてください

#### 累積フロー図 (CFD) を役立てるには

CFD は、Status ごとの件数を積み上げた推移のグラフです。バーンアップと同じ形で、Y 軸を見積ではなく **件数** (Count of items) にしたものです。
`is:issue` で Issue だけを数えます (PR やドラフトは数えません)。見積の量で見たい場合はバーンアップを見てください。

- **Status に作業中の段階を作る**: Status が Todo と Done だけだと、作業中の滞留が見えません。
  Project の **Settings → Status** で「In Progress」「In Review」などを追加し、流れの順 (Todo → In Progress → In Review → Done) に並べてください。
  Setup の Summary に今の Status の並びが出て、段階が足りないと警告も出ます。
- **見方**
  - 帯の **縦の幅** = その Status にいる件数 (仕掛かり量)。作業中の帯が太り続けていたら、どこかで詰まっています
  - 帯の **横の幅** ≒ その Status を通り抜けるのにかかる時間 (リードタイム)
  - Done の帯の **増え方** = 完了のペース。全体の高さ (全件数) が増え続けていたら、スコープが増えています
- 進め方でスプリントや工程を使っている場合は、Filter に `sprint:@current` や `phase:実装` を足すと、その範囲だけの CFD になります。

以上で完了です。以後は平日 09:00 (日本時間) に自動更新されます。

## どこに何が保存されるか

| もの | 場所 |
|---|---|
| 設定 (期間・進め方・完了とみなす Status など) | Project 内のドラフト「📈 Fever: 進捗率」本文の「⚙️ 設定」JSON |
| 日々の推移 (表) | 同じドラフト本文 (直近 30 日分を表示、400 日分を保存) |
| フィーバーチャートの図 (散布図 SVG) | 各実行結果の **Artifacts** (`fever-chart`、30 日保存) |

リポジトリにはコミットしないので、ブランチ保護があっても動きます。

## 進め方の選び方

| 選択肢 | 追加されるもの | 向いているケース |
|---|---|---|
| **`phase+sprint-2w`** (おすすめ) | Phase (要件定義〜リリース) + Sprint (2週間) | 会社の工程管理はウォーターフォールのまま、実装・テストを2週間単位で回したい |
| `sprint-2w` | Sprint (2週間) | アジャイルで進める。迷ったらこれ |
| `sprint-1w` | Sprint (1週間) | 短期・小規模、変化が激しい |
| `phase` | Phase のみ | 工程ごとの進捗だけ見たい |
| `none` | なし | 期間の区切りは使わず、全体のバーンダウンとフィーバーだけ |

スプリントは「2週間ごとに *終わらせる量* を決めて、終わったかを振り返る」ための箱です。
ウォーターフォールでも、工程 (Phase) の中をスプリントで区切ると遅れに早く気づけます。

## フィーバーチャートの計算

```
進捗率        p = 完了した見積 / 全体の見積          (見積が1件も無ければ件数で計算)
バッファ消費率 b = max(0, 経過営業日 − p × 計画営業日) / バッファ営業日
ゾーン        b が 黄線 (10%→70%) 以上で 🟡、赤線 (30%→90%) 以上で 🔴   ※進捗 0%→100% で直線補間
```

**バッファの自動計算 (`auto`)**: 各 Issue の標準偏差 σ = (悲観 − 楽観) / 6 から、
`バッファ = 計画営業日 × 2 × √Σσ² / 全体見積` (約 95% の確度) を計算します。
楽観・悲観が1件も入っていない間は、計画営業日の 50% を使います。見積が入るにつれて毎朝見直されます。

「完了」は Status が `Done`、または Issue が Close されたものです。PR は集計しません。

## 設定の調整

Project のドラフト「📈 Fever: 進捗率」を開き、本文の「⚙️ 設定」の JSON を編集すると、次回の実行から反映されます。

| キー | 内容 |
|---|---|
| `done_status` | 完了とみなす Status 名 (例: `["Done", "Released"]`) |
| `schedule.extra_holidays` | 会社独自の休日 (例: `["2026-12-29", "2026-12-30"]`) |
| `fever_zones` | ゾーンの境界線 |
| `buffer_auto.k` | 自動バッファの係数 (2.0 ≒ 95%、1.0 ≒ 68%) |
| `fields` | フィールド名 (Project 側で名前を変えたらここも合わせる。`Fever Metric` は変更しないでください) |

## 会社で使うときの注意 (情報の扱い)

- **実行されるコードはコピー先のリポジトリにあるものだけ**です。このキットの元リポジトリが後から変更されても、コピー済みのものには影響しません。
  更新を取り込むときは、差分をレビューしてから 2 ファイルを置き換えてください。
- **通信先は GitHub だけ**です。`insights_kit.py` は `gh` CLI で GitHub API を呼ぶ以外の通信をしません (テストで確認しています)。
  祝日はファイル内で計算し、計測・テレメトリの類もありません。
- 使う外部アクションは GitHub 公式の `actions/checkout` と `actions/upload-artifact` だけです。社内ルールでさらに固定したい場合は、`@v7` をコミット SHA に置き換えてください。
- ワークフローの権限は `contents: read` のみ、checkout は `persist-credentials: false` です。トークン (Secret `PROJECT_TOKEN`) はログで伏せられ、`gh` にだけ渡されます。
- 結果の保存先は、対象 Project 内のドラフト Issue と、そのリポジトリの Actions の Artifacts (30 日) だけです。

## トークンの権限について (管理者向けの説明)

### Organization への書き込みではなく、Projects 権限だけです

fine-grained トークンで求めるのは **Organization permissions → Projects: Read and write の 1 項目だけ**です。
Organization の Project (`github.com/orgs/<組織>/projects/N`) はリポジトリではなく Organization が持ち主なので、
GitHub の画面上は「Organization permissions」の欄に入っていますが、メンバー管理・Organization の設定・リポジトリの作成や削除などの権限は一切使いません。
リポジトリ側も **Issues / Pull requests の読み取りのみ**です (コードやリポジトリの内容には書き込みません)。

### なぜ読み取りではなく書き込みが必要か

キットが Project に対して行う書き込みは、次の GraphQL 操作だけです (`insights_kit.py` で確認できます)。

| 操作 | いつ | 内容 |
|---|---|---|
| `createProjectV2Field` | setup のときだけ | 楽観・最可能・悲観・見積・Phase・Fever などのフィールドを追加 |
| `addProjectV2DraftIssue` | setup のときだけ | 設定と履歴を保存するドラフト Issue「📈 Fever: …」を 2 件追加 |
| `updateProjectV2DraftIssue` | 毎朝 | 上のドラフト Issue の本文 (設定と履歴) を更新 |
| `updateProjectV2ItemFieldValue` | 毎朝 | 各 Issue の「見積 (人日)」と、フィーバーの値を書き込む |

既存 Issue のタイトル・本文・状態の変更、フィールドやアイテム・Project の削除は行いません。

### 書き込める範囲を対象の Project 1 つに絞る方法

fine-grained トークンの Projects 権限は、Project 単位では絞れません。トークンを作ったアカウントが書き込める **Organization の Project すべて**が対象になります。
ただしトークンは、**作ったアカウント自身の権限を超えることはできません**。そこで次のように運用すると、実際に書き込めるのは対象の Project だけになります。

1. キット専用のアカウント (bot ユーザー) を用意し、Organization のメンバーにする (Base permissions は最小に)
2. 対象 Project の **Settings → Manage access** で、そのアカウントに **Write** を付ける (ほかの Project には付けない)
3. 対象の Issue があるリポジトリには **Read** だけを付ける
4. そのアカウントで、上の手順 2 のとおりトークンを作る (Only select repositories・短い有効期限・Projects / Issues / Pull requests 以外の権限なし)

### そのほか確認しておくとよい点

- トークンはリポジトリの Secret (`PROJECT_TOKEN`) にだけ保存され、ログでは伏せられます。ワークフローの `GITHUB_TOKEN` は `contents: read` のみです。
- 通信先は GitHub API だけです。Project の情報が GitHub 以外に送られることはありません (テストで確認しています)。
- 実行されるのはコピー先のリポジトリにある 2 ファイルと、GitHub 公式の `actions/checkout` / `actions/upload-artifact` だけです。
- やめるときは、トークンを削除 (または期限切れに) し、ワークフローのファイルを消せば止まります。追加したフィールドとドラフト Issue は Project 上で手動で削除できます。

## よくある質問

- **既存の Issue や Project が壊れませんか?** — 追加するのはフィールドとドラフト Issue 2 件だけです。書き換えるのは「見積 (人日)」(三点見積が入っている Issue のみ) と、フィーバー用のフィールドだけです。
- **`PROJECT_URL の YOUR-ORG を書き換えてください` と出る** — `insights-kit.yml` の `PROJECT_URL:` を書き換えてください。
- **`Resource not accessible` で失敗する** — トークンの Projects 権限、Resource owner、Organization の承認を確認してください。
- **手元で動かしたい** — `.env.example` を `.env` にコピーして `PROJECT_TOKEN` を書き、そのディレクトリで
  `python .github/insights-kit/insights_kit.py daily --project-url <URL>` を実行します (gh CLI が必要)。

## このキットを保守する人向け

`insights-kit.yml` か `insights_kit.py` を変更したら、`python tools/sync_setup_html.py` で setup.html の埋め込みを更新し、
`python -m unittest discover -s tests` で確認します (外部依存が入っていないこともテストします)。
