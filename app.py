from __future__ import annotations
import base64
import datetime as dt
import os
import random
import threading
from typing import Any, Dict, List

# 新SDK のインポート
from google import genai
from google.genai import types

import gspread
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials as SACredentials

sa_info = json.loads(os.environ["CREDENTIALS_JSON"])
creds = SACredentials.from_service_account_info(
    sa_info,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive = build("drive", "v3", credentials=creds)


def purge_service_account_sheets(sa_info: dict):
    """
    サービスアカウントのドライブ内にある
    すべてのスプレッドシートを削除します。
    """
    # サービスアカウント情報から直接 Credentials を作成
    creds = SACredentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive = build("drive", "v3", credentials=creds)

    # スプレッドシートのみをリストアップして削除
    resp = drive.files().list(
        q="mimeType='application/vnd.google-apps.spreadsheet'",
        fields="files(id, name)",
        pageSize=1000
    ).execute()
    for f in resp.get("files", []):
        print(f"Deleting: {f['name']} ({f['id']})")
        drive.files().delete(fileId=f["id"]).execute()

    print("❌ All service-account sheets purged.")


import requests
from dotenv import load_dotenv
from flask import Flask, request

# -------------------------------------------------------------
# 環境変数 & モデル設定
# -------------------------------------------------------------
load_dotenv()
# Environment 変数から JSON をファイルとして書き出し
cred_json = os.environ.get("CREDENTIALS_JSON")
if cred_json:
    with open("/opt/render/project/src/credentials.json", "w", encoding="utf-8") as f:
        f.write(cred_json)

# （同様に TOKEN_JSON があれば書き出す）
token_json = os.environ.get("TOKEN_JSON")
if token_json:
    with open("/opt/render/project/src/token.json", "w", encoding="utf-8") as f:
        f.write(token_json)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT")
MASTER_SHEET_NAME = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (GEMINI_API_KEY and LINE_CHANNEL_ACCESS_TOKEN and GOOGLE_JSON):
    raise RuntimeError(
        "環境変数 GEMINI_API_KEY / LINE_CHANNEL_ACCESS_TOKEN / GOOGLE_CREDENTIALS_JSON を設定してください"
    )

MODEL_TEXT = "models/gemini-1.5-pro-latest"
MODEL_VISION = "models/gemini-1.5-pro-latest"

# -------------------------------------------------------------
# Gemini クライアント初期化
# -------------------------------------------------------------
client = genai.Client(api_key=GEMINI_API_KEY)
app = Flask(__name__)
user_state: Dict[str, Dict[str, Any]] = {}

# -------------------------------------------------------------
# Google Sheets 認証
# -------------------------------------------------------------
# token.json が書き出された直後に、その中身をログ出力する
token_path = "/opt/render/project/src/token.json"
if os.path.exists(token_path):
    print("===== token.json の中身 =====")
    with open(token_path, "r", encoding="utf-8") as f:
        print(f.read())
    print("===== 以上 token.json の中身 =====")
else:
    print("token.json が見つかりませんでした。")

import json
# 環境変数からサービスアカウントキーJSONを読み込んで認証
sa_info = json.loads(os.environ["CREDENTIALS_JSON"])
# サービスアカウント認証で gspread クライアントを生成
gc = gspread.service_account_from_dict(sa_info)
# ← ここに一時的に purge_service_account_sheets を呼び出します
purge_service_account_sheets(sa_info)

def _get_master_ws() -> gspread.Worksheet:
    try:
        sh = gc.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(MASTER_SHEET_NAME)
        sh.sheet1.append_row([
            "店舗名", "店舗ID", "座席数", "シートURL", "登録日時", "時間枠"
        ])
    return sh.sheet1

# -------------------------------------------------------------
# 新規シート作成
# -------------------------------------------------------------
def create_store_sheet(
    name: str,
    store_id: int,
    seat_info: str,
    times: List[str]
) -> str:
    SHARED_DRIVE_ID = "XXXXXXXXXXXXXXXXX"  # ← 先に作成した Shared Drive の ID

    # 1) Drive API でスプレッドシートを Shared Drive に作成
    metadata = {
        "name": f"予約表 - {name} ({store_id})",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [SHARED_DRIVE_ID]
    }
    file = drive.files().create(
        body=metadata,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        fields="id, webViewLink"
    ).execute()
    sheet_url = file["webViewLink"]

    # 2) gspread でそのシートを開いて初期行をセット
    ws = gc.open_by_url(sheet_url).sheet1
    ws.update([["月", "日", "時間帯", "名前", "人数", "備考"]])
    if times:
        ws.append_rows([["", "", t, "", "", ""] for t in times],
                       value_input_option="USER_ENTERED")

    # 3) マスターシートにも登録
    master = _get_master_ws()
    master.append_row([
        name,
        store_id,
        seat_info.replace("\n", " "),
        sheet_url,
        dt.datetime.now().isoformat(timespec="seconds"),
        ",".join(times)
    ])
    return sheet_url

# -------------------------------------------------------------
# 予約情報追記
# -------------------------------------------------------------
def append_reservations(
    sheet_url: str,
    rows: List[Dict[str, Any]]
) -> None:
    sh = gc.open_by_url(sheet_url)
    ws = sh.sheet1
    values = [
        [
            r.get("month", ""),
            r.get("day", ""),
            r.get("time", ""),
            r.get("name", ""),
            r.get("size", ""),
            r.get("note", "")
        ]
        for r in rows
    ]
    if values:
        ws.append_rows(values, value_input_option="USER_ENTERED")

# -------------------------------------------------------------
# LINE メッセージ送受信
# -------------------------------------------------------------
def _line_reply(token: str, text: str) -> None:
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"replyToken": token, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )

