import os
from google import genai
from google.genai import types

# 1) 新SDK クライアント初期化
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# 2) 画像読み込み
with open("sample_reservation_form.jpg", "rb") as f:
    img_bytes = f.read()
print(f"[TEST] 画像バイト数: {len(img_bytes)}")

# 3) プロンプト定義
prompt = (
    "画像は、手書きで記入するための予約表です。以下のように簡潔に構成をまとめてください：\n"
    "- 表のタイトル\n- 日付欄\n- 列の構成（時間帯、名前、人数、卓番など）\n"
    "- 注意書きの内容\n- テーブル番号の使い分け"
)

# 4) SDK 呼び出し（max_output_tokens をトップレベルで指定）
try:
    response = client.models.generate_content(
        model="models/gemini-1.5-pro-latest",
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            types.Part.from_text(text=prompt)
        ],
        max_output_tokens=1024
    )
    print("=== SDK 呼び出し 成功 ===")
    print("レスポンス全体：", response)
    print("要約テキスト：", response.text)
except Exception as e:
    print("=== SDK 呼び出し エラー ===")
    print(e)
