"""
LINE予約管理BOT (Google Sheets 連携 + GPT‑4o 画像解析)
------------------------------------------------------------------
このスクリプトは LINE Bot で受信したメッセージをもとに
店舗登録 ➜ 予約表スプレッドシート生成 ➜ 画像解析で予約行を追記
までをワンストップで行います。

【主要フロー】
1. 店舗名入力 → 座席数入力 → 確認
2. 店舗シート自動生成 & マスターシート追記
3. 予約表画像を GPT‑4o Vision で JSON 抽出
4. シートに append_rows
5. 二段階メッセージ: （完了通知）→（以後の案内）

環境変数:
    OPENAI_API_KEY
    LINE_CHANNEL_ACCESS_TOKEN
    GOOGLE_SERVICE_ACCOUNT            # JSONそのまま or base64
    MASTER_SHEET_NAME   (任意, 既定 "契約店舗一覧")
    PORT                (Render 用, 任意)
"""

from flask import Flask, request
import os
import requests
import base64
import threading
import random
import json
import datetime
from dotenv import load_dotenv
from openai import OpenAI
from oauth2client.service_account import ServiceAccountCredentials
import gspread

# ------------------------------------------------------------------
# Flask & 環境変数
# ------------------------------------------------------------------
app = Flask(__name__)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
MASTER_SHEET_NAME = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (OPENAI_API_KEY and LINE_CHANNEL_ACCESS_TOKEN):
    raise RuntimeError("OPENAI_API_KEY と LINE_CHANNEL_ACCESS_TOKEN を設定してください")

client = OpenAI(api_key=OPENAI_API_KEY)
user_state: dict[str, dict] = {}

# ------------------------------------------------------------------
# Google Sheets 認証
# ------------------------------------------------------------------

def load_service_account(scope: list):
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT")
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if raw:
        raw = raw if raw.strip().startswith('{') else base64.b64decode(raw)
        info = json.loads(raw)
    elif path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            info = json.load(f)
    else:
        raise RuntimeError("Service Account 情報が見つかりません")
    return ServiceAccountCredentials.from_json_keyfile_dict(info, scope)

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds = load_service_account(scope)
gs_client = gspread.authorize(creds)

# ------------------------------------------------------------------
# Sheets ユーティリティ
# ------------------------------------------------------------------

def get_master():
    try:
        sh = gs_client.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs_client.create(MASTER_SHEET_NAME)
        sh.sheet1.update([["店舗名", "店舗ID", "座席数", "シートURL", "登録日時"]])
    return sh.sheet1


def create_store_sheet(store_name: str, store_id: int, seat_info: str) -> str:
    sh = gs_client.create(f"予約表 - {store_name} ({store_id})")
    sh.sheet1.update("A1", [["月", "日", "時間帯", "名前", "人数", "備考"]])
    get_master().append_row([
        store_name,
        store_id,
        seat_info.replace("\n", " "),
        sh.url,
        datetime.datetime.now().isoformat(),
    ])
    return sh.url

# ------------------------------------------------------------------
# Vision 解析
# ------------------------------------------------------------------

def dl_image(mid: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{mid}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.content


def vision_parse(img: bytes):
    b64 = base64.b64encode(img).decode()
    prompt = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": (
            "画像は飲食店の紙予約表です。各予約行を JSON 配列で返してください。"
            "形式: [{\"month\":int,\"day\":int,\"time\":\"HH:MM\",\"name\":str,\"size\":int,\"note\":str}]"
        )}
    ]
    res = client.chat.completions.create(
        model="gpt-4o",
        messages=prompt,
        response_format={"type": "json_object"},
        max_tokens=1024,
    )
    try:
        return json.loads(res.choices[0].message.content)
    except Exception:
        return []


def sheet_append(url: str, rows: list):
    if not rows:
        return
    ws = gs_client.open_by_url(url).sheet1
    ws.append_rows([[r.get(k) for k in ("month","day","time","name","size","note")] for r in rows],
                   value_input_option="USER_ENTERED")

# ------------------------------------------------------------------
# LINE 返信
# ------------------------------------------------------------------

def reply(token: str, text: str):
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {"replyToken": token, "messages": [{"type": "text", "text": text}]}
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body)

# ------------------------------------------------------------------
# Webhook
# ------------------------------------------------------------------

@app.route("/", methods=["POST", "GET", "HEAD"])
def webhook():
    if request.method != "POST":
        return "OK", 200
    body = request.get_json()
    if not body or not body.get("events"):
        return "No events", 200
    threading.Thread(target=handle, args=(body,)).start()
    return "OK", 200


def handle(body):
    ev = body["events"][0]
    if ev.get("type") != "message":
        return

    uid = ev["source"]["userId"]
    token = ev["replyToken"]
    mtype = ev["message"]["type"]
    text = ev["message"].get("text", "")
    st = user_state.setdefault(uid, {"step": "start"})

    # ---------- テキスト ----------
    if mtype == "text":
        if st["step"] == "start":
            g = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": f"文から店舗名だけ返して:\n{text}"}],
                max_tokens=20,
            )
            name = g.choices[0].message.content.strip()
            sid = random.randint(100000, 999999)
            st.update({"step": "confirm_store", "name": name, "sid": sid})
            reply(token, f"店舗名: {name}\n店舗ID: {sid}\nこの内容でよろしいですか？ (はい/いいえ)")
            return

        if st["step"] == "confirm_store":
            if "はい" in text:
                st["step"] = "ask_seats"
                reply(token, "座席数を教えてください。例: 1人席:3、2人席:2、4人席:1")
            else:
                st["step"] = "start"
                reply(token, "もう一度店舗名を送ってください。")
            return

        if st["step"] == "ask_seats":
            st["seats"] = text.strip()
            st["step"] = "confirm_seats"
            reply(token, f"座席情報:\n{text}\nこれでよろしいですか？ (はい/いいえ)")
            return

        if st["step"] == "confirm_seats":
            if "はい" in text:
                url = create_store_sheet(st["name"], st["sid"], st["seats"])
                st.update({"url": url, "step": "wait_img"})
                reply(token, "店舗登録完了！\n予約表の写真を送ってください。")
            else:
                st["step"] = "ask_seats"
                reply(token, "もう一度座席数を入力してください。")
            return

        if st["step"] == "confirm_struct":
            if "はい" in text:
                st["step"] = "processing"
                reply(token, "ありがとうございます！認識内容をもとに予約表フォーマットを作成します。\nしばらくお待ちください…")
            else:
                st["step"] = "wait_img"
                reply(token, "修正後の画像を再送してください。")
            return

    # ---------- 画像 ----------
    if mtype == "image" and st.get("step") == "wait_img":
        st["img"] = dl_image(ev["message"]["id"])
        st["step"] = "confirm_struct"
        reply(token, "画像を解析しました。この内容で登録してよいですか？ (はい/いいえ)")
        return

    if st.get("step") == "processing":
        rows = vision_parse(st.pop("img", b""))
        sheet_append(st["url"], rows)
        st["step"] = "done"
        reply(token, "✅ 予約表のデータ取得を完了しました！\n\n---\n\n📷 以後は予約表写真の再送、または\n『18:30, 2名, 田中様, 090-xxxx』のようにテキストで送ってください。変更・キャンセルも同様にどうぞ。")
        return

# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