def _line_push(uid: str, text: str) -> None:
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"to": uid, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )

# -------------------------------------------------------------
# 画像ダウンロード
# -------------------------------------------------------------
def _download_line_img(msg_id: str) -> bytes:
    r = requests.get(
        f"https://api-data.line.me/v2/bot/message/{msg_id}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=15
    )
    r.raise_for_status()
    return r.content

# -------------------------------------------------------------
# 画像解析・要約
# -------------------------------------------------------------
def _vision_describe_sheet(img: bytes) -> str:
    prompt = (
        "画像は、手書きで記入するための予約表です。\n"
        "以下のように簡潔に構成をまとめてください：\n"
        "- 表のタイトル\n"
        "- 日付欄\n"
        "- 列の構成（時間帯、名前、人数、備考など）\n"
        "- 注意書きの内容\n"
        "- テーブル番号の使い分け"
    )
    try:
        res = client.models.generate_content(
            model=MODEL_VISION,
            contents=types.Content(parts=[
                types.Part.from_bytes(data=img, mime_type="image/jpeg"),
                types.Part.from_text(text=prompt)
            ]),
            config=types.GenerateContentConfig(max_output_tokens=1024)
        )
        return res.text.strip()
    except Exception as e:
        print(f"[_vision_describe_sheet] exception={e}")
        return "画像解析に失敗しました。もう一度鮮明な画像をお送りください。"

def _vision_extract_times(img: bytes) -> List[str]:
    prompt = (
        "画像は空欄の飲食店予約表です。\n"
        "予約可能な時間帯 (HH:MM) を、左上→右下の順に重複なく昇順で JSON 配列として返してください。"
    )
    try:
        res = client.models.generate_content(
            model=MODEL_VISION,
            contents=types.Content(parts=[
                types.Part.from_bytes(data=img, mime_type="image/jpeg"),
                types.Part.from_text(text=prompt)
            ]),
            config=types.GenerateContentConfig(max_output_tokens=256)
        )
        data = json.loads(res.text)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[_vision_extract_times] exception={e}")
        return []

def _vision_extract_rows(img: bytes) -> List[Dict[str, Any]]:
    prompt = (
        "画像は手書きの予約表です。各行の予約情報を JSON 配列で返してください。\n"
        "形式: [{\"month\":int,\"day\":int,\"time\":\"HH:MM\",\"name\":str,\"size\":int,\"note\":str}]"
    )
    try:
        res = client.models.generate_content(
            model=MODEL_VISION,
            contents=types.Content(parts=[
                types.Part.from_bytes(data=img, mime_type="image/jpeg"),
                types.Part.from_text(text=prompt)
            ]),
            config=types.GenerateContentConfig(max_output_tokens=2048)
        )
        data = json.loads(res.text)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[_vision_extract_rows] exception={e}")
        return []

def _process_template(uid: str, msg_id: str) -> None:
    st = user_state.get(uid)
    if not st or st.get("step") != "wait_template_img":
        return
    img = _download_line_img(msg_id)
    desc = _vision_describe_sheet(img)
    if "失敗しました" in desc:
        _line_push(uid, desc)
        return
    st.update({"template_img": img, "step": "confirm_template"})
    _line_push(uid, f"{desc}\n\nこの内容でスプレッドシートを作成してよろしいですか？（はい／いいえ）")

def _process_filled(uid: str, msg_id: str) -> None:
    st = user_state.get(uid)
    if not st or st.get("step") != "wait_filled_img":
        return
    img = _download_line_img(msg_id)
    rows = _vision_extract_rows(img)
    if not rows:
        _line_push(uid, "予約情報が検出できませんでした。もう一度鮮明な画像を送ってください。")
        return
    try:
        append_reservations(st["sheet_url"], rows)
    except Exception as e:
        print(f"[_process_filled] error={e}")
        _line_push(uid, "予約情報の追記に失敗しました。再度お試しください。")
        return
    _line_push(uid, f"✅ 予約情報を追記しました！ 最新シート: {st['sheet_url']}")
    st['step'] = 'done'

