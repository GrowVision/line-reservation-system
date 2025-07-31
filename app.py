from flask import Flask, request
import os
import requests
import base64
import threading
from dotenv import load_dotenv
from openai import OpenAI

# Flaskアプリ初期化
app = Flask(__name__)
load_dotenv()  # .envから環境変数を読み込む

# 環境変数の読み込み
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# OpenAIクライアントの初期化
client = OpenAI(api_key=OPENAI_API_KEY)

@app.route("/", methods=['GET', 'HEAD', 'POST'])
def webhook():
    if request.method in ['GET', 'HEAD']:
        return "OK", 200  # LINEの疎通確認用

    try:
        body = request.get_json()
        print("📩 受信リクエスト:", body)

        if 'events' not in body or len(body['events']) == 0:
            print("[⚠️ イベントなし]")
            return "No events", 200

        print("✅ イベントを検出、非同期で処理開始")
        threading.Thread(target=handle_event, args=(body,)).start()
        return "OK", 200

    except Exception as e:
        print("[❌ webhookエラー]", e)
        return "Internal Server Error", 500

def handle_event(body):
    try:
        event = body['events'][0]
        print("✅ 処理対象イベント:", event)

        if event['type'] == 'message':
            msg_type = event['message']['type']
            reply_token = event['replyToken']

            if msg_type == 'image':
                message_id = event['message']['id']
                headers = {
                    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
                }
                image_response = requests.get(
                    f"https://api-data.line.me/v2/bot/message/{message_id}/content", headers=headers)
                image_binary = image_response.content
                mime_type = image_response.headers.get('Content-Type', 'image/jpeg')
                image_b64 = base64.b64encode(image_binary).decode("utf-8")

                # OpenAIに画像と指示を送信
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
                reply_text = "画像を送ると、AIが予約状況を読み取ってお返事します！"
            else:
                reply_text = "画像を送ってください。"

            reply(reply_token, reply_text)

    except Exception as e:
        print("[❌ handle_event エラー]", e)

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

# アプリの起動（RenderではPORTが環境変数で渡される）
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
