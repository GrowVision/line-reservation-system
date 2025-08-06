# LINE予約管理BOT ─ Gemini + Google Sheets 版
# -------------------------------------------------------------
# 1️⃣ 店舗登録 → 2️⃣ 空テンプレ画像解析 → 3️⃣ シート生成 → 4️⃣ 記入済み追記
# -------------------------------------------------------------
from __future__ import annotations
import base64, datetime as dt, json, os, random, threading, traceback
from typing import Any, Dict, List

import google.generativeai as genai      # Gemini SDK
import gspread, requests
from dotenv import load_dotenv
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials

# ---------- 0. 環境変数 ----------
load_dotenv()
GEMINI_API_KEY            = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GOOGLE_JSON               = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT")
MASTER_SHEET_NAME         = os.getenv("MASTER_SHEET_NAME", "契約店舗一覧")

if not (GEMINI_API_KEY and LINE_CHANNEL_ACCESS_TOKEN and GOOGLE_JSON):
    raise RuntimeError("GEMINI_API_KEY / LINE_CHANNEL_ACCESS_TOKEN / GOOGLE_CREDENTIALS_JSON を設定してください")

genai.configure(api_key=GEMINI_API_KEY)

# ---------- 1. Flask ----------
app = Flask(__name__)
user_state: Dict[str, Dict[str, Any]] = {}

# ---------- 2. Google Sheets ----------
SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/drive"]
creds     = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_JSON), SCOPES)
gs_client = gspread.authorize(creds)


def _master_ws():
    try:
        sh = gs_client.open(MASTER_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gs_client.create(MASTER_SHEET_NAME)
        sh.sheet1.append_row(["店舗名", "店舗ID", "座席数", "シートURL", "登録日時", "時間枠"])
    return sh.sheet1


def create_store_sheet(name: str, sid: int, seats: str, times: List[str]) -> str:
    sh = gs_client.create(f"予約表 - {name} ({sid})")
    sh.share(None, perm_type="anyone", role="writer")            # 必要に応じて変更
    ws = sh.sheet1
    ws.update([["月", "日", "時間帯", "名前", "人数", "備考"]])
    if times:
        ws.append_rows([["", "", t, "", "", ""] for t in times], value_input_option="USER_ENTERED")
    _master_ws().append_row([name, sid, seats.replace("\n", " "), sh.url,
                             dt.datetime.now().isoformat(timespec="seconds"), ",".join(times)])
    return sh.url


def append_reservations(url: str, rows: List[Dict[str, Any]]):
    if not rows:
        return
    ws     = gs_client.open_by_url(url).sheet1
    header = ws.row_values(1)
    idx    = header.index("時間帯") + 1 if "時間帯" in header else 3
    exist  = {ws.cell(r, idx).value: r
              for r in range(2, ws.row_count + 1) if ws.cell(r, idx).value}
    for r in rows:
        tgt = exist.get(r.get("time")) or ws.row_count + 1
        ws.update(f"A{tgt}:F{tgt}",
                  [[r.get(k, "") for k in ("month", "day", "time", "name", "size", "note")]])

# ---------- 3. LINE utils ----------
def _line_reply(token: str, text: str):
    requests.post("https://api.line.me/v2/bot/message/reply",
                  headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                           "Content-Type": "application/json"},
                  json={"replyToken": token,
                        "messages": [{"type": "text", "text": text}]},
                  timeout=10)


def _line_push(uid: str, text: str):
    requests.post("https://api.line.me/v2/bot/message/push",
                  headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                           "Content-Type": "application/json"},
                  json={"to": uid,
                        "messages": [{"type": "text", "text": text}]},
                  timeout=10)

# ---------- 4. Gemini wrappers ----------
MODEL_TEXT   = "gemini-pro"          # ✅ v1beta で使える ID
MODEL_VISION = "gemini-pro-vision"   # ✅ v1beta で使える ID


def _gemini_text(prompt: str, max_t: int = 256) -> str:
    try:
        res = genai.GenerativeModel(MODEL_TEXT).generate_content(
            prompt, generation_config={"max_output_tokens": max_t})
        return res.text.strip()
    except Exception:
        traceback.print_exc()
        return ""


def _gemini_vis(img_b64: str, prompt: str, max_t: int = 1024) -> str:
    try:
        res = genai.GenerativeModel(MODEL_VISION).generate_content(
            [{"type": "image", "data": img_b64, "mime_type": "image/jpeg"},
             {"type": "text",  "text": prompt}],
            generation_config={"max_output_tokens": max_t})
        return res.text
    except Exception:
        traceback.print_exc()
        return ""

# ---------- 5. Vision helpers ----------
def _extract_times(img: bytes) -> List[str]:
    p = ("画像は空欄の飲食店予約表です。予約可能な時間帯 (HH:MM) を左上→右下の順に "
         "重複なく JSON 配列で返してください。")
    res = _gemini_vis(base64.b64encode(img).decode(), p, 256)
    try:
        data = json.loads(res)
        return [str(t) for t in data] if isinstance(data, list) else []
    except Exception:
        return []


def _extract_rows(img: bytes) -> List[Dict[str, Any]]:
    p = ("画像は手書きの予約表です。各行の予約情報を JSON 配列で返してください。"
         "形式:[{\"month\":int,\"day\":int,\"time\":\"HH:MM\",\"name\":str,\"size\":int,\"note\":str}]")
    res = _gemini_vis(base64.b64encode(img).decode(), p, 2048)
    try:
        data = json.loads(res)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _download(msg_id: str) -> bytes:
    r = requests.get(f"https://api-data.line.me/v2/bot/message/{msg_id}/content",
                     headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}, timeout=15)
    r.raise_for_status()
    return r.content