def _handle_event(event: Dict[str, Any]) -> None:
    try:
        if event.get("type") != "message":
            return
        uid = event["source"]["userId"]
        token = event.get("replyToken", "")
        msg = event["message"]
        mtype = msg.get("type")
        text = msg.get("text", "")
        msg_id = msg.get("id", "")
        st = user_state.setdefault(uid, {"step": "start"})
        step = st.get("step")

        if mtype == "text":
            if step == "start":
                resp = client.models.generate_content(
                    model=MODEL_TEXT,
                    contents=types.Content(parts=[
                        types.Part.from_text(text=f"以下の文から店舗名だけを抽出してください：\n{text}")
                    ]),
                    config=types.GenerateContentConfig(max_output_tokens=64)
                )
                name = resp.text.strip()
                sid = random.randint(100000, 999999)
                st.update({"step": "confirm_store", "store_name": name, "store_id": sid})
                _line_reply(token, f"登録完了：店舗名：{name}\n店舗ID：{sid}\nこの内容でよろしいですか？（はい／いいえ）")
                return
            if step == "confirm_store":
                if "はい" in text:
                    st['step'] = 'ask_seats'
                    _line_reply(token, "座席数を入力してください（例：1人席:3 2人席:2 4人席:1）")
                else:
                    st.update({'step': 'start'})
                    _line_reply(token, "店舗名をもう一度送ってください。")
                return
            if step == 'ask_seats':
                resp = client.models.generate_content(
                    model=MODEL_TEXT,
                    contents=types.Content(parts=[
                        types.Part.from_text(text=(
                            f"以下の文から座席数を抽出し、形式「1人席:◯ 2人席:◯ 4人席:◯」で出力してください：\n{text}"
                        ))
                    ]),
                    config=types.GenerateContentConfig(max_output_tokens=128)
                )
                seat_info = resp.text.strip()
                st.update({'step': 'confirm_seats', 'seat_info': seat_info})
                _line_reply(token, f"座席数確認：{seat_info}\nこの内容で登録しますか？（はい／いいえ）")
                return
            if step == 'confirm_seats':
                if 'はい' in text:
                    st['step'] = 'wait_template_img'
                    _line_reply(token, 'テンプレート画像をお送りください。解析後にシートを作成します。')
                else:
                    st['step'] = 'ask_seats'
                    _line_reply(token, '座席数を再度入力してください。')
                return
            if step == 'confirm_template':
                if 'はい' in text:
                    _line_reply(token, 'シートを作成中です…')
                    times = _vision_extract_times(st['template_img'])
                    url = create_store_sheet(
                        st['store_name'], st['store_id'], st['seat_info'], times
                    )
                    st.update({'step': 'wait_filled_img', 'sheet_url': url})
                    _line_push(uid, f"✅ シート作成完了！ {url}\n記入済みの画像を送ってください。")
                else:
                    st.update({'step': 'wait_template_img'})
                    _line_reply(token, 'テンプレート画像を再度お送りください。')
                return
        if mtype == 'image':
            if step == 'wait_template_img':
                threading.Thread(target=_process_template, args=(uid, msg_id)).start()
                _line_reply(token, '画像を受信しました。解析中…')
                return
            if step == 'wait_filled_img':
                threading.Thread(target=_process_filled, args=(uid, msg_id)).start()
                _line_reply(token, '画像を受信しました。予約情報を抽出中…')
                return
            _line_reply(token, '現在この画像は処理できません。')
    except Exception as e:
        print(f"[handle_event error] {e}")
        _line_reply(event.get('replyToken', ''), '内部エラーが発生しました。再度お試しください。')

@app.route('/', methods=['GET', 'HEAD', 'POST'])
def webhook() -> tuple[str, int]:
    if request.method in ('GET', 'HEAD'):
        return 'OK', 200
    body = request.get_json(force=True, silent=True) or {}
    events = body.get('events', [])
    if not events:
        return 'NOEVENT', 200
    threading.Thread(target=_handle_event, args=(events[0],)).start()
    return 'OK', 200

if __name__ == '__main__':
    # ===== 以下はスプレッドシート自動作成テスト用コード =====
    # テスト用の店舗情報でシートを自動生成します。不要なら削除してください。
    test_url = create_store_sheet(
        name='自動テスト店',
        store_id=111111,
        seat_info='1人席:3 2人席:2 4人席:1',
        times=['18:00', '19:00']
    )
    print(f'自動作成されたシートURL: {test_url}')
    # ===========================================

    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
