from __future__ import annotations
import base64
import datetime as dt
import json
import os
import random
import threading
from typing import Any, Dict, List

# 新SDK のインポート
from google import genai
from google.genai import types

import gspread
import requests
from dotenv import load_dotenv
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials


# -------------------------------------------------------------
# 0. 環境変数 & Gemini モデル ID
# -------------------------------------------------------------
load_dotenv()
GEMINI_API_KEY            = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_JSON               = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT")
MASTER_SHEET_NAME         = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (GEMINI_API_KEY and LINE_CHANNEL_ACCESS_TOKEN and GOOGLE_JSON):
    raise RuntimeError(
        "環境変数 GEMINI_API_KEY / LINE_CHANNEL_ACCESS_TOKEN / GOOGLE_CREDENTIALS_JSON を設定してください"
    )

# Gemini モデル ID
MODEL_TEXT   = "models/gemini-1.5-pro-latest"
MODEL_VISION = "models/gemini-1.5-pro-latest"

# 初期設定
# 初期設定
client = genai.Client(api_key=GEMINI_API_KEY)
app = Flask(__name__)
user_state: Dict[str, Dict[str, Any]] = {}

# -------------------------------------------------------------
# Google Sheets 認証
# -------------------------------------------------------------
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_JSON), SCOPES)
gs = gspread.authorize(creds)

def _get_master_ws():
    try:
        sh = gs.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs.create(MASTER_SHEET_NAME)
        sh.sheet1.append_row([
            "店舗名", "店舗ID", "座席数", "シートURL", "登録日時", "時間枠"
        ])
    return sh.sheet1

# -------------------------------------------------------------
# スプレッドシート操作
# -------------------------------------------------------------
def create_store_sheet(name: str, store_id: int, seat_info: str, times: List[str]) -> str:
    sh = gs.create(f"予約表 - {name} ({store_id})")
    sh.share(None, perm_type="anyone", role="writer")
    ws = sh.sheet1
    ws.update([["月", "日", "時間帯", "名前", "人数", "備考"]])
    if times:
        ws.append_rows([["", "", t, "", "", ""] for t in times], value_input_option="USER_ENTERED")
    _get_master_ws().append_row([
        name,
        store_id,
        seat_info.replace("\n", " "),
        sh.url,
        dt.datetime.now().isoformat(timespec="seconds"),
        ",".join(times)
    ])
    return sh.url


def append_reservations(sheet_url: str, rows: List[Dict[str, Any]]) -> None:
    sh = gs.open_by_url(sheet_url)
    ws = sh.sheet1
    values = [[r.get(k, "") for k in ("month","day","time","name","size","note")] for r in rows]
    if values:
        ws.append_rows(values, value_input_option="USER_ENTERED")

# -------------------------------------------------------------
# LINE メッセージ送信
# -------------------------------------------------------------
def _line_reply(token: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"replyToken": token, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )

def _line_push(uid: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"to": uid, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )

# -------------------------------------------------------------
# デバッグヘルパ
# -------------------------------------------------------------
def _log_image_info(img: bytes, label: str):
    print(f"[{label}] img bytes length = {len(img)}")

# -------------------------------------------------------------
# Gemini 呼び出しヘルパ (1.5 Vision 向け修正版)
# -------------------------------------------------------------
def _gemini_text(prompt: str, max_t: int = 256) -> str:
    res = genai.GenerativeModel(MODEL_TEXT).generate_content(
        prompt, generation_config={"max_output_tokens": max_t}
    )
    print(f"[Gemini Text] prompt={prompt}\n-> {res.text}")
    return res.text.strip()

def _gemini_vision(img: bytes, prompt: str, max_t: int = 1024) -> str:
    img_b64 = base64.b64encode(img).decode()
    print(f"[_gemini_vision] Base64 length = {len(img_b64)}")
    contents = [
        {"type": "image", "data": img_b64, "mime_type": "image/jpeg"},
        {"type": "text",  "text": prompt}
    ]
    try:
        res = genai.GenerativeModel(MODEL_VISION).generate_content(
            contents, generation_config={"max_output_tokens": max_t}
        )
        print(f"[Gemini Vision] full response = {res}")
        return res.text.strip()
    except Exception as e:
        print(f"[Gemini Vision] error = {e}")
        raise

# -------------------------------------------------------------
# 画像解析ロジック
# -------------------------------------------------------------
def _vision_describe_sheet(img: bytes) -> str:
    prompt = (
        "画像は、手書きで記入するための予約表です。以下のように簡潔に構成をまとめてください：\n"
        "- 表のタイトル\n"
        "- 日付欄\n"
        "- 列の構成（時間帯、名前、人数、卓番など）\n"
        "- 注意書きの内容\n"
        "- テーブル番号の使い分け"
    )
    try:
        print(f"[_vision_describe_sheet] prompt = {prompt}")
        result = _gemini_vision(img, prompt, 1024)
        print(f"[_vision_describe_sheet] result = {result}")
        return result
    except Exception as e:
        print(f"[_vision_describe_sheet] exception = {e}")
        return "画像解析に失敗しました。もう一度鮮明な画像をお送りください。"

# -------------------------------------------------------------
# 時間帯抽出
# -------------------------------------------------------------
def _vision_extract_times(img: bytes) -> List[str]:
    _log_image_info(img, "_vision_extract_times img")
    prompt = (
        "画像は空欄の飲食店予約表です。予約可能な時間帯 (HH:MM) を、"
        "左上→右下の順に重複なく昇順で JSON 配列として返してください。"
    )
    try:
        data = json.loads(_gemini_vision(img, prompt, 256))
        print(f"[_vision_extract_times] data = {data}")
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[_vision_extract_times] exception = {e}")
        return []

