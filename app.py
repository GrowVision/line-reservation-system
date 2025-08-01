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
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

client = OpenAI(api_key=OPENAI_API_KEY)

@app.route("/", methods=['GET', 'HEAD', 'POST'])
def webhook():
    if request.method in ['GET', 'HEAD']:
        return "OK", 200

    try:
        body = request.get_json()
        print("📩 受信リクエスト:", body)

        if 'events' not in body or len(body['events']) == 0:
            print("[⚠️ イベントなし]")
            return "No events", 200

        threading.Thread(target=handle_event, args=(body,)).start()
        return "OK", 200

    except Exception as e:
        print("[❌ webhookエラー]", e)
        return "Internal Server Error", 500

def handle_event(body):
    try:
        event = body['events'][0]
        print("✅ イベント:", event)

        if event['type'] == 'message':
            msg_type = event['message']['type']
            reply_token = event['replyToken']

            if msg_type == 'image':
                print("🖼️ 画像メッセージ処理開始")
                message_id = event['message']['id']
                headers = {
                    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
                }
                image_response = requests.get(
                    f"https://api-data.line.me/v2/bot/message/{message_id}/content", headers=headers)
                image_binary = image_response.content
                mime_type = image_response.headers.get('Content-Type', 'image/jpeg')
                image_b64 = base64.b64encode(image_binary).decode("utf-8")

                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "この画像は飲食店の予約表です。何時に何席空いているか読み取ってください。"},
                                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}}
                            ]
                        }
                    ],
                    max_tokens=500
                )
                reply_text = response.choices[0].message.content

            elif msg_type == 'text':
                user_message = event['message']['text']
                print("📝 テキストメッセージ:", user_message)

                if "店舗名" in user_message:
                    # 店舗名抽出をGPTに任せる
                    gpt_response = client.chat.completions.create(
                        model="gpt-4o",
                        messages=[
                            {
                                "role": "user",
                                "content": f"以下の文から店舗名だけを抽出してください。記号や余計な語を除いて、店舗名そのものだけを返してください：\n\n{user_message}"
                            }
                        ],
                        max_tokens=50
                    )
                    store_name = gpt_response.choices[0].message.content.strip()
                    store_id = random.randint(100000, 999999)
                    reply_text = f"登録完了：店舗名：{store_name}、店舗ID：{store_id}"

                    # TODO: スプレッドシートやDBに保存する場合はここで実装

                else:
                    reply_text = "画像を送ると、AIが予約状況を読み取ってお返事します！"

            else:
                reply_text = "画像を送ってください。"

            reply(reply_token, reply_text)

    except Exception as e:
        print("[❌ handle_eventエラー]", e)

def reply(reply_token, text):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    body = {
        "replyToken": reply_token,
        "messages": [
            {"type": "text", "text": text}
        ]
    }
    res = requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body)
    print("📨 LINE返信ステータス:", res.status_code)
    print("📨 LINE返信レスポンス:", res.text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

