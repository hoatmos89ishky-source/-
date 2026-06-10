# Automated Daily Stock Recommender

日本時間の平日朝（東京市場の寄り付き前）に、為替・米国市場・日経主要構成銘柄のテクニカル指標と、あなたの**保有株・保有投信**の動向を自動分析し、Claude が **Web 検索で最新の決算・ニュース・世情まで踏まえて**生成した「相場見通し＋おすすめ10銘柄＋保有銘柄の動向と売却判断」レポートを **Gmail** で配信する Bot です。GitHub Actions cron で動くのでローカル常駐不要。

## アーキテクチャ

```
[GitHub Actions cron 07:30 JST]
  └─ python main.py
       ├─ yfinance         : USD/JPY, NYダウ, NASDAQ, SOX, NVDA, 日経平均,
       │                     日経主要30銘柄ユニバース, 保有株の値動き・ニュース・決算予定
       ├─ pandas/numpy     : 25日移動平均線・乖離率, 26週移動平均線, RSI(14)
       ├─ HOLDINGS_JSON    : 保有株(コード/数量/平均取得単価)＋保有投信(ファンド名)
       ├─ Claude Sonnet 4.6: web_search ツールで最新ニュース/決算/世情を参照しレポート生成
       └─ Gmail (SMTP)     : smtp.gmail.com:587 (STARTTLS) で配信
```

## レポート内容

メール本文は以下7セクションで届きます（保有情報未設定の場合は4〜6は省略）:

1. **【相場見通し】**
2. **【為替・米国市場】**
3. **【今日のおすすめ10銘柄】** — 日経225主要構成銘柄から、為替バイアスとテクニカルを踏まえ Claude が10銘柄選定＋簡単な理由
4. **【保有銘柄の動向】** — 銘柄ごとに 1日 / 5日 / 30日 変化率、含み損益、RSI、25MA 乖離率
5. **【保有銘柄の売却判断】** — `HOLD / WATCH / TRIM / SELL` の4段階＋決算予定・最新ニュース・テクニカル・マクロ環境を踏まえた根拠
6. **【保有投信のコメント】** — Claude が Web 検索でファンド名を調べ、直近の方向感・注目材料をコメント
7. **免責事項**

## 分析ロジック

- **為替バイアス**: USD/JPY の直近5営業日終値の変化率が `+0.5%` 以上 → 円安 / `-0.5%` 以下 → 円高 / その間 → 中立
- **テクニカル**: 25日移動平均線・乖離率, 26週移動平均線, RSI(14, Wilder)
- **おすすめ10銘柄**: 日経225 構成の流動性上位30銘柄をテクニカルスクリーニング → 為替バイアスと併せて Claude が10銘柄ピック
- **売却判断**: 保有株の含み損益・テクニカル＋ Claude が web 検索で取りに行く決算情報・最新 IR ニュース・マクロ環境から鋭く判定

## ディレクトリ構成

```
.
├── main.py                                   # メインロジック
├── requirements.txt                          # 依存ライブラリ
├── tools/
│   └── import_rakuten.py                     # 楽天証券 CSV → HOLDINGS_JSON 変換
├── .github/workflows/daily_stock_report.yml  # cron 定義
└── README.md
```

## セットアップ

### 1. リポジトリを取得

