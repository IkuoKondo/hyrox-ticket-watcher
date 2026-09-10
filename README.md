# HYROX 大阪 2027 チケット空き監視ツール

完売した HYROX 大阪 2027 のチケットに、キャンセル等で **空き（再販）が出た瞬間を検知して Slack に通知**するツールです。

- 監視先：`https://japan.hyrox.com/checkout/6a8dc6065cedd66e43f47850`（公式の vivenu 販売ページ）
- 監視対象：**Singles / Doubles / Relay**（Spectator・写真パックは対象外）
- 判定：カテゴリーが「完売」→「購入可能」に変わったら通知
- 実行場所：**GitHub Actions（無料）** で 10 分おきに自動チェック
- 通知先：**Slack**（空き検知時は `@channel` 付き。毎月1回「稼働中」ヘルスチェックも投稿）
- 60日ルール対策：ツールが**毎日ちいさな自動コミット**を打つので、自動停止しない（手動操作は不要）
- 購入は**あなたが手動**で行います（自動購入はしません）

---

## 仕組み（かんたんに）

1. GitHub Actions が 5 分おきにプログラムを起動
2. プログラムが本物のブラウザで販売ページを開き、各カテゴリーの「完売／購入可能」を読む
3. 前回（`state.json`）と比べて、完売だったものが買える状態に変わっていたら Slack に通知
4. 状態を保存して次回に備える

> 例：Doubles が「完売」だった翌チェックで「購入可能」に変わっていたら、
> 「🎟️ HYROX 大阪 2027 空き検知！ Doubles が『完売』→『購入可能』に変化しました」と Slack に届きます。

---

## セットアップ手順（あなたの作業）

技術的な準備は3つだけです。順番にやれば 15 分ほどで動きます。

### ① Slack の通知用 URL（Incoming Webhook）を作る

1. ブラウザで https://api.slack.com/apps を開き、右上の **「Create New App」→「From scratch」** を選ぶ
2. アプリ名（例：`hyrox-watcher`）と、通知を受け取りたいワークスペースを選んで作成
3. 左メニューの **「Incoming Webhooks」** を開き、スイッチを **On** にする
4. 下の **「Add New Webhook to Workspace」** を押し、通知を受け取りたい**チャンネル**を選んで許可
5. 表示された **Webhook URL**（`https://hooks.slack.com/services/...` という長い URL）をコピーして控える

> この URL は「鍵」です。人に見せない・SNSに貼らないでください。

### ② GitHub に公開リポジトリを作ってコードを置く

GitHub アカウントが無ければ https://github.com/join で作成（無料）。

**リポジトリは「Public（公開）」で作ってください。** 理由：Public なら GitHub Actions の実行時間が無料で実質無制限になります。コード自体に秘密情報は含みません（Slack の URL は次の③で暗号化保存します）。

このフォルダ（`hyrox-ticket-watcher`）を GitHub に上げます。PC のターミナル（PowerShell）で、このフォルダの中で以下を実行します。

```bash
git init
git add .
git commit -m "init: hyrox ticket watcher"
git branch -M main
git remote add origin https://github.com/＜あなたのユーザー名＞/hyrox-ticket-watcher.git
git push -u origin main
```

※ 事前に GitHub 上で空のリポジトリ `hyrox-ticket-watcher` を **Public** で作成しておいてください。

### ③ Slack の URL を GitHub の秘密の金庫（Secrets）に登録する

1. GitHub の該当リポジトリのページを開く
2. **Settings → 左メニュー Secrets and variables → Actions** を開く
3. **「New repository secret」** を押す
4. Name（名前）に **`SLACK_WEBHOOK_URL`** と入力（この名前は完全一致で！）
5. Secret（値）に ① でコピーした Slack の Webhook URL を貼り付けて保存

### ④ 動作テスト

1. リポジトリの **Actions** タブを開く（初回は「I understand my workflows, enable them」を押して有効化）
2. 左の **「HYROX ticket watch」** を選び、右の **「Run workflow」** で手動実行
3. 緑のチェックが付けば成功。ログに各カテゴリーの状態（soldout / onsale）が出ます
4. 以降は 5 分おきに自動で動きます

**通知テストをしたい場合**：`state.json` の中の `"Doubles": "soldout"` を一時的に `"onsale"` … ではなく、逆に「完売中のカテゴリーを onsale と偽って保存 → 実際が soldout」だと通知は出ません。確実なテストは、`"Spectator"` を監視対象に一時追加する等が必要です。まずは④の手動実行でログが正しく出ることを確認できれば十分です。

---

## 大事な注意点（正直なところ）

- **チェック間隔は現在10分**（最短5分まで短縮可）。さらに GitHub の混雑時は数分ずれたり、まれに間引かれます（ベストエフォート）。1〜2分を争う争奪戦では取りこぼす可能性があります。イベントが近づいたら5分に縮めるのがおすすめです。
- **完全自動購入はしません。** 検知→通知までが役割で、購入はあなたが手動で行います。
- **Pro / 障がい者（Adaptive）部門も含めた「カテゴリー単位」の判定**です。「Doubles に空き」通知が来ても、それが Pro 枠の場合があります。購入画面で狙いの種目かをご確認ください。
- 販売サイト（vivenu / HYROX）の**利用規約で自動アクセスが禁止されている可能性**があります。本ツールは「低頻度で見て通知するだけ」ですが、規約変更やアクセス制限が入った場合は使用を中止してください。
- サイトのデザインや文言（「完売」等）が変わると、読み取りが合わなくなることがあります。その場合は連絡ください、直します。

---

## カスタマイズ

`check_tickets.py` の上部を編集します。

- `TARGETS` … 監視するカテゴリー（例：Doubles だけにするなら `["Doubles"]`）
- `CHECKOUT_URL` … 別イベントを監視したいときに差し替え

`.github/workflows/watch.yml` の `cron: "*/10 * * * *"` を変えると間隔を調整できます（例：5分なら `*/5 * * * *`。5 分未満は不可）。

---

## ローカル（自分の PC）で試したいとき

```bash
pip install -r requirements.txt
python -m playwright install chromium
# Slack に飛ばさず内容を画面表示するだけ（URL未設定で動く）
python check_tickets.py
```
