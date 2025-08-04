# LINE予約管理BOT (Google Sheets 連携 + GPT-4o 画像解析)
# -------------------------------------------------------------
#   1. 店舗登録（店舗名・ID・座席数）
#   2. 空の予約表テンプレ画像を解析し時間枠を抽出
#   3. 時間枠を使って店舗専用スプレッドシートを自動生成
#   4. 記入済み予約表画像を解析し "当日" シートに追記
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
    # 全員編集可リンク（必要に応じて権限は調整してください）
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
    # "時間帯" 列インデックス (1-based)
    col_idx = header.index("時間帯") + 1 if "時間帯" in header else 3
    existing = {ws.cell(r, col_idx).value: r for r in range(2, ws.row_count + 1) if ws.cell(r, col_idx).value}
    for r in rows:
        tgt = existing.get(r.get("time")) or ws.row_count + 1
        ws.update(
            f"A{tgt}:F{tgt}",
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
# Vision 解析ユーティリティ（GPT-4o）
# -------------------------------------------------------------

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
    return client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=0.0,
    )


def _vision_extract_times(img: bytes) -> List[str]:
    b64 = base64.b64encode(img).decode()
    task = "画像は空欄の飲食店予約表です。予約可能な時間帯 (HH:MM) をすべて昇順で JSON 配列として返してください。"
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
        if not times:
            _line_push(uid, "画像の時間帯を検出できませんでした。もう一度、なるべく鮮明な画像を送ってください。")
            return
        st["times"] = times
        times_view = "\n".join(f"・{t}〜" for t in times)
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
            "異なる点がある場合は、「いいえ」とご返信のうえ、修正点をご連絡ください。"
        )
        st["step"] = "confirm_structure"
    except Exception:
        _line_push(uid, "画像の解析に失敗しました。もう一度、なるべく鮮明な画像を送ってください。")


def _process_filled_image(uid: str, message_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_filled_img":
        return
    try:
        img = _download_line_image(message_id)
        rows = _vision_extract_rows(img)
        if not rows:
            _line_push(uid, "画像の解析に失敗しました。もう一度、なるべく鮮明な画像を送ってください。")
            return
        append_reservations(st["sheet_url"], rows)
        _line_push(uid, "✅ 予約内容をスプレッドシートに記録しました！")
        st["step"] = "done"
    except Exception:
        _line_push(uid, "画像の解析に失敗しました。もう一度、なるべく鮮明な画像を送ってください。")

# -------------------------------------------------------------
# Flask Webhook
# -------------------------------------------------------------

@app.route("/", methods=["POST"])
def webhook():
    body = request.get_json()
    for e in body.get("events", []):
        threading.Thread(target=_handle_event, args=(e,)).start()
    return "OK"

# -------------------------------------------------------------
# イベントハンドラー
# -------------------------------------------------------------

def _handle_event(event: Dict[str, Any]):
    uid        = event["source"]["userId"]
    msg_type   = event["message"]["type"]
    reply_tok  = event["replyToken"]
    user_msg   = ""
    if msg_type == "text":
        user_msg = event["message"]["text"].strip()
    elif msg_type == "image":
        user_msg = ""
    else:
        _line_reply(reply_tok, "テキストか画像を送信してください。")
        return

    st = user_state.setdefault(uid, {"step": "ask_store_name"})

    # -----------------------------------------------------
    # 店舗登録フロー
    # -----------------------------------------------------
    if st["step"] == "ask_store_name":
        if user_msg:
            st["store_name"] = user_msg
            st["store_id"]   = random.randint(100000, 999999)
            _line_reply(
                reply_tok,
                f"店舗名: {st['store_name']} です。\n"
                f"店舗ID: {st['store_id']}\n"
                "この内容でよろしいですか？（はい/いいえ）",
            )
            st["step"] = "confirm_store"
        else:
            _line_reply(reply_tok, "店舗名を教えてください。")

    elif st["step"] == "confirm_store":
        if user_msg == "はい":
            st["step"] = "ask_seats"
            _line_reply(
                reply_tok,
                "座席数を入力してください。\n例：1人席:3\n2人席:2\n4人席:1",
            )
        elif user_msg == "いいえ":
            st["step"] = "ask_store_name"
            _line_reply(reply_tok, "もう一度、店舗名を入力してください。")
        else:
            _line_reply(reply_tok, "「はい」または「いいえ」でお答えください。")

    elif st["step"] == "ask_seats":
        if user_msg:
            st["seats"] = user_msg
            _line_reply(
                reply_tok,
                "空の予約表テンプレート画像を送ってください。\n時間帯を自動で抽出します。",
            )
            st["step"] = "wait_template_img"
        else:
            _line_reply(reply_tok, "座席数を入力してください。")

    # -----------------------------------------------------
    # 画像テンプレ解析フロー
    # -----------------------------------------------------
    elif msg_type == "image" and st["step"] == "wait_template_img":
        _line_reply(reply_tok, "🖼️ 画像を受け取りました。解析中です…")
        threading.Thread(target=_process_template_image, args=(uid, event["message"]["id"])).start()

    elif st["step"] == "confirm_structure":
        if user_msg == "はい":
            st["sheet_url"] = create_store_sheet(
                st["store_name"],
                st["store_id"],
                st["seats"],
                st.get("times", []),
            )
            _line_reply(
                reply_tok,
                "✅ スプレッドシートを作成しました！\n\n"
                f"{st['sheet_url']}\n\n"
                "今後は記入済みの予約表を写真で送っていただくと、予約内容を自動で転記します。"
            )
            st["step"] = "wait_filled_img"
        elif user_msg == "いいえ":
            st["step"] = "wait_template_img"
            _line_reply(reply_tok, "修正したい予約表テンプレート画像を再度お送りください。")
        else:
            _line_reply(reply_tok, "「はい」または「いいえ」でお答えください。")

    # -----------------------------------------------------
    # 記入済み予約表画像フロー
    # -----------------------------------------------------
    elif msg_type == "image" and st["step"] == "wait_filled_img":
        _line_reply(reply_tok, "🖼️ 画像を受け取りました。解析中です…")
        threading.Thread(target=_process_filled_image, args=(uid, event["message"]["id"])).start()

    elif st["step"] == "wait_filled_img":
        _line_reply(reply_tok, "予約表画像をご送信ください。")

    else:
        _line_reply(reply_tok, "現在のフローで処理できない入力です。")

# -------------------------------------------------------------
# アプリ起動
# -------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
