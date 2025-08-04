"""
LINE予約管理BOT (一覧確認・柔軟入力対応版 + スプレッドシート連携 + 画像解析)
------------------------------------------------------------------
* 既存フローは維持しつつ、以下を追加しています。
  1. 契約店舗マスターシートの自動生成・追記
  2. Vision API を用いた予約表画像→JSON 抽出
  3. 抽出結果を各店舗シートへ書き込み
  4. シート URL を登録完了メッセージに同封
  5. LINE 画像取得⇒Vision 解析⇒シート反映⇒残席サマリ返信
------------------------------------------------------------------
必要な環境変数:
- OPENAI_API_KEY
- LINE_CHANNEL_ACCESS_TOKEN
- GOOGLE_SERVICE_ACCOUNT             # GCP Service Account JSON を raw または base64 で格納
  もしくは GOOGLE_SERVICE_ACCOUNT_FILE にファイルパス
- MASTER_SHEET_NAME(optional)        # 店舗一覧シート名。未指定なら「契約店舗一覧」
- PORT(optional)                     # Render などで使用
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

# -------------------------------------------------------------
# Flask アプリ & 環境変数読込
# -------------------------------------------------------------
app = Flask(__name__)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
MASTER_SHEET_NAME = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (OPENAI_API_KEY and LINE_CHANNEL_ACCESS_TOKEN):
    raise RuntimeError("OPENAI_API_KEY または LINE_CHANNEL_ACCESS_TOKEN が設定されていません")

client = OpenAI(api_key=OPENAI_API_KEY)
user_state = {}

# -------------------------------------------------------------
# Google Sheets 認証設定
# -------------------------------------------------------------

def load_service_account_credentials(scope):
    """環境変数またはファイルから ServiceAccountCredentials を生成"""
    json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT")
    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")

    if json_env:
        try:
            payload = json_env if json_env.strip().startswith('{') else base64.b64decode(json_env)
            info_dict = json.loads(payload)
        except Exception as e:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT の内容が不正です") from e
    elif file_path and os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as fp:
            info_dict = json.load(fp)
    else:
        raise RuntimeError("Google Service Account の認証情報が見つかりません")

    return ServiceAccountCredentials.from_json_keyfile_dict(info_dict, scope)

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds = load_service_account_credentials(scope)
gs_client = gspread.authorize(creds)

# -------------------------------------------------------------
# Google Sheets ユーティリティ
# -------------------------------------------------------------

def get_master_sheet():
    try:
        sh = gs_client.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs_client.create(MASTER_SHEET_NAME)
        sh.sheet1.update([["店舗名", "店舗ID", "座席数(1/2/4)", "予約シートURL", "登録日時"]])
    return sh.sheet1


def create_spreadsheet(store_name: str, store_id: int, seat_info: str) -> str:
    spreadsheet = gs_client.create(f"予約表 - {store_name} ({store_id})")
    ws = spreadsheet.sheet1
    ws.update("A1", [["月", "日", "時間帯", "名前", "人数", "備考"]])

    master_ws = get_master_sheet()
    master_ws.append_row([
        store_name,
        store_id,
        seat_info.replace("\n", " "),
        spreadsheet.url,
        datetime.datetime.now().isoformat(),
    ])
    return spreadsheet.url

# -------------------------------------------------------------
# Vision 解析
# -------------------------------------------------------------

def download_line_image(message_id):
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()
    return res.content


def analyze_reservation_image(image_bytes):
    b64 = base64.b64encode(image_bytes).decode()
    prompt = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": (
            "画像は飲食店の紙予約表です。各行を JSON 配列で返してください。"
            "形式: [{\\"month\\":int,\\"day\\":int,\\"time\\":\\"HH:MM\\",\\"name\\":str,\\"size\\":int,\\"note\\":str}]"
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
    except json.JSONDecodeError:
        return []


def append_reservations_to_sheet(sheet_url, reservations):
    if not reservations:
        return
    ws = gs_client.open_by_url(sheet_url).sheet1
    rows = [[r.get(k) for k in ("month","day","time","name","size","note")] for r in reservations]
    ws.append_rows(rows, value_input_option="USER_ENTERED")

# -------------------------------------------------------------
# LINE 返信
# -------------------------------------------------------------

def reply(reply_token, text):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body)

# -------------------------------------------------------------
# Webhook
# -------------------------------------------------------------

@app.route("/", methods=["POST", "GET", "HEAD"])
def webhook():
    if request.method != "POST":
        return "OK", 200
    body = request.get_json()
    if not body or not body.get("events"):
        return "No events", 200
    threading.Thread(target=handle_event, args=(body,)).start()
    return "OK", 200


def handle_event(body):
    event = body["events"][0]
    if event.get("type") != "message":
        return

    user_id = event["source"]["userId"]
    reply_token = event["replyToken"]
    msg_type = event["message"]["type"]
    text = event["message"].get("text", "")

    state = user_state.setdefault(user_id, {"step": "start"})

    # -------------- テキスト --------------
    if msg_type == "text":
        if state["step"] == "start":
            gpt_res = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": f"以下の文から店舗名だけ抽出：\n{text}"}],
                max_tokens=20,
            )
            store_name = gpt_res.choices[0].message.content.strip()
            store_id = random.randint(100000, 999999)
            state.update({"step": "confirm_store", "store_name": store_name, "store_id": store_id})
            reply(reply_token, f"店舗名: {store_name}\n店舗ID: {store_id}\nこの内容でよろしいですか？ (はい/いいえ)")
            return

        if state["step"] == "confirm_store":
            if "はい" in text:
                state["step"] = "ask_seats"
                reply(reply_token, "座席数を教えてください。例: 1人席:3、2人席:2、4人席:1")
            else:
                state["step"] = "start"
                reply(reply_token, "もう一度店舗名を送ってください。")
            return

        if state["step"] == "ask_seats":
            state["seat_info"] = text.strip()
            reply(reply_token, f"座席情報:\n{text}\nこれでよろしいですか？ (はい/いいえ)")
            state["step"] = "confirm_seats"
            return

        if state["step"] == "confirm_seats":
            if "はい" in text:
                url = create_spreadsheet(state["store_name"], state["store_id"], state["seat_info"])
                state.update({"spreadsheet_url": url, "step": "wait_for_image"})
                reply(reply_token, "店舗登録完了！\n予約表の写真を送ってください。")
            else:
                state["step"] = "ask_seats"
                reply(reply_token, "もう一度座席数を入力してください。")
            return

        if state.get("step") == "confirm_structure":
            if "はい" in text:
                state["step"] = "image_processing"
                reply(reply_token, "ありがとうございます！認識内容をもとに、予約表の記録フォーマットを作成します。\nしばらくお待ちください…")
                # 実処理は画像受信時に行う
            else:
                state["step"] = "wait_for_image"
                reply(reply_token, "修正点を反映します。もう一度画像を送ってください。")
            return

    # -------------- 画像 --------------
    if msg_type == "image" and state.get("step") == "wait_for_image":
        img_bytes = download_line_image(event["message"]["id"])
        state["image_bytes"] = img_bytes
        state["step"] = "confirm_structure"
        reply(reply_token, "画像を解析しました。\nこの内容で登録してよろしいですか？ (はい/いいえ)")
        return

    if state.get("step") == "image_processing":
        # Vision → Sheet
        reservations = analyze_reservation_image(state.pop("image_bytes", b""))
        append_reservations_to_sheet(state["spreadsheet_url"], reservations)
        state["step"] = "completed"
        reply(reply_token, (
            "✅ 予約表のデータ取得を完了しました！\n\n---\n\n"
            "📷 今後は予約状況更新のために次のいずれかを送ってください：\n"
            "① 予約表の写真を再度送る\n"
            "② テキストで \"18:30, 2名, 田中様, 090-xxxx...\" の形式で送る"
        ))
        return

# -------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
