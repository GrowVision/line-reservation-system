# LINE予約管理BOT (Google Sheets 連携 + GPT-4o 画像解析 + Gemini テキスト解析)
# ---------------------------------------------------------------------
#   1. 店舗登録（店舗名・ID・座席数）
#   2. 空の予約表テンプレ画像を解析し時間帯を抽出
#   3. 時間帯をもとに店舗専用シートを自動生成
#   4. 記入済み予約表画像を解析し "当日" シートに追記
# ---------------------------------------------------------------------
"""
必要な環境変数（Render の Environment Variables で設定）
----------------------------------------------------------------
OPENAI_API_KEY            : OpenAI GPT-4o の API キー
GEMINI_API_KEY            : Google Gemini の API キー
LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging API のアクセストークン
GOOGLE_SERVICE_ACCOUNT    : サービスアカウント JSON 全文（1 行）
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

# --- 外部ライブラリ ---------------------------------------------------
import google.generativeai as genai
import gspread
import requests
from dotenv import load_dotenv
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI

# ---------------------------------------------------------------------
# 初期設定
# ---------------------------------------------------------------------

app = Flask(__name__)
load_dotenv()

# ▶ Gemini テキスト用
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# ▶ GPT-4o 画像用
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ▶ LINE
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# ▶ Google Sheets
MASTER_SHEET_NAME = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (OPENAI_API_KEY and LINE_CHANNEL_ACCESS_TOKEN and GEMINI_API_KEY):
    raise RuntimeError("API キーが不足しています。環境変数を確認してください。")

# ユーザー状態
user_state: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------
# Google Sheets ユーティリティ
# ---------------------------------------------------------------------

def _load_service_account(scope: List[str]):
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT が設定されていません")
    info = json.loads(raw)
    return ServiceAccountCredentials.from_json_keyfile_dict(info, scope)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
gs_client = gspread.authorize(_load_service_account(SCOPES))

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
        store_name, store_id, seat_info.replace("\n", " "),
        sh.url, dt.datetime.now().isoformat(timespec="seconds"),
        ",".join(times)
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
        tgt = existing.get(r.get("time")) or ws.row_count + 1
        ws.update(
            f"A{tgt}:F{tgt}",
            [[r.get(k, "") for k in ("month", "day", "time", "name", "size", "note")]],
        )

# ---------------------------------------------------------------------
# LINE API ユーティリティ
# ---------------------------------------------------------------------

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

def _line_push(uid: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    requests.post(url, headers=headers, json={
        "to": uid,
        "messages": [{"type": "text", "text": text}]
    }, timeout=10)

# ---------------------------------------------------------------------
# GPT-4o Vision ユーティリティ
# ---------------------------------------------------------------------

def _download_line_image(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.content

def _make_image_prompt(img_b64: str, task: str):
    return [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": task},
        ],
    }]

def _vision_request(messages: List[Dict[str, Any]], max_tokens: int = 512):
    return openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.0,
    )

def _vision_extract_times(img: bytes) -> List[str]:
    b64 = base64.b64encode(img).decode()
    task = (
        "画像は空欄の飲食店予約表です。"
        "予約可能な時間帯 (HH:MM) を、左上→右下の順にすべて抽出し、"
        "重複なく昇順で JSON 配列として返してください。"
    )
    res = _vision_request(_make_image_prompt(b64, task), 256)
    try:
        data = json.loads(res.choices[0].message.content)
        return [str(t) for t in data] if isinstance(data, list) else []
    except Exception:
        return []

def _vision_extract_rows(img: bytes) -> List[Dict[str, Any]]:
    b64 = base64.b64encode(img).decode()
    task = (
        "画像は手書きの予約表です。各行の予約情報を JSON 配列で返してください。"
        "形式: [{\"month\":int,\"day\":int,\"time\":\"HH:MM\",\"name\":str,\"size\":int,\"note\":str}]"
    )
    res = _vision_request(_make_image_prompt(b64, task), 1024)
    try:
        data = json.loads(res.choices[0].message.content)
        return data if isinstance(data, list) else []
    except Exception:
        return []

# ---------------------------------------------------------------------
# 背景スレッド処理
# ---------------------------------------------------------------------

def _process_template_image(uid: str, message_id: str):
    st = user_state.get(uid)
    if not st or st.get("step") != "wait_template_img":
        return
    try:
        img = _download_line_image(message_id)
        times = _vision_extract_times(img)
        if not times:
            _line_push(uid, "画像の解析に失敗しました。もう一度、鮮明な “空欄の予約表” 画像を送ってください。")
            return
        st["times"] = times
        st["step"] = "confirm_times"
        times_msg = "\n".join(f"・{t}〜" for t in times)
        _line_push(uid,
            "📊 予約表構造の分析が完了しました！\n\n"
            "画像を分析した結果、以下の時間帯が検出されました：\n\n"
            "───────────────\n\n"
            f"{times_msg}\n\n"
            "───────────────\n\n"
            "この内容でスプレッドシートを作成してよろしいですか？（はい／いいえ）")
    except Exception as e:
        print("[template image error]", e)
        _line_push(uid, "画像解析中にエラーが発生しました。もう一度お試しください。")

def _process_filled_image(uid: str, message_id: str):
    st = user_state.get(uid)
    if not st or st.get("step") != "wait_filled_img":
        return
    try:
        img = _download_line_image(message_id)
        rows = _vision_extract_rows(img)
        if not rows:
            _line_push(uid, "予約情報を検出できませんでした。もう一度、鮮明な画像を送ってください。")
            return
        append_reservations(st["sheet_url"], rows)
        _line_push(uid, "✅ 予約情報をスプレッドシートに追記しました！ありがとうございます。")
    except Exception as e:
        print("[filled image error]", e)
        _line_push(uid, "画像解析中にエラーが発生しました。もう一度お試しください。")

# ---------------------------------------------------------------------
# Flask Webhook
# ---------------------------------------------------------------------

@app.route("/", methods=["GET", "HEAD", "POST"])
def webhook():
    if request.method in {"GET", "HEAD"}:
        return "OK", 200
    body = request.get_json()
    if not body.get("events"):
        return "NOEVENT", 200
    threading.Thread(target=_handle_event, args=(body["events"][0],)).start()
    return "OK", 200

def _handle_event(event: Dict[str, Any]):
    try:
        if event["type"] != "message":
            return
        uid        = event["source"]["userId"]
        token      = event["replyToken"]
        msg_type   = event["message"]["type"]
        text       = event["message"].get("text", "")
        message_id = event["message"].get("id")

        st = user_state.setdefault(uid, {"step": "start"})
        step = st["step"]

        # ---------------- TEXT ----------------
        if msg_type == "text":

            # 1️⃣ 店舗名抽出（Gemini）
            if step == "start":
                prompt = f"以下の文から店舗名だけを抽出してください：\n{text}"
                model  = genai.GenerativeModel("gemini-pro")
                resp   = model.generate_content(prompt)
                store_name = resp.text.strip()

                store_id = random.randint(100000, 999999)
                st.update({"step": "confirm_store", "store_name": store_name, "store_id": store_id})
                _line_reply(token,
                    f"店舗名: {store_name} です。これで登録します。\n"
                    f"店舗ID: {store_id}\n"
                    "この内容でよろしいですか？（はい／いいえ）")
                return

            # 2️⃣ 店舗名確認
            if step == "confirm_store":
                if "はい" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "座席数を入力してください (例: 1人席:3 2人席:2 4人席:1)")
                elif "いいえ" in text:
                    st.clear()
                    st["step"] = "start"
                    _line_reply(token, "店舗名をもう一度入力してください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            # 3️⃣ 座席数抽出（Gemini）
            if step == "ask_seats":
                prev = st.get("seat_info", "")
                prompt = (
                    "以下の文と、前に入力された座席数「{prev}」を参考に "
                    "1人席・2人席・4人席の数を抽出し、必ず次の形式で答えてください：\n"
                    "1人席：◯席\n2人席：◯席\n4人席：◯席\n\n"
                    f"文：{text}"
                )
                model = genai.GenerativeModel("gemini-pro")
                resp  = model.generate_content(prompt)
                seat_info = resp.text.strip()

                st["seat_info"] = seat_info
                st["step"]      = "confirm_seats"
                _line_reply(token,
                    "✅ 登録情報の確認です：\n\n"
                    f"- 店舗名：{st['store_name']}\n"
                    f"- 店舗ID：{st['store_id']}\n"
                    f"- 座席数：\n{seat_info}\n\n"
                    "この内容で登録してよろしいですか？\n\n「はい」「いいえ」でお答えください。")
                return

            # 4️⃣ 座席数確認
            if step == "confirm_seats":
                if "はい" in text:
                    st["step"] = "wait_template_img"
                    _line_reply(token,
                        "ありがとうございます！店舗登録が完了しました🎉\n\n"
                        "まず、空欄の予約表画像を送ってください。\n"
                        "AI がフォーマットを学習し、スプレッドシートを作成します。")
                elif "いいえ" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "もう一度、座席数を入力してください。(例: 1人席:3 2人席:2 4人席:1)")
                else:
                    _line_reply(token, "座席数が正しいか「はい」または「いいえ」でお答えください。")
                return

            # 5️⃣ 時間帯確認
            if step == "confirm_times":
                if "はい" in text:
                    sheet_url = create_store_sheet(st["store_name"], st["store_id"], st["seat_info"], st["times"])
                    st["sheet_url"] = sheet_url
                    st["step"] = "wait_filled_img"
                    _line_reply(token,
                        "スプレッドシートを作成しました！\n"
                        f"📄 {sheet_url}\n\n"
                        "当日の予約を書き込んだ紙の写真を送っていただくと、自動でスプレッドシートに追記します。")
                elif "いいえ" in text:
                    st["step"] = "wait_template_img"
                    _line_reply(token, "わかりました。もう一度、空欄の予約表画像を送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

        # -------------- IMAGE --------------
        if msg_type == "image":
            if step == "wait_template_img":
                threading.Thread(target=_process_template_image, args=(uid, message_id)).start()
                _line_reply(token, "予約表画像を受信しました。AI がフォーマットを解析中です。少々お待ちください…")
                return
            if step == "wait_filled_img":
                threading.Thread(target=_process_filled_image, args=(uid, message_id)).start()
                _line_reply(token, "画像を受信しました。AI が予約内容を読み取っています。少々お待ちください…")
                return
            _line_reply(token, "画像を受信しましたが、現在は画像解析の準備ができていません。")
            return

    except Exception as e:
        print("[handle_event error]", e)
        try:
            _line_reply(event.get("replyToken", ""), "エラーが発生しました。もう一度お試しください。")
        except Exception:
            pass

# ---------------------------------------------------------------------
# アプリ起動
# ---------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
