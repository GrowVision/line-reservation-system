# -------------------------------------------------------------
#  LINE 予約管理 BOT  (Gemini + Google Sheets)
#  1. 店舗登録       2. 空予約表テンプレ画像→時間枠抽出
#  3. スプレッドシート自動生成  4. 記入済み画像→当日シート追記
# -------------------------------------------------------------
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

# -------------------------------------------------------------
#   環境変数
# -------------------------------------------------------------
load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY            = os.getenv("GEMINI_API_KEY")
MASTER_SHEET_NAME         = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (LINE_CHANNEL_ACCESS_TOKEN and GEMINI_API_KEY):
    raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN または GEMINI_API_KEY が未設定です")

genai.configure(api_key=GEMINI_API_KEY)

# -------------------------------------------------------------
#   Flask アプリ
# -------------------------------------------------------------
app = Flask(__name__)
user_state: Dict[str, Dict[str, Any]] = {}

# -------------------------------------------------------------
#   Google Sheets 認証
# -------------------------------------------------------------
def _load_service_account(scope: List[str]):
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT")
    if not raw:
        raise RuntimeError("サービスアカウント JSON が環境変数に設定されていません")
    return ServiceAccountCredentials.from_json_keyfile_dict(json.loads(raw), scope)

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
    sh.share(None, perm_type="anyone", role="writer")          # 必要なら権限制御を調整
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
        tgt = existing.get(r.get("time")) or ws.row_count + 1
        ws.update(
            f"A{tgt}:F{tgt}",
            [[r.get(k, "") for k in ("month", "day", "time", "name", "size", "note")]],
        )

# -------------------------------------------------------------
#   LINE 送受信ユーティリティ
# -------------------------------------------------------------
def _line_reply(token: str, text: str):
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=headers,
        json={"replyToken": token, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )

def _line_push(uid: str, text: str):
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=headers,
        json={"to": uid, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )

