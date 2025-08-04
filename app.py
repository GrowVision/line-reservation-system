# LINE予約管理BOT (Google Sheets 連携 + GPT‑4o 画像解析)
# -------------------------------------------------------------
#   1. 店舗登録（店舗名・ID・座席数）
#   2. 空の予約表テンプレ画像を解析し時間枠を抽出
#   3. 時間枠を使って店舗専用スプレッドシートを自動生成
#   4. 記入済み予約表画像を解析し "当日" シートに追記
# -------------------------------------------------------------
"""
必要な環境変数（Render の Environment Variables で設定）
----------------------------------------------------------------
OPENAI_API_KEY            : OpenAI GPT‑4o の API キー
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
    info = json.loads(raw)
    return ServiceAccountCredentials.from_json_keyfile_dict(info, scope)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds = _load_service_account(SCOPES)
gs_client = gspread.authorize(creds)

# -------------------------------------------------------------
# Sheets 操作ユーティリティ
# -------------------------------------------------------------

def _get_master_ws():
    try:
        sh = gs_client.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs_client.create(MASTER_SHEET_NAME)
        sh.sheet1.append_row(["店舗名", "店舗ID", "座席数", "シートURL", "登録日時", "時間枠"])
    return sh.sheet1

def create_store_sheet(store_name: str, store_id: int, seat_info: str, times: List[str]) -> str:
    sh = gs_client.create(f"予約表 - {store_name} ({store_id})")
    sh.share(None, perm_type="anyone", role="writer")
    ws = sh.sheet1
    ws.update([["月", "日", "時間帯", "名前", "人数", "備考"]])
    if times:
        ws.append_rows([["", "", t, "", "", ""] for t in times], value_input_option="USER_ENTERED")
    _get_master_ws().append_row([
        store_name,
        store_id,
        seat_info.replace("\n", " "),
        sh.url,
        dt.datetime.now().isoformat(timespec="seconds"),
        ",".join(times),
    ])
    return sh.url

def append_reservations(sheet_url: str, rows: List[Dict[str, Any]]):
    if not rows:
        return
    ws = gs_client.open_by_url(sheet_url).sheet1
    header = ws.row_values(1)
    col_idx = header.index("時間帯") + 1 if "時間帯" in header else 3
    existing = {ws.cell(r, col_idx).value: r for r in range(2, ws.row_count + 1) if ws.cell(r, col_idx).value}
    for r in rows:
        target_row = existing.get(r.get("time")) or ws.row_count + 1
        ws.update(
            f"A{target_row}:F{target_row}",
            [[r.get(k, "") for k in ("month", "day", "time", "name", "size", "note")]],
        )

# -------------------------------------------------------------
# LINE Messaging API ユーティリティ
# -------------------------------------------------------------

def _line_reply(token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    requests.post(url, headers=headers, json={"replyToken": token, "messages": [{"type": "text", "text": text}]}, timeout=10)

def _line_push(uid: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    requests.post(url, headers=headers, json={"to": uid, "messages": [{"type": "text", "text": text}]}, timeout=10)

# -------------------------------------------------------------
# Vision 解析ユーティリティ
# -------------------------------------------------------------

def _download_line_image(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.content

def _vision_extract_times(img: bytes) -> List[str]:
    b64 = base64.b64encode(img).decode()
    res = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": "画像は空欄の飲食店予約表です。予約可能な時間帯 (HH:MM) をすべて抽出し、昇順の JSON 配列で返してください。"},
        ],
        response_format={"type": "json_object"},
        max_tokens=256,
    )
    try:
        times = json.loads(res.choices[0].message.content)
        return times if isinstance(times, list) else []
    except Exception:
        return []

def _vision_extract_rows(img: bytes) -> List[Dict[str, Any]]:
    b64 = base64.b64encode(img).decode()
    res = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": (
                "画像は手書きの予約表です。各行の予約情報を JSON 配列で返してください。"
                "フォーマット: [{\"month\":int,\"day\":int,\"time\":\"HH:MM\",\"name\":str,\"size\":int,\"note\":str}]"
            )},
        ],
        response_format={"type": "json_object"},
        max_tokens=1024,
    )
    try:
        return json.loads(res.choices[0].message.content)
    except Exception:
        return []

# -------------------------------------------------------------
# 背景処理スレッド
# -------------------------------------------------------------

def _process_template_image(uid: str, message_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_template_img":
        return
    try:
        img = _download_line_image(message_id)
        times = _vision_extract_times(img)
        st["times"] = times
        times_view = "\n".join(f"・{t}〜" for t in times) or "（時間枠を検出できませんでした）"
        _line_push(
            uid,
            "📊 予約表構造の分析が完了しました！\n\n"
            "画像を分析した結果、以下のような形式で記録されている可能性があります：\n\n"
            "───────────────\n\n"
            "■ 検出された時間帯：\n" + times_view + "\n\n"
            "■ 記入項目：\n"
            "・名前またはイニシャル\n"
            "・人数（例：1人、2人、4人）\n"
            "・備考欄（自由記入、空欄もあり）\n\n"
            "■ その他の特徴：\n"
            "・上部に日付（◯月◯日）記入欄あり\n"
            "・最下部に営業情報や注意事項が記載\n\n"
            "───────────────\n\n"
            "このような構成で問題なければ、「はい」とご返信ください。\n"
            "異なる点がある場合は、「いいえ」とご返信のうえ、修正点をご連絡ください。",
        )
        st["step"] = "confirm_times"
    except Exception as e:
        print("[template image error]", e)
        _line_push(uid, "画像の解析に失敗しました。もう一度、なるべく鮮明な画像を送ってください。")

def _process_filled_image(uid: str, message_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_filled_img":
        return
    try:
        img = _download_line_image(message_id)
        rows = _vision_extract_rows(img)
        if not rows:
            _line_push(uid, "予約行を検出できませんでした。もう一度鮮明な画像を送ってください。")
            return
        preview = "\n".join(f"{r.get('time','')}: {r.get('name','')} ({r.get('size','')}名)" for r in rows[:5])
        st["pending_rows"] = rows
        _line_push(
            uid,
            "抽出した予約内容 (先頭5件):\n" + preview +
            "\n\nこの内容でスプレッドシートに追記してよろしいですか？ (はい / いいえ)",
        )
        st["step"] = "confirm_reservations"
    except Exception as e:
        print("[filled image error]", e)
        _line_push(uid, "画像解析中にエラーが発生しました。再度お試しください。")

# -------------------------------------------------------------
# Webhook ハンドラー
# -------------------------------------------------------------

@app.route("/", methods=["POST"])
def webhook():
    body = request.get_json()
    for e in body.get("events", []):
        handle_event(e)
    return "OK"

def _init_state(uid: str):
    user_state[uid] = {
        "step": "wait_store_name",
        "store_name": "",
        "store_id": random.randint(100000, 999999),
        "seat_info": "",
        "times": [],
        "sheet_url": "",
        "pending_rows": [],
    }

def handle_event(event):
    uid   = event["source"]["userId"]
    mtype = event["message"]["type"]
    token = event["replyToken"]

    if uid not in user_state:
        _init_state(uid)
    st = user_state[uid]

    # ---- 1. 店舗名入力 ----
    if st["step"] == "wait_store_name" and mtype == "text":
        st["store_name"] = event["message"]["text"].strip()
        _line_reply(token, f"店舗名: {st['store_name']}\n店舗ID: {st['store_id']}\nこの内容でよろしいですか？ (はい/いいえ)")
        st["step"] = "confirm_store_name"
        return

    if st["step"] == "confirm_store_name" and mtype == "text":
        if event["message"]["text"].strip() == "はい":
            _line_reply(token, "座席数を入力してください (例: 1人席:3\n2人席:2\n4人席:1)")
            st["step"] = "wait_seat"
        else:
            _line_reply(token, "店舗名を入力し直してください。")
            st["step"] = "wait_store_name"
        return

    # ---- 2. 座席数 ----
    if st["step"] == "wait_seat" and mtype == "text":
        st["seat_info"] = event["message"]["text"].strip()
        _line_reply(token, "空の予約表テンプレート画像を送ってください。\n時間帯を自動で抽出します。")
        st["step"] = "wait_template_img"
        return

    # ---- 3. テンプレート画像 ----
    if st["step"] == "wait_template_img" and mtype == "image":
        threading.Thread(target=_process_template_image, args=(uid, event["message"]["id"])).start()
        _line_reply(token, "🖼️ 画像を受け取りました。解析中です…")
        return

    # ---- 3.5 時間枠確認 ----
    if st["step"] == "confirm_times" and mtype == "text":
        txt = event["message"]["text"].strip()
        if txt == "はい":
            st["sheet_url"] = create_store_sheet(st["store_name"], st["store_id"], st["seat_info"], st["times"])
            _line_reply(token, f"✅ スプレッドシートを作成しました！\n{st['sheet_url']}\n\n記入済み予約表の写真を送ってください。")
            st["step"] = "wait_filled_img"
        else:
            _line_reply(token, "修正点をご記入いただくか、再度テンプレート画像を送ってください。")
            st["step"] = "wait_template_img"
        return

    # ---- 4. 記入済み画像 ----
    if st["step"] == "wait_filled_img" and mtype == "image":
        threading.Thread(target=_process_filled_image, args=(uid, event["message"]["id"])).start()
        _line_reply(token, "📸 画像を受け取りました。予約内容を抽出しています…")
        return

    # ---- 5. 抽出結果確認 ----
    if st["step"] == "confirm_reservations" and mtype == "text":
        txt = event["message"]["text"].strip()
        if txt == "はい":
            append_reservations(st["sheet_url"], st["pending_rows"])
            _line_reply(token, "✅ 予約内容をスプレッドシートに追記しました！ ご確認ください。")
            st["step"] = "done"
        else:
            _line_reply(token, "わかりました。もう一度記入済み予約表の画像を送ってください。")
            st["step"] = "wait_filled_img"
        return

    # ---- その他 ----
    if mtype == "text" and st["step"] == "done":
        _line_reply(token, "登録は完了しています。追加の予約表画像を送ると自動で追記します。")
        return

    # フォールバック
    if mtype == "text":
        _line_reply(token, "メニュー:\n1. 最初からやり直す場合は『リセット』と入力してください。")
        if event["message"]["text"].strip() == "リセット":
            _init_state(uid)
    else:
        _line_reply(token, "画像またはテキストを送信してください。")

# -------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
