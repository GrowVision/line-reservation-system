# LINE予約管理 BOT  ── Gemini + Google Sheets 版
# -------------------------------------------------------------
# 1️⃣  店舗登録（店舗名・ID・座席数）
# 2️⃣  空の予約表テンプレ画像 → 時間枠抽出
# 3️⃣  店舗専用スプレッドシート自動生成
# 4️⃣  記入済み予約表画像 → 当日シートに追記
# -------------------------------------------------------------

from __future__ import annotations
import base64, datetime as dt, json, os, random, threading
from typing import Any, Dict, List

import google.generativeai as genai          # Gemini SDK
import gspread, requests
from dotenv import load_dotenv
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials

# -------------------------------------------------------------
# 0. 環境変数 & Gemini モデル ID
# -------------------------------------------------------------
load_dotenv()
GEMINI_API_KEY            = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_JSON               = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT")
MASTER_SHEET_NAME         = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (GEMINI_API_KEY and LINE_CHANNEL_ACCESS_TOKEN and GOOGLE_JSON):
    raise RuntimeError("環境変数 GEMINI_API_KEY / LINE_CHANNEL_ACCESS_TOKEN / GOOGLE_CREDENTIALS_JSON を設定してください")

# ✅ 404 を防ぐため “models/…” で始まる完全 ID を使用
MODEL_TEXT   = "models/gemini-1.5-pro-latest"   # テキスト専用
MODEL_VISION = "models/gemini-1.5-pro-latest"   # 画像入力対応

genai.configure(api_key=GEMINI_API_KEY)

# -------------------------------------------------------------
# 1. Flask アプリ
# -------------------------------------------------------------
app = Flask(__name__)
user_state: Dict[str, Dict[str, Any]] = {}

# -------------------------------------------------------------
# 2. Google Sheets 認証
# -------------------------------------------------------------
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds  = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_JSON), SCOPES)
gs     = gspread.authorize(creds)

def _get_master_ws():
    try:
        sh = gs.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs.create(MASTER_SHEET_NAME)
        sh.sheet1.append_row(["店舗名", "店舗ID", "座席数", "シートURL", "登録日時", "時間枠"])
    return sh.sheet1

def create_store_sheet(name: str, store_id: int, seat_info: str, times: List[str]) -> str:
    sh = gs.create(f"予約表 - {name} ({store_id})")
    sh.share(None, perm_type="anyone", role="writer")         # 必要に応じて権限制御
    ws = sh.sheet1
    ws.update([["月", "日", "時間帯", "名前", "人数", "備考"]])
    if times:
        ws.append_rows([["", "", t, "", "", ""] for t in times], value_input_option="USER_ENTERED")
    _get_master_ws().append_row([
        name, store_id, seat_info.replace("\n", " "), sh.url,
        dt.datetime.now().isoformat(timespec="seconds"), ",".join(times)
    ])
    return sh.url

def append_reservations(sheet_url: str, rows: List[Dict[str, Any]]):
    if not rows:
        return
    ws      = gs.open_by_url(sheet_url).sheet1
    header  = ws.row_values(1)
    col_tm  = header.index("時間帯") + 1 if "時間帯" in header else 3
    existing = {ws.cell(r, col_tm).value: r for r in range(2, ws.row_count + 1) if ws.cell(r, col_tm).value}
    for r in rows:
        tgt = existing.get(r.get("time")) or ws.row_count + 1
        ws.update(
            f"A{tgt}:F{tgt}",
            [[r.get(k, "") for k in ("month", "day", "time", "name", "size", "note")]]
        )

# -------------------------------------------------------------
# 3. LINE 返信ユーティリティ
# -------------------------------------------------------------
def _line_reply(token: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                 "Content-Type": "application/json"},
        json={"replyToken": token, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )

def _line_push(uid: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                 "Content-Type": "application/json"},
        json={"to": uid, "messages": [{"type": "text", "text": text}]},
        timeout=10
    )

# -------------------------------------------------------------
# 4. Gemini 呼び出しヘルパ
# -------------------------------------------------------------
def _gemini_text(prompt: str, max_t: int = 256) -> str:
    res = genai.GenerativeModel(MODEL_TEXT).generate_content(
        prompt, generation_config={"max_output_tokens": max_t}
    )
    return res.text.strip()

def _gemini_vision(img_b64: str, prompt: str, max_t: int = 1024) -> str:
    res = genai.GenerativeModel(MODEL_VISION).generate_content(
        [
            {"type": "image", "data": img_b64, "mime_type": "image/jpeg"},
            {"type": "text",  "text": prompt}
        ],
        generation_config={"max_output_tokens": max_t}
    )
    return res.text

# -------------------------------------------------------------
# 5. 画像解析
# -------------------------------------------------------------
def _vision_extract_times(img: bytes) -> List[str]:
    prompt = ("画像は空欄の飲食店予約表です。予約可能な時間帯 (HH:MM) を、"
              "左上→右下の順に重複なく昇順で JSON 配列として返してください。")
    try:
        data = json.loads(_gemini_vision(base64.b64encode(img).decode(), prompt, 256))
        return [str(t) for t in data] if isinstance(data, list) else []
    except Exception:
        return []

def _vision_extract_rows(img: bytes) -> List[Dict[str, Any]]:
    prompt = ("画像は手書きの予約表です。各行の予約情報を JSON 配列で返してください。"
              "形式: [{\"month\":int,\"day\":int,\"time\":\"HH:MM\","
              "\"name\":str,\"size\":int,\"note\":str}]")
    try:
        data = json.loads(_gemini_vision(base64.b64encode(img).decode(), prompt, 2048))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _download_line_img(msg_id: str) -> bytes:
    r = requests.get(f"https://api-data.line.me/v2/bot/message/{msg_id}/content",
                     headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}, timeout=15)
    r.raise_for_status()
    return r.content

