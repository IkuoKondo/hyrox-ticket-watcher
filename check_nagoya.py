#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HYROX 名古屋 2027 「監視できる状態になったか」を定期チェックするスクリプト

名古屋はvivenuの「待機列（バーチャル待合室）」で守られているため、
大阪のように毎回チケット画面を直接読むことが今はできない。
そこでこのスクリプトは「待機列なしでチケット画面に到達できる状態になったか」を見張り、
到達できるようになったら Slack に1回だけ通知する（＝キャンセル枠監視を本設定できる合図）。

状態は nagoya_state.json に保存し、同じ通知を毎回出さないようにする。
"""

import json
import os
import sys
import datetime
import urllib.request

from playwright.sync_api import sync_playwright

# ---- 設定 -----------------------------------------------------------------
# 名古屋イベントの入口（埋め込みページ）。混雑時はここから待機列へ飛ばされる。
NAGOYA_ENTRY_URL = "https://japan.hyrox.com/event/hyrox-nagoya-season-27-28-vyhk3j?useEmbed=true"
EVENT_LABEL = "JR Central HYROX 名古屋 2027"

ALL_CATEGORIES = ["Singles", "Doubles", "Relay", "Spectator"]
SOLD_OUT_MARK = "完売"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "nagoya_state.json")
DEBUG_DIR = os.path.join(HERE, "debug")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
JST = datetime.timezone(datetime.timedelta(hours=9))

# 待機列（待合室）を示すサイン
ROOM_MARKERS = ["待機場所", "もうしばらくお待ち", "/room"]

# チケット画面まで入れたかを調べ、入れたらカテゴリー状況も読むロジック（ブラウザ内実行）
PROBE_JS = r"""
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
  const names = ['Singles', 'Doubles', 'Relay', 'Spectator'];

  // まず数秒待って、待機列に飛ばされるかを見る
  await sleep(4000);
  const isRoom = () => /待機場所|もうしばらくお待ち/.test(document.body ? document.body.innerText : '')
                    || location.href.includes('/room');
  if (isRoom()) return { state: 'queued', url: location.href };

  // Cookie バナーを閉じる
  for (let i = 0; i < 8; i++) { if (clickByText(/必要なものだけ|Essential|Deny/)) break; await sleep(300); }
  await sleep(300);

  // 「チケットを購入する」を押してカテゴリー画面へ
  let clicked = false;
  for (let i = 0; i < 16; i++) { if (clickByText(/チケットを購入する|Buy tickets|Get tickets/)) { clicked = true; break; } await sleep(300); }

  // カテゴリー行にバッジが出るまで待つ（最大 ~20 秒）。途中で待機列に飛ぶ場合もある
  const readRows = () => {
    const out = {};
    for (const n of names) {
      const leaf = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && txt(e) === n);
      if (!leaf) { out[n] = null; continue; }
      let node = leaf.parentElement, row = null;
      for (let i = 0; i < 8 && node; i++) { const t = txt(node); if (BADGE.test(t)) { row = t; break; } node = node.parentElement; }
      out[n] = row || (leaf.parentElement ? txt(leaf.parentElement) : n);
    }
    return out;
  };
  let rows = {};
  for (let i = 0; i < 40; i++) {
    if (isRoom()) return { state: 'queued', url: location.href };
    rows = readRows();
    const hasBadge = names.some(n => rows[n] && BADGE.test(rows[n]));
    if (hasBadge) return { state: 'reachable', url: location.href, rows };
    await sleep(500);
  }
  // ボタンすら出ない等、判断できなかった
  return { state: 'unknown', url: location.href, clicked };
}
"""


def now_jst():
    return datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def today_jst():
    return datetime.datetime.now(JST).strftime("%Y-%m-%d")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
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
    if not row_text:
        return "unknown"
    if SOLD_OUT_MARK in row_text:
        return "soldout"
    if any(k in row_text for k in ("まもなく売り切れ", "購入可能", "Available")):
        return "onsale"
    return "unknown"


def summarize_rows(rows):
    if not rows:
        return "（状況は読み取れませんでした）"
    label = {"soldout": "完売", "onsale": "購入可能", "unknown": "不明"}
    parts = []
    for cat in ALL_CATEGORIES:
        parts.append(f"{cat}={label.get(row_to_status(rows.get(cat)), '?')}")
    return " / ".join(parts)


def probe():
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
            page.goto(NAGOYA_ENTRY_URL, wait_until="domcontentloaded", timeout=60000)
            result = page.evaluate(PROBE_JS)
        except Exception:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            try:
                page.screenshot(path=os.path.join(DEBUG_DIR, "nagoya_error.png"), full_page=True)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()
    return result


def main():
    prev = load_state()

    result = None
    last_err = None
    for attempt in range(1, 4):
        try:
            result = probe()
            break
        except Exception as e:  # noqa
            last_err = e
            print(f"[warn] 名古屋チェック失敗 ({attempt}/3): {e}")
    if result is None:
        print(f"[error] 3回とも失敗: {last_err}")
        sys.exit(1)

    state = result.get("state")
    rows = result.get("rows")
    print(f"=== {now_jst()} JST / 名古屋 state={state} / url={result.get('url')} ===")
    if rows:
        print("  " + summarize_rows(rows))

    new_state = dict(prev)
    new_state["_heartbeat_date"] = today_jst()
    new_state["last_state"] = state
    new_state["last_checked"] = now_jst()

    # 「待機列なしでアクセスできる状態」になったら1回だけ通知
    if state == "reachable":
        if not prev.get("_notified_reachable"):
            msg = "\n".join([
                "🔔 *" + EVENT_LABEL + "：監視できる状態になりました*",
                f"待機列なしでチケット画面にアクセスできる状態を確認しました（{now_jst()} JST）。",
                "現在の状況: " + summarize_rows(rows),
                "",
                "👉 キャンセル枠監視をこのイベント向けに本設定できます。設定するには「名古屋やって」と伝えてください。",
            ])
            try:
                send_slack(msg)
                new_state["_notified_reachable"] = True
                new_state["_notified_date"] = today_jst()
                print("[info] 到達可能の通知を送信しました。")
            except Exception as e:  # noqa
                print(f"[warn] 通知送信に失敗（次回再試行）: {e}")
        else:
            print("[info] 既に到達可能を通知済み。重複送信はしません。")
    else:
        print(f"[info] まだ監視不可（{state}）。通知はしません。")

    save_state(new_state)


if __name__ == "__main__":
    main()
