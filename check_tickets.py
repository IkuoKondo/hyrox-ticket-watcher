#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HYROX 大阪 2027 チケット空き監視スクリプト

vivenu のチェックアウト画面を実際に開き、各カテゴリー
(Singles / Doubles / Relay) が「完売」か「購入可能」かを読み取る。
完売だったカテゴリーが購入可能に変わった（＝キャンセル再販が出た）ら Slack へ通知する。

- 前回の状態は state.json に保存し、変化した瞬間だけ通知する。
- 監視対象カテゴリーは TARGETS で指定（Spectator / Photo は除外）。
- Adaptive(障がい者) / Pro はカテゴリー単位の表示では分離できないため、
  「Singles / Doubles / Relay のどれかに空きが出たら通知 → 実際の購入時に自分で種目を選ぶ」方針。
"""

import json
import os
import sys
import datetime
import urllib.request

from playwright.sync_api import sync_playwright

# ---- 設定 -----------------------------------------------------------------
CHECKOUT_URL = "https://japan.hyrox.com/checkout/6a8dc6065cedd66e43f47850"
EVENT_NAME = "BYD HYROX Osaka | Season 26/27"

# 監視するカテゴリー（この中の1つでも「完売→購入可能」になったら通知）
TARGETS = ["Singles", "Doubles", "Relay"]
# 画面に出る全カテゴリー（ログ用）
ALL_CATEGORIES = ["Singles", "Doubles", "Relay", "Spectator"]

SOLD_OUT_MARK = "完売"  # この文字が行に含まれていれば「売り切れ」

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
DEBUG_DIR = os.path.join(HERE, "debug")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
JST = datetime.timezone(datetime.timedelta(hours=9))

# ブラウザ内で実行する取得ロジック（実機で動作確認済み）
READ_JS = r"""
async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = el => (el.textContent || '').replace(/\s+/g, ' ').trim();
  const clickByText = (re) => {
    for (const b of document.querySelectorAll('button')) {
      if (re.test(txt(b))) { b.click(); return true; }
    }
    return false;
  };
  const BADGE = /完売|まもなく売り切れ|購入可能|Sold ?out|Available/;

  // Cookie バナーを閉じる（必要なものだけ）
  for (let i = 0; i < 10; i++) {
    if (clickByText(/必要なものだけ|Essential|Deny/)) break;
    await sleep(300);
  }
  await sleep(400);

  // 「チケットを購入する」を押してカテゴリー選択画面へ
  for (let i = 0; i < 20; i++) {
    if (clickByText(/チケットを購入する|Buy tickets|Get tickets/)) break;
    await sleep(300);
  }

  const names = ['Singles', 'Doubles', 'Relay', 'Spectator'];
  const readRows = () => {
    const out = {};
    for (const n of names) {
      const leaf = [...document.querySelectorAll('*')]
        .find(e => e.children.length === 0 && txt(e) === n);
      if (!leaf) { out[n] = null; continue; }
      let node = leaf.parentElement, row = null;
      for (let i = 0; i < 8 && node; i++) {
        const t = txt(node);
        if (BADGE.test(t)) { row = t; break; }
        node = node.parentElement;
      }
      out[n] = row || (leaf.parentElement ? txt(leaf.parentElement) : n);
    }
    return out;
  };

  // カテゴリー行にバッジが出るまで待つ（最大 ~20 秒）
  let rows = {};
  for (let i = 0; i < 40; i++) {
    rows = readRows();
    const hasBadge = names.some(n => rows[n] && BADGE.test(rows[n]));
    if (hasBadge) break;
    await sleep(500);
  }
  return rows;
}
"""


def now_jst():
    return datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def today_jst():
    return datetime.datetime.now(JST).strftime("%Y-%m-%d")


def month_jst():
    return datetime.datetime.now(JST).strftime("%Y-%m")


def status_summary(state):
    label = {"soldout": "完売", "onsale": "購入可能", "unknown": "不明"}
    return " / ".join(
        f"{cat}={label.get(state.get(cat), '?')}" for cat in ALL_CATEGORIES
    )


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        # キー順を固定 → 変化が無ければファイルも不変（無駄なコミットを防ぐ）
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def send_slack(text):
    if not SLACK_WEBHOOK_URL:
        print("[warn] SLACK_WEBHOOK_URL 未設定。送信内容:\n" + text)
        return
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    print("[info] Slack へ通知しました。")


def row_to_status(row_text):
    """行テキスト -> 'soldout' / 'onsale' / 'unknown'"""
    if not row_text:
        return "unknown"
    if SOLD_OUT_MARK in row_text:
        return "soldout"
    if any(k in row_text for k in ("まもなく売り切れ", "購入可能", "Available")):
        return "onsale"
    return "unknown"


def fetch_statuses():
    """ブラウザで実際に開いて各カテゴリーの状態を取得する。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(60000)
        try:
            page.goto(CHECKOUT_URL, wait_until="domcontentloaded", timeout=60000)
            rows = page.evaluate(READ_JS)
        except Exception:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            try:
                page.screenshot(path=os.path.join(DEBUG_DIR, "error.png"), full_page=True)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()

    statuses = {}
    for cat in ALL_CATEGORIES:
        statuses[cat] = {"status": row_to_status(rows.get(cat)), "raw": rows.get(cat)}
    return statuses


