# Automated Daily Stock Recommender

日本時間の平日朝（東京市場の寄り付き直前）に、為替・米国市場・日本株のテクニカル指標を自動分析し、Claude が生成した「相場見通し＋注目銘柄レポート」を **Gmail** で配信する Bot です。GitHub Actions の cron で定期実行されるため、ローカルでの常時起動は不要です。

## アーキテクチャ

```
[GitHub Actions cron]
  └─ python main.py
       ├─ yfinance        : USD/JPY, NYダウ, NASDAQ, SOX, NVDA, 日経平均, 日本株ウォッチリスト
       ├─ pandas/numpy    : 25日移動平均線・乖離率, 26週移動平均線, RSI(14)
       ├─ シグナル判定    : 為替バイアス → 優先セクター, 押し目買い候補
       ├─ Anthropic API   : Claude Opus 4.7 がレポートを日本語生成
       └─ Gmail (SMTP)    : smtp.gmail.com:587 (STARTTLS) で配信
```

## 分析ロジック

- **為替バイアス**: USD/JPY の直近5営業日終値の変化率が `+0.5%` 以上 → 円安 / `-0.5%` 以下 → 円高 / その間 → 中立
- **セクター優先**:
  - 円安バイアス → 輸出関連（輸送用機器等）を優先
  - 円高バイアス → 内需株・金融株を優先
- **押し目買い候補**: `RSI(14) ≤ 30` かつ `|25MA 乖離率| ≤ 3%`
- ウォッチリストの既定銘柄: トヨタ(7203), ヤマハ発動機(7272), 楽天グループ(4755), 三菱UFJ FG(8306)
- レポート末尾には必ず免責事項が付与されます。

## ディレクトリ構成

```
.
├── main.py                                   # メインロジック
├── requirements.txt                          # 依存ライブラリ
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

| 名前 | 種別 | 説明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Secret | Anthropic Console で発行する API キー |
| `GMAIL_SENDER` | Secret | 送信元 Gmail アドレス（アプリパスワードを発行したアカウント） |
| `GMAIL_APP_PASSWORD` | Secret | Google アカウントの「アプリパスワード」16文字（スペース無し） |
| `GMAIL_TO` | Variable (任意) | 受信先アドレス。未指定時は `ho.atmos.89.ishky@gmail.com` |
| `ANTHROPIC_MODEL` | Variable (任意) | 既定 `claude-opus-4-7` |

### 3. Anthropic API キーの取得

1. <https://console.anthropic.com> にサインイン
2. **Settings → API Keys → Create Key**
3. 発行されたキーを `ANTHROPIC_API_KEY` に登録

### 4. Gmail アプリパスワードの取得手順

Gmail SMTP は通常のログインパスワードではなく**アプリパスワード**を使います。事前に **2段階認証プロセスを有効化**しておく必要があります。

1. <https://myaccount.google.com/security> にアクセス
2. **「2段階認証プロセス」を有効化**（未設定の場合）
3. <https://myaccount.google.com/apppasswords> を開く
4. アプリ名（例: `daily-stock-recommender`）を入力して **「作成」**
5. 表示される **16文字のパスワード**（スペース無しで貼り付け）を控える
6. `GMAIL_SENDER` に送信元の Gmail アドレス、`GMAIL_APP_PASSWORD` にこのパスワードを設定

> 受信側と送信側を同じアカウントにしても問題ありません。自分宛にメールが届きます。

### 5. GitHub Secrets / Variables 設定

GitHub リポジトリの **Settings → Secrets and variables → Actions** で以下を登録します。

- **Secrets** タブ: `ANTHROPIC_API_KEY` / `GMAIL_SENDER` / `GMAIL_APP_PASSWORD`
- **Variables** タブ（任意）: `GMAIL_TO` / `ANTHROPIC_MODEL`

## 実行

### ローカル

```bash
# 1) データ取得とテクニカル計算のみ（API 未設定でも動く）
python main.py --dry-run

# 2) LLM までは呼ぶがメール送信はしない
ANTHROPIC_API_KEY=... python main.py --skip-notify

# 3) メール経路だけ確認（LLM はスキップ）
GMAIL_SENDER=you@gmail.com GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx python main.py --skip-llm

# 4) 本番フロー
ANTHROPIC_API_KEY=... \
GMAIL_SENDER=you@gmail.com \
GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx \
python main.py
```

### GitHub Actions

- cron: `50 23 * * 0-4`（UTC 日〜木 23:50 = **JST 月〜金 08:50**）
- 手動実行: **Actions → Daily Stock Report → Run workflow**
- `dry_run=true` を指定すると LLM/メールをスキップして計算結果のみログ出力

> GitHub Actions の cron はピーク時間帯に数分〜十数分遅延することがあります。配信時刻が厳密に必要な場合は実行時刻を早めるか、別のスケジューラを検討してください。
> 祝日判定は行っていません。日本の祝日にも実行されますが、レポート内容としては素直に直近データを提示するだけのため大きな問題はありません。

## 免責事項

本ツールが生成・配信する情報は投資勧誘を目的としたものではなく、投資の最終決定はご自身の判断で行ってください。
