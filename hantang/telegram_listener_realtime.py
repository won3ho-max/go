#!/usr/bin/env python3
"""
한탕 스터디 — 텔레그램 실시간 종목 감지 리스너 (GCP 상주용)
═══════════════════════════════════════════════════════════════════════════
long-polling(getUpdates timeout=50)으로 그룹 메시지를 실시간 수신.
  • 매수 감지(#종목추천 / #매수) → 구글시트 J/K열 자동 기록
  • 매도 감지(#매도 / #청산)     → 개인 텔레그램 알림만 (시트는 manual_sell로 처리)
  • 감지 결과(성공/실패)는 항상 운영자 개인 텔레그램으로 통지

⚠️ 전제조건: BotFather에서 봇 privacy mode를 Disable 해야 그룹 일반 메시지를
   수신할 수 있음. (can_read_all_group_messages == true)

환경변수:
  TELEGRAM_TOKEN       - 한탕 봇 토큰
  GSHEETS_CREDENTIALS  - 서비스 계정 JSON 문자열
  GSHEETS_ID           - 스프레드시트 ID
  ADMIN_CHAT_ID        - 운영자 개인 텔레그램 chat_id (기본 1633958343)
  GROUP_CHAT_ID        - (선택) 감시 대상 그룹 chat_id. 지정 시 해당 그룹만 처리.
"""

import os
import re
import sys
import json
import time
import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
import requests

# ── 설정 ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID  = os.environ.get("ADMIN_CHAT_ID", "1633958343")
GROUP_CHAT_ID  = os.environ.get("GROUP_CHAT_ID", "").strip()  # 빈값이면 모든 그룹 처리
COLLECT_MODE   = os.environ.get("COLLECT_MODE", "").lower() in ("1", "true", "yes")  # 1일 수집 모드
BASE_DIR       = Path(os.path.dirname(os.path.abspath(__file__)))
OFFSET_FILE    = BASE_DIR / "_realtime_offset.txt"
SCOPES         = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
POLL_TIMEOUT   = 50   # long-poll 초
SHEET_TTL      = 60   # 시트 캐시 유효시간(초)

BUY_TAGS  = ("#종목추천", "#매수", "#추천")
SELL_TAGS = ("#매도", "#청산", "#매도종목")

STUDY_MEMBERS = ["안병열", "김동환", "이광훈", "송지호",
                 "조형오", "어정윤", "이원호", "김태완"]

# 텔레그램 user_id → 스터디 멤버 (수집 결과 기반 매핑)
MEMBER_MAP = {
    304508615:  "안병열",
    6299662296: "김동환",
    5495509979: "송지호",
    5806062535: "김태완",
    7153765145: "이광훈",   # KH / @kjrwq
    656841455:  "어정윤",   # Jy
    721276353:  "조형오",   # 모모
    1087968824: "이원호",   # 익명 GroupAnonymousBot = 원호 본인
}

_seen_senders = set()   # 수집 모드: 이미 보고한 보낸이 id


def collect_sender(msg: dict, cache):
    """수집 모드: 그룹 글쓴이의 id/username/표시이름을 개인텔레+_collect 시트에 1회 기록."""
    frm = msg.get("from") or {}
    uid = frm.get("id")
    if uid is None or uid in _seen_senders:
        return
    _seen_senders.add(uid)
    uname = frm.get("username", "") or ""
    fname = ((frm.get("first_name", "") or "") + " " + (frm.get("last_name", "") or "")).strip()
    sample = (msg.get("text", "") or msg.get("caption", "") or "")[:40]
    try:
        ss = cache.get_ss()
        try:
            wc = ss.worksheet("_collect")
        except gspread.WorksheetNotFound:
            wc = ss.add_worksheet(title="_collect", rows=200, cols=6)
            wc.append_row(["ts", "user_id", "username", "name", "sample"])
        wc.append_row([datetime.datetime.now().isoformat(timespec="seconds"),
                       str(uid), uname, fname, sample])
    except Exception as e:
        log(f"[수집] 시트 기록 실패: {e}")
    notify_admin(f"📇 수집: {fname} @{uname} id={uid}\n  예: {sample}")
    log(f"[수집] {fname} @{uname} id={uid}")


def load_seen_from_sheet(cache):
    try:
        ss = cache.get_ss()
        wc = ss.worksheet("_collect")
        for row in wc.get_all_values()[1:]:
            if len(row) > 1 and row[1].strip().isdigit():
                _seen_senders.add(int(row[1]))
    except Exception:
        pass


def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── 텔레그램 유틸 ─────────────────────────────────────────────────────────
def tg_get(method, **params):
    return requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
        params=params, timeout=POLL_TIMEOUT + 15).json()


def tg_send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception as e:
        log(f"[경고] tg_send 실패: {e}")


def notify_admin(text: str):
    """운영자 개인 텔레그램으로 통지."""
    tg_send(ADMIN_CHAT_ID, text)


