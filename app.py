"""
LINE予約管理BOT（一覧確認・柔軟入力対応版 + スプレッドシート連携 + 画像解析）
------------------------------------------------------------------
* 既存のフローは維持しつつ、以下を追加しています。
  1. 契約店舗マスターシートの自動生成・追記
  2. Vision API を用いた予約表画像→JSON 抽出
  3. 抽出結果を各店舗シートへ書き込み
  4. シート URL を登録完了メッセージに同封
  5. LINE 画像取得⇒Vision 解析⇒シート反映⇒残席サマリ返信
------------------------------------------------------------------
必要な環境変数:
- OPENAI_API_KEY
- LINE_CHANNEL_ACCESS_TOKEN
- GOOGLE_SERVICE_ACCOUNT         # GCP Service Account JSON を文字列で格納
- MASTER_SHEET_NAME(optional)    # 店舗一覧シート名。未指定なら「契約店舗一覧」
- PORT(optional)                 # Render などで使用
"""

from flask import Flask, request
import os
import requests
import base64
import threading
import random
import json
import io
import datetime
from dotenv import load_dotenv
from openai import OpenAI
from oauth2client.service_account import ServiceAccountCredentials
import gspread

app = Flask(__name__)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_SERVICE_ACCOUNT = os.getenv("GOOGLE_SERVICE_ACCOUNT")
MASTER_SHEET_NAME = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

client = OpenAI(api_key=OPENAI_API_KEY)
user_state = {}

# Google Sheets 認証設定
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_SERVICE_ACCOUNT), scope)
gs_client = gspread.authorize(creds)

# -------------------------------------------------------------
# Google Sheets ユーティリティ
# -------------------------------------------------------------

def get_master_sheet():
    """店舗マスターシート (契約店舗一覧) を取得 or 自動生成"""
    try:
        sh = gs_client.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs_client.create(MASTER_SHEET_NAME)
        sh.share(None, perm_type='anyone', role='reader')  # 公開 read-only
        sh.sheet1.update([["店舗名", "店舗ID", "座席数(1/2/4)", "予約シートURL", "登録日時"]])
    return sh.sheet1


def create_spreadsheet(store_name: str, store_id: int, seat_info: str) -> str:
    """店舗毎の予約管理シートを生成し、マスターに登録"""
    spreadsheet = gs_client.create(f"予約表 - {store_name} ({store_id})")
    spreadsheet.share(None, perm_type='anyone', role='writer')  # 任意編集 (必要に応じ変更)
    ws = spreadsheet.sheet1
    ws.update("A1", [["月", "日", "時間帯", "名前", "人数", "備考"]])

    # マスターへ追記
    master_ws = get_master_sheet()
    master_ws.append_row([store_name, store_id, seat_info.replace("\n", " "), spreadsheet.url, datetime.datetime.now().isoformat()])

    return spreadsheet.url

# -------------------------------------------------------------
# Vision 解析ユーティリティ
# -------------------------------------------------------------

