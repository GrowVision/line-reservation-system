# ✅ LINE予約管理BOT（一覧確認付き）
from flask import Flask, request
import os
import requests
import base64
import threading
import random
from dotenv import load_dotenv
from openai import OpenAI

app = Flask(__name__)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
client = OpenAI(api_key=OPENAI_API_KEY)

user_state = {}

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
        if event['type'] != 'message': return

        user_id = event['source']['userId']
        reply_token = event['replyToken']
        msg_type = event['message']['type']
        user_message = event['message'].get('text', '')

        state = user_state.get(user_id, {"step": "start"})

        if msg_type == 'text':
            # ステップ1: 店舗名抽出
            if state['step'] == 'start' and '店舗名' in user_message:
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
                reply_text = f"登録完了：店舗名：{store_name} 店舗ID：{store_id}\n\nこの内容で間違いないですか？「はい」「いいえ」でお答えください。"

            # ステップ2: 店舗名確認
            elif state["step"] == "confirm_store":
                if "はい" in user_message:
                    user_state[user_id]["step"] = "ask_seats"
                    reply_text = "次に、座席数を教えてください。\n例：「1人席: 3、2人席: 2、4人席: 1」"
                elif "いいえ" in user_message:
                    user_state[user_id] = {"step": "start"}
                    reply_text = "もう一度、店舗名を「店舗名は〇〇です」の形式で送ってください。"
                else:
                    reply_text = "店舗情報が正しいか「はい」または「いいえ」でお答えください。"

            # ステップ3: 座席数入力
            elif state['step'] == 'ask_seats':
                gpt_response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": f"以下の文から1人席、2人席、4人席の数を抽出して次の形式で答えてください：\n1人席：◯席\n2人席：◯席\n4人席：◯席\n\n文：{user_message}"}],
                    max_tokens=100
                )
                seat_info = gpt_response.choices[0].message.content.strip()
                user_state[user_id]["seat_info"] = seat_info
                user_state[user_id]["step"] = "confirm_seats"
                store_name = user_state[user_id]['store_name']
                store_id = user_state[user_id]['store_id']
                reply_text = f"✅ 登録情報の確認です：\n\n- 店舗名：{store_name}\n- 店舗ID：{store_id}\n- 座席数：\n{seat_info}\n\nこの内容で登録してもよろしいですか？「はい」「いいえ」でお答えください。"

            # ステップ4: 座席確認
            elif state["step"] == "confirm_seats":
                if "はい" in user_message:
                    reply_text = f"ありがとうございます！店舗登録が完了しました🎉\n今後は予約表画像を送ると、AIが予約状況を読み取って返信します。"
                    user_state[user_id]["step"] = "done"
                elif "いいえ" in user_message:
                    user_state[user_id]["step"] = "ask_seats"
                    reply_text = "もう一度、座席数を入力してください。\n例：「1人席: 3、2人席: 2、4人席: 1」"
                else:
                    reply_text = "座席数が正しいか「はい」または「いいえ」でお答えください。"

            else:
                reply_text = "画像を送ると、AIが予約状況を読み取ってお返事します！"

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