# ── Google Sheets ────────────────────────────────────────────────────────
def open_spreadsheet() -> gspread.Spreadsheet:
    creds_json = os.environ.get("GSHEETS_CREDENTIALS", "")
    if creds_json:
        info = json.loads(creds_json)
    else:
        info = json.loads((BASE_DIR / "credentials.json").read_text())
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet_id = os.environ.get("GSHEETS_ID", "")
    if not sheet_id:
        sheet_id = (BASE_DIR / "gsheets_id.txt").read_text().strip()
    return gc.open_by_key(sheet_id)


def get_worksheet(ss: gspread.Spreadsheet) -> gspread.Worksheet:
    sheets = [s for s in ss.worksheets() if not s.title.startswith("_")]
    return sheets[-1]   # 최신 분기 시트


def find_person_blocks(all_values: list) -> list:
    header_rows, sogyae_rows = [], []
    for i, row in enumerate(all_values):
        j = row[9]  if len(row) > 9  else ""
        p = row[15] if len(row) > 15 else ""
        if j == "종목명":
            header_rows.append(i + 1)
        if "실현수익률 소계" in str(p):
            sogyae_rows.append(i + 1)
    blocks = []
    for h_row in header_rows:
        s_row = next((r for r in sogyae_rows if r > h_row), None)
        if not s_row:
            continue
        i_val = all_values[h_row][8] if len(all_values[h_row]) > 8 else ""
        person = str(i_val).strip().replace("\n", "") if i_val else ""
        blocks.append({"person": person,
                       "row_start": h_row + 1, "row_end": s_row - 1})
    return blocks


def add_stock(ws, all_values, person_name, stock_name, rec_date):
    blocks = find_person_blocks(all_values)
    block = next((b for b in blocks
                  if b["person"] == person_name or person_name in b["person"]),
                 None)
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    empty_row = next(
        (r for r in range(block["row_start"], block["row_end"] + 1)
         if r - 1 < len(all_values)
         and not (all_values[r - 1][9] if len(all_values[r - 1]) > 9 else "")),
        None)
    if not empty_row:
        return False, f"'{person_name}' 블록에 빈 행 없음"

    cell_j = ws.cell(empty_row, 10)
    cell_k = ws.cell(empty_row, 11)
    cell_j.value = stock_name
    cell_k.value = str(rec_date)
    ws.update_cells([cell_j, cell_k], value_input_option="USER_ENTERED")
    return True, f"{person_name} / {stock_name} 기록 완료 (기준가는 오늘 장 마감 후 자동입력)"


# ── 메시지 파싱 ───────────────────────────────────────────────────────────
def detect_action(text: str):
    """반환: ('buy'|'sell'|None, person, stock)
    형식: '<이름> <종목> #종목추천'  또는  '<이름> <종목> #매도'
    이름/종목 순서가 바뀌어도 STUDY_MEMBERS로 보정."""
    if not text:
        return None, None, None

    has_buy  = any(t in text for t in BUY_TAGS)
    has_sell = any(t in text for t in SELL_TAGS)
    if not (has_buy or has_sell):
        return None, None, None
    action = "sell" if has_sell else "buy"

    clean = re.sub(r"#\S+", "", text).strip()
    clean = re.sub(r"[\(\)\[\]:,/]", " ", clean)
    tokens = [t for t in clean.split() if t]
    if len(tokens) < 2:
        return action, None, None

    # 멤버 이름을 토큰 중에서 우선 식별 (순서 무관)
    person = next((t for t in tokens
                   if any(m == t or m in t or t in m for m in STUDY_MEMBERS)), None)
    if person:
        rest = [t for t in tokens if t != person]
        stock = rest[0] if rest else None
    else:
        person, stock = tokens[0], tokens[1]
    return action, person, stock


def detect_tag(text: str):
    """매수/매도 태그만 판별 (신원은 보낸이 user_id로 확정)."""
    if not text:
        return None
    if any(t in text for t in SELL_TAGS):
        return "sell"
    if any(t in text for t in BUY_TAGS):
        return "buy"
    return None


def stock_candidate(text: str) -> str:
    """원문에서 종목명 후보(해시태그·기호 제거 후 첫 토큰)."""
    clean = re.sub(r"#\S+", "", text or "")
    clean = re.sub(r"[\(\)\[\]:,/]", " ", clean).strip()
    toks = clean.split()
    return toks[0] if toks else ""


# ── 처리 ──────────────────────────────────────────────────────────────────
class SheetCache:
    def __init__(self):
        self.ss = None
        self.ws = None
        self.values = None
        self.loaded_at = 0

    def get_ss(self):
        if self.ss is None:
            self.ss = open_spreadsheet()
        return self.ss

    def ensure(self):
        if self.ss is None:
            self.ss = open_spreadsheet()
            self.ws = get_worksheet(self.ss)
        if self.values is None or (time.time() - self.loaded_at) > SHEET_TTL:
            self.values = self.ws.get_all_values()
            self.loaded_at = time.time()
        return self.ws, self.values

    def refresh(self):
        self.values = self.ws.get_all_values()
        self.loaded_at = time.time()