def _download_line_image(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.content

# -------------------------------------------------------------
#   Gemini Vision ラッパ
# -------------------------------------------------------------
def _vision_request(img_b64: str, prompt: str, max_tokens: int = 2048):
    model = genai.GenerativeModel("gemini-pro-vision")
    res = model.generate_content(
        [
            {"type": "image", "data": img_b64, "mime_type": "image/jpeg"},
            {"type": "text",  "text": prompt}
        ],
        generation_config={"max_output_tokens": max_tokens}
    )
    return res.text           # → str

def _vision_extract_times(img: bytes) -> List[str]:
    task = (
        "画像は空欄の飲食店予約表です。予約可能な時間帯 (HH:MM) を、"
        "左上→右下の順で重複なく抽出し、JSON 配列として返してください。"
    )
    try:
        data = json.loads(_vision_request(base64.b64encode(img).decode(), task, 512))
        return [str(t) for t in data] if isinstance(data, list) else []
    except Exception:
        return []

def _vision_extract_rows(img: bytes) -> List[Dict[str, Any]]:
    task = (
        "画像は手書きの予約表です。各行の予約情報を次の JSON 配列で返してください。\n"
        '例: [{"month":8,"day":6,"time":"18:00","name":"山田","size":2,"note":""}]'
    )
    try:
        data = json.loads(_vision_request(base64.b64encode(img).decode(), task, 1024))
        return data if isinstance(data, list) else []
    except Exception:
        return []

# -------------------------------------------------------------
#   画像処理スレッド
# -------------------------------------------------------------
def _process_template_image(uid: str, mid: str):
    try:
        img   = _download_line_image(mid)
        times = _vision_extract_times(img)
        if not times:
            _line_push(uid, "画像の解析に失敗しました。もう一度、鮮明な『空欄の予約表』の写真を送ってください。")
            return
        st = user_state[uid]
        st.update({"times": times, "step": "confirm_times"})
        _line_push(
            uid,
            "📊 予約表構造の分析が完了しました！\n\n"
            "検出した時間帯：\n" + "\n".join(f"・{t}" for t in times) +
            "\n\nこの内容でスプレッドシートを作成してよろしいですか？（はい／いいえ）"
        )
    except Exception as e:
        traceback.print_exc()
        _line_push(uid, "画像解析中にエラーが発生しました。再度お試しください。")

def _process_filled_image(uid: str, mid: str):
    try:
        img  = _download_line_image(mid)
        rows = _vision_extract_rows(img)
        if not rows:
            _line_push(uid, "予約情報が検出できませんでした。もう一度、鮮明な画像を送ってください。")
            return
        append_reservations(user_state[uid]["sheet_url"], rows)
        _line_push(uid, "✅ 予約情報をスプレッドシートに追記しました！")
    except Exception:
        traceback.print_exc()
        _line_push(uid, "画像解析中にエラーが発生しました。再度お試しください。")

# -------------------------------------------------------------
#   Webhook エンドポイント
# -------------------------------------------------------------
@app.route("/", methods=["GET", "HEAD", "POST"])
def webhook():
    if request.method in {"GET", "HEAD"}:
        return "OK", 200
    body = request.get_json()
    if not body.get("events"):
        return "NOEVENT", 200
    threading.Thread(target=_handle_event, args=(body["events"][0],)).start()
    return "OK", 200

# -------------------------------------------------------------
#   イベントハンドラ
# -------------------------------------------------------------
def _handle_event(event: Dict[str, Any]):
    try:
        if event["type"] != "message":
            return
        uid      = event["source"]["userId"]
        token    = event["replyToken"]
        msg_type = event["message"]["type"]
        text     = event["message"].get("text", "")
        mid      = event["message"].get("id")

        state = user_state.setdefault(uid, {"step": "start"})
        step  = state["step"]

        # ---------------- TEXT ----------------
        if msg_type == "text":
            # ① 店舗名登録
            if step == "start":
                prompt   = f"以下の文から店舗名だけを抽出してください：\n{text}"
                store_name = genai.GenerativeModel("gemini-pro").generate_content(prompt).text.strip()
                store_id   = random.randint(100000, 999999)
                state.update({"step": "confirm_store", "store_name": store_name, "store_id": store_id})
                _line_reply(token,
                    f"登録完了：店舗名：{store_name}\n店舗ID：{store_id}\n\n"
                    "この内容で間違いないですか？（はい／いいえ）")
                return

            # ② 店舗名確認
            if step == "confirm_store":
                if "はい" in text:
                    state["step"] = "ask_seats"
                    _line_reply(token, "座席数を入力してください (例: 1人席:3 2人席:2 4人席:1)")
                elif "いいえ" in text:
                    state.clear(); state["step"] = "start"
                    _line_reply(token, "もう一度、店舗名を送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            # ③ 座席数入力
            if step == "ask_seats":
                prev   = state.get("seat_info", "")
                prompt = (
                    "以下の文と、前に把握している座席数『{prev}』をもとに、\n"
                    "1人席、2人席、4人席の数を抽出して次の形式で答えてください：\n"
                    "1人席：◯席\n2人席：◯席\n4人席：◯席\n\n文：{text}"
                ).format(prev=prev, text=text)
                seat_info = genai.GenerativeModel("gemini-pro").generate_content(prompt).text.strip()
                state.update({"seat_info": seat_info, "step": "confirm_seats"})
                _line_reply(token,
                    "✅ 登録情報の確認です：\n\n"
                    f"店舗名：{state['store_name']}\n店舗ID：{state['store_id']}\n\n"
                    f"{seat_info}\n\nこの内容でよろしいですか？（はい／いいえ）")
                return

            # ④ 座席数確認
            if step == "confirm_seats":
                if "はい" in text:
                    state["step"] = "wait_template_img"
                    _line_reply(token,
                        "ありがとうございます！店舗登録が完了しました🎉\n\n"
                        "まず “空欄” の予約表画像を送ってください。時間枠を解析します。")
                elif "いいえ" in text:
                    state["step"] = "ask_seats"
                    _line_reply(token, "もう一度、座席数を入力してください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            # ⑤ 時間枠確認
            if step == "confirm_times":
                if "はい" in text:
                    sheet_url = create_store_sheet(
                        state["store_name"], state["store_id"], state["seat_info"], state["times"]
                    )
                    state.update({"sheet_url": sheet_url, "step": "wait_filled_img"})
                    _line_reply(token,
                        f"スプレッドシートを作成しました！\n📄 {sheet_url}\n\n"
                        "当日の予約を書き込んだ紙の写真を送ってください。自動で記録します。")
                elif "いいえ" in text:
                    state["step"] = "wait_template_img"
                    _line_reply(token, "わかりました。もう一度、空欄の予約表画像を送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

        # ---------------- IMAGE ----------------
        if msg_type == "image":
            if step == "wait_template_img":
                threading.Thread(target=_process_template_image, args=(uid, mid)).start()
                _line_reply(token, "画像を受信しました。AI がフォーマットを解析中です…")
                return
            if step == "wait_filled_img":
                threading.Thread(target=_process_filled_image, args=(uid, mid)).start()
                _line_reply(token, "画像を受信しました。AI が予約情報を読み取り中です…")
                return
            _line_reply(token, "画像を受信しましたが、現在は画像解析の準備ができていません。")
            return

    except Exception:
        traceback.print_exc()
        try:
            _line_reply(event.get("replyToken", ""), "サーバーでエラーが発生しました。しばらくしてから再試行してください。")
        except Exception:
            pass

# -------------------------------------------------------------
#   アプリ起動
# -------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)
