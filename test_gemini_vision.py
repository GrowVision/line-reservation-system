import os
import base64
import google.generativeai as genai
from google.generativeai import types   # ← ここを追加

# 1) APIキー設定
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# 2) 画像読み込み＆Base64化
with open("sample_reservation_form.jpg", "rb") as f:
    img_bytes = f.read()
print(f"[TEST] Raw image bytes: {len(img_bytes)}")

# 3) プロンプト定義
prompt = (
    "画像は、手書きで記入するための予約表です。以下のように簡潔に構成をまとめてください：\n"
    "- 表のタイトル\n"
    "- 日付欄\n"
    "- 列の構成（時間帯、名前、人数、卓番など）\n"
    "- 注意書きの内容\n"
    "- テーブル番号の使い分け"
)

# 4) SDK 呼び出し（型オブジェクトを使う）
try:
    res = genai.GenerativeModel("models/gemini-1.5-pro-latest").generate_content(
        [
            types.Image(blob=img_bytes, mime_type="image/jpeg"),  # ← ここ
            types.Text(text=prompt)                               # ← ここ
        ],
        generation_config={"max_output_tokens": 1024}
    )
    print("=== SDK 呼び出し 成功 ===")
    print("Full response object:", res)
    print("Text output:", res.text)
except Exception as e:
    print("=== SDK 呼び出し エラー ===")
    print(e)
