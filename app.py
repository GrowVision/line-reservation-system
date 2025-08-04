# LINE予約管理BOT (Google Sheets 連携 + GPT-4o 画像解析)
# -------------------------------------------------------------
# このスクリプトは LINE Bot で受信したメッセージをもとに
#   1. 店舗登録（店舗名・ID・座席数）
#   2. 予約表スプレッドシートの自動生成
#   3. 画像解析で予約行を抽出して "当日" シートに追記
# をワンストップで行います。
# -------------------------------------------------------------
"""
必要な環境変数（Render の Environment Variables で設定）
----------------------------------------------------------------
OPENAI_API_KEY            : OpenAI GPT-4o の API キー
LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging API のアクセストークン
GOOGLE_SERVICE_ACCOUNT    : サービスアカウント JSON 全文（1 行で）
MASTER_SHEET_NAME         : 契約店舗一覧シート名（省略時 "契約店舗一覧"）
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import random
import threading
from typing import Any, Dict, List

import gspread
import requests
from dotenv import load_dotenv
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI

# -------------------------------------------------------------
# 初期設定
# -------------------------------------------------------------

app = Flask(__name__)
load_dotenv()

# --- 必須キー読み込み ---
OPENAI_API_KEY            = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
MASTER_SHEET_NAME         = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (OPENAI_API_KEY and LINE_CHANNEL_ACCESS_TOKEN):
    raise RuntimeError("OPENAI_API_KEY と LINE_CHANNEL_ACCESS_TOKEN を設定してください")

client = OpenAI(api_key=OPENAI_API_KEY)
user_state: Dict[str, Dict[str, Any]] = {}

# -------------------------------------------------------------
# Google Sheets 認証ユーティリティ
# -------------------------------------------------------------

def _load_service_account(scope: List[str]):
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT が設定されていません")
    # Render の環境変数は 1 行文字列なのでそのまま json.loads 可能
    info = json.loads(raw)
    return ServiceAccountCredentials.from_json_keyfile_dict(info, scope)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds            = _load_service_account(SCOPES)
gs_client        = gspread.authorize(creds)

# -------------------------------------------------------------
# Sheets 操作ユーティリティ
# -------------------------------------------------------------

def _get_master_ws():
    """契約店舗一覧シート (1 シート) を返す。無ければ作成。"""
    try:
        sh = gs_client.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs_client.create(MASTER_SHEET_NAME)
        sh.sheet1.append_row(["店舗名", "店舗ID", "座席数", "シートURL", "登録日時"])
    return sh.sheet1

def create_store_sheet(store_name: str, store_id: int, seat_info: str) -> str:
    """店舗用予約表スプレッドシートを生成して URL を返す"""
    sh = gs_client.create(f"予約表 - {store_name} ({store_id})")
    sh.share(None, perm_type="anyone", role="reader")  # URL で閲覧可（必要に応じて変更）
    ws = sh.sheet1
    ws.update([[
        "月", "日", "時間帯", "名前", "人数", "備考"
    ]])
    _get_master_ws().append_row([
        store_name,
        store_id,
        seat_info.replace("\n", " "),
        sh.url,
        dt.datetime.now().isoformat(timespec="seconds"),
    ])
    return sh.url

def append_reservations(sheet_url: str, rows: List[Dict[str, Any]]):
    if not rows:
        return
    ws = gs_client.open_by_url(sheet_url).sheet1
    ws.append_rows([
        [r.get(k, "") for k in ("month", "day", "time", "name", "size", "note")]
        for r in rows
    ], value_input_option="USER_ENTERED")

# -------------------------------------------------------------
# LINE Messaging API ユーティリティ
# -------------------------------------------------------------

def _line_reply(token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    requests.post(url, headers=headers, json={
        "replyToken": token,
        "messages": [{"type": "text", "text": text}]
    }, timeout=10)

# -------------------------------------------------------------
# Vision 解析
# -------------------------------------------------------------

def _download_line_image(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.content

def _vision_extract(img_bytes: bytes) -> List[Dict[str, Any]]:
    b64 = base64.b64encode(img_bytes).decode()
    prompt = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": (
            "画像は飲食店の紙予約表です。行ごとに予約情報を抽出し、"
            "JSON 配列で返してください。フォーマットは次に従います。\n"
            "[{\"month\":int,\"day\":int,\"time\":\"HH:MM\",\"name\":str,\"size\":int,\"note\":str}]"
        )},
    ]
    res = client.chat.completions.create(
        model="gpt-4o",  # Vision 対応モデル
        messages=prompt,
        response_format={"type": "json_object"},
        max_tokens=1024,
    )
    try:
        return json.loads(res.choices[0].message.content)
    except Exception:
        return []

# -------------------------------------------------------------
# Webhook ハンドラ
# -------------------------------------------------------------

@app.route("/", methods=["POST", "GET", "HEAD"])
def webhook():
    if request.method != "POST":
        return "OK", 200
    body = request.get_json(silent=True) or {}
    events = body.get("events", [])
    if not events:
        return "NO EVENT", 200
    threading.Thread(target=_handle_event, args=(events[0],)).start()
    return "OK", 200

# -------------------------------------------------------------
# 会話ステートマシン
# -------------------------------------------------------------

def _handle_event(ev: Dict[str, Any]):
    if ev.get("type") != "message":
        return

    uid        = ev["source"]["userId"]
    mtype      = ev["message"]["type"]
    token      = ev["replyToken"]
    text       = ev["message"].get("text", "") if mtype == "text" else ""
    state      = user_state.setdefault(uid, {"step": "store_name"})

    # ---------- 店舗名入力 ----------
    if state["step"] == "store_name" and mtype == "text":
        store_name = text.strip()
        store_id   = random.randint(100000, 999999)
        state.update({"step": "confirm_store", "store_name": store_name, "store_id": store_id})
        _line_reply(token, f"店舗名: {store_name}\n店舗ID: {store_id}\nこの内容でよろしいですか？ (はい/いいえ)")
        return

    # ---------- 店舗名確認 ----------
    if state["step"] == "confirm_store" and mtype == "text":
        if text.strip() == "はい":
            state["step"] = "seat_input"
            _line_reply(token, "座席数を入力してください (例: 1人席:3\n2人席:2\n4人席:1)")
        else:
            state["step"] = "store_name"
            _line_reply(token, "では店舗名をもう一度入力してください。")
        return

    # ---------- 座席数入力 ----------
    if state["step"] == "seat_input" and mtype == "text":
        state.update({"seat_info": text.strip(), "step": "create_sheet"})
        sheet_url = create_store_sheet(state["store_name"], state["store_id"], state["seat_info"])
        state.update({"sheet_url": sheet_url, "step": "wait_image"})
        _line_reply(token, f"✅ 予約表スプレッドシートを作成しました！\n{sheet_url}\n\n予約表の画像を送ってください。")
        return

    # ---------- 画像受信 ----------
    if state["step"] == "wait_image" and mtype == "image":
        img_bytes         = _download_line_image(ev["message"]["id"])
        extracted_rows    = _vision_extract(img_bytes)
        state.update({"img_rows": extracted_rows, "step": "confirm_rows"})
        preview_lines = "\n".join([f"{r.get('time','??')} {r.get('name','?')} {r.get('size','?')}名" for r in extracted_rows[:5]])
        preview_lines = preview_lines or "(予約行が検出できませんでした)"
        _line_reply(token, f"抽出した予約行の例:\n{preview_lines}\n\nこの内容で登録してよろしいですか？ (はい/いいえ)")
        return

    # ---------- 画像解析結果確認 ----------
    if state["step"] == "confirm_rows" and mtype == "text":
        if text.strip() == "はい":
            append_reservations(state["sheet_url"], state.get("img_rows", []))
            state["step"] = "done"
            _line_reply(token, "✅ 予約表に登録しました！ご確認ください。")
        else:
            state["step"] = "wait_image"
            _line_reply(token, "了解しました。もう一度予約表の画像を送ってください。")
        return

    # ---------- その他 ----------
    _line_reply(token, "メッセージを理解できませんでした。もう一度お試しください。")

# -------------------------------------------------------------
# ローカル実行用
# -------------------------------------------------------------

if __name__ == "__main__":
    # Flask のデバッグサーバー
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