```bash
git clone <this repo>
cd <this repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 必要な環境変数 / Secrets

| 名前 | 種別 | 必須 | 説明 |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Secret | ✓ | Anthropic Console で発行 |
| `GMAIL_SENDER` | Secret | ✓ | 送信元 Gmail（アプリパスワード発行アカウント） |
| `GMAIL_APP_PASSWORD` | Secret | ✓ | Google アカウントのアプリパスワード16文字 |
| `HOLDINGS_JSON` | Secret |   | 保有情報 JSON（未設定でも動く。下記参照） |
| `GMAIL_TO` | Variable |   | 受信先。既定 `ho.atmos.89.ishky@gmail.com` |
| `ANTHROPIC_MODEL` | Variable |   | 既定 `claude-sonnet-4-6`（Web Search 対応モデルが必要） |

### 3. Anthropic API キーの取得

1. <https://console.anthropic.com> にサインイン
2. **Settings → API Keys → Create Key**
3. キーを `ANTHROPIC_API_KEY` に登録

> **コスト目安**: Claude Sonnet 4.6 + Web 検索 で1配信あたり $0.15〜$0.30（数十円）、月間およそ **$5〜$10**。Web 検索ツールは 1検索 = $0.01・`max_uses=8` 設定。残高は <https://console.anthropic.com> の **Plans & Billing** から監視し、必要に応じて **Auto-recharge** を有効化しておくと安心です。残高切れになった場合は専用のエラーメールが届きます。

### 4. Gmail アプリパスワードの取得手順

Gmail SMTP は通常パスワードでなく**アプリパスワード**を使います（2段階認証が必要）。

1. <https://myaccount.google.com/security> で **2段階認証プロセスを有効化**
2. <https://myaccount.google.com/apppasswords> でアプリ名（例: `daily-stock-recommender`）を入力して作成
3. 表示される **16文字のパスワード**（スペース無しで貼り付け）を `GMAIL_APP_PASSWORD` に登録
4. `GMAIL_SENDER` に送信元の Gmail アドレスを登録

### 5. 保有情報 `HOLDINGS_JSON` の登録（任意）

保有株・保有投信を Bot に教えるための JSON 文字列を `HOLDINGS_JSON` Secret に格納します。**未設定でも動作**し、その場合は保有関連セクションが省略されます。

#### 5-A. 楽天証券 CSV から自動生成（推奨）

楽天証券は公開 API を提供していないため完全自動連携はできませんが、CSV エクスポートを変換するツール `tools/import_rakuten.py` を同梱しています。

1. 楽天証券Web > マイメニュー > **保有商品一覧** > **CSV ダウンロード**（株式・投信それぞれ）
2. ローカルで変換:
   ```bash
   python tools/import_rakuten.py stocks.csv funds.csv > holdings.json
   ```
   - 一般 / 特定 / NISA に分かれた同一銘柄は数量加重平均で自動集約されます
   - CP932 / Shift-JIS / UTF-8 を自動判別
3. 出力された JSON を `HOLDINGS_JSON` Secret に貼り付け（`holdings.json` 自体はリポジトリにコミットしないでください）
4. 保有銘柄が変わったときだけ再実行（数ヶ月に1回でOK）

詳しくは `tools/import_rakuten.py` の docstring 参照。

#### 5-B. 手書きする場合の JSON フォーマット

> ⚠️ **以下の値はサンプルです。必ずご自身の実際の保有銘柄に書き換えてください**（コピー＆ペーストするとこの架空の保有内容で Bot が動作します）。

```json
{
  "stocks": [
    {"symbol": "XXXX.T", "name": "（あなたの保有銘柄名）", "shares": 100, "avg_cost": 1500}
  ],
  "funds": [
    {"name": "（あなたの保有ファンド名）"}
  ]
}
```

何も保有していない・保有関連セクションを省略したい場合は次のようにします:

```json
{"stocks": [], "funds": []}
```

- `symbol` は yfinance 形式（東証銘柄は `XXXX.T`）
- `shares` / `avg_cost` は省略可（省略時は含み損益計算をスキップ）
- 投資信託は **ファンド名だけ**保持し、価格データは Claude が Web 検索でカバーします

> ⚠️ `HOLDINGS_JSON` は **Secret** タブに登録してください。Variables ではログに出るリスクがあります。

### 6. GitHub Secrets / Variables 設定

**Settings → Secrets and variables → Actions** で

- Secrets: `ANTHROPIC_API_KEY` / `GMAIL_SENDER` / `GMAIL_APP_PASSWORD` / `HOLDINGS_JSON`
- Variables (任意): `GMAIL_TO` / `ANTHROPIC_MODEL`

## 実行

### ローカル

```bash
# 1) データ取得とテクニカル計算のみ（API 未設定でも動く）
python main.py --dry-run

# 2) 保有情報込みドライラン（XXXX を実際の銘柄コードに置換して試す）
HOLDINGS_JSON='{"stocks":[{"symbol":"XXXX.T","shares":100,"avg_cost":1500}],"funds":[]}' \
  python main.py --dry-run

# 3) Claude までは呼ぶがメール送信はしない
ANTHROPIC_API_KEY=... python main.py --skip-notify

# 4) Web 検索無しで Claude 呼び出し（コスト節約・動作確認）
ANTHROPIC_API_KEY=... python main.py --skip-notify --no-web-search

# 5) 本番フロー
ANTHROPIC_API_KEY=... \
GMAIL_SENDER=you@gmail.com GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx \
HOLDINGS_JSON='{...}' \
python main.py
```

### GitHub Actions

- cron: `30 22 * * 0-4`（UTC 日〜木 22:30 = **JST 月〜金 07:30**）
- 寄り付き(09:00) の90分前起動なので、Actions の混雑遅延（典型 5〜15 分、ピーク時 30 分超）と処理時間 1〜2 分を踏まえても余裕で 09:00 前に到着します
- 手動実行: **Actions → Daily Stock Report → Run workflow**（`dry_run=true` で LLM/メールスキップ）

> GitHub Actions の cron は混雑時に遅延します。万一 8:30 を過ぎても届かない日が続く場合は cron をさらに早めるか、別スケジューラ（例: Render Cron など）への移行を検討してください。
> 日本の祝日 / 土日 / 年末年始（12/31〜1/3）は **東証休場日として自動でスキップ**し、メールは送信されません。`--force` フラグでローカルから強制実行は可能。

## CLI フラグ一覧

| フラグ | 用途 |
|---|---|
| `--dry-run` | データ取得＋テクニカル計算のみ。LLM/メールは呼ばない |
| `--skip-llm` | Claude 呼び出しを省略（メール送信は試みる） |
| `--skip-notify` | Gmail 送信を省略 |
| `--no-web-search` | Claude の Web Search ツールを無効化（コスト節約） |
| `--force` | 東証休場日でも強制実行する |
| `--model <id>` | モデル ID を上書き（既定 `claude-sonnet-4-6`） |

## 免責事項

本ツールが生成・配信する情報は投資勧誘を目的としたものではなく、投資の最終決定はご自身の判断で行ってください。
