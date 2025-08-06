# -------------------------------------------------------------
# LINE 予約管理 BOT 〈Gemini 版〉
#   1. 店舗登録（店舗名・ID・座席数）
#   2. 予約表テンプレ画像の解析（時間枠抽出）
#   3. Google Sheets に店舗専用シートを自動生成
#   4. 記入済み画像を解析し当日シートへ追記
# -------------------------------------------------------------
"""
必要な Render 環境変数
----------------------------------------------------------------
GEMINI_API_KEY               : Google AI Studio で発行したキー
LINE_CHANNEL_ACCESS_TOKEN    : LINE Messaging API アクセストークン
GOOGLE_CREDENTIALS_JSON      : サービスアカウント JSON 全文（1行）
MASTER_SHEET_NAME（任意）     : 店舗一覧シート名（デフォルト '契約店舗一覧'）
"""
from __future__ import annotations

# ---------- 標準 / 外部ライブラリ ----------
import base64
import datetime as dt
import json
import os
import random
import threading
from typing import Any, Dict, List

import requests
import gspread
from flask import Flask, request
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
import google.generativeai as genai      # ★ Gemini SDK

# ---------- Flask & 基本設定 ----------
app = Flask(__name__)
load_dotenv()

# --- LINE & Gemini キー読み込み ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY            = os.getenv("GEMINI_API_KEY")
MASTER_SHEET_NAME         = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (LINE_CHANNEL_ACCESS_TOKEN and GEMINI_API_KEY):
    raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN と GEMINI_API_KEY を必ず設定してください")

genai.configure(api_key=GEMINI_API_KEY)
model_chat   = genai.GenerativeModel("gemini-pro")        # テキスト用
model_vision = genai.GenerativeModel("gemini-pro-vision") # 画像用（試用枠内で利用可）

user_state: Dict[str, Dict[str, Any]] = {}

# ---------- Google Sheets 認証 ----------
def _load_sa(scopes: List[str]):
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("環境変数 GOOGLE_CREDENTIALS_JSON がありません")
    return ServiceAccountCredentials.from_json_keyfile_dict(json.loads(raw), scopes)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
gs_client = gspread.authorize(_load_sa(SCOPES))

def _get_master_ws():
    try:
        sh = gs_client.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs_client.create(MASTER_SHEET_NAME)
        sh.sheet1.append_row(["店舗名", "店舗ID", "座席数", "シートURL", "登録日時", "時間枠"])
    return sh.sheet1

# ---------- LINE ユーティリティ ----------
def _line_reply(token: str, text: str):
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {"replyToken": token, "messages": [{"type": "text", "text": text}]}
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body, timeout=10)

def _line_push(uid: str, text: str):
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {"to": uid, "messages": [{"type": "text", "text": text}]}
    requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=body, timeout=10)

# ---------- Sheets 操作 ----------
def create_store_sheet(store: str, store_id: int, seats: str, times: List[str]) -> str:
    sh = gs_client.create(f"予約表 - {store} ({store_id})")
    sh.share(None, perm_type="anyone", role="writer")  # 任意で変更
    ws = sh.sheet1
    ws.update([["月", "日", "時間帯", "名前", "人数", "備考"]])
    if times:
        ws.append_rows([["", "", t, "", "", ""] for t in times])
    _get_master_ws().append_row([
        store, store_id, seats.replace("\n", " "),
        sh.url, dt.datetime.now().isoformat(timespec="seconds"),
        ",".join(times)
    ])
    return sh.url

def append_reservations(sheet_url: str, rows: List[Dict[str, Any]]):
    if not rows:
        return
    ws = gs_client.open_by_url(sheet_url).sheet1
    header = ws.row_values(1)
    col_time = header.index("時間帯") + 1
    exist = {ws.cell(r, col_time).value: r for r in range(2, ws.row_count + 1)
             if ws.cell(r, col_time).value}
    for r in rows:
        idx = exist.get(r["time"]) or ws.row_count + 1
        ws.update(
            f"A{idx}:F{idx}",
            [[r.get(k, "") for k in ("month", "day", "time", "name", "size", "note")]],
        )

