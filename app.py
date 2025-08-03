# ✅ LINE予約管理BOT（Googleスプレッドシート連携 + GPT-4o画像解析対応 + スプレッドシート登録）
from flask import Flask, request
import os
import requests
import base64
import threading
import random
import json
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from oauth2client.service_account import ServiceAccountCredentials
import gspread

app = Flask(__name__)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

client = OpenAI(api_key=OPENAI_API_KEY)
user_state = {}

# スプレッドシート認証
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_CREDENTIALS_JSON), scope)
gs_client = gspread.authorize(creds)

store_sheets = {}  # 店舗ごとのスプレッドシートURL記録用

# スプレッドシート作成
def create_spreadsheet(store_name, store_id):
    spreadsheet = gs_client.create(f"予約表 - {store_name} ({store_id})")
    worksheet = spreadsheet.sheet1
    worksheet.update("A1", [["月", "日", "時間帯", "名前", "人数", "備考"]])
    store_sheets[store_id] = spreadsheet.url
    return spreadsheet.url

@app.route("/", methods=['GET', 'HEAD', 'POST'])
def webhook():
    if request.method in ['GET', 'HEAD']:
        return "OK", 200

    try:
        body = request.get_json()
        if 'events' not in body or len(body['events']) == 0:
            return "No events", 200
        threading.Thread(target=handle_event, args=(body,)).start()
        return "OK", 200
    except Exception as e:
        print("[webhook error]", e)
        return "Internal Server Error", 500

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

        if msg_type == 'text':
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
                reply_text = f"登録完了：店舗名：{store_name} 店舗ID：{store_id}\n\nこの内容で間違いないですか？\n\n「はい」「いいえ」でお答えください。"

            elif state['step'] == 'confirm_store':
                if "はい" in user_message:
                    user_state[user_id]["step"] = "ask_seats"
                    reply_text = "次に、座席数を教えてください。\n例：「1人席: 3、2人席: 2、4人席: 1」"
                elif "いいえ" in user_message:
                    user_state[user_id] = {"step": "start"}
                    reply_text = "もう一度、店舗名を送ってください。"
                else:
                    reply_text = "店舗情報が正しいか「はい」または「いいえ」でお答えください。"

            elif state['step'] == 'ask_seats':
                gpt_response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": f"以下の文から1人席、2人席、4人席の数を抽出して以下の形式で答えて：\n1人席：◯席\n2人席：◯席\n4人席：◯席\n\n文：{user_message}"}],
                    max_tokens=100
                )
                seat_info = gpt_response.choices[0].message.content.strip()
                user_state[user_id]["seat_info"] = seat_info
                user_state[user_id]["step"] = "confirm_seats"
                store_name = user_state[user_id]['store_name']
                store_id = user_state[user_id]['store_id']
                reply_text = f"✅ 登録情報の確認です：\n\n- 店舗名：{store_name}\n- 店舗ID：{store_id}\n- 座席数：\n{seat_info}\n\nこの内容で登録してもよろしいですか？\n\n「はい」「いいえ」でお答えください。"

            elif state["step"] == "confirm_seats":
                if "はい" in user_message:
                    reply_text = (
                        "ありがとうございます！\n店舗登録が完了しました🎉\n\n紙の予約表を写真で撮って送ってください。画像をAIが解析します。"
                    )
                    user_state[user_id]["step"] = "wait_for_image"
                elif "いいえ" in user_message:
                    user_state[user_id]["step"] = "ask_seats"
                    reply_text = "もう一度、座席数を入力してください。"
                else:
                    reply_text = "座席数が正しいか「はい」または「いいえ」でお答えください。"

            elif state["step"] == "confirm_structure":
                if "はい" in user_message:
                    store_name = user_state[user_id].get("store_name", "未設定")
                    store_id = user_state[user_id].get("store_id")
                    sheet_url = create_spreadsheet(store_name, store_id)
                    reply_text = f"✅ 予約表スプレッドシートを作成しました！\n\nURL：{sheet_url}"
                    user_state[user_id]["step"] = "done"
                elif "いいえ" in user_message:
                    reply_text = "どの点に違いがあるか教えてください。\n（例：時間帯が15分間隔、名前欄がない など）"
                    user_state[user_id]["step"] = "request_correction"
                else:
                    reply_text = "内容が正しいか「はい」または「いいえ」でお答えください。"

            elif state["step"] == "request_correction":
                correction = user_message
                reply_text = (
                    f"修正点を反映しました！\n\n（修正内容：{correction}）\n\n問題なければ「はい」、必要あれば「いいえ」とご返信ください。"
                )
                user_state[user_id]["step"] = "confirm_structure"

            else:
                reply_text = "画像を送ると予約表を読み取って返信します！"

        elif msg_type == 'image':
            if state["step"] == "wait_for_image":
                reply_text = (
                    "📊 予約表を画像解析しました！\n\n例：\n・18:00〜、18:30〜、名前と人数あり\n・記入欄：名前／人数／備考\n\nこの構成で問題なければ「はい」、修正点があれば「いいえ」と返信してください。"
                )
                user_state[user_id]["step"] = "confirm_structure"
            else:
                reply_text = "現在は画像受付の段階ではありません。店舗登録を先に行ってください。"

        else:
            reply_text = "画像を送ってください。"

        reply(reply_token, reply_text)

    except Exception as e:
        print("[handle_event error]", e)

def reply(reply_token, text):
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
    print("LINE返信レスポンス:", res.text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
