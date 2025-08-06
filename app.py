# ---------------- LINE 予約 BOT  (Gemini + Google Sheets) ----------------
# 1. 店舗登録 → 2. 空欄予約表で時間帯学習 → 3. シート自動生成
# 4. 記入済み予約表画像で “当日” シートへ追記
# -------------------------------------------------------------------------
"""
必要環境変数（Render の Environment Variables）
-------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging API アクセストークン
GEMINI_API_KEY            : Gemini API キー
GOOGLE_CREDENTIALS_JSON   : サービスアカウント JSON 全文（改行 → \\n の 1 行）
MASTER_SHEET_NAME         : 契約店舗一覧シート名（省略時 “契約店舗一覧”）
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import random
import threading
import traceback
from typing import Any, Dict, List

import gspread
import google.generativeai as genai
import requests
from dotenv import load_dotenv
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials

# -------------------------------------------------------------------------
# 初期化
# -------------------------------------------------------------------------
load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY            = os.getenv("GEMINI_API_KEY")
MASTER_SHEET_NAME         = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

for k in ("LINE_CHANNEL_ACCESS_TOKEN", "GEMINI_API_KEY"):
    if not globals()[k]:
        raise RuntimeError(f"{k} が未設定です")

genai.configure(api_key=GEMINI_API_KEY)
model_chat   = genai.GenerativeModel("gemini-pro")
model_vision = genai.GenerativeModel("gemini-pro-vision")

user_state: Dict[str, Dict[str, Any]] = {}
app = Flask(__name__)

# -------------------------------------------------------------------------
# Google Sheets 認証
# -------------------------------------------------------------------------
def _load_sa(scopes: List[str]):
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT")
    if not raw:
        raise RuntimeError("Google サービスアカウント JSON が未設定です")
    return ServiceAccountCredentials.from_json_keyfile_dict(json.loads(raw), scopes)

SCOPES   = ["https://www.googleapis.com/auth/drive",
            "https://spreadsheets.google.com/feeds"]
gs_client = gspread.authorize(_load_sa(SCOPES))

# -------------------------------------------------------------------------
# Sheets ユーティリティ
# -------------------------------------------------------------------------
def _master_ws():
    try:
        sh = gs_client.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs_client.create(MASTER_SHEET_NAME)
        sh.sheet1.append_row(["店舗名", "店舗ID", "座席数",
                              "シートURL", "登録日時", "時間枠"])
    return sh.sheet1


def create_store_sheet(store: str, sid: int,
                       seats: str, times: List[str]) -> str:
    sh = gs_client.create(f"予約表 - {store} ({sid})")
    sh.share(None, perm_type="anyone", role="writer")
    ws = sh.sheet1
    ws.update([["月", "日", "時間帯", "名前", "人数", "備考"]])
    if times:
        ws.append_rows([["", "", t, "", "", ""] for t in times],
                       value_input_option="USER_ENTERED")
    _master_ws().append_row(
        [store, sid, seats.replace("\n", " "), sh.url,
         dt.datetime.now().isoformat(timespec="seconds"), ",".join(times)]
    )
    return sh.url


def append_rows(sheet_url: str, rows: List[Dict[str, Any]]):
    if not rows:
        return
    ws       = gs_client.open_by_url(sheet_url).sheet1
    header   = ws.row_values(1)
    col_idx  = header.index("時間帯") + 1
    existing = {ws.cell(r, col_idx).value: r
                for r in range(2, ws.row_count + 1)
                if ws.cell(r, col_idx).value}

    for r in rows:
        dst = existing.get(r["time"]) or ws.row_count + 1
        ws.update(
            f"A{dst}:F{dst}",
            [[r.get(k, "") for k in
              ("month", "day", "time", "name", "size", "note")]]
        )

# -------------------------------------------------------------------------
# LINE API
# -------------------------------------------------------------------------
def _reply(token: str, text: str):
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                     "Content-Type": "application/json"},
            json={"replyToken": token,
                  "messages": [{"type": "text", "text": text}]},
            timeout=10
        )
    except Exception:
        print("[LINE reply error]", traceback.format_exc())


def _push(uid: str, text: str):
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                     "Content-Type": "application/json"},
            json={"to": uid, "messages": [{"type": "text", "text": text}]},
            timeout=10
        )
    except Exception:
        print("[LINE push error]", traceback.format_exc())

# -------------------------------------------------------------------------
# Gemini 画像解析
# -------------------------------------------------------------------------
def _dl_img(mid: str) -> bytes:
    r = requests.get(
        f"https://api-data.line.me/v2/bot/message/{mid}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=15)
    r.raise_for_status()
    return r.content


def _extract_times(img: bytes) -> List[str]:
    parts = [
        {"mime_type": "image/jpeg", "data": img},
        "画像は空欄の予約表です。予約可能な時間帯 (HH:MM) を左上→右下順に抽出し、昇順 JSON 配列で返してください。"
    ]
    try:
        data = json.loads(model_vision.generate_content(parts).text)
        return [str(t) for t in data] if isinstance(data, list) else []
    except Exception:
        return []


def _extract_rows(img: bytes) -> List[Dict[str, Any]]:
    parts = [
        {"mime_type": "image/jpeg", "data": img},
        ("画像は手書きの予約表です。各行を JSON 配列で返してください。\n"
         "形式: [{\"month\":int,\"day\":int,\"time\":\"HH:MM\","
         "\"name\":str,\"size\":int,\"note\":str}]")
    ]
    try:
        data = json.loads(model_vision.generate_content(parts).text)
        return data if isinstance(data, list) else []
    except Exception:
        return []

# -------------------------------------------------------------------------
# 背景スレッド
# -------------------------------------------------------------------------
def _proc_template(uid: str, mid: str):
    try:
        st    = user_state.get(uid)
        if not st or st["step"] != "wait_template":
            return
        times = _extract_times(_dl_img(mid))
        if not times:
            _push(uid, "解析に失敗しました。鮮明な空欄予約表画像をもう一度送ってください。")
            return
        st.update({"times": times, "step": "confirm_times"})
        _push(uid,
            "📊 予約表構造の分析が完了しました！\n\n"
            "検出した時間帯：\n" + "\n".join(f"・{t}〜" for t in times) +
            "\n\nこの内容でスプレッドシートを作成してよろしいですか？（はい／いいえ）")
    except Exception:
        print("[proc_template]", traceback.format_exc())


def _proc_filled(uid: str, mid: str):
    try:
        st = user_state.get(uid)
        if not st or st["step"] != "wait_filled":
            return
        rows = _extract_rows(_dl_img(mid))
        if not rows:
            _push(uid, "予約情報を読み取れませんでした。鮮明な画像を送ってください。")
            return
        append_rows(st["sheet_url"], rows)
        _push(uid, "✅ 予約情報をスプレッドシートへ追記しました！")
    except Exception:
        print("[proc_filled]", traceback.format_exc())

# -------------------------------------------------------------------------
# Webhook
# -------------------------------------------------------------------------
@app.route("/", methods=["GET", "HEAD", "POST"])
def webhook():
    if request.method in {"GET", "HEAD"}:
        return "OK", 200

    data = request.get_json(silent=True) or {}
    # LINE の Verify リクエストは events が空なので 200 をすぐ返す
    if not data.get("events"):
        return "OK", 200

    try:
        event = data["events"][0]
        threading.Thread(target=_handle, args=(event,)).start()
        return "OK", 200
    except Exception:
        print("[webhook error]", traceback.format_exc())
        return "ERROR", 500


def _handle(e: Dict[str, Any]):
    try:
        if e.get("type") != "message":
            return
        uid   = e["source"]["userId"]
        tok   = e["replyToken"]
        mtype = e["message"]["type"]
        text  = e["message"].get("text", "")
        mid   = e["message"].get("id")

        st = user_state.setdefault(uid, {"step": "start"})

        # ---------- TEXT ----------
        if mtype == "text":
            if st["step"] == "start":
                name = model_chat.generate_content(
                    f"以下の文から店舗名だけを抽出してください：\n{text}").text.strip()
                sid  = random.randint(100_000, 999_999)
                st.update({"step": "confirm_store", "store": name, "sid": sid})
                _reply(tok,
                    f"店舗名: {name} です。これで登録します。\n"
                    f"店舗ID: {sid}\nこの内容でよろしいですか？（はい／いいえ）")
                return

            if st["step"] == "confirm_store":
                if "はい" in text:
                    st["step"] = "ask_seats"
                    _reply(tok, "座席数を入力してください。\n例: 1人席:3 2人席:2 4人席:1")
                elif "いいえ" in text:
                    st.clear(); st["step"] = "start"
                    _reply(tok, "もう一度、店舗名を送ってください。")
                else:
                    _reply(tok, "「はい」か「いいえ」で答えてください。")
                return

            if st["step"] == "ask_seats":
                seats = model_chat.generate_content(
                    "以下の文から 1人席・2人席・4人席 の数を抽出し "
                    "次の形式で返してください：\n1人席：◯席\n2人席：◯席\n4人席：◯席\n\n"
                    f"文：{text}"
                ).text.strip()
                st.update({"seat_info": seats, "step": "confirm_seats"})
                _reply(tok,
                    "✅ 登録情報の確認：\n\n"
                    f"店舗名：{st['store']}\n店舗ID：{st['sid']}\n\n{seats}\n\n"
                    "この内容で登録してよろしいですか？（はい／いいえ）")
                return

            if st["step"] == "confirm_seats":
                if "はい" in text:
                    st["step"] = "wait_template"
                    _reply(tok,
                        "店舗登録完了！🎉\n"
                        "空欄の予約表画像を送ってください。")
                elif "いいえ" in text:
                    st["step"] = "ask_seats"
                    _reply(tok, "もう一度、座席数を入力してください。")
                else:
                    _reply(tok, "「はい」か「いいえ」で答えてください。")
                return

            if st["step"] == "confirm_times":
                if "はい" in text:
                    url = create_store_sheet(
                        st["store"], st["sid"],
                        st["seat_info"], st["times"])
                    st.update({"sheet_url": url, "step": "wait_filled"})
                    _reply(tok,
                        f"スプレッドシートを作成しました！\n{url}\n\n"
                        "当日の予約を書いた紙を撮影して送ってください。")
                elif "いいえ" in text:
                    st["step"] = "wait_template"
                    _reply(tok, "わかりました。空欄の予約表画像をもう一度送ってください。")
                else:
                    _reply(tok, "「はい」か「いいえ」で答えてください。")
                return

        # ---------- IMAGE ----------
        if mtype == "image":
            if st["step"] == "wait_template":
                threading.Thread(target=_proc_template, args=(uid, mid)).start()
                _reply(tok, "画像を受信しました。AI が解析中です…")
            elif st["step"] == "wait_filled":
                threading.Thread(target=_proc_filled, args=(uid, mid)).start()
                _reply(tok, "画像を受信しました。AI が読み取り中です…")
            else:
                _reply(tok, "まだ画像解析の準備ができていません。")
    except Exception:
        print("[handle error]", traceback.format_exc())
        _reply(e.get("replyToken", ""), "システムエラーが発生しました。再度お試しください。")

# -------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0",
            port=int(os.environ.get("PORT", 5000)),
            debug=False)
