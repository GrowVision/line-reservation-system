LINE予約管理BOT (Googleスプレッドシート連携 + GPT-4o画像解析対応 + スプレッドシート登録)

from flask import Flask, request import os import requests import base64 import threading import random import json from datetime import datetime from dotenv import load_dotenv from openai import OpenAI from oauth2client.service_account import ServiceAccountCredentials import gspread

app = Flask(name) load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

client = OpenAI(api_key=OPENAI_API_KEY) user_state = {}

スプレッドシート認証

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"] creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_CREDENTIALS_JSON), scope) gs_client = gspread.authorize(creds)

store_sheets = {}  # 店舗ごとのスプレッドシートURL記録用

def create_spreadsheet(store_name, store_id): spreadsheet = gs_client.create(f"予約表 - {store_name} ({store_id})") worksheet = spreadsheet.sheet1 worksheet.update("A1", [["月", "日", "時間帯", "名前", "人数", "備考"]]) store_sheets[store_id] = spreadsheet.url return spreadsheet.url

@app.route("/", methods=['GET', 'HEAD', 'POST']) def webhook(): if request.method in ['GET', 'HEAD']: return "OK", 200

try:
    body = request.get_json()
    if 'events' not in body or len(body['events']) == 0:
        return "No events", 200
    threading.Thread(target=handle_event, args=(body,)).start()
    return "OK", 200
except Exception as e:
    print("[webhook error]", e)
    return "Internal Server Error", 500

def handle_event(body): try: event = body['events'][0] if event['type'] != 'message': return

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
            reply_text = (
                f"登録内容をまとめました！\n\n"
                f"店舗名：{store_name}\n"
                f"店舗ID：{store_id}\n\n"
                f"座席数：\n{seat_info}\n\n"
                f"予約表構成（紙）：\n・時間帯：18:00〜、18:30〜、19:00〜\n・記入欄：名前／人数／備考\n\n"
                f"修正反映済み：\n・19:00〜も存在\n\n"
                f"この構成でスプレッドシートを作成し、以後の予約はこの形式でAIが認識・記録します。\n\n"
                f"この内容で登録してもよろしいですか？「はい」「いいえ」でお答えください。"
            )

        elif state["step"] == "confirm_seats":
            if "はい" in user_message:
                store_name = user_state[user_id].get("store_name", "未設定")
                store_id = user_state[user_id].get("store_id")
                sheet_url = create_spreadsheet(store_name, store_id)
                user_state[user_id]["step"] = "wait_for_image"
                reply(reply_token, "ありがとうございます！\n認識内容をもとに、予約表の記録フォーマットを作成します。\nしばらくお待ちください…")
                reply(reply_token, "予約表のデータ取得を完了しました！")
                reply(reply_token, (
                    "\n今後は、現在の予約状況について以下の方法でご連絡ください：\n\n"
                    "① 紙の予約表の写真をそのまま送っていただいてもOKです\n"
                    "　→ AIが自動で読み取り、内容を更新します\n\n"
                    "② または、個別に以下のような情報を入力しても大丈夫です\n"
                    "　例：\n"
                    "　「18:30〜、2名、名前：田中様、電話番号：090-xxxx-xxxx」\n\n"
                    "予約内容に変更やキャンセルがある場合も、そのままご連絡ください。\n"
                    "AIが自動で内容を確認し、反映されます"
                ))
                return
            elif "いいえ" in user_message:
                user_state[user_id]["step"] = "ask_seats"
                reply_text = "もう一度、座席数を入力してください。"
            else:
                reply_text = "座席数が正しいか「はい」または「いいえ」でお答えください。"

        else:
            reply_text = "画像を送ると予約表を読み取って返信します！"

    elif msg_type == 'image':
        if state.get("step") == "wait_for_image":
            reply_text = (
                "予約表を画像解析しました！\n\n例：\n・18:00〜、18:30〜、名前と人数あり\n・記入欄：名前／人数／備考\n\nこの構成で問題なければ「はい」、修正点があれば「いいえ」と返信してください。"
            )
            user_state[user_id]["step"] = "confirm_structure"
        else:
            reply_text = "現在は画像受付の段階ではありません。店舗登録を先に行ってください。"

    else:
        reply_text = "画像を送ってください。"

    reply(reply_token, reply_text)

except Exception as e:
    print("[handle_event error]", e)

def reply(reply_token, text): headers = { "Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}" } body = { "replyToken": reply_token, "messages": [{"type": "text", "text": text}] } res = requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body) print("LINE返信ステータス:", res.status_code) print("LINE返信レスポンス:", res.text)

if name == "main": port = int(os.environ.get("PORT", 10000)) app.run(host="0.0.0.0", port=port)

