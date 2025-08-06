import os, base64, json
import google.generativeai as genai

# 1) APIキー設定
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# 2) 画像読み込み＆Base64化
with open("sample_reservation_form.jpg", "rb") as f:
    img_bytes = f.read()
img_b64 = base64.b64encode(img_bytes).decode()
print(f"[TEST] Image bytes: {len(img_bytes)}, Base64 len: {len(img_b64)}")

# 3) プロンプト定義
prompt = (
    "画像は、手書きで記入するための予約表です。以下のように簡潔に構成をまとめてください：\n"
    "- 表のタイトル\n- 日付欄\n- 列の構成（時間帯、名前、人数、卓番など）\n"
    "- 注意書きの内容\n- テーブル番号の使い分け"
)

# 4) SDK 呼び出し
try:
    res = genai.GenerativeModel("models/gemini-1.5-pro-latest").generate_content(
        [
            {"type": "image", "data": img_b64, "mime_type": "image/jpeg"},
            {"type": "text",  "text": prompt}
        ],
        generation_config={"max_output_tokens": 1024}
    )
    print("=== SDK 呼び出し 成功 ===")
    print("Full response object:", res)
    print("Text output:", res.text)
except Exception as e:
    print("=== SDK 呼び出し エラー ===")
    print(e)