# ---------- 6. image threads ----------
def _proc_template(uid: str, msg_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_template_img":
        return
    times = _extract_times(_download(msg_id))
    if not times:
        _line_push(uid, "画像解析に失敗しました。鮮明な『空欄の予約表』を再送ください。")
        return
    st.update({"times": times, "step": "confirm_times"})
    _line_push(uid,
               "📊 解析完了！\n\n検出時間帯:\n" +
               "\n".join(f"・{t}〜" for t in times) +
               "\n\nこの内容でシートを作成しますか？（はい／いいえ）")


def _proc_filled(uid: str, msg_id: str):
    st = user_state.get(uid)
    if not st or st["step"] != "wait_filled_img":
        return
    rows = _extract_rows(_download(msg_id))
    if not rows:
        _line_push(uid, "予約情報が検出できませんでした。鮮明な画像をもう一度お願いします。")
        return
    append_reservations(st["sheet_url"], rows)
    _line_push(uid, "✅ スプレッドシートに予約を追記しました！")

# ---------- 7. webhook ----------
@app.route("/", methods=["GET", "HEAD", "POST"])
def webhook():
    if request.method in {"GET", "HEAD"}:
        return "OK", 200
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("events"):
        return "NOEVENT", 200
    threading.Thread(target=_handle_event, args=(body["events"][0],)).start()
    return "OK", 200

# ---------- 8. main logic ----------
def _handle_event(ev: Dict[str, Any]):
    try:
        if ev["type"] != "message":
            return
        uid     = ev["source"]["userId"]
        token   = ev["replyToken"]
        mtype   = ev["message"]["type"]
        text    = ev["message"].get("text", "")
        msg_id  = ev["message"].get("id", "")
        st      = user_state.setdefault(uid, {"step": "start"})
        step    = st["step"]

        # ----- TEXT -----
        if mtype == "text":
            # ① 店舗名入力
            if step == "start":
                name = _gemini_text(f"以下の文から店舗名だけを抽出してください：\n{text}")
                if not name:
                    _line_reply(token, "店舗名を抽出できませんでした。もう一度送ってください。")
                    return
                sid = random.randint(100000, 999999)
                st.update({"step": "confirm_store", "store_name": name, "store_id": sid})
                _line_reply(token,
                            f"登録完了：店舗名：{name}\n店舗ID：{sid}\n\n"
                            "この内容でよろしいですか？（はい／いいえ）")
                return

            # ② 店舗名確認
            if step == "confirm_store":
                if "はい" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "座席数を入力してください (例: 1人席:3 2人席:2 4人席:1)")
                elif "いいえ" in text:
                    st.clear()
                    st["step"] = "start"
                    _line_reply(token, "もう一度、店舗名を送ってください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            # ③ 座席数入力
            if step == "ask_seats":
                seat = _gemini_text(
                    "次の文から 1人席, 2人席, 4人席 の数を抽出し、"
                    "次の形式で出力してください:\n1人席: ◯席\n2人席: ◯席\n4人席: ◯席\n\n" + text, 128)
                if not seat:
                    _line_reply(token, "座席数を抽出できませんでした。もう一度入力してください。")
                    return
                st.update({"seat_info": seat, "step": "confirm_seats"})
                _line_reply(token,
                            "✅ 確認:\n\n"
                            f"・店舗名：{st['store_name']}\n"
                            f"・店舗ID：{st['store_id']}\n"
                            f"・座席数：\n{seat}\n\n"
                            "登録しますか？（はい／いいえ）")
                return

            # ④ 座席数確認
            if step == "confirm_seats":
                if "はい" in text:
                    st["step"] = "wait_template_img"
                    _line_reply(token,
                                "ありがとうございます！\n"
                                "まず空欄の予約表の写真を送ってください。")
                elif "いいえ" in text:
                    st["step"] = "ask_seats"
                    _line_reply(token, "座席数をもう一度入力してください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

            # ⑤ 時間枠確認
            if step == "confirm_times":
                if "はい" in text:
                    st["sheet_url"] = create_store_sheet(st["store_name"], st["store_id"],
                                                         st["seat_info"], st["times"])
                    st["step"] = "wait_filled_img"
                    _line_reply(token,
                                "スプレッドシートを作成しました！\n"
                                f"📄 {st['sheet_url']}\n\n"
                                "当日の予約を書き込んだ紙の写真を送ってください。")
                elif "いいえ" in text:
                    st["step"] = "wait_template_img"
                    _line_reply(token,
                                "空欄の予約表画像をもう一度お送りください。")
                else:
                    _line_reply(token, "「はい」または「いいえ」でお答えください。")
                return

        # ----- IMAGE -----
        if mtype == "image":
            if step == "wait_template_img":
                threading.Thread(target=_proc_template, args=(uid, msg_id)).start()
                _line_reply(token, "画像を受け取りました。解析中です…")
                return
            if step == "wait_filled_img":
                threading.Thread(target=_proc_filled, args=(uid, msg_id)).start()
                _line_reply(token, "画像を受け取りました。予約内容を抽出中です…")
                return
            _line_reply(token, "画像を受信しましたが、いまは解析できません。")

    except Exception:
        traceback.print_exc()
        try:
            _line_reply(ev.get("replyToken", ""), "エラーが発生しました。もう一度お試しください。")
        except Exception:
            pass

# ---------- 9. run ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