# -------------------------------------------------------------
# 6. 背景スレッド処理
# -------------------------------------------------------------
def _process_template(uid: str, msg_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_template_img":
        return
    img   = _download_line_img(msg_id)
    times = _vision_extract_times(img)
    if not times:
        _line_push(uid, "画像の解析に失敗しました。鮮明な『空欄の予約表』画像をもう一度お送りください。")
        return
    st["times"] = times
    st["step"]  = "confirm_times"
    _line_push(uid,
        "📊 予約表構造の分析が完了しました！\n\n"
        "検出された時間帯：\n" + "\n".join(f"・{t}〜" for t in times) + "\n\n"
        "この内容でスプレッドシートを作成してよろしいですか？（はい／いいえ）")

def _process_filled(uid: str, msg_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_filled_img":
        return
    img  = _download_line_img(msg_id)
    rows = _vision_extract_rows(img)
    if not rows:
        _line_push(uid, "予約情報が検出できませんでした。鮮明な画像をもう一度お送りください。")
        return
    append_reservations(st["sheet_url"], rows)
    _line_push(uid, "✅ 予約情報をスプレッドシートに追記しました！")

# -------------------------------------------------------------
# 7. Webhook
# -------------------------------------------------------------
@app.route("/", methods=["GET", "HEAD", "POST"])
def webhook():
    if request.method in {"GET", "HEAD"}:
        return "OK", 200
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("events"):
        return "NOEVENT", 200
    threading.Thread(target=_handle_event, args=(body["events"][0],)).start()
    return "OK", 200

# -------------------------------------------------------------
# 8. メインロジック
# -------------------------------------------------------------
def _handle_event(event: Dict[str, Any]):
    try:
        if event["type"] != "message":
            return
        uid      = event["source"]["userId"]
        token    = event["replyToken"]
        mtype    = event["message"]["type"]
        text     = event["message"].get("text", "")
        msg_id   = event["message"].get("id", "")
        st       = user_state.setdefault(uid, {"step": "start"})
        step     = st["step"]

        # ---------- TEXT ----------
        if mtype == "text":

            # ① 店舗名登録
            if step == "start":
                store_name = _gemini_text(f"以下の文から店舗名だけを抽出してください：\n{text}", 64)
                store_id   = random.randint(100000, 999999)
                st.update({"step": "confirm_store", "store_name": store_name, "store_id": store_id})
                _line_reply(token,
                    f"登録完了：店舗名：{store_name}\n店舗ID：{store_id}\n\n"
                    "この内容で間違いないですか？（はい／いいえ）")
                return

            # ② 店舗名確認
            if step == "confirm_store":
                if "はい" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "座席数を入力してください (例: 1人席:3 2人席:2 4人席:1)")
                elif "いいえ" in text:
                    st.clear(); st["step"] = "start"
                    _line_reply(token, "もう一度、店舗名を送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            # ③ 座席数入力
            if step == "ask_seats":
                seat_info = _gemini_text(
                    "以下の文から 1人席, 2人席, 4人席 の数を抽出し、"
                    "次の形式で出力してください:\n1人席: ◯席\n2人席: ◯席\n4人席: ◯席\n\n" + text,
                    128
                )
                st.update({"seat_info": seat_info, "step": "confirm_seats"})
                _line_reply(token,
                    "✅ 登録情報の確認です：\n\n"
                    f"・店舗名：{st['store_name']}\n"
                    f"・店舗ID：{st['store_id']}\n"
                    f"・座席数：\n{seat_info}\n\n"
                    "この内容で登録してもよろしいですか？（はい／いいえ）")
                return

            # ④ 座席数確認
            if step == "confirm_seats":
                if "はい" in text:
                    st["step"] = "wait_template_img"
                    _line_reply(token,
                        "ありがとうございます！店舗登録が完了しました🎉\n\n"
                        "まず『空欄の予約表』の写真を送ってください。\n"
                        "AI がフォーマットを学習し、スプレッドシートを作成します。")
                elif "いいえ" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "もう一度、座席数を入力してください。(例: 1人席:3 2人席:2 4人席:1)")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            # ⑤ 時間枠確認
            if step == "confirm_times":
                if "はい" in text:
                    st["sheet_url"] = create_store_sheet(
                        st["store_name"], st["store_id"], st["seat_info"], st["times"])
                    st["step"] = "wait_filled_img"
                    _line_reply(token,
                        "スプレッドシートを作成しました！\n"
                        f"📄 {st['sheet_url']}\n\n"
                        "当日の予約を書き込んだ紙の写真を送っていただくと、自動で追記します。")
                elif "いいえ" in text:
                    st["step"] = "wait_template_img"
                    _line_reply(token, "わかりました。空欄の予約表画像をもう一度送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

        # ---------- IMAGE ----------
        if mtype == "image":
            if step == "wait_template_img":
                threading.Thread(target=_process_template, args=(uid, msg_id)).start()
                _line_reply(token, "画像を受け取りました。AI がフォーマットを解析中です…")
                return
            if step == "wait_filled_img":
                threading.Thread(target=_process_filled, args=(uid, msg_id)).start()
                _line_reply(token, "画像を受け取りました。AI が予約内容を抽出中です…")
                return
            _line_reply(token, "画像を受信しましたが、現在は解析の準備ができていません。")

    except Exception as e:
        print("[handle_event error]", e)
        try:
            _line_reply(event.get("replyToken", ""), "エラーが発生しました。もう一度お試しください。")
        except Exception:
            pass

# -------------------------------------------------------------
# 9. アプリ起動
# -------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