# -------------------------------------------------------------
# 予約行抽出
# -------------------------------------------------------------
def _vision_extract_rows(img: bytes) -> List[Dict[str, Any]]:
    _log_image_info(img, "_vision_extract_rows img")
    prompt = (
        "画像は手書きの予約表です。各行の予約情報を JSON 配列で返してください。"
        "形式: [{\"month\":int,\"day\":int,\"time\":\"HH:MM\",\"name\":str," +
        "\"size\":int,\"note\":str}]"
    )
    try:
        data = json.loads(_gemini_vision(img, prompt, 2048))
        print(f"[_vision_extract_rows] data = {data}")
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[_vision_extract_rows] exception = {e}")
        return []

# -------------------------------------------------------------
# LINE 画像ダウンロード
# -------------------------------------------------------------
def _download_line_img(msg_id: str) -> bytes:
    r = requests.get(
        f"https://api-data.line.me/v2/bot/message/{msg_id}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}, timeout=15
    )
    r.raise_for_status()
    data = r.content
    print(f"[_download_line_img] downloaded bytes = {len(data)}")
    return data

# -------------------------------------------------------------
# 背景スレッド処理
# -------------------------------------------------------------
def _process_template(uid: str, msg_id: str):
    st = user_state.get(uid)
    if not st or st.get("step") != "wait_template_img": return
    img = _download_line_img(msg_id)
    _log_image_info(img, "_process_template img")
    desc = _vision_describe_sheet(img)
    if "失敗しました" in desc:
        _line_push(uid, desc)
        return
    st.update({"template_img": img, "step": "confirm_template"})
    _line_push(uid, f"{desc}\n\nこの内容でスプレッドシートを作成してよろしいですか？（はい／いいえ）")

# -------------------------------------------------------------
# 記入済み画像処理
# -------------------------------------------------------------
def _process_filled(uid: str, msg_id: str):
    st = user_state.get(uid)
    if not st or st.get("step") != "wait_filled_img": return
    img = _download_line_img(msg_id)
    _log_image_info(img, "_process_filled img")
    rows = _vision_extract_rows(img)
    print(f"[_process_filled] rows = {rows}")
    if not rows:
        _line_push(uid, "予約情報が検出できませんでした。鮮明な画像をもう一度お送りください。")
        return
    append_reservations(st["sheet_url"], rows)
    _line_push(uid, "✅ 予約情報をスプレッドシートに追記しました！")

# -------------------------------------------------------------
# メインイベントハンドラ
# -------------------------------------------------------------
def _handle_event(event: Dict[str, Any]):
    try:
        if event.get("type") != "message":
            return
        uid    = event["source"]["userId"]
        token  = event.get("replyToken", "")
        mtype  = event["message"]["type"]
        text   = event["message"].get("text", "")
        msg_id = event["message"].get("id", "")
        st     = user_state.setdefault(uid, {"step": "start"})
        step   = st.get("step")

        # 解析状況問い合わせ対応
        if mtype == "text" and "まだ分析" in text:
            _line_reply(token, "まだ解析中です。しばらくお待ちください。解析できない場合は再度画像を送ってください。")
            return

        # テキストメッセージ処理
        if mtype == "text":
            if step == "start":
                name = _gemini_text(f"以下の文から店舗名だけを抽出してください：\n{text}", 64)
                sid  = random.randint(100000, 999999)
                st.update({"step": "confirm_store", "store_name": name, "store_id": sid})
                _line_reply(token, f"登録完了：店舗名：{name}\n店舗ID：{sid}\nこの内容で間違いないですか？（はい／いいえ）")
                return
            if step == "confirm_store":
                if "はい" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "座席数を入力してください (例: 1人席:3 2人席:2 4人席:1)")
                elif "いいえ" in text:
                    st.clear(); st["step"] = "start"
                    _line_reply(token, "店舗名をもう一度送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」で答えてください。")
                return
            if step == "ask_seats":
                info = _gemini_text(
                    f"以下の文から 1人席, 2人席, 4人席 の数を抽出し、\n1人席:◯席\n2人席:◯席\n4人席:◯席 の形式で出力してください。\n{text}",
                    128
                )
                st.update({"seat_info": info, "step": "confirm_seats"})
                _line_reply(token, f"座席数確認：\n{info}\nこの内容で登録しますか？（はい／いいえ）")
                return
            if step == "confirm_seats":
                if "はい" in text:
                    st["step"] = "wait_template_img"
                    _line_reply(token, "空欄の予約表の写真を送ってください。解析します…")
                elif "いいえ" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "座席数を再度入力してください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」で答えてください。")
                return
        # 画像メッセージ処理
        if mtype == "image":
            if step == "wait_template_img":
                threading.Thread(target=_process_template, args=(uid, msg_id)).start()
                _line_reply(token, "画像受信。解析中です…")
                return
            if step == "wait_filled_img":
                threading.Thread(target=_process_filled, args=(uid, msg_id)).start()
                _line_reply(token, "画像受信。予約情報抽出中…")
                return
            _line_reply(token, "画像受信しましたが、解析できない状態です。")
    except Exception as e:
        print(f"[handle_event error] {e}")
        _line_reply(event.get("replyToken", ""), "エラーが発生しました。もう一度お試しください。")

# -------------------------------------------------------------
# Webhook
# -------------------------------------------------------------
@app.route("/", methods=["GET", "HEAD", "POST"])
def webhook():
    if request.method in {"GET", "HEAD"}:
        return "OK", 200
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("events"):
        return "NOEVENT", 200
    threading.Thread(target=_handle_event, args=(body["events"][0],)).start()
    return "OK", 200

# -------------------------------------------------------------
# アプリ起動
# -------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
