# Automated Daily Stock Recommender

日本時間の平日朝（東京市場の寄り付き直前）に、為替・米国市場・日本株のテクニカル指標を自動分析し、Claude が生成した「相場見通し＋注目銘柄レポート」を **LINE Messaging API** で配信する Bot です。GitHub Actions の cron で定期実行されるため、ローカルでの常時起動は不要です。

## アーキテクチャ

```
[GitHub Actions cron]
  └─ python main.py
       ├─ yfinance        : USD/JPY, NYダウ, NASDAQ, SOX, NVDA, 日経平均, 日本株ウォッチリスト
       ├─ pandas/numpy    : 25日移動平均線・乖離率, 26週移動平均線, RSI(14)
       ├─ シグナル判定    : 為替バイアス → 優先セクター, 押し目買い候補
       ├─ Anthropic API   : Claude Opus 4.7 がレポートを日本語生成
       └─ LINE Messaging  : push API でユーザー/グループへ配信
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
├── main.py                            # メインロジック
├── requirements.txt                   # 依存ライブラリ
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

### 2. 必要なシークレット

| 環境変数 | 説明 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Console で発行する API キー |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API チャンネルの long-lived アクセストークン |
| `LINE_TO_USER_ID` | push 送信先の `userId` または `groupId` |
| `ANTHROPIC_MODEL` (任意) | 既定 `claude-opus-4-7`。上書きしたい場合のみ設定 |

### 3. Anthropic API キーの取得

1. <https://console.anthropic.com> にサインイン
2. **Settings → API Keys → Create Key**
3. 発行されたキーを `ANTHROPIC_API_KEY` として保存

### 4. LINE Messaging API のトークン取得手順

1. <https://developers.line.biz/console/> にログインし、プロバイダーを作成
2. **「Messaging API」チャンネル**を新規作成
3. チャンネル設定の **「Messaging API設定」タブ**で:
   - **Channel access token (long-lived)** を発行 → `LINE_CHANNEL_ACCESS_TOKEN`
   - **応答メッセージ / あいさつメッセージ**は任意で無効化
4. 自分の LINE 公式アカウントを QR コードから**友だち追加**
5. 自分の `userId` を取得する方法（いずれか）:
   - **LINE Official Account Manager → チャット**で対象ユーザーを開き URL の末尾 ID を確認
   - もしくは Webhook を一時的に有効化し、何かメッセージを送って Webhook イベントに含まれる `source.userId` を控える
   - グループに通知したい場合は `source.groupId` を `LINE_TO_USER_ID` に設定
6. `LINE_TO_USER_ID` に控えた ID を保存

> 注: LINE Messaging API のフリープランは月あたりの送信数に上限があります（プランにより 200 通など）。本 Bot は平日朝1通なので通常は問題になりません。

### 5. GitHub Secrets 設定

GitHub リポジトリの **Settings → Secrets and variables → Actions** で、上記3つを Secret として登録します。`ANTHROPIC_MODEL` を上書きしたい場合は **Variables** タブで設定可能です。

## 実行

### ローカル

```bash
# 1) データ取得とテクニカル計算のみ（API 未設定でも動く）
python main.py --dry-run

# 2) LLM までは呼ぶが LINE 通知はしない
ANTHROPIC_API_KEY=... python main.py --skip-notify

# 3) LINE 経路だけ確認（固定的なメッセージを送る）
LINE_CHANNEL_ACCESS_TOKEN=... LINE_TO_USER_ID=... python main.py --skip-llm

# 4) 本番フロー
ANTHROPIC_API_KEY=... LINE_CHANNEL_ACCESS_TOKEN=... LINE_TO_USER_ID=... python main.py
```

### GitHub Actions

- cron: `50 23 * * 0-4`（UTC 日〜木 23:50 = **JST 月〜金 08:50**）
- 手動実行: **Actions → Daily Stock Report → Run workflow**
- `dry_run=true` を指定すると LLM/LINE をスキップして計算結果のみログ出力

> GitHub Actions の cron はピーク時間帯に数分〜十数分遅延することがあります。配信時刻が厳密に必要な場合は実行時刻を早めるか、別のスケジューラを検討してください。
> 祝日判定は行っていません。日本の祝日にも実行されますが、レポート内容としては素直に直近データを提示するだけのため大きな問題はありません。

## 免責事項

本ツールが生成・配信する情報は投資勧誘を目的としたものではなく、投資の最終決定はご自身の判断で行ってください。