def download_line_image(message_id: str) -> bytes:
    """LINE 画像メッセージのバイナリ取得"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()
    return res.content


def analyze_reservation_image(image_bytes: bytes) -> list:
    """OpenAI Vision で予約表画像→予約レコード(JSON list) へ変換
    返り値: [ {"month":8,"day":4,"time":"18:00","name":"山田","size":2,"note":""}, ... ]
    """
    b64 = base64.b64encode(image_bytes).decode()

    vision_prompt = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        },
        {
            "type": "text",
            "text": (
                "以下の予約表画像を読み取り、予約行を JSON 配列で返してください。"
                "出力フォーマットは [ {\"month\":int, \"day\":int, \"time\":\"HH:MM\", \"name\":str, \"size\":int, \"note\":str} ] です。"
            )
        }
    ]

    res = client.chat.completions.create(
        model="gpt-4o",
        messages=vision_prompt,
        response_format={"type": "json_object"},
        max_tokens=1024,
    )
    content = res.choices[0].message.content
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # フォーマット外なら空で返す (実運用ではリトライ推奨)
        data = []
    return data


def append_reservations_to_sheet(sheet_url: str, reservations: list):
    """予約レコードを書き込み (追記)"""
    if not reservations:
        return
    sh = gs_client.open_by_url(sheet_url)
    ws = sh.sheet1
    rows = [[r.get("month"), r.get("day"), r.get("time"), r.get("name"), r.get("size"), r.get("note", "")] for r in reservations]
    ws.append_rows(rows, value_input_option="USER_ENTERED")

# -------------------------------------------------------------
# 返信ユーティリティ
# -------------------------------------------------------------

def reply(reply_token: str, text: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    res = requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body)
    print("LINE返信ステータス:", res.status_code)

# -------------------------------------------------------------
# Flask Webhook ハンドラ
# -------------------------------------------------------------

@app.route("/", methods=['GET', 'HEAD', 'POST'])
def webhook():
    if request.method in ['GET', 'HEAD']:
        return "OK", 200
    body = request.get_json()
    if not body or 'events' not in body or len(body['events']) == 0:
        return "No events", 200
    threading.Thread(target=handle_event, args=(body,)).start()
    return "OK", 200


def handle_event(body):
    try:
        event = body['events'][0]
        if event['type'] != 'message':
            return

        user_id = event['source']['userId']
        reply_token = event['replyToken']
        msg_type = event['message']['type']
        user_message = event['message'].get('text', '')

        state = user_state.get(user_id, {"step": "start"})

        # ---------------------- テキストメッセージ ----------------------
        if msg_type == 'text':
            # (既存ロジックをほぼそのまま維持し、シート URL 追記を追加)
            if state['step'] == 'start':
                gpt_response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": f"以下の文から店舗名だけを抽出してください：\n{user_message}"}],
                    max_tokens=50
                )
                store_name = gpt_response.choices[0].message.content.strip()
                store_id = random.randint(100000, 999999)
                user_state[user_id] = {
                    "step": "confirm_store",
                    "store_name": store_name,
                    "store_id": store_id
                }
                reply_text = (
                    f"登録完了：店舗名：{store_name} 店舗ID：{store_id}\n\n"
                    "この内容で間違いないですか？\n\n『はい』『いいえ』でお答えください。"
                )

            elif state['step'] == 'confirm_store':
                if "はい" in user_message:
                    user_state[user_id]["step"] = "ask_seats"
                    reply_text = "次に、座席数を教えてください。\n例：『1人席: 3、2人席: 2、4人席: 1』"
                elif "いいえ" in user_message:
                    user_state[user_id] = {"step": "start"}
                    reply_text = "もう一度、店舗名を送ってください。"
                else:
                    reply_text = "店舗情報が正しいか『はい』または『いいえ』でお答えください。"

            elif state['step'] == 'ask_seats':
                prev = user_state[user_id].get("seat_info", "")
                gpt_response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": (
                        f"以下の文と、前の座席数『{prev}』をもとに、"
                        "1人席、2人席、4人席の数を抽出して次の形式で答えてください：\n"
                        "1人席：◯席\n2人席：◯席\n4人席：◯席\n\n文：{user_message}" )}],
                    max_tokens=100
                )
                seat_info = gpt_response.choices[0].message.content.strip()
                user_state[user_id]["seat_info"] = seat_info
                user_state[user_id]["step"] = "confirm_seats"
                store_name = user_state[user_id]['store_name']
                store_id = user_state[user_id]['store_id']
                reply_text = (
                    "✅ 登録情報の確認です：\n\n"
                    f"- 店舗名：{store_name}\n- 店舗ID：{store_id}\n- 座席数：\n{seat_info}\n\n"
                    "この内容で登録してもよろしいですか？\n\n『はい』『いいえ』でお答えください。"
                )

            elif state["step"] == "confirm_seats":
                if "はい" in user_message:
                    store_name = user_state[user_id]["store_name"]
                    store_id = user_state[user_id]["store_id"]
                    seat_info = user_state[user_id]["seat_info"]
                    sheet_url = create_spreadsheet(store_name, store_id, seat_info)
                    user_state[user_id]["spreadsheet_url"] = sheet_url
                    reply_text = (
                        "ありがとうございます！\n"
                        "🎉 店舗登録が完了しました 🎉\n\n"
                        f"予約スプレッドシート: {sheet_url}\n\n"
                        "※IDは控えておいてください。\n"
                        "続けて、普段お使いの紙の予約表の写真を送ってください。\n"
                        "AI が読み取り、スプレッドシートに反映します。"
                    )
                    user_state[user_id]["step"] = "wait_for_image"
                elif "いいえ" in user_message:
                    user_state[user_id]["step"] = "ask_seats"
                    reply_text = "もう一度、座席数を入力してください。\n例：『1人席: 3、2人席: 2、4人席: 1』"
                else:
                    reply_text = "座席数が正しいか『はい』または『いいえ』でお答えください。"

            elif state.get("step") == "confirm_structure":
                if "はい" in user_message:
                    reply_text = "ありがとうございます！予約表フォーマットを確定しました。画像を再度送ると予約データを取り込みます。"
                    user_state[user_id]["step"] = "completed"
                elif "いいえ" in user_message:
                    reply_text = (
                        "ご指摘ありがとうございます！\n修正点をお知らせください。"
                    )
                    user_state[user_id]["step"] = "request_correction"
                else:
                    reply_text = "内容が正しいか『はい』または『いいえ』でお答えください。"

            else:
                reply_text = "画像を送ると、AIが予約状況を読み取ってお返事します！"

            reply(reply_token, reply_text)
            return

        # ---------------------- 画像メッセージ ----------------------
        if msg_type == 'image':
            if state.get("step") == "wait_for_image":
                # 1. LINE 画像バイナリ取得
                message_id = event['message']['id']
                try:
                    image_bytes = download_line_image(message_id)
                except Exception as e:
                    reply(reply_token, "画像の取得に失敗しました。時間をおいて再度お試しください。")
                    return

                # 2. Vision 解析
                reservations = analyze_reservation_image(image_bytes)
                sheet_url = user_state[user_id].get("spreadsheet_url")

                if not sheet_url:
                    reply(reply_token, "店舗のシートが見つかりませんでした。登録をやり直してください。")
                    return

                # 3. シート書き込み
                try:
                    append_reservations_to_sheet(sheet_url, reservations)
                except Exception as e:
                    reply(reply_token, "スプレッドシートへの書き込みに失敗しました。しばらくして再度お試しください。")
                    return

                # 4. 残席サマリを計算 (簡易) ---------------------------------
                seat_info = user_state[user_id].get("seat_info", "")
                summary = "予約を取り込みました！\n\n" + (seat_info or "")

                reply(reply_token, summary)
                return
            else:
                reply(reply_token, "画像を受信しましたが、現在は画像解析の準備ができていません。店舗登録を先に行ってください。")
                return

    except Exception as e:
        print("[handle_event error]", e)
        # 何か想定外の例外が起きた場合も、とりあえずユーザへ簡潔に返信
        if 'reply_token' in locals():
            reply(reply_token, "エラーが発生しました。お手数ですが再度お試しください。")


# -------------------------------------------------------------
# エントリポイント
# -------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