def main():
    # Slack 疎通テスト（Run workflow の test_slack=true のときだけ）
    if os.environ.get("TEST_SLACK", "").strip().lower() == "true":
        msg = (
            "✅ HYROX 大阪 2027 監視ツールのテスト通知です（"
            + now_jst()
            + " JST）。\nこの文面が届いていれば Slack 連携は正常です。"
            "実際に空きが出たときも、同じチャンネルに通知します。"
        )
        try:
            send_slack(msg)
            print("[info] テスト通知を送信しました。")
        except Exception as e:  # noqa
            print(f"[error] テスト通知に失敗: {e}")
            sys.exit(1)
        return

    # 「空き検知」通知の見本を送る（Run workflow の test_alert=true のときだけ）
    if os.environ.get("TEST_ALERT", "").strip().lower() == "true":
        sample = "\n".join([
            "<!channel>",
            "🧪 *【テスト送信】* 本番の「空き検知」通知はこのように届きます ↓↓↓",
            "──────────────",
            "🎟️ *HYROX 大阪 2027 空き検知！*",
            f"次のカテゴリーで空き（再販）が出た可能性があります（{now_jst()} JST）:",
            "　• *Doubles* が「完売」→「購入可能」に変化しました",
            "",
            f"👉 今すぐ確認 / 購入: {CHECKOUT_URL}",
            "（※ Pro・障がい者部門も含む可能性があります。購入画面で種目をご確認ください）",
            "──────────────",
            "※これはテストです。実際に空きが出たわけではありません。本番も先頭に @channel が付き、チャンネル全員に通知が飛びます。",
        ])
        try:
            send_slack(sample)
            print("[info] 空き検知の見本（テスト）を送信しました。")
        except Exception as e:  # noqa
            print(f"[error] 見本テスト送信に失敗: {e}")
            sys.exit(1)
        return

    prev = load_state()

    # 一時的な失敗に備えて最大3回まで取得を試す
    statuses = None
    last_err = None
    for attempt in range(1, 4):
        try:
            statuses = fetch_statuses()
            break
        except Exception as e:  # noqa
            last_err = e
            print(f"[warn] 取得失敗 ({attempt}/3): {e}")
    if statuses is None:
        print(f"[error] 3回とも取得に失敗しました: {last_err}")
        sys.exit(1)

    # ログ出力
    print(f"=== {now_jst()} JST ===")
    for cat in ALL_CATEGORIES:
        print(f"  {cat}: {statuses[cat]['status']}  (raw: {statuses[cat]['raw']})")

    # 判定：完売 -> 購入可能 になった監視対象を集める
    opened = []
    unknown_targets = []
    for cat in TARGETS:
        now = statuses[cat]["status"]
        before = prev.get(cat)
        if now == "unknown":
            unknown_targets.append(cat)
            continue
        if before == "soldout" and now == "onsale":
            opened.append(cat)

    if unknown_targets:
        print(f"[warn] 状態を読めなかったカテゴリー（通知判定はスキップ）: {unknown_targets}")

    # 通知
    if opened:
        lines = [
            "<!channel> 🎟️ *HYROX 大阪 2027 空き検知！*",
            f"次のカテゴリーで空き（再販）が出た可能性があります（{now_jst()} JST）:",
        ]
        for cat in opened:
            lines.append(f"　• *{cat}* が「完売」→「購入可能」に変化しました")
        lines.append("")
        lines.append(f"👉 今すぐ確認 / 購入: {CHECKOUT_URL}")
        lines.append("（※ Pro・障がい者部門も含む可能性があります。購入画面で種目をご確認ください）")
        try:
            send_slack("\n".join(lines))
        except Exception as e:  # noqa
            print(f"[error] Slack 通知に失敗: {e}")
            sys.exit(1)
    else:
        print("[info] 監視対象の新規の空きはありません。")

    # 状態を保存（unknown は前回値を維持して取りこぼしを防ぐ）
    new_state = dict(prev)
    for cat in ALL_CATEGORIES:
        st = statuses[cat]["status"]
        if st != "unknown":
            new_state[cat] = st

    # 毎日ハートビート: state.json に変化を起こしてコミットを発生させ、
    # GitHub の「60日間コミットなしで自動停止」ルールを自動で回避する
    if new_state.get("_heartbeat_date") != today_jst():
        new_state["_heartbeat_date"] = today_jst()

    # 毎月1回、稼働中であることを Slack に投稿（ヘルスチェック）
    if new_state.get("_last_reminder_month") != month_jst():
        try:
            send_slack(
                "🟢 HYROX 大阪 2027 監視ツールは稼働中です（"
                + today_jst()
                + " JST）。\n現在の空き状況: "
                + status_summary(new_state)
                + "\n※これは毎月の自動ヘルスチェックです。毎月これが届いていれば正常稼働中です。"
            )
            new_state["_last_reminder_month"] = month_jst()
            print("[info] 月次ヘルスチェックを Slack に送信しました。")
        except Exception as e:  # noqa
            print(f"[warn] 月次ヘルスチェックの送信に失敗（次回再試行）: {e}")

    save_state(new_state)


if __name__ == "__main__":
    main()