def handle_message(msg: dict, cache: SheetCache):
    text = msg.get("text", "") or msg.get("caption", "") or ""
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    sender = (msg.get("from") or {}).get("first_name", "")

    # 그룹 필터 (지정된 경우)
    if GROUP_CHAT_ID and str(chat_id) != GROUP_CHAT_ID:
        return

    # 수집 모드: 감지/기록 없이 보낸이 정보만 모은다
    if COLLECT_MODE:
        collect_sender(msg, cache)
        return

    tag = detect_tag(text)
    if tag is None:
        return  # 매수/매도 태그 없음 → 무시(알림 없음)

    frm = msg.get("from") or {}
    sid = frm.get("id")
    member = MEMBER_MAP.get(sid)
    who = member if member else f"미매핑(id={sid}, {sender})"
    cand = stock_candidate(text)
    body = text.strip().replace("\n", " ")[:200]

    if tag == "buy":
        notify_admin(
            f"🟢 매수 감지 — {who}\n"
            f"후보종목: {cand}\n원문: {body}\n"
            f"※ 시트 기록은 batch_add로 확정")
        log(f"[매수감지] {who} / {cand}")
    else:  # sell
        notify_admin(
            f"🔴 매도 감지 — {who}\n"
            f"후보종목: {cand}\n원문: {body}\n"
            f"※ manual_sell로 확정 (매도가=매도일 종가)")
        log(f"[매도감지] {who} / {cand}")


# ── offset 영속화 (로컬 파일) ─────────────────────────────────────────────
def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def save_offset(offset: int):
    try:
        OFFSET_FILE.write_text(str(offset))
    except Exception as e:
        log(f"[경고] offset 저장 실패: {e}")


# ── 메인 루프 ─────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log("[치명] TELEGRAM_TOKEN 미설정"); sys.exit(1)

    me = tg_get("getMe")
    if not me.get("ok"):
        log(f"[치명] getMe 실패: {me}"); sys.exit(1)
    bot = me["result"]
    can_read = bot.get("can_read_all_group_messages")
    log(f"봇 시작: @{bot.get('username')} (privacy_disabled={can_read})")
    if not can_read:
        notify_admin(
            "⚠️ 리스너 시작됨 — 그러나 봇 privacy mode가 켜져 있습니다.\n"
            "BotFather → /setprivacy → 봇 선택 → Disable 후\n"
            "그룹에서 봇을 내보냈다가 다시 추가하세요.\n"
            "(현재 상태로는 그룹 일반 메시지를 수신하지 못합니다.)")

    if COLLECT_MODE:
        notify_admin("📇 수집 모드 ON — 1일간 그룹 글쓴이 정보를 모읍니다 (감지/기록 일시중지)")
        log("수집 모드 ON")

    offset = load_offset()
    if not OFFSET_FILE.exists():
        # 첫 실행: 텔레그램에 쌓인 과거 백로그(최대 24h)는 처리하지 않고 건너뛴다
        try:
            r0 = tg_get("getUpdates", offset=-1, timeout=0)
            if r0.get("ok") and r0.get("result"):
                offset = r0["result"][-1]["update_id"] + 1
            save_offset(offset)
            log(f"첫 실행 — 백로그 건너뜀 (offset={offset})")
        except Exception as e:
            log(f"[경고] 백로그 스킵 실패: {e}")
    log(f"시작 offset: {offset}")
    notify_admin(f"🟢 한탕 실시간 리스너 가동 (@{bot.get('username')})")

    cache = SheetCache()
    if COLLECT_MODE:
        load_seen_from_sheet(cache)
        log(f"기존 수집 인원: {len(_seen_senders)}명")
    backoff = 1

    while True:
        try:
            resp = tg_get("getUpdates", offset=offset,
                          timeout=POLL_TIMEOUT,
                          allowed_updates='["message","channel_post"]')
            if not resp.get("ok"):
                # 409: 다른 getUpdates 소비자 충돌 (구 워크플로우 등)
                log(f"[오류] getUpdates: {resp}")
                time.sleep(min(backoff, 60)); backoff = min(backoff * 2, 60)
                continue
            backoff = 1
            updates = resp.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post") or {}
                try:
                    handle_message(msg, cache)
                except Exception as e:
                    log(f"[오류] handle_message: {e}")
                    notify_admin(f"⚠️ 메시지 처리 오류: {e}")
            if updates:
                save_offset(offset)
        except requests.exceptions.RequestException as e:
            log(f"[네트워크] {e}")
            time.sleep(min(backoff, 30)); backoff = min(backoff * 2, 30)
        except Exception as e:
            log(f"[루프오류] {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