# ---------- LINE 画像ダウンロード ----------
def _line_image_blob(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()
    return res.content

# ---------- Gemini Vision 解析 ----------
def _vision_times(img: bytes) -> List[str]:
    b64 = base64.b64encode(img).decode()
    prompt = (
        "画像は空欄の飲食店予約表です。予約可能な時間帯（HH:MM）を左上→右下の順で重複なく昇順 JSON 配列で返してください。"
    )
    res = model_vision.generate_content(
        [
            prompt,
            genai.types.upload_pb2.FileData(mime_type="image/jpeg", data=img)
        ]
    )
    try:
        return [str(t) for t in json.loads(res.text)]
    except Exception:
        return []

def _vision_rows(img: bytes) -> List[Dict[str, Any]]:
    prompt = (
        "画像は手書きで記入済みの予約表です。各行を次の JSON 配列形式で返してください："
        '[{"month":int,"day":int,"time":"HH:MM","name":"...","size":int,"note":"..."}]'
    )
    res = model_vision.generate_content(
        [
            prompt,
            genai.types.upload_pb2.FileData(mime_type="image/jpeg", data=img)
        ]
    )
    try:
        return json.loads(res.text)
    except Exception:
        return []

# ---------- 画像解析処理スレッド ----------
def _process_template(uid: str, message_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_template":
        return
    try:
        img   = _line_image_blob(message_id)
        times = _vision_times(img)
        if not times:
            _line_push(uid, "画像の解析に失敗しました。もう一度鮮明な『空欄の予約表』画像を送ってください。")
            return
        st["times"] = times
        st["step"]  = "confirm_times"
        _line_push(uid,
            "📊 予約表の時間枠を検出しました！\n\n" +
            "\n".join(f"・{t}〜" for t in times) +
            "\n\nこの内容でスプレッドシートを作成してよろしいですか？（はい／いいえ）")
    except Exception as e:
        print("[template-img error]", e)
        _line_push(uid, "画像解析中にエラーが発生しました。再度お試しください。")

def _process_filled(uid: str, message_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_filled":
        return
    try:
        img  = _line_image_blob(message_id)
        rows = _vision_rows(img)
        if not rows:
            _line_push(uid, "予約内容を検出できませんでした。もう一度鮮明な画像を送ってください。")
            return
        append_reservations(st["sheet_url"], rows)
        _line_push(uid, "✅ 予約情報をスプレッドシートに追記しました！")
    except Exception as e:
        print("[filled-img error]", e)
        _line_push(uid, "画像解析中にエラーが発生しました。再度お試しください。")

# ---------- Webhook ----------
@app.route("/", methods=["GET", "HEAD", "POST"])
def webhook():
    if request.method in {"GET", "HEAD"}:
        return "OK", 200
    evt = request.get_json().get("events", [])
    if not evt:
        return "NOEVENT", 200
    threading.Thread(target=_handle_event, args=(evt[0],)).start()
    return "OK", 200

def _handle_event(ev: Dict[str, Any]):
    try:
        if ev["type"] != "message":
            return
        uid    = ev["source"]["userId"]
        token  = ev["replyToken"]
        mtype  = ev["message"]["type"]
        text   = ev["message"].get("text", "")
        mid    = ev["message"].get("id")

        st = user_state.setdefault(uid, {"step": "start"})

        # ---------- TEXT ----------
        if mtype == "text":
            step = st["step"]

            if step == "start":
                prompt = f"以下の文から店舗名だけを抽出してください：\n{text}"
                store  = model_chat.generate_content(prompt).text.strip()
                st.update({"step": "confirm_store", "store": store, "sid": random.randint(100000, 999999)})
                _line_reply(token,
                    f"登録完了：店舗名：{store} 店舗ID：{st['sid']}\n\n"
                    "この内容で間違いないですか？「はい」「いいえ」でお答えください。")
                return

            if step == "confirm_store":
                if "はい" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "次に座席数を教えてください。\n例：「1人席:3、2人席:2、4人席:1」")
                elif "いいえ" in text:
                    st.clear(); st["step"] = "start"
                    _line_reply(token, "もう一度店舗名を送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            if step == "ask_seats":
                prev = st.get("seats", "")
                prompt = (
                    "以下の文と前回の座席数を踏まえ、1人席・2人席・4人席の数を抽出し\n"
                    "1人席：◯席\n2人席：◯席\n4人席：◯席\nの形式で返してください。\n\n"
                    f"文：{text}\n前回：{prev}"
                )
                seats = model_chat.generate_content(prompt).text.strip()
                st.update({"seats": seats, "step": "confirm_seats"})
                _line_reply(token,
                    f"以下の座席数で登録してよいですか？\n\n{seats}\n\n「はい」「いいえ」でお答えください。")
                return

            if step == "confirm_seats":
                if "はい" in text:
                    st["step"] = "wait_template"
                    _line_reply(token,
                        "ありがとうございます！店舗登録が完了しました🎉\n\n"
                        "空欄の予約表（テンプレート）写真を送ってください。AIが時間枠を学習します。")
                elif "いいえ" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "もう一度座席数を入力してください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            if step == "confirm_times":
                if "はい" in text:
                    url = create_store_sheet(st["store"], st["sid"], st["seats"], st["times"])
                    st.update({"sheet_url": url, "step": "wait_filled"})
                    _line_reply(token,
                        f"スプレッドシートを作成しました！\n{url}\n\n"
                        "当日の予約を書き込んだ紙の写真を送っていただくと、自動で追記します。")
                elif "いいえ" in text:
                    st["step"] = "wait_template"
                    _line_reply(token, "わかりました。もう一度テンプレ画像を送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

        # ---------- IMAGE ----------
        if mtype == "image":
            if st["step"] == "wait_template":
                threading.Thread(target=_process_template, args=(uid, mid)).start()
                _line_reply(token, "画像を受信しました。AIが解析中です。少々お待ちください…")
                return
            if st["step"] == "wait_filled":
                threading.Thread(target=_process_filled, args=(uid, mid)).start()
                _line_reply(token, "画像を受信しました。AIが予約内容を読み取っています。少々お待ちください…")
                return
            _line_reply(token, "画像を受信しましたが、現在は画像解析の準備ができていません。")
            return

    except Exception as e:
        print("[handle_event] error:", e)
        try:
            _line_reply(ev.get("replyToken", ""), "サーバーでエラーが発生しました。再度お試しください。")
        except Exception:
            pass

# ---------- アプリ起動 ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
