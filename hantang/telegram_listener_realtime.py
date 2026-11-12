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
    1633958343: "이원호",   # 원호 본인 계정(비익명)
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
    """텔레그램 발송. 429(retry_after)·5xx는 기다렸다 다시 보낸다.
    예전엔 응답을 보지 않아, 텔레그램이 429/502를 주면 알림이 소리 없이 사라졌다
    (2026-11-09 21:52 이원호 '신한 금 선물 ETN' — 시트엔 기록됐는데 기록 알림이 안 옴)."""
    for i in range(5):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text}, timeout=15)
            if r.ok:
                return True
            wait = 3 * (i + 1)
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", wait)) + 1
            except Exception:
                pass
            if r.status_code not in (429, 500, 502, 503, 504):
                log(f"[경고] tg_send 거부 {r.status_code}: {r.text[:120]}")
                return False
            time.sleep(min(wait, 30))
        except Exception as e:
            log(f"[경고] tg_send 예외: {e}")
            time.sleep(3 * (i + 1))
    log(f"[경고] tg_send 최종 실패 — 알림 유실: {text[:120]}")
    return False


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
    return sheets[-1]   # 최신 분기 시트 (분기 탭을 못 찾을 때의 폴백)


# ── 거래일·분기 ───────────────────────────────────────────────────────────
# VM에는 exchange_calendars가 없다(배포 pip 목록 밖). 있으면 쓰고, 없으면 아래 표.
# 표는 exchange_calendars 4.13 XKRX에서 뽑은 평일 휴장일. 매년 갱신 필요.
KRX_HOLIDAYS = {
    "2026-09-24", "2026-09-25", "2026-10-05", "2026-10-09", "2026-12-25", "2026-12-31",
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-01", "2027-05-05", "2027-05-13",
    "2027-08-16", "2027-09-14", "2027-09-15", "2027-09-16", "2027-10-04",
}
# 휴장 안내용 사유. 판정은 is_krx_session(위 표·달력)이 하고, 여기선 이름만 붙인다.
# ※ 2027-10-11(한글날 10/9 토 → 대체공휴일 예상)·2027-12-27(성탄절 대체 예상)은
#   달력·위 표에 아직 없다. KRX 공지가 나오면 KRX_HOLIDAYS와 함께 추가할 것.
KRX_HOLIDAY_NAMES = {
    "2026-12-25": "성탄절", "2026-12-31": "연말 휴장일",
    "2027-01-01": "신정", "2027-02-08": "설날", "2027-02-09": "설날 대체공휴일",
    "2027-03-01": "삼일절", "2027-05-05": "어린이날", "2027-05-13": "부처님오신날",
    "2027-08-16": "광복절 대체공휴일", "2027-09-14": "추석 연휴", "2027-09-15": "추석",
    "2027-09-16": "추석 연휴", "2027-10-04": "개천절 대체공휴일",
}
# 뉴욕증권거래소(NYSE) 평일 휴장일. 매년 갱신 필요.
US_HOLIDAYS = {
    "2026-11-26": "추수감사절", "2026-12-25": "크리스마스",
    "2027-01-01": "새해 첫날", "2027-01-18": "마틴 루터 킹 데이",
    "2027-02-15": "대통령의 날", "2027-03-26": "성금요일",
    "2027-05-31": "메모리얼 데이", "2027-06-18": "준틴스 대체휴일",
    "2027-07-05": "독립기념일 대체휴일", "2027-09-06": "노동절",
    "2027-11-25": "추수감사절", "2027-12-24": "크리스마스 대체휴일",
}
# 휴장은 아니지만 알아둘 장 일정(개장 지연·조기 폐장 등). 휴장 안내와 같은 시각에 함께 나간다.
# [(국기, 문구)] — 매년 갱신. 수능일은 교육부 발표 후 추가.
MARKET_EVENTS = {
    "2026-11-19": [("🇰🇷", "수능 — 국내 증시 1시간 늦게 개장 (10:00 개장 · 16:30 마감)")],
    "2026-11-27": [("🇺🇸", "추수감사절 다음날 — 미국 증시 조기 폐장 (현지 13시 = 한국시간 11/28 새벽 3시)")],
    "2026-12-24": [("🇺🇸", "크리스마스 이브 — 미국 증시 조기 폐장 (현지 13시 = 한국시간 12/25 새벽 3시)")],
    "2026-12-30": [("🇰🇷", "올해 마지막 거래일(폐장일)")],
    "2027-01-04": [("🇰🇷", "새해 첫 거래일 — 국내 증시 1시간 늦게 개장 (10:00 개장)")],
    "2027-11-26": [("🇺🇸", "추수감사절 다음날 — 미국 증시 조기 폐장 (현지 13시 = 한국시간 11/27 새벽 3시)")],
}
try:
    import exchange_calendars as _xcals
    _XKRX = _xcals.get_calendar("XKRX")
except Exception:
    _XKRX = None


def is_krx_session(d: datetime.date) -> bool:
    if _XKRX is not None:
        try:
            return bool(_XKRX.is_session(d.isoformat()))
        except Exception:
            pass
    return d.weekday() < 5 and d.isoformat() not in KRX_HOLIDAYS


def next_krx_session(d: datetime.date) -> datetime.date:
    """d가 거래일이면 d, 아니면 다음 거래일.
    휴장일(주말·공휴일)에 올린 추천을 그날로 적으면 기준가가 '직전 거래일 종가'가
    된다(이미 아는 가격으로 사는 셈). 또 라운드일(그 주 첫 거래일)과 날짜가 어긋나
    데일리가 미추천 패널티를 매긴다. 2026-10-05(개천절 대체휴일)부터 적용."""
    for _ in range(15):
        if is_krx_session(d):
            return d
        d += datetime.timedelta(days=1)
    return d


_Q_RE = re.compile(r"(\d{2})년\s*(\d)\s*분기")


def quarter_key(d: datetime.date):
    return (d.year % 100, (d.month - 1) // 3 + 1)


def prev_quarter_key(key):
    yy, q = key
    return (yy - 1, 4) if q == 1 else (yy, q - 1)


def quarter_tabs(ss):
    """{(yy, q): worksheet}. '테스트' 탭과 '_' 시스템 탭은 제외."""
    out = {}
    for w in ss.worksheets():
        if w.title.startswith("_") or "테스트" in w.title:
            continue
        m = _Q_RE.search(w.title)
        if m:
            k = (int(m.group(1)), int(m.group(2)))
            # 같은 분기 이름의 사본('... 사본' 등)이 있으면 제목이 짧은 쪽을 정본으로 본다
            if k not in out or len(w.title) < len(out[k].title):
                out[k] = w
    return out


def ensure_quarter_tab(ss, key):
    """key 분기 탭이 없으면 직전 분기 탭을 복제해 만든다.
    종목(J~N)·실현(P~U) 칸만 비우고 나머지 양식·수식은 그대로 둔다.
    데일리(update_gsheets.ensure_quarter_sheet)와 같은 규칙 — 먼저 도는 쪽이 만든다."""
    tabs = quarter_tabs(ss)
    if key in tabs:
        return tabs[key], False
    src = tabs.get(prev_quarter_key(key))
    if src is None:
        return None, False
    yy, q = key
    pyy, pq = prev_quarter_key(key)
    title = src.title.replace(f"{pyy}년 {pq}분기", f"{yy}년 {q}분기")
    if title == src.title:
        title = _Q_RE.sub(f"{yy}년 {q}분기", src.title)
    n = len(ss.worksheets())
    try:
        new = ss.duplicate_sheet(src.id, insert_sheet_index=n, new_sheet_name=title)
    except Exception:
        # 데일리가 같은 순간 먼저 만들었으면 이름 충돌 — 다시 찾아본다
        tabs = quarter_tabs(ss)
        if key in tabs:
            return tabs[key], False
        raise
    vals = new.get_all_values()
    ranges = []
    for b in find_person_blocks(vals):
        ranges.append(f"J{b['row_start']}:N{b['row_end']}")
        ranges.append(f"P{b['row_start']}:U{b['row_end']}")
    if ranges:
        new.batch_clear(ranges)
    # 제목성 텍스트('26년 3분기')만 바꾼다. 수식 셀은 건드리지 않는다.
    try:
        fv = new.get_values(value_render_option="FORMULA")
        fixes = []
        old_tag, new_tag = f"{pyy}년 {pq}분기", f"{yy}년 {q}분기"
        for i, row in enumerate(fv, start=1):
            for j, v in enumerate(row, start=1):
                if isinstance(v, str) and not v.startswith("=") and old_tag in v:
                    fixes.append(gspread.Cell(i, j, v.replace(old_tag, new_tag)))
        if fixes:
            new.update_cells(fixes, value_input_option="USER_ENTERED")
    except Exception:
        pass
    return new, True


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


def find_active_dup(all_values, person_name, stock_name):
    """이미 활성(J열)에 같은 종목을 보유 중이면 'J행=종목명(추천일)' 문자열 반환.
    추천일이 달라도 잡아낸다(날짜 기준 중복체크로는 못 막는 재기록 방지)."""
    blocks = find_person_blocks(all_values)
    block = next((b for b in blocks
                  if b["person"] == person_name or person_name in b["person"]), None)
    if not block:
        return ""
    t_code, t_base = _split_stock_label(stock_name)
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        j = all_values[idx][9] if len(all_values[idx]) > 9 else ""
        if not j:
            continue
        k = all_values[idx][10] if len(all_values[idx]) > 10 else ""
        j_code, j_base = _split_stock_label(str(j))
        same = (t_code and j_code and t_code == j_code) or (
            len(t_base) >= 2 and len(j_base) >= 2
            and (t_base == j_base or t_base in j_base or j_base in t_base))
        if same:
            return f"J{r}='{j}' (추천일 {k})"
    return ""


def dup_on_date(all_values, person_name, stock_name, rec_date) -> bool:
    """같은 종목이 같은 추천일로 이미 활성에 있으면 True (메시지 중복 전송·재실행 방지).
    find_active_dup은 첫 매칭 1건만 돌려줘서 날짜 비교용으로 못 쓴다."""
    t_code, t_base = _split_stock_label(stock_name)
    for b in find_person_blocks(all_values):
        if not (b["person"] == person_name or person_name in b["person"]):
            continue
        for r in range(b["row_start"], b["row_end"] + 1):
            row = all_values[r - 1] if r - 1 < len(all_values) else []
            j = row[9] if len(row) > 9 else ""
            k = row[10] if len(row) > 10 else ""
            if not j or str(rec_date) not in str(k):
                continue
            j_code, j_base = _split_stock_label(str(j))
            if (t_code and t_code == j_code) or (
                    len(t_base) >= 2 and len(j_base) >= 2
                    and (t_base == j_base or t_base in j_base or j_base in t_base)):
                return True
    return False


def active_holdings(all_values, person_name):
    """해당 멤버가 현재 활성(J열)으로 들고 있는 종목명 목록(중복 제거)."""
    blocks = find_person_blocks(all_values)
    block = next((b for b in blocks
                  if b["person"] == person_name or person_name in b["person"]), None)
    if not block:
        return []
    out = []
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        j = (all_values[idx][9] if len(all_values[idx]) > 9 else "") or ""
        if j.strip() and j.strip() not in out:
            out.append(j.strip())
    return out


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

    # ws.cell()은 값 하나를 읽으려고 매번 시트 API를 때린다. 그 호출은
    # sheet_retry 밖에 있었던 탓에 2026-08-31 22:42 김태완/S-Oil 메시지가
    # 503 한 번에 통째로 유실됐다. 셀 객체는 로컬에서 만들면 그만이다.
    cells = [gspread.Cell(empty_row, 10, stock_name),
             gspread.Cell(empty_row, 11, str(rec_date))]
    sheet_retry(lambda: ws.update_cells(cells,
                                        value_input_option="USER_ENTERED"), "매수 기록")
    return True, f"{person_name} / {stock_name} 기록 완료 (기준가는 오늘 장 마감 후 자동입력)"


def _pending_add(cache, sheet_title, row, name, sell_date):
    """자동매도한 행을 _pending_sell에 남겨 데일리가 매도일 종가로 확정하게 한다."""
    try:
        ss = cache.get_ss()
        try:
            wp = ss.worksheet("_pending_sell")
        except gspread.WorksheetNotFound:
            wp = ss.add_worksheet(title="_pending_sell", rows=200, cols=5)
            wp.append_row(["sheet", "row", "stock", "sell_date"])
        wp.append_row([sheet_title, str(row), name, str(sell_date)])
    except Exception as e:
        log(f"[경고] _pending_sell 기록 실패: {e}")


UNRESOLVED_TAB = "_unresolved_buy"
UNRESOLVED_HEADER = ["rec_date", "member", "reason", "text", "logged_at", "status"]


def unresolved_add(cache, rec_day, member, reason, body):
    """기록에 실패한 매수 메시지를 남긴다. 데일리는 여기 남은 (멤버, 추천일)에
    미추천 패널티를 매기지 않고 보류한다 — 봇의 인식 실패가 멤버 감점이 되지 않게.
    (§10-7: 미인식 → 자동기록 안 함 → 다음 날 아침 -10% 가 그대로 붙던 구조)
    보류 해제: 수동으로 종목을 기록하면 자연히 '추천함'이 되고, 정말 미추천이면
    운영자가 이 탭 status 칸에 '패널티'를 적으면 다음 데일리가 패널티를 매긴다."""
    try:
        ss = cache.get_ss()
        try:
            wu = ss.worksheet(UNRESOLVED_TAB)
        except gspread.WorksheetNotFound:
            wu = ss.add_worksheet(title=UNRESOLVED_TAB, rows=200, cols=len(UNRESOLVED_HEADER))
            wu.append_row(UNRESOLVED_HEADER)
        now = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
        sheet_retry(lambda: wu.append_row([str(rec_day), member, reason[:80], body[:200], now, ""]),
                    "미인식 기록")
        return True
    except Exception as e:
        log(f"[경고] {UNRESOLVED_TAB} 기록 실패: {e}")
        return False


def sell_stock(ws, all_values, person_name, stock_name, sell_date, cache, _grown=False,
               rec_hint=None, p_row_hint=None):
    """가장 과거(위쪽) 매칭 활성 종목을 실현 섹션으로 이동. manual_sell과 동일 규칙.
    rec_hint(추천일 'YYYY-MM-DD')가 있으면 그 추천일 보유분만 판다(없으면 실패).
    (2026-11-10 김태완 '스튜디오드래곤 (10/26일자)'를 10/19분으로 판 사고)"""
    blocks = find_person_blocks(all_values)
    block = next((b for b in blocks
                  if b["person"] == person_name or person_name in b["person"]), None)
    if not block:
        return False, f"'{person_name}' 블록을 찾을 수 없음"

    # 매칭: ① 티커 일치 우선 ② 괄호 제외 이름 정규화 비교(표기 흔들림 흡수)
    t_code, t_base = _split_stock_label(stock_name)
    stock_row = None
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            continue
        j = all_values[idx][9] if len(all_values[idx]) > 9 else ""
        if not j:
            continue
        j_code, j_base = _split_stock_label(str(j))
        k = all_values[idx][10] if len(all_values[idx]) > 10 else ""
        if rec_hint and rec_hint not in str(k):
            continue
        if t_code and j_code and t_code == j_code:
            stock_row = r
            break
        if (len(t_base) >= 2 and len(j_base) >= 2
                and (t_base == j_base or t_base in j_base or j_base in t_base)):
            stock_row = r
            break
    if stock_row is None:
        if rec_hint:
            return False, f"'{stock_name}' 추천일 {rec_hint} 활성 종목을 찾을 수 없음"
        return False, f"'{stock_name}' 활성 종목을 찾을 수 없음"

    row = all_values[stock_row - 1]
    orig     = row[9]  if len(row) > 9  else ""
    rec_date = row[10] if len(row) > 10 else ""
    base     = row[11] if len(row) > 11 else ""
    cur      = row[12] if len(row) > 12 else ""

    p_row = None
    if p_row_hint:
        pv = all_values[p_row_hint - 1][15] if len(all_values[p_row_hint - 1]) > 15 else ""
        if not str(pv).strip():
            p_row = p_row_hint
    for r in ([] if p_row else range(block["row_start"], block["row_end"] + 1)):
        ri = r - 1
        if ri >= len(all_values):
            break
        pv = all_values[ri][15] if len(all_values[ri]) > 15 else ""
        if not pv or not pv.strip():
            p_row = r
            break
    if p_row is None:
        # 실현 칸이 꽉 찼으면 블록 마지막 행 앞에 한 줄 끼워 넣고 다시 읽는다
        # (2026-10-19 조형오 블록 14행 소진 사고). 소계 SUM 범위는 자동으로 늘어난다.
        if _grown:
            return False, "실현 섹션에 빈 행 없음(행 추가 후에도)"
        at = block["row_end"]
        sheet_retry(lambda: ws.insert_rows([[""]], row=at, inherit_from_before=True), "행 추가")
        log(f"[행 추가] {person_name} 블록 실현 칸 부족 → 행 {at}에 1행 추가")
        fresh = sheet_retry(ws.get_all_values, "시트 재읽기")
        return sell_stock(ws, fresh, person_name, stock_name, sell_date, cache, _grown=True,
                          rec_hint=rec_hint)

    try:
        base_f = float(str(base).replace(",", ""))
    except Exception:
        return False, f"기준가 파싱 실패: {base!r}"
    try:
        price = float(str(cur).replace(",", ""))
    except Exception:
        price = base_f

    sheet_retry(lambda: ws.batch_update([
        {"range": f"P{p_row}", "values": [[orig]]},
        {"range": f"Q{p_row}", "values": [[rec_date]]},
        {"range": f"R{p_row}", "values": [[str(sell_date)]]},
        {"range": f"S{p_row}", "values": [[base_f]]},
        {"range": f"T{p_row}", "values": [[price]]},
        {"range": f"U{p_row}", "values": [[f"=(T{p_row}-S{p_row})/S{p_row}"]]},
        {"range": f"J{stock_row}:N{stock_row}", "values": [["", "", "", "", ""]]},
    ], value_input_option="USER_ENTERED"), "매도 기록")

    _pending_add(cache, ws.title, p_row, orig, sell_date)
    ret = (price - base_f) / base_f * 100 if base_f else 0.0
    return True, (f"{orig} 매도 (추천일 {rec_date} → 매도일 {sell_date}, 행 {stock_row}→실현 {p_row})\n"
                  f"기준가 {base_f:,.0f} → 매도가 {price:,.0f} ({ret:+.2f}%)\n"
                  f"※ 매도가는 오늘 밤 데일리가 매도일 종가로 확정")


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


_LEAD_NOISE = {"매수", "매도", "추천", "재추천", "종목추천", "종목", "추천주",
               "신규", "비중확대", "분할매수", "오늘", "매수추천"}
_stock_cache = {}


_ADJUST_WORDS = ("미추천", "패널티", "보너스")


def past_labels(all_values, person_name):
    """해당 멤버가 이 분기에 쓴 종목 라벨 전부(활성 J + 실현 P, 중복 제거).
    패널티·보너스 같은 조정 행은 종목이 아니므로 뺀다."""
    blocks = find_person_blocks(all_values)
    block = next((b for b in blocks
                  if b["person"] == person_name or person_name in b["person"]), None)
    if not block:
        return []
    out = []
    for r in range(block["row_start"], block["row_end"] + 1):
        idx = r - 1
        if idx >= len(all_values):
            break
        row = all_values[idx]
        for ci in (9, 15):
            v = (row[ci] if len(row) > ci else "") or ""
            v = v.strip()
            if not v or any(w in v for w in _ADJUST_WORDS):
                continue
            if v not in out:
                out.append(v)
    return out


def _lcp(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _from_history(text: str, history):
    """본인이 전에 쓴 라벨과 대조해 '재추천'을 건져낸다.

    네이버는 사람이 줄여 쓴 ETF 이름을 못 찾는다. 2026-09-08 조형오의
    'SOL 조선TOP3레버리지'는 정식명이 'SOL 조선TOP3플러스레버리지'라
    완전일치·접두 어느 쪽에도 안 걸렸고, 남은 토큰 'SOL'이 해외 ADR로
    잘못 걸리면서 인식이 통째로 실패했다.

    시트에 이미 있는 라벨은 그 자체가 정답 표기이므로 네이버 재검증이
    필요 없다. 다만 오인식을 막기 위해 앞에서부터 6글자 이상 일치하고,
    그 조건을 만족하는 과거 라벨이 하나뿐일 때만 채택한다."""
    if not history:
        return None
    for tok in _heuristic_names(text):
        nt = _norm(tok)
        if len(nt) < 6:
            continue
        hits = [h for h in history if _lcp(nt, _norm(h)) >= 6]
        if len(hits) == 1:
            return hits[0], tok
    return None


def _norm(s: str) -> str:
    return re.sub(r"[\s\(\)\[\]{}·\-_/\.“”‘’\"']", "", s or "").upper()


# 국내 ETF 환헤지 표기. 티커가 아니다.
_HEDGE_SUFFIX = re.compile(r"\(\s*(?:H|UH|합성|합성\s*H|환헤지|언헤지)\s*\)\s*$", re.I)


def _split_stock_label(label: str):
    """'코히런트(COHR)' → ('COHR', '코히런트'). 괄호 티커가 없으면 코드는 ''.
    '(H)' 같은 환헤지 표기는 티커로 보지 않는다 — 'KODEX WTI원유선물(H)'와
    'KODEX WTI원유선물인버스(H)'가 둘 다 코드 'H'로 잡혀 같은 종목 취급됐다."""
    s = str(label or "").strip()
    m = None if _HEDGE_SUFFIX.search(s) else re.search(r"\(([A-Za-z0-9]{2,7})\)\s*$", s)
    code = m.group(1).upper() if m else ""
    base = _norm(re.sub(r"\([^)]*\)\s*$", "", s))
    return code, base


# 원문에 거래소와 함께 명시된 티커 (예: NYSE: COHR, NASDAQ:NVDA, 나스닥 TSLA)
_EXCH_TICKER_RE = re.compile(
    r"(?:NYSE\s*American|NYSE|NASDAQ|AMEX|ARCA|CBOE|나스닥|뉴욕증권거래소|뉴욕거래소)"
    r"\s*[:：]?\s*([A-Z]{1,5})\b")
# 괄호 안 단독 티커 (예: (COHR))
# 괄호 티커: (BITX)뿐 아니라 [BITX]도 받는다. 2026-09-21 이원호 원문이
# '[BITX]'였는데 대괄호를 못 읽어 뒤따르는 '비트코인'이 엉뚱한 종목에 걸렸다.
_PAREN_TICKER_RE = re.compile(r"[\(\[]\s*([A-Z]{2,5})\s*[\)\]]")


def _naver_items(query: str):
    """네이버 자동완성 원본 items 반환. 조회 자체 실패면 None(빈 결과 []와 구분)."""
    q = (query or "").strip()
    if not q:
        return []
    if q in _stock_cache:
        return _stock_cache[q]
    try:
        r = requests.get("https://ac.stock.naver.com/ac",
                         params={"q": q, "target": "stock"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=6).json()
        items = r.get("items", []) or []
    except Exception as e:
        log(f"[종목조회 오류] {q}: {e}")
        return None
    _stock_cache[q] = items
    return items


def _pick(items, code: str = "", name: str = "", strict_foreign: bool = False):
    """items에서 조건에 맞는 종목을 (정식명, 코드, KR여부)로 반환. 국내 우선.
    code 지정 시 티커 완전일치, name 지정 시 공백무시 완전일치/부분포함.
    strict_foreign=True면 해외 종목은 '완전일치'일 때만 채택(약어 오탐 방지).
      예: 'SKT'가 'SK Telecom Co Ltd ADR'에 부분포함되어 채택되던 사고 차단."""
    # 2패스: 완전일치를 먼저 훑고, 없을 때만 부분포함을 본다.
    #   네이버는 'KODEX WTI원유선물' 검색에 인버스(271050)를 정방향(261220)보다
    #   앞에 준다. 1패스로 훑으면 부분포함에 걸린 인버스가 이겨 반대 종목이 기록된다.
    #   (2026-07-13 이원호 건 — 원문은 'KODEX WTI원유선물(H)'였다)
    for exact_only in (True, False):
        kr = us = None
        for it in items or []:
            nm = (it.get("name") or "").strip()
            cd = (it.get("code") or "").strip()
            if not nm or not cd:
                continue
            tc = it.get("typeCode", "")
            nation = it.get("nationCode", "")
            is_kr = tc in ("KOSPI", "KOSDAQ")
            if code:
                if cd.upper() != code.strip().upper():
                    continue
            elif name:
                a, b = _norm(name), _norm(nm)
                if not (a and b):
                    continue
                if a != b:
                    if exact_only:
                        continue
                    # 앞에서부터 이어지는 일치(접두)는 '이름을 줄여 쓴 것'으로 본다.
                    #   '트럼프 미디어' → '트럼프 미디어 & 테크놀로지 그룹'(DJT)
                    # 4글자 미만은 우연히 걸리기 쉬워 접두라도 인정하지 않는다.
                    #   'SKT'·'SOL'이 여기서 걸러진다.
                    # 단, 접두가 정식명의 1/3도 못 되면 우연으로 본다.
                    #   '비트코인'(4) → '비트코인 인프라스트럭처 애퀴지션'(15) 오탐
                    #   (2026-09-21 이원호 BITX 건). '트럼프미디어'(6/15)는 통과.
                    prefix_ok = (len(a) >= 4 and len(a) * 3 >= len(b)
                                 and (b.startswith(a) or a.startswith(b)))
                    if not is_kr and strict_foreign:
                        # 휴리스틱 경로의 해외 종목은 완전일치·접두·ADR만 인정한다.
                        # (ADR은 뒤이어 국내 상장분 대체 검증을 통과해야 채택된다)
                        if not (prefix_ok or _is_adr(nm)):
                            continue
                    elif not (a in b or b in a):
                        continue
                    # 후보가 종목명의 절반도 설명하지 못하면 우연한 부분일치로 본다.
                    #   'SOL' → 'SOL AI반도체TOP2플러스' 같은 오탐 차단.
                    # 접두 일치와 ADR은 예외 — 각각 축약 표기와 국내상장 대체 검증이
                    # 뒷받침한다. 이 가드가 'SKT'→SK텔레콤 경로를 막고 있었다.
                    if len(a) * 2 < len(b) and not prefix_ok and not _is_adr(nm):
                        continue
            if is_kr:
                if kr is None or len(_norm(nm)) < len(_norm(kr[0])):
                    kr = (nm, cd, True)   # 부분일치 땐 군더더기가 가장 적은 이름
            elif nation in ("USA", ""):
                if us is None or len(_norm(nm)) < len(_norm(us[0])):
                    us = (nm, cd, False)
        hit = kr or us
        if hit:
            return hit
        if code:
            break   # 티커 매칭은 완전일치뿐이라 2패스가 의미 없다
    return None


def _is_adr(name: str) -> bool:
    return bool(re.search(r"\bADR\s*$", str(name or "").strip(), re.I))


_SUFFIX_RE = re.compile(r"(지주회사|지주|홀딩스|그룹|코퍼레이션|주식회사)\s*$")


def _domestic_for_adr(nm: str, cd: str):
    """미국 ADR로 판정된 종목의 국내 상장분을 찾는다. (res, 사용한 검색어) 반환.

    ① 티커로 재조회해 한글 표기 확보(예: SKM → 'SK텔레콤 ADR')
    ② 'ADR' 제거 후 표기 변형으로 국내(KOSPI/KOSDAQ) 검색
       (예: '포스코 홀딩스' → '포스코' → POSCO홀딩스, 'KB금융지주' → 'KB금융')
    국내 상장이 없으면 (None, "") — TSMC·알리바바 같은 순수 해외 ADR은 그대로 둔다.
    """
    ko = nm
    for it in (_naver_items(cd) or []):
        if (it.get("code") or "").upper() == cd.upper() and it.get("name"):
            ko = it["name"]
            break
    base = re.sub(r"\s*ADR\s*$", "", ko, flags=re.I).strip()
    variants = [base, base.replace(" ", ""), _SUFFIX_RE.sub("", base).strip()]
    if base.split():
        variants.append(base.split()[0])
    for v in dict.fromkeys(v for v in variants if len(v) >= 2):
        for it in (_naver_items(v) or []):
            if it.get("typeCode") in ("KOSPI", "KOSDAQ") and it.get("code"):
                return (it["name"], it["code"], True), v
    return None, ""


def _fmt_stock(res) -> str:
    """시트 표기 규칙: 국내는 정식명, 해외는 '정식명(티커)' (update_gsheets와 동일)."""
    nm, cd, is_kr = res
    return nm if is_kr else f"{nm}({cd})"


_REASON_CUT = re.compile(
    r"(?m)^\s*(?:\d+[.)]|[-*>•]|ㅇ)\s*\S"   # 줄머리 사유 표식
    r"|\s[-*>•]\s*\S"                        # 줄 가운데 '- ', '> '
    r"|사유")


def _name_region(text: str) -> str:
    """종목명이 적히는 구간만 잘라낸다(앞 3줄 + 사유 시작 이전)."""
    head = "\n".join((text or "").split("\n")[:3])
    m = _REASON_CUT.search(head)
    return head[:m.start()] if m else head


def _heuristic_names(text: str):
    """앞 5줄에서 종목명 후보 토큰열 생성(기존 폴백 로직)."""
    out = []
    t = (text or "").replace("#", " ")
    for line in t.split("\n")[:5]:
        # 스마트 따옴표(“ ” ‘ ’ 「 」 『 』 <> 등)도 털어낸다. 카카오·텔레그램
        # 모바일 입력기가 곧은 따옴표를 자동으로 바꿔 넣는다. 2026-09-28
        # 김동환의 '“제이에스링크”'가 이것 때문에 통째로 인식에 실패했다.
        # 마침표도 턴다. '대덕전자.'처럼 문장부호가 붙으면 네이버가 못 찾는다
        # (2026-09-28 어정윤 건). 줄머리 번호 '1.'은 뒤에서 따로 걸러진다.
        line = re.sub(r"[·:;,/|_\-\*\"\'`!?.()\[\]“”‘’「」『』〈〉《》<>]", " ", line)
        toks = [w for w in line.split() if w]
        while toks and (toks[0] in _LEAD_NOISE or re.fullmatch(r"\d+[.)]?", toks[0])):
            toks.pop(0)
        for n in range(min(4, len(toks)), 0, -1):
            out.append(" ".join(toks[:n]))
    return out


_llm_last_error = ""


def resolve_stock_llm(text: str, holdings=None):
    """클로드로 종목 1개 추출.
    반환: {'name_ko','name_en','ticker','market'} / {}(특정 불가) / None(호출 실패)
    실패 사유는 _llm_last_error에 남긴다 — '키 미설정'과 '크레딧 소진'을 구분해야
    원인을 추적할 수 있다(2026-08-09~14 크레딧 소진을 키 문제로 오인)."""
    global _llm_last_error
    _llm_last_error = ""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        _llm_last_error = "ANTHROPIC_API_KEY 미설정"
        return None
    if not text:
        return None
    prompt = (
        "다음은 주식 스터디 단톡방의 종목 추천/매도 메시지다. 언급된 대상 종목 1개를 식별해 "
        "JSON 한 줄만 출력해라.\n"
        '형식: {"name_ko":"한글 종목명","name_en":"영문 정식명","ticker":"티커","market":"KR 또는 US"}\n'
        "- 아는 값은 최대한 채운다. 특히 ticker는 아는 경우 반드시 채운다(미국=대문자 영문, 한국=6자리 숫자).\n"
        "- SKT·삼전·하이닉스·현차처럼 줄임말이면 국내 정식 종목명으로 풀어서 name_ko에 넣는다.\n"
        "- 한국거래소 상장사면 market=KR, ticker=6자리 숫자로 답한다. "
        "미국 ADR(SKM·PKX·KB 등)은 원문이 명시적으로 미국 상장을 지목한 경우가 아니면 쓰지 않는다.\n"
        "- 모르는 항목은 빈 문자열.\n"
        '- 종목을 특정할 수 없으면 {"none":true} 만 출력.\n'
        "- 설명·코드블록 없이 JSON만.\n"
        + (f"- 참고: 이 사람이 현재 보유 중인 종목은 {', '.join(holdings)} 이다. "
           "매도 메시지라면 이 중에서 고른다.\n" if holdings else "")
        + "\n메시지:\n" + text[:1500]
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=25)
        r = resp.json()
        if resp.status_code != 200:
            msg = ((r.get("error") or {}).get("message") or "")[:130]
            _llm_last_error = f"HTTP {resp.status_code} {msg}"
            log(f"[LLM오류] {_llm_last_error}")
            return None
        ans = (r.get("content", [{}])[0].get("text", "") or "").strip()
        m = re.search(r"\{.*\}", ans, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        if not isinstance(data, dict) or data.get("none"):
            return {}
        return {k: str(data.get(k) or "").strip()
                for k in ("name_ko", "name_en", "ticker", "market")}
    except Exception as e:
        log(f"[LLM추출 실패] {e}")
        return None


def _loose(s: str) -> str:
    """오타·연결어 흔들림 흡수용 정규화. '&'·'앤'·'엔'·'AND'와 문장부호를 지운다."""
    s = _norm(s)
    s = re.sub(r"(&|＆|AND|앤|엔|#|,|:)", "", s)
    return s


def _cover(hb: str, tl: str) -> int:
    """보유 라벨 hb의 글자 중 원문 tl의 한 구간과 순서대로 맞는 글자 수(최대값).
    오타 한두 글자는 건너뛰고, 브랜드 접두만 같은 다른 종목은 낮게 나온다."""
    from difflib import SequenceMatcher
    w = len(hb) + 2
    best = 0
    for st in range(0, max(1, len(tl) - len(hb) + 3)):
        win = tl[st:st + w]
        m = sum(b.size for b in SequenceMatcher(None, hb, win, autojunk=False).get_matching_blocks())
        best = max(best, m)
    return best


def _fuzzy_holding(text: str, holdings):
    """매도 전용 느슨한 보유종목 대조. 후보가 본인 보유분(보통 1~5개)으로 이미
    좁혀져 있어서 신규 매수 인식보다 오탐 여지가 훨씬 작다.
    2026-10-06 김동환 '트럼프 미디어 앤 테크노로지 그룹' — '&'→'앤', '놀'→'노'
    오타 때문에 보유 중인 '트럼프 미디어 & 테크놀로지 그룹(DJT)'을 못 찾았다.
    조건: 보유 라벨(5자 이상) 글자의 85% 이상이 원문 한 구간과 순서대로 일치,
    조건을 넘는 보유종목이 정확히 하나일 때만 채택(둘 이상이면 fail-closed).
    'KODEX 200' 보유 중 'KODEX 인버스' 매도 같은 브랜드 접두 일치는 62%라 걸러진다."""
    tl = _loose(text)
    hits = []
    for hd in holdings:
        _, base = _split_stock_label(hd)
        hb = _loose(base)
        if len(hb) < 5:
            continue
        n = _cover(hb, tl)
        if n >= 0.85 * len(hb):
            hits.append((n, len(hb), hd))
    if len(hits) != 1:
        return None
    n, L, hd = hits[0]
    return (re.sub(r"\([^)]*\)\s*$", "", hd).strip(),
            f"보유 종목 근사 대조({n}/{L}자 일치)")


def resolve_stock(text: str, holdings=None, history=None):
    """종목 확정. 반환: (시트표기 문자열 or None, 판정근거 문자열)

    후보 우선순위 — ① 보유 종목 직접 대조(매도 시) ② 원문 거래소표기 티커
    ③ LLM 티커 ④ LLM 종목명(한/영) ⑤ 원문 괄호 티커 ⑥ 앞 5줄 토큰.
    모든 후보는 네이버 조회로 검증하며, 실패 시 자동기록을 포기(fail-closed)한다.
    holdings를 주면 매도 대상을 보유 종목으로 좁힌다.
    """
    if not text:
        return None, "빈 메시지"

    cands, notes = [], []

    # ① 보유 종목이 원문에 그대로 등장하면 그게 가장 확실하다(매도 경로).
    if holdings:
        tn = _norm(text)
        exact = False
        for hd in holdings:
            _, base = _split_stock_label(hd)
            if len(base) >= 2 and base in tn:
                cands.append(("name", re.sub(r"\([^)]*\)\s*$", "", hd).strip(), "보유 종목 대조"))
                exact = True
        if not exact:
            fz = _fuzzy_holding(text, holdings)
            if fz:
                cands.append(("name", fz[0], fz[1]))

    exch = sorted({m.group(1).upper() for m in _EXCH_TICKER_RE.finditer(text)})
    if len(exch) == 1:
        cands.append(("code", exch[0], "원문 거래소표기"))
    elif len(exch) > 1:
        notes.append(f"원문 티커 후보 다수({','.join(exch)}) → 미사용")

    info = resolve_stock_llm(text, holdings=holdings)
    if info is None:
        notes.append(f"LLM 실패({_llm_last_error or '원인 미상'})")
    elif not info:
        notes.append("LLM 판단: 종목 특정 불가")
    else:
        if info.get("ticker"):
            cands.append(("code", info["ticker"].upper(), "LLM 티커"))
        for k in ("name_ko", "name_en"):
            if info.get(k):
                cands.append(("name", info[k], f"LLM {k}"))

    # 괄호 티커는 '종목명(TICKER)' 관례가 통하는 '이름 구간'에서만 읽는다.
    # 사유 본문에서 괄호는 한글 용어의 영문 약어 자리로 훨씬 자주 쓰인다.
    #   '인쇄회로기판(PCB)' → 티커 PCB → 'PCB 뱅코프' 오기록 (2026-09-28 송지호)
    #   '비상발전기(UPS)'   → 티커 UPS → 택배회사 UPS
    # 사유가 시작되는 지점('1.', '-', '>', 'ㅇ', '사유')에서 잘라낸다.
    paren = sorted({m.group(1).upper()
                    for m in _PAREN_TICKER_RE.finditer(_name_region(text))})
    if len(paren) == 1 and not exch:
        cands.append(("code", paren[0], "원문 괄호 티커"))
    elif len(paren) > 1:
        notes.append(f"앞 2줄 괄호 티커 다수({','.join(paren)}) → 미사용")

    for nm in _heuristic_names(text):
        cands.append(("name", nm, "원문 토큰"))

    # 원문 토큰이 네이버에서 안 잡히는 경우를 대비한 마지막 보루.
    # 후보 목록 맨 뒤에 두지 않고 여기서 바로 확정하는 이유는, 시트에 이미
    # 있는 라벨이 네이버 검색결과보다 확실한 정답이기 때문이다.
    hist_hit = _from_history(text, history)

    prefer_kr = bool(info) and (info.get("market", "").upper() == "KR")
    deferred = None      # 국내 후보를 더 찾아본 뒤에야 채택할 해외 결과
    seen = set()

    for kind, val, why in cands:
        key = (kind, val.upper())
        if key in seen:
            continue
        seen.add(key)
        items = _naver_items(val)
        if items is None:
            notes.append(f"{why}('{val}') 네이버 조회 오류")
            continue
        loose = not why.startswith("원문 토큰")   # 휴리스틱 후보는 해외 완전일치만 허용
        res = _pick(items, code=val if kind == "code" else "",
                    name="" if kind == "code" else val, strict_foreign=not loose)
        # 이름 매칭은 실패했지만 검색결과가 1건뿐이면 단독 채택(update_gsheets와 동일 규칙)
        if not res and kind == "name" and len(items) == 1 and loose:
            res = _pick(items)
        if not res:
            if kind == "code" or why.startswith("LLM"):
                notes.append(f"{why}('{val}') 네이버 미검증")
            continue

        nm, cd, is_kr = res
        # 국내 상장사의 미국 ADR이면 국내 상장분으로 대체(스터디는 국내 상장 기준)
        if not is_kr and _is_adr(nm):
            dom, via = _domestic_for_adr(nm, cd)
            if dom:
                return _fmt_stock(dom), (f"{why} '{val}' → ADR({nm}/{cd}) 감지 "
                                         f"→ 국내상장 대체 '{via}' → {dom[0]}({dom[1]})")
            if prefer_kr or not loose:
                notes.append(f"{why}('{val}') 해외 ADR({cd})만 확인 — 국내상장 미발견")
                continue
        if is_kr:
            return _fmt_stock(res), f"{why} '{val}' → 네이버 {nm}({cd})"
        if prefer_kr:
            deferred = deferred or (res, why, val)
            continue
        return _fmt_stock(res), f"{why} '{val}' → 네이버 {nm}({cd})"

    if deferred:
        res, why, val = deferred
        return _fmt_stock(res), f"{why} '{val}' → 네이버 {res[0]}({res[1]}) (국내 상장 없음)"
    if hist_hit:
        label, tok = hist_hit
        return label, f"본인 과거 추천 대조 '{tok}' → 시트 기존 표기 '{label}'"
    return None, "; ".join(notes[:4]) or "후보 없음"


def today_kst():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date()


# ── 처리 ──────────────────────────────────────────────────────────────────
def sheet_retry(fn, what="시트 작업", tries=4):
    """구글 시트 API의 일시 장애(5xx)를 재시도한다.

    2026-08-18 11:07 '메시지 처리 오류: APIError: [503]'로 메시지 한 건이
    통째로 유실됐다. 503/500/502/504는 대개 수 초 뒤 정상화되므로 즉시 포기하지
    않는다. 권한·범위 오류 같은 4xx는 재시도해도 소용없으니 바로 올린다."""
    delay = 2
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            transient = any(c in str(e) for c in ("[500]", "[502]", "[503]", "[504]",
                                                  "Read timed out", "Connection aborted"))
            if not transient or i == tries:
                raise
            log(f"[재시도 {i}/{tries - 1}] {what}: {e}")
            time.sleep(delay)
            delay *= 2


class SheetCache:
    """분기 탭별 캐시. 예전엔 기동 시 고른 '맨 뒤 탭' 하나를 계속 붙잡고 있어서,
    분기가 바뀌어 새 탭이 생겨도 재시작 전까지 옛 분기 탭에 기록했다."""
    TAB_TTL = 600   # 탭 목록 재조회 주기(초)

    def __init__(self):
        self.ss = None
        self.tabs = {}
        self.tabs_at = 0
        self.vals = {}      # title -> (values, loaded_at)

    def get_ss(self):
        if self.ss is None:
            self.ss = sheet_retry(open_spreadsheet, "스프레드시트 열기")
        return self.ss

    def _tabs(self, force=False):
        if force or not self.tabs or (time.time() - self.tabs_at) > self.TAB_TTL:
            self.tabs = sheet_retry(lambda: quarter_tabs(self.get_ss()), "분기 탭 조회")
            self.tabs_at = time.time()
        return self.tabs

    def tab(self, key, create=False):
        ws = self._tabs().get(key) or self._tabs(force=True).get(key)
        if ws is None and create:
            ws, made = sheet_retry(lambda: ensure_quarter_tab(self.get_ss(), key), "분기 탭 생성")
            if ws is not None:
                self._tabs(force=True)
                if made:
                    notify_admin(f"🆕 {ws.title} 탭을 새로 만들었습니다 "
                                 f"(직전 분기 탭 복제, 종목·실현 칸 비움)")
                    log(f"[분기탭 생성] {ws.title}")
        return ws

    def values(self, ws, fresh=False):
        hit = self.vals.get(ws.title)
        if fresh or hit is None or (time.time() - hit[1]) > SHEET_TTL:
            v = sheet_retry(ws.get_all_values, "시트 읽기")
            self.vals[ws.title] = (v, time.time())
            return v
        return hit[0]

    def refresh(self, ws):
        return self.values(ws, fresh=True)

    def ensure(self, d=None):
        """기록 대상 탭(d가 속한 분기)과 값. 탭이 없으면 직전 분기를 복제해 만든다."""
        key = quarter_key(d or today_kst())
        ws = self.tab(key, create=True)
        if ws is None:
            return None, None
        return ws, self.values(ws)

    def prev(self, d=None):
        """직전 분기 탭과 값(없으면 None, None). 분기 전환 직후 이전 분기 보유분 매도용."""
        ws = self.tab(prev_quarter_key(quarter_key(d or today_kst())))
        if ws is None:
            return None, None
        return ws, self.values(ws)


# ── '지역 날씨' 답장 ──────────────────────────────────────────────────────
# 단톡방에서 누구든 '서울 중구 날씨'처럼 치면 그 지역의 지금 날씨를 답장한다.
# 지명 → 좌표: OpenStreetMap Nominatim(무료, 초당 1회 제한 → 결과 캐시)
# 날씨·미세먼지: Open-Meteo(무료, 카드뉴스 날씨와 같은 소스)
# '날씨' 뒤에 짧은 말이 붙어도 받는다('청라 날씨 알려줘', '샌프란시스코 날씨 이자식아' — 11-12 무응답)
_WEATHER_RE = re.compile(r"^\s*([가-힣A-Za-z0-9·/.\s]{1,30}?)\s*날씨(?:는|가|좀)?\s*([가-힣\s?？!.~ㅋㅎㅠ]{0,12})$")
# 장소 없이 이것만 있으면 잡담('오늘 날씨', '요즘 날씨 미쳤네')으로 보고 반응하지 않는다
_WEATHER_SKIP = {"오늘", "내일", "모레", "글피", "어제", "요즘", "지금", "이번주", "주말", "오늘의",
                 "현재", "이런", "무슨", "좋은", "날씨", "추운", "더운", "미친", "실화", "와", "다음주"}
WEATHER_DESC = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
    51: "이슬비", 53: "이슬비", 55: "이슬비", 56: "어는 이슬비", 57: "어는 이슬비",
    61: "비", 63: "비", 65: "강한 비", 66: "어는 비", 67: "어는 비",
    71: "눈", 73: "눈", 75: "강한 눈", 77: "싸락눈",
    80: "소나기", 81: "소나기", 82: "강한 소나기", 85: "눈 소나기", 86: "눈 소나기",
    95: "뇌우", 96: "우박 동반 뇌우", 99: "우박 동반 뇌우",
}
WEATHER_ICON = {0: "☀️", 1: "🌤", 2: "⛅", 3: "☁️", 45: "🌫", 48: "🌫"}
_GEO_OK = {"city", "town", "village", "hamlet", "borough", "suburb", "quarter", "neighbourhood",
           "city_district", "district", "county", "state", "province", "municipality", "region",
           "country", "island", "islet", "railway", "administrative", "locality", "legal"}
_geo_cache = {}
_weather_last = {"t": 0.0}
FORECAST_DAYS = 16   # Open-Meteo 예보 한도
PAST_DAYS = 30       # 지난 날짜도 이 범위면 실측으로 답한다

_KO_DAY = {"오늘": 0, "내일": 1, "낼": 1, "모레": 2, "내일모레": 2, "낼모레": 2, "글피": 3,
           "어제": -1, "그제": -2, "그저께": -2}
_WD_IDX = {c: i for i, c in enumerate("월화수목금토일")}


def _parse_weather_dates(s: str, today: datetime.date):
    """문자열에서 날짜 표현을 뽑는다. (남은 문자열, [날짜들] 또는 None=지금)."""
    days = None

    def take(pat, fn):
        nonlocal s, days
        m = re.search(pat, s)
        if m and days is None:
            days = fn(m)
            s = (s[:m.start()] + " " + s[m.end():]).strip()

    def md(mo, d):
        try:
            x = datetime.date(today.year, mo, d)
        except ValueError:
            return []
        if x < today - datetime.timedelta(days=60):   # 1/5에 '12/30'이면 작년, 12월에 '1/3'이면 내년
            x = datetime.date(today.year + 1, mo, d)
        elif x > today + datetime.timedelta(days=300):
            x = datetime.date(today.year - 1, mo, d)
        return [x]

    monday = today - datetime.timedelta(days=today.weekday())
    take(r"(?<!\d)(1[0-2]|0?[1-9])\s*월\s*([12]\d|3[01]|0?[1-9])\s*일?",
         lambda m: md(int(m.group(1)), int(m.group(2))))
    take(r"(?<![\d/.])(1[0-2]|0?[1-9])\s*[/.]\s*([12]\d|3[01]|0?[1-9])(?![\d/.])",
         lambda m: md(int(m.group(1)), int(m.group(2))))
    take(r"(이번\s*주|다음\s*주|담주)?\s*주말",
         lambda m: [monday + datetime.timedelta(days=(7 if m.group(1) and "이번" not in m.group(1) else 0) + k)
                    for k in (5, 6)])
    take(r"(이번\s*주|다음\s*주|담주)?\s*([월화수목금토일])\s*요일",
         lambda m: [(lambda base: base if (m.group(1) or base >= today) else base + datetime.timedelta(days=7))(
             monday + datetime.timedelta(days=(7 if m.group(1) and "이번" not in m.group(1) else 0)
                                         + _WD_IDX[m.group(2)]))])
    take(r"(다음\s*주|담주)",
         lambda m: [monday + datetime.timedelta(days=7 + k) for k in range(7)])
    take(r"이번\s*주",
         lambda m: [today + datetime.timedelta(days=k) for k in range(7 - today.weekday())])
    # '13일'처럼 일만 쓰면 이번 달(이미 지났으면 다음 달)
    def dom(m):
        d = int(m.group(1))
        y, mo = today.year, today.month
        if d < today.day:
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        try:
            return [datetime.date(y, mo, d)]
        except ValueError:
            return []
    take(r"(?<![\d/.])([12]\d|3[01]|0?[1-9])\s*일(?![가-힣])", dom)
    take(r"(?<![가-힣])(내일\s*모레|낼\s*모레|오늘|내일|낼|모레|글피|어제|그저께|그제)(?![가-힣])",
         lambda m: [today + datetime.timedelta(days=_KO_DAY[re.sub(r"\s", "", m.group(1))])])
    if days is not None and days == [today]:
        days = None   # '오늘 ○○ 날씨'는 지금 날씨로
    return re.sub(r"\s+", " ", s).strip(" ·"), days


def weather_query(text: str, today=None):
    """'서울 중구 날씨' → ('서울 중구', None), '내일 판교 날씨' → ('판교', [내일]).
    날씨 요청 형식이 아니면 None."""
    if not text or "#" in text or "\n" in text.strip():
        return None
    m = _WEATHER_RE.match(text)
    if not m:
        return None
    q, days = _parse_weather_dates(re.sub(r"\s+", " ", m.group(1)).strip(" ·"), today or today_kst())
    if not q or q in _WEATHER_SKIP or q.split(" ")[0] in _WEATHER_SKIP:
        return None
    loose = bool((m.group(2) or "").strip(" ?？!.~"))   # '날씨' 뒤에 말이 더 붙음(잡담일 수도)
    return q, days, loose


def _nominatim(q, kr=True):
    params = {"q": q, "format": "json", "limit": 8, "accept-language": "ko"}
    if kr:
        params["countrycodes"] = "kr"
    try:
        rows = requests.get("https://nominatim.openstreetmap.org/search", params=params,
                            headers={"User-Agent": "hantang-study-bot/1.0"}, timeout=10).json()
    except Exception as e:
        log(f"[날씨] 지명 검색 오류 {q}: {e}")
        rows = []
    time.sleep(1.1)   # Nominatim 이용 규칙: 초당 1회
    return rows or []


def _clean_parts(display_name):
    parts = [p.strip() for p in (display_name or "").split(",")]
    return [p for p in parts if p and not re.fullmatch(r"[\d\-]+", p) and p != "대한민국"]


# 구·시·군 이름 줄임말. '동대문 날씨'가 동대문역·흥인지문(종로구)으로, '용산 날씨'가
# 대구 용산역으로 잡혔다(2026-11-12). 여기 있는 이름이면 '동대문구'·'용산구'로 먼저 찾는다.
_GU_NAMES = set("""종로 용산 성동 광진 동대문 중랑 성북 강북 도봉 노원 은평 서대문 마포 양천 강서 구로 금천
영등포 동작 관악 서초 강남 송파 강동 영도 부산진 동래 해운대 사하 금정 연제 수영 사상 미추홀 연수 남동 부평
계양 서해 검단 제물포 영종 수성 달서 광산 유성 대덕 분당 수지 기흥 처인 일산동 일산서 덕양 영통 팔달 권선 장안
단원 상록 동안 만안 원미 소사 오정 수정 중원 상당 서원 흥덕 청원 의창 성산 마산합포 마산회원 진해""".split())
_SI_NAMES = set("""수원 성남 고양 용인 부천 안산 안양 남양주 화성 평택 의정부 시흥 파주 김포 광명 군포 하남 오산
이천 안성 의왕 양주 구리 포천 동두천 과천 여주 천안 청주 전주 창원 김해 포항 제주 서귀포 춘천 원주 강릉 목포
여수 순천 경주 구미 진주 아산 양산 거제 익산 군산 충주 제천 당진 서산 논산 공주 보령 안동 김천 영주 상주
경산 통영 사천 밀양 속초 동해 삼척 태백 정읍 남원 김제 나주 광양""".split())
_GUN_NAMES = set("기장 달성 군위 울주 가평 양평 연천 강화 옹진 홍천 횡성 평창 정선 양양 고성 철원 화천 양구 인제".split())


def _named_hit(rows, name):
    """첫 칸 이름이 정확히 name인 행정구역 결과(여럿이면 서울 우선)."""
    c = [x for x in rows if x.get("addresstype") in _GEO_OK
         and x.get("addresstype") not in ("railway", "island", "islet")
         and (_clean_parts(x.get("display_name")) or [""])[0] == name]
    c.sort(key=lambda x: "서울특별시" not in x.get("display_name", ""))
    return c[0] if c else None


def geocode(q: str):
    """(위도, 경도, 표시 이름, 국내여부) 또는 None. 찾는 순서:
    ① 구·시·군 줄임말이면 '○○구/시/군'으로(동대문→동대문구) ② 이름이 정확히 같은 행정구역
    ③ 국내 행정구역·역·섬 ④ '○○동'으로 다시(청라→청라동) ⑤ 주소에 그 이름이 든 동네 ⑥ 해외.
    2026-11-12 '청라'가 강남 식당·고속도로 나들목으로만, '동대문'이 종로구 동대문역으로 잡혔다."""
    if q in _geo_cache:
        return _geo_cache[q]
    hit, label, rows = None, None, []
    bare = " " not in q and not q.endswith(("동", "구", "시", "군", "읍", "면", "도"))
    if bare:
        for suf, names in (("구", _GU_NAMES), ("시", _SI_NAMES), ("군", _GUN_NAMES)):
            if q in names:
                r = _nominatim(q + suf)
                hit = _named_hit(r, q + suf)
                if hit:
                    break
    if not hit:
        rows = _nominatim(q)
        hit = _named_hit(rows, q) or next((x for x in rows if x.get("addresstype") in _GEO_OK), None)
    if not hit and bare:
        rows2 = _nominatim(q + "동")
        hit = next((x for x in rows2 if x.get("addresstype") in _GEO_OK), None)
        rows = rows + rows2
    if not hit:
        # 주소 칸(첫 칸=장소 이름 제외)에 q로 시작하는 동네가 있는 결과
        key = q.replace(" ", "")
        for x in rows:
            parts = _clean_parts(x.get("display_name"))
            for i, p in enumerate(parts[1:], start=1):
                if p.replace(" ", "").startswith(key) and re.search(r"(동|구|시|군|읍|면|신도시|지구)$", p):
                    hit = x
                    label = " ".join(list(reversed(parts[i:]))[:3])
                    break
            if hit:
                break
    if not hit:
        rows = _nominatim(q, kr=False)
        hit = next((x for x in rows if x.get("addresstype") in _GEO_OK), None)
    if not hit:
        _geo_cache[q] = None
        return None
    if not label:
        # '중구, 서울특별시' → '서울특별시 중구' (큰 단위부터, 최대 3단계)
        label = " ".join(list(reversed(_clean_parts(hit.get("display_name"))))[:3]) or q
    out = (float(hit["lat"]), float(hit["lon"]), label, "대한민국" in hit.get("display_name", ""))
    _geo_cache[q] = out
    return out


def _pm_grade(v, kind):
    if v is None:
        return ""
    cuts = (30, 80, 150) if kind == "pm10" else (15, 35, 75)   # 환경부 기준
    g = "좋음" if v <= cuts[0] else "보통" if v <= cuts[1] else "나쁨" if v <= cuts[2] else "매우나쁨"
    return f"{g}({round(v)})"


def _wx_icon(code):
    if code in WEATHER_ICON:
        return WEATHER_ICON[code]
    if code is None:
        return "🌡"
    if 51 <= code <= 67 or code in (80, 81, 82):
        return "🌧"
    if 71 <= code <= 86:
        return "🌨"
    if code >= 95:
        return "⛈"
    return "🌡"


def _md_wd(d: datetime.date) -> str:
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]})"


def weather_now_text(lat, lon, label, is_kr) -> str:
    w = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "wind_speed_unit": "ms",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
                   "weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 1}, timeout=10).json()
    cur, day = w["current"], w.get("daily", {})
    code = cur.get("weather_code")
    hhmm = str(cur.get("time", ""))[-5:]
    lines = [f"{_wx_icon(code)} {label} 지금 날씨 ({hhmm}{'' if is_kr else ' 현지'})",
             f"{WEATHER_DESC.get(code, '')} {cur['temperature_2m']:.1f}°C (체감 {cur['apparent_temperature']:.1f}°C)"]
    extra = []
    try:
        extra.append(f"최고 {day['temperature_2m_max'][0]:.0f}° / 최저 {day['temperature_2m_min'][0]:.0f}°")
        if day.get("precipitation_probability_max") and day["precipitation_probability_max"][0] is not None:
            extra.append(f"강수확률 {day['precipitation_probability_max'][0]}%")
    except Exception:
        pass
    extra.append(f"습도 {cur['relative_humidity_2m']}%")
    extra.append(f"바람 {cur['wind_speed_10m']:.1f}m/s")
    lines.append(" · ".join(extra))
    try:
        aq = requests.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
            "latitude": lat, "longitude": lon, "current": "pm10,pm2_5", "timezone": "auto"},
            timeout=10).json().get("current", {})
        if aq.get("pm10") is not None:
            lines.append(f"미세먼지 {_pm_grade(aq.get('pm10'), 'pm10')} · 초미세먼지 {_pm_grade(aq.get('pm2_5'), 'pm25')}")
    except Exception:
        pass
    return "\n".join(lines)


def weather_days_text(lat, lon, label, days, today) -> str:
    """특정일(들) 날씨. 지난 날은 실측, 오늘 이후는 예보. 미세먼지는 예보가 있는 날(약 5일)만."""
    lo, hi = today - datetime.timedelta(days=PAST_DAYS), today + datetime.timedelta(days=FORECAST_DAYS - 1)
    ok = [d for d in days if lo <= d <= hi]
    if not ok:
        d0 = days[0]
        if d0 > hi:
            return f"{_md_wd(d0)} 날씨는 아직 예보가 없어요 (약 {FORECAST_DAYS}일 뒤까지만 나와요)"
        return f"{_md_wd(d0)}은 너무 지난 날짜라 조회가 안 돼요 (최근 {PAST_DAYS}일까지)"
    start, end = min(ok), max(ok)
    w = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "wind_speed_unit": "ms",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,wind_speed_10m_max",
        "start_date": str(start), "end_date": str(end)}, timeout=10).json()
    dl = w.get("daily", {})
    rows = {}
    for i, t in enumerate(dl.get("time", [])):
        rows[t] = {k: (v[i] if i < len(v) else None) for k, v in dl.items()}
    # 미세먼지 예보(일평균) — 있는 날만
    pm = {}
    try:
        aq = requests.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
            "latitude": lat, "longitude": lon, "hourly": "pm10,pm2_5", "timezone": "auto",
            "start_date": str(start), "end_date": str(end)}, timeout=10).json().get("hourly", {})
        acc = {}
        for t, a, b in zip(aq.get("time", []), aq.get("pm10", []), aq.get("pm2_5", [])):
            if a is not None and b is not None:
                acc.setdefault(t[:10], []).append((a, b))
        pm = {d: (sum(x for x, _ in v) / len(v), sum(y for _, y in v) / len(v)) for d, v in acc.items()}
    except Exception:
        pass

    def one(d, r):
        code = r.get("weather_code")
        bits = [f"{WEATHER_DESC.get(code, '')} 최고 {r['temperature_2m_max']:.0f}° / 최저 {r['temperature_2m_min']:.0f}°"]
        if d >= today and r.get("precipitation_probability_max") is not None:
            bits.append(f"강수확률 {r['precipitation_probability_max']}%")
        if (r.get("precipitation_sum") or 0) >= 0.1:
            bits.append(f"강수량 {r['precipitation_sum']:.1f}mm")
        return code, bits

    past = all(d < today for d in ok)
    tail = " (실측)" if past else ""
    if len(ok) == 1:
        d = ok[0]
        r = rows.get(str(d))
        if not r or r.get("temperature_2m_max") is None:
            return f"{label} {_md_wd(d)} 날씨 정보를 가져오지 못했어요."
        code, bits = one(d, r)
        lines = [f"{_wx_icon(code)} {label} {_md_wd(d)} 날씨{tail}", bits[0]]
        rest = bits[1:]
        if r.get("wind_speed_10m_max") is not None:
            rest.append(f"바람 최대 {r['wind_speed_10m_max']:.1f}m/s")
        if rest:
            lines.append(" · ".join(rest))
        if str(d) in pm:
            a, b = pm[str(d)]
            lines.append(f"미세먼지 {_pm_grade(a, 'pm10')} · 초미세먼지 {_pm_grade(b, 'pm25')} "
                         f"(하루 평균{'' if d < today else ' 예보'})")
        return "\n".join(lines)
    lines = [f"📅 {label} 날씨 {_md_wd(ok[0])}~{_md_wd(ok[-1])}{tail}"]
    for d in ok:
        r = rows.get(str(d))
        if not r or r.get("temperature_2m_max") is None:
            continue
        code, bits = one(d, r)
        lines.append(f"{_md_wd(d)} {_wx_icon(code)} " + " · ".join(bits))
    if len(ok) < len(days):
        lines.append(f"※ {FORECAST_DAYS}일 뒤부터는 아직 예보가 없어요")
    return "\n".join(lines)


def weather_text(q: str, days=None, today=None) -> str:
    geo = geocode(q)
    if not geo:
        return f"'{q}' 지역을 찾지 못했어요. 예) 서울 중구 날씨, 청라동 내일 날씨"
    lat, lon, label, is_kr = geo
    if not days:
        return weather_now_text(lat, lon, label, is_kr)
    return weather_days_text(lat, lon, label, days, today or today_kst())


def handle_weather(msg: dict) -> bool:
    """날씨 요청이면 답장하고 True."""
    parsed = weather_query(msg.get("text", "") or "")
    if not parsed:
        return False
    q, days, _loose = parsed
    # 지명을 못 찾아도 반드시 '못 찾았어요'로 답한다. 조용히 넘기면 서버 장애로 오해한다(사용자 지시 11-12).
    if time.time() - _weather_last["t"] < 3:   # 연속 요청 과부하 방지
        return True
    _weather_last["t"] = time.time()
    try:
        txt = weather_text(q, days)
    except Exception as e:
        log(f"[날씨] 조회 실패 {q} {days}: {e}")
        txt = "날씨 정보를 가져오지 못했어요. 잠시 뒤 다시 시도해 주세요."
    chat_id = (msg.get("chat") or {}).get("id")
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": chat_id, "text": txt,
                            "reply_to_message_id": msg.get("message_id"),
                            "allow_sending_without_reply": "true"}, timeout=15)
    except Exception as e:
        log(f"[날씨] 답장 실패: {e}")
    log(f"[날씨] '{q}' {days or '지금'} → {txt.splitlines()[0]}")
    return True


# ── '생년월일 운세' 답장 (사주명리 일주 기준) ──────────────────────────────
# '1990.5.17 운세', '900517 운세', '1990년 5월 17일 운세'처럼 치면 오늘 운세를 답장한다.
# 생시를 모르므로 사주 중 일주(日柱)만 쓴다: 태어난 날의 일간(나)과 오늘 일진의 천간 관계(십신),
# 태어난 날 일지와 오늘 일지의 합·충으로 풀이한다. LLM 없이 규칙과 문구표로 만든다(크레딧 소진 §10-1).
_STEMS = "갑을병정무기경신임계"
_STEMS_H = "甲乙丙丁戊己庚辛壬癸"
_BRANCHES = "자축인묘진사오미신유술해"
_BRANCHES_H = "子丑寅卯辰巳午未申酉戌亥"
_ELEM = ["목", "목", "화", "화", "토", "토", "금", "금", "수", "수"]   # 천간 → 오행
_ELEM_H = {"목": "木", "화": "火", "토": "土", "금": "金", "수": "水"}
_GEN = {"목": "화", "화": "토", "토": "금", "금": "수", "수": "목"}    # 생(生)
_CTRL = {"목": "토", "토": "수", "수": "화", "화": "금", "금": "목"}   # 극(剋)
_LUCKY_COLOR = {"목": "초록색", "화": "빨간색", "토": "노란색·베이지", "금": "흰색·은색", "수": "검정·남색"}
_LUCKY_NUM = {"목": "3, 8", "화": "2, 7", "토": "5, 10", "금": "4, 9", "수": "1, 6"}   # 하도(河圖) 수
_LUCKY_DIR = {"목": "동쪽", "화": "남쪽", "토": "중앙", "금": "서쪽", "수": "북쪽"}
_LIUHE = {frozenset(p) for p in [(0, 1), (2, 11), (3, 10), (4, 9), (5, 8), (6, 7)]}     # 육합
_CHONG = {frozenset((i, i + 6)) for i in range(6)}                                  # 육충

# 십신별 풀이(입말): (별점 기본, 쉬운 이름, 총평들, 돈·투자, 사람, 한마디)
# 화면엔 '편재·일간·충' 같은 용어를 쓰지 않는다(사용자 지시 11-12) — 계산만 사주 일주 기준.
_SIPSIN = {
    "비견": (3, "내 편이 생기는",
           ["나랑 닮은 기운이 들어와서 힘이 나는 날이에요. 밀어붙이면 잘 나가요.",
            "든든한 친구가 옆에 있는 느낌의 날이에요. 혼자보다 같이 할 때 잘 풀려요."],
           "다들 같은 데 몰려서 몫이 나뉘기 쉬워요. 남 따라 사는 건 조심!",
           "친구·동료랑 손발이 잘 맞아요. 다만 고집 싸움은 피하세요.",
           "내 페이스는 지키되, 남 얘기도 한 번은 들어보기."),
    "겁재": (2, "경쟁이 붙는",
           ["욕심이 확 올라오는 날이에요. 과감함이 오히려 독이 될 수 있어요.",
            "괜히 남이랑 부딪히면서 힘이 새기 쉬운 날이에요."],
           "충동 매수, 몰빵은 참으세요. 돈 빌려주는 것도 오늘은 미루기.",
           "말다툼이 나기 쉬워요. 말은 한 박자 늦게!",
           "오늘은 지키는 게 버는 거예요."),
    "식신": (4, "먹을 복·여유가 들어오는",
           ["마음에 여유가 생기고 먹을 복이 있는 날이에요. 하던 일이 술술 풀려요.",
            "재주가 자연스럽게 드러나는 날이에요. 표정부터 밝아져요."],
           "들고 있는 종목은 편하게 지켜봐도 되는 날. 무리해서 더 사진 마세요.",
           "분위기 메이커가 되는 날! 맛있는 거 같이 먹으면 운이 붙어요.",
           "떠오른 아이디어는 바로 메모해 두세요."),
    "상관": (3, "말발·재치가 살아나는",
           ["말발이 서고 재치가 넘치는 날이에요. 대신 말실수는 조심!",
            "틀을 깨고 뭔가 새로 해보고 싶어지는 날이에요."],
           "새 테마, 새 종목이 눈에 들어와요. 공부는 좋은데 진입은 천천히.",
           "윗사람이랑 부딪히기 쉬워요. 단톡방도 한 번 더 읽고 전송!",
           "번뜩이는 생각은 하루 묵혔다가 실행하기."),
    "편재": (4, "큰돈·기회가 들어오는",
           ["돈 흐름이 눈에 잘 보이는 날이에요. 여기저기 움직일 일이 많아져요.",
            "기회를 잡는 감이 좋아지는 날이에요."],
           "투자 감이 살아나요. 다만 수익 났다고 판을 키우진 마세요.",
           "사람 만나는 자리에서 좋은 정보가 들어와요.",
           "기회는 잡되, 손절 라인은 미리 정해 두기."),
    "정재": (4, "돈이 차곡차곡 쌓이는",
           ["성실하게 한 만큼 그대로 돌아오는 날이에요.",
            "돈 운이 안정적인 날이에요. 계획대로만 하면 돼요."],
           "나눠 사기, 길게 들고 가기처럼 원칙대로 하는 매매가 빛나요.",
           "약속 잘 지키면 믿음이 쌓여요.",
           "가계부나 계좌 정리하기 딱 좋은 날."),
    "편관": (2, "압박·시험이 들어오는",
           ["일이 몰리고 어깨가 무거워지는 날이에요.",
            "생각지 못한 변수가 생기기 쉬운 날이에요."],
           "출렁임에 흔들리기 쉬워요. 손절·익절은 정해둔 대로 기계적으로!",
           "윗사람이나 규칙이랑 부딪히지 않게 조심. 무리한 약속은 금물.",
           "버티는 게 실력이에요. 건강도 꼭 챙기세요."),
    "정관": (4, "인정받는",
           ["일이 바르게 풀리고 인정받는 날이에요.",
            "책임감이 빛나는 날이에요. 공식적인 일에 특히 좋아요."],
           "원칙대로 한 매매가 결과로 이어져요. 공시·리포트 꼼꼼히 보기.",
           "윗사람한테 점수 따기 좋은 날. 예의 바르게!",
           "정공법이 지름길이에요."),
    "편인": (3, "촉·영감이 오는",
           ["촉이 좋고 아이디어가 번뜩이는 날이에요. 혼자 생각할 시간이 필요해요.",
            "남들과 다른 게 보이는 날이에요. 대신 마음이 자주 바뀌어요."],
           "남들이 못 보는 게 보여요. 그래도 확신 들 때까지는 조금만!",
           "혼자만의 시간이 오히려 관계를 지켜줘요.",
           "공부·리서치에 푹 빠지기 좋은 날."),
    "정인": (5, "도와주는 사람이 생기는",
           ["귀인이 나타나고 배우는 운이 좋은 날이에요.",
            "든든한 지원군이 생기는 날이에요. 마음이 편안해져요."],
           "좋은 정보·조언이 들어와요. 스터디 멤버들 말에 귀 기울여 보세요.",
           "선배·윗사람이 도와줘요. 고맙다는 말이 운을 키워요.",
           "계약이나 서류 일은 오늘 처리하면 좋아요."),
}
# 천간을 쉬운 그림으로(일간 = 타고난 기운)
_STEM_IMG = ["큰 나무", "꽃", "태양", "촛불", "큰 산", "논밭", "바위", "보석", "바다", "빗물"]
_STEM_ICON = ["🌳", "🌷", "☀️", "🕯", "⛰", "🌾", "🪨", "💎", "🌊", "🌧"]


def _jdn(d: datetime.date) -> int:
    return d.toordinal() + 1721425   # 율리우스 적일(JDN)


def day_pillar(d: datetime.date):
    """(천간 index, 지지 index). 2000-01-01 = 무오(戊午)일로 검증."""
    j = _jdn(d)
    return (j + 9) % 10, (j + 1) % 12


def sipsin(me: int, other: int) -> str:
    """일간 me 기준 천간 other의 십신."""
    a, b = _ELEM[me], _ELEM[other]
    same = (me % 2) == (other % 2)
    if a == b:
        return "비견" if same else "겁재"
    if _GEN[a] == b:
        return "식신" if same else "상관"
    if _CTRL[a] == b:
        return "편재" if same else "정재"
    if _CTRL[b] == a:
        return "편관" if same else "정관"
    return "편인" if same else "정인"   # b가 a를 생함


_FORTUNE_RE = re.compile(
    r"^\s*(?:(\d{4}|\d{2})\s*(?:년|[./\-])\s*(\d{1,2})\s*(?:월|[./\-])\s*(\d{1,2})\s*일?"
    r"|(\d{8}|\d{6}))\s*(?:생)?\s*(?:오늘\s*)?운세\s*[?？!.]*\s*$")


def fortune_query(text: str, today=None):
    """'1990.5.17 운세' → date(1990,5,17). 형식이 아니면 None, 날짜가 틀리면 'bad'."""
    if not text or "#" in text:
        return None
    m = _FORTUNE_RE.match(text.strip())
    if not m:
        return None
    if m.group(4):
        s = m.group(4)
        y, mo, d = (s[:4], s[4:6], s[6:]) if len(s) == 8 else (s[:2], s[2:4], s[4:])
    else:
        y, mo, d = m.group(1), m.group(2), m.group(3)
    y, mo, d = int(y), int(mo), int(d)
    t = today or today_kst()
    if y < 100:
        y += 2000 if y <= t.year % 100 else 1900
    try:
        b = datetime.date(y, mo, d)
    except ValueError:
        return "bad"
    if b > t or b.year < 1900:
        return "bad"
    return b


def _josa(word: str, a: str, b: str) -> str:
    """받침 있으면 a, 없으면 b ('바위'+을/를 → '바위를')."""
    c = word[-1]
    has = "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 != 0
    return word + (a if has else b)


def _why(god: str, me: str, other: str) -> str:
    """오늘 기운이 왜 그런지 그림으로 한 줄."""
    if god == "비견":
        return f"오늘도 나랑 똑같은 {me} 기운이 들어오는 날이에요"
    if god == "겁재":
        el = {"목": "나무", "화": "불", "토": "흙", "금": "쇠", "수": "물"}[_ELEM[_STEM_IMG.index(me)]]
        return f"{_josa(other, '도', '도')} 나처럼 {el} 기운이라, 한편이면서 경쟁자인 날이에요"
    if god in ("식신", "상관"):
        return f"{me}인 내가 {_josa(other, '을', '를')} 키워내는 날이에요"
    if god in ("편재", "정재"):
        return f"{me}인 내가 {_josa(other, '을', '를')} 다루는 날이에요"
    if god in ("편관", "정관"):
        return f"{_josa(other, '이', '가')} {me}인 나를 다잡는 날이에요"
    return f"{_josa(other, '이', '가')} {me}인 나를 채워주는 날이에요"


def fortune_text(birth: datetime.date, today=None) -> str:
    t = today or today_kst()
    ms, mb = day_pillar(birth)
    ts, tb = day_pillar(t)
    god = sipsin(ms, ts)
    base, easy, heads, money, people, tip = _SIPSIN[god]
    me, other = _STEM_IMG[ms], _STEM_IMG[ts]
    pair = frozenset((mb, tb))
    rel = ""
    stars = base
    if pair in _LIUHE:
        stars += 1
        rel = "🤝 오늘은 날이랑 궁합이 잘 맞아요. 사람도 일도 착착 붙어요."
    elif pair in _CHONG:
        stars -= 1
        rel = "⚡ 오늘은 날이랑 살짝 부딪혀요. 일정이 바뀌거나 움직일 일이 많으니 서두르지 마세요."
    stars = max(1, min(5, stars))
    seed = (birth.toordinal() * 31 + t.toordinal()) % len(heads)   # 같은 사람·같은 날 = 같은 문장
    me_el = _ELEM[ms]
    helper = [k for k, v in _GEN.items() if v == me_el][0]   # 나를 채워주는 기운 = 행운
    lines = [
        f"🔮 {birth.year}.{birth.month}.{birth.day}생 오늘 운세 ({t.month}/{t.day})",
        f"{_STEM_ICON[ms]} 타고난 기운은 '{me}'이에요." if (ord(me[-1]) - 0xAC00) % 28 else
        f"{_STEM_ICON[ms]} 타고난 기운은 '{me}'예요.",
        f"오늘은 {easy} 날! ({_why(god, me, other)})",
        f"오늘 점수 {'★' * stars}{'☆' * (5 - stars)}",
        heads[seed],
    ]
    if rel:
        lines.append(rel)
    lines += [
        f"💰 돈·투자: {money}",
        f"👥 사람: {people}",
        f"💡 한마디: {tip}",
        f"🍀 행운: {_LUCKY_COLOR[helper]} · 숫자 {_LUCKY_NUM[helper]} · {_LUCKY_DIR[helper]}",
        "※ 태어난 날 기준 사주로 본 재미용이에요. 매매는 원칙대로!",
    ]
    return "\n".join(lines)


def handle_fortune(msg: dict) -> bool:
    """운세 요청이면 답장하고 True."""
    b = fortune_query(msg.get("text", "") or "")
    if b is None:
        return False
    if time.time() - _weather_last["t"] < 2:
        return True
    _weather_last["t"] = time.time()
    if b == "bad":
        txt = "생년월일을 다시 확인해 주세요. 예) 1990.5.17 운세 / 900517 운세"
    else:
        try:
            txt = fortune_text(b)
        except Exception as e:
            log(f"[운세] 생성 실패: {e}")
            txt = "운세를 만들지 못했어요. 잠시 뒤 다시 시도해 주세요."
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": (msg.get("chat") or {}).get("id"), "text": txt,
                            "reply_to_message_id": msg.get("message_id"),
                            "allow_sending_without_reply": "true"}, timeout=15)
    except Exception as e:
        log(f"[운세] 답장 실패: {e}")
    head = txt.splitlines()[1] if len(txt.splitlines()) > 1 else txt
    log(f"[운세] {b} → {head}")
    return True


GROUP_ID_FILE = BASE_DIR / "_group_chat_id.txt"


def group_chat_id() -> str:
    """라운드 현황을 보낼 단톡방. GROUP_CHAT_ID 환경변수가 있으면 그것, 없으면
    멤버가 그룹에서 보낸 메시지로 학습해 둔 값(VM에 본채널 시크릿이 없어서)."""
    if GROUP_CHAT_ID:
        return GROUP_CHAT_ID
    try:
        return GROUP_ID_FILE.read_text().strip()
    except Exception:
        return ""


def _remember_group(msg: dict):
    chat = msg.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return
    if MEMBER_MAP.get((msg.get("from") or {}).get("id")) is None:
        return
    cid = str(chat.get("id"))
    if cid and cid != group_chat_id():
        try:
            GROUP_ID_FILE.write_text(cid)
            log(f"[단톡방 등록] chat_id={cid} ({chat.get('title', '')})")
            notify_admin(f"📌 단톡방 등록: {chat.get('title', '')} — 라운드 현황을 여기로 보냅니다")
        except Exception as e:
            log(f"[경고] 단톡방 chat_id 저장 실패: {e}")


def handle_message(msg: dict, cache: SheetCache):
    text = msg.get("text", "") or msg.get("caption", "") or ""
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    sender = (msg.get("from") or {}).get("first_name", "")

    # 그룹 필터 (지정된 경우)
    if GROUP_CHAT_ID and str(chat_id) != GROUP_CHAT_ID:
        return
    _remember_group(msg)

    # 수집 모드: 감지/기록 없이 보낸이 정보만 모은다
    if COLLECT_MODE:
        collect_sender(msg, cache)
        return

    tag = detect_tag(text)
    if tag is None:
        handle_weather(msg) or handle_fortune(msg)   # '서울 중구 날씨', '1990.5.17 운세' 요청이면 답장
        return  # 매수/매도 태그 없음 → 무시(알림 없음)

    frm = msg.get("from") or {}
    sid = frm.get("id")
    member = MEMBER_MAP.get(sid)
    who = member if member else f"미매핑(id={sid}, {sender})"
    body = text.strip().replace("\n", " ")[:200]

    if tag == "buy":
        if not member:
            notify_admin(f"🟢 매수 감지(미매핑) — id={sid} {sender}\n원문: {body}\n"
                         f"※ 멤버 매핑 안 됨 → 자동기록 안 함")
            log(f"[매수-미매핑] id={sid}")
            return
        rec_day = next_krx_session(today_kst())
        ws, values = cache.ensure(rec_day)
        if ws is None:
            held = unresolved_add(cache, rec_day, member, "분기 탭 없음", body)
            notify_admin(f"🟢 매수 감지 — {member}\n원문: {body}\n"
                         f"※ {rec_day} 분기 탭을 찾지도 만들지도 못함 → 자동기록 안 함"
                         + (f"\n※ {rec_day} 미추천 패널티 보류 등록" if held else ""))
            log(f"[매수-탭없음] {member} {rec_day} | 원문: {body}")
            return
        pws, pvals = cache.prev(rec_day)
        hist = past_labels(values, member)
        if pvals:
            hist += [h for h in past_labels(pvals, member) if h not in hist]
        stock, why = resolve_stock(text, history=hist)
        if not stock:
            held = unresolved_add(cache, rec_day, member, "종목 미인식", body)
            notify_admin(f"🟢 매수 감지 — {member}\n원문: {body}\n"
                         f"※ 종목명 자동인식 실패 → 자동기록 안 함(수동 확정 필요)\n"
                         f"사유: {why}"
                         + (f"\n※ {rec_day} 미추천 패널티 보류 등록 — 종목을 수동 기록하면 해제, "
                            f"정말 미추천이면 {UNRESOLVED_TAB} status에 '패널티'" if held else ""))
            log(f"[매수-미인식] {member}: {why}")
            return
        dup = find_active_dup(values, member, stock)
        # 중복 판정은 '기록할 분기 탭' 안에서만 한다. 분기가 바뀌면 점수도 새로 시작하므로
        # 직전 분기 보유분과 같은 종목을 새 분기에 추천하는 건 정상이다.
        # (2026-10-12 이원호 KODEX 미국10년국채선물 — 3분기 보유분 때문에 보류됐던 사고)
        # 같은 분기에 같은 종목을 이미 들고 있어도, 추천일이 다르면 재추천으로 기록한다.
        # 규칙상 같은 종목 연속 추천이 허용되고, 보류했던 건은 매번 운영자가 '정당한
        # 재추천'으로 확인했다(10/12 김태완 한화솔루션, 10/19 이광훈 대주전자재료).
        # 같은 추천일로 이미 있으면(메시지 중복 전송) 그대로 막는다.
        if dup and not dup_on_date(values, member, stock, rec_day):
            why += f" | 재추천 → 기존 보유({dup})와 별도로 기록"
            dup = ""
        if dup:
            notify_admin(f"⚠️ 매수 감지 — {member} / {stock}\n원문: {body}\n"
                         f"※ 이미 활성 보유 중: {dup}\n"
                         f"→ 중복 의심으로 자동기록 보류(정당한 재추천이면 "
                         f"batch_add allow_dup=true로 수동 추가)\n근거: {why}")
            log(f"[매수-중복보류] {member}/{stock}: {dup} | 근거: {why} | 원문: {body}")
            return
        ok, result = add_stock(ws, values, member, stock, rec_day)
        if not ok and unresolved_add(cache, rec_day, member, f"기록 실패: {stock}", body):
            result += f"\n※ {rec_day} 미추천 패널티 보류 등록"
        if ok:
            cache.refresh(ws)
            result = f"[{ws.title}] {result}"
            if rec_day != today_kst():
                result += f"\n※ 휴장일 추천 → 추천일을 다음 거래일 {rec_day}로 기록"
        icon = "✅" if ok else "❌"
        notify_admin(f"{icon} 매수 자동기록 — {member} / {stock}\n원문: {body}\n{result}\n근거: {why}")
        # 원문을 함께 남긴다. 예전엔 미인식 건만 원문을 찍어서, 엉뚱한 종목이
        # '정상 기록'되면 무엇을 보고 그렇게 판정했는지 되짚을 수가 없었다.
        # (2026-09-28 송지호 → 'PCB 뱅코프(PCB)' 오기록)
        log(f"[매수-{'기록' if ok else '실패'}] {member}/{stock}: {result} | 근거: {why} | 원문: {body}")
    else:  # sell
        if not member:
            notify_admin(f"🔴 매도 감지(미매핑) — id={sid} {sender}\n원문: {body}\n"
                         f"※ 멤버 매핑 안 됨 → 자동처리 안 함")
            log(f"[매도-미매핑] id={sid}")
            return
        # 매도는 보유 종목 중에서 고르는 일이므로 후보를 보유분으로 좁힌다.
        # 분기 전환 직후엔 직전 분기 보유분(최장 한 달)이 남아 있다. 두 탭을 함께 본다.
        ws, values = cache.ensure()
        pws, pvals = cache.prev()
        held_cur = active_holdings(values, member) if values else []
        held_prev = active_holdings(pvals, member) if pvals else []
        held = held_prev + [h for h in held_cur if h not in held_prev]
        stock, why = resolve_stock(text, holdings=held)
        if not stock:
            notify_admin(f"🔴 매도 감지 — {member}\n원문: {body}\n"
                         f"※ 종목명 자동인식 실패 → 자동처리 안 함(수동 확정 필요)\n"
                         f"사유: {why}\n"
                         f"보유 중: {', '.join(held) if held else '없음'}")
            log(f"[매도-미인식] {member}: {why}")
            return
        # 한 메시지에 여러 종목을 줄마다 적은 경우(10/29 안병열 '#아모텍 1단위 / #SK하이닉스')
        # 줄별로 따로 인식해 각각 판다. 예전엔 한 종목만 잡고 나머지는 버렸다.
        plan = _multi_sell_plan(text, held)
        if len(plan) >= 2:
            lines = []
            ok_any = False
            for stk, cnt, ln, hint in plan:
                ok1, res1 = sell_across(cache, member, stk, today_kst(), count=cnt, rec_hint=hint)
                ok_any = ok_any or ok1
                lines.append(f"{'✅' if ok1 else '❌'} {stk} ({'전량' if cnt is None else f'{cnt}건'}) ← '{ln}'\n{res1}")
            notify_admin(f"{'✅' if ok_any else '❌'} 매도 자동처리(복수 종목) — {member}\n원문: {body}\n" + "\n".join(lines))
            log(f"[매도-복수] {member}: " + " || ".join(x.replace(chr(10), ' ') for x in lines) + f" | 원문: {body}")
            return
        cnt = _sell_count(text)
        hint = _rec_hint(_name_head(text))
        ok, result = sell_across(cache, member, stock, today_kst(), count=cnt, rec_hint=hint)
        if hint:
            why += f" | 원문 날짜 → 추천일 {hint} 보유분 지정"
        if cnt is None:
            why += " | 전량 표현 감지 → 같은 종목 보유분 전부 매도"
        elif cnt > 1:
            why += f" | 수량 표현 감지 → {cnt}건 매도"
        icon = "✅" if ok else "❌"
        notify_admin(f"{icon} 매도 자동처리 — {member} / {stock}\n원문: {body}\n{result}\n근거: {why}")
        log(f"[매도-{'처리' if ok else '실패'}] {member}/{stock}: {result} | 근거: {why} | 원문: {body}")


# '전량' 의도 표현. 같은 종목을 여러 번 추천해 여러 줄 들고 있을 때 한 메시지로 다 판다.
# (2026-10-15 안병열 '아이크래프트 2단위 다 매도'가 1건만 팔렸던 건)
_SELL_ALL_RE = re.compile(r"(전량|전부|모두|몽땅|싹\s*다|다\s*(?:매도|팔|정리|청산))")
_KO_NUM = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4}
_UNIT_RE = re.compile(r"(\d+|한|하나|두|둘|세|셋|네|넷)\s*(?:단위|개|건|주|번)")


_DATE_HINT_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9])\s*[/.월]\s*([12]\d|3[01]|0?[1-9])(?!\d)\s*(?:일)?\s*(?:자|추천|매수|분|건|일자)?")


def _rec_hint(seg: str, today=None):
    """'(10/26일자)', '10.26 추천분', '10월 26일자' → '2026-10-26'. 없으면 None.
    여러 개면 첫 번째. 연도는 오늘 기준(미래 날짜면 작년)."""
    m = _DATE_HINT_RE.search(seg or "")
    if not m:
        return None
    t = today or today_kst()
    mo, d = int(m.group(1)), int(m.group(2))
    try:
        dt = datetime.date(t.year, mo, d)
        if dt > t:
            dt = datetime.date(t.year - 1, mo, d)
        return str(dt)
    except ValueError:
        return None


def _sell_count(seg: str):
    """매도 수량. 1(기본) / N / None(전량).
    10/29 안병열 '#아모텍 1단위'를 'N단위=전량'으로 읽어 2건을 다 팔았던 사고 —
    숫자가 있으면 그 숫자만큼만 판다. '둘 다'는 2."""
    if re.search(r"둘\s*다", seg):
        return 2
    m = _UNIT_RE.search(seg)
    if m:
        v = m.group(1)
        return int(v) if v.isdigit() else _KO_NUM.get(v, 1)
    if _SELL_ALL_RE.search(seg):
        return None
    return 1


def _name_head(text: str) -> str:
    """사유(>, -, 1. 등) 시작 전 줄들. 날짜 지정은 여기서만 읽는다(사유 속 날짜 오인 방지)."""
    out = []
    for ln in (text or "").split("\n")[:8]:
        t = ln.strip()
        if re.match(r"^(>|-|\d+[.)]\s|ㅇ|사유)", t):
            break
        out.append(t)
    return "\n".join(out)


def _multi_sell_plan(text: str, held):
    """사유 시작 전 줄들을 한 줄씩 보유종목과 대조. [(종목, 수량, 줄)] — 서로 다른 종목만."""
    plan, seen = [], set()
    for ln in (text or "").split("\n")[:8]:
        t = ln.strip()
        if not t:
            continue
        if re.match(r"^(>|-|\d+[.)]|ㅇ|사유)", t):
            break
        body = re.sub(r"#?(종목추천|매도종목|매도|청산|매수|추천)\b", "", t).strip(" #")
        if len(body) < 2:
            continue
        stk, _w = resolve_stock(body, holdings=held)
        if not stk:
            continue
        key = _split_stock_label(stk)[1]
        if key in seen:
            continue
        seen.add(key)
        plan.append((stk, _sell_count(t), t, _rec_hint(t)))
    return plan


def sell_across(cache, member, stock, sell_date, sell_all=False, count=1, rec_hint=None):
    """직전 분기 → 현재 분기 순(가장 과거부터)으로 판다. sell_all이면 매칭이 없어질
    때까지 반복한다. 반환: (하나라도 성공, 결과 문자열)."""
    done, last = [], "보유 탭 없음"
    n = 10 if (sell_all or count is None) else max(1, int(count))
    for _ in range(n):
        sold = False
        for tws, _v in (cache.prev(), cache.ensure()):
            if tws is None:
                continue
            tvals = cache.values(tws, fresh=bool(done))
            ok, res = sell_stock(tws, tvals, member, stock, sell_date, cache, rec_hint=rec_hint)
            if ok:
                cache.refresh(tws)
                done.append(f"[{tws.title}] {res}")
                sold = True
                break
            if "활성 종목을 찾을 수 없음" not in res and "블록을 찾을 수 없음" not in res:
                last = f"[{tws.title}] {res}"
                break   # 그 탭에서 찾았는데 실패(빈 행 없음 등) — 다른 분기 탭으로 넘기지 않는다
            last = res
        if not sold:
            break
    if not done:
        if rec_hint and "추천일" in str(last):
            ok2, res2 = sell_across(cache, member, stock, sell_date, sell_all, count, rec_hint=None)
            return ok2, f"(원문 날짜 {rec_hint}와 맞는 추천분이 없어 기본 규칙 적용)\n{res2}"
        return False, last
    head = f"총 {len(done)}건 매도\n" if len(done) > 1 else ""
    return True, head + "\n".join(done)


# ── 기동 시 1회 작업(OPS) ─────────────────────────────────────────────────
# 작업 세션 PAT에 workflow_dispatch·workflow 권한이 없을 때 시트에 쓰는 유일한 통로.
# 리스너 파일을 push하면 배포 워크플로가 VM에서 재기동시키고, 기동 직후 아래 목록을
# 한 번씩 실행한다. 실행한 id는 VM 로컬 파일에 남겨 재기동해도 반복하지 않는다.
#   op=sell : sell_stock()과 동일 규칙(가장 위 매칭 1건 → 실현, _pending_sell 등록 →
#             다음 데일리가 매도일 종가로 확정). dry=True면 판정 결과만 운영자에게 보낸다.
# 처리 후엔 목록에서 지워도 된다(남겨둬도 id 때문에 재실행 안 됨).
STARTUP_OPS = [
    {"id": "20261111-daily-test-alerts", "op": "dispatch", "workflow": "daily_report.yml",
     "inputs": {"test_mode": "true"},
     "why": "아침 알림(자동매도 예고·급등락) 추가 후 데일리 정상 동작 확인(개인 텔레그램)"},
    # 처리 완료: 20261006-kdh-djt, 20261012-lwh-kodex10y, 20261012-ktw-hanwhasol, 20261015-abr-icraft-rest,
    #           20261019-daily-test, 20261019-daily-group, 20261019-lkh-daejoo,
    #           20261030-abr-amotech-restore, 20261030-abr-hynix-sell
    # 처리 완료: 20261103-daily-test, 20261103-daily-test2
    # 처리 완료: 20261103-daily-group, 20261103-daily-krx-test
    # 8/14~11/3 네이버 통합(NXT 포함) 종가로 기록된 실현 행을 KRX 정규장 종가(다음 일봉)로 정정.
    # 행: [연, 분기, 행, 종목, 추천일, 매도일, 기존S, 기존T, 새S, 새T]
    # 처리 완료: 20261106-krx-refix (실현 40건 KRX 종가 정정)
    {"id": "20261110-log-resell", "op": "dispatch", "workflow": "listener_log.yml",
     "inputs": {"pattern": "OPS", "lines": "15", "scan": "3000"},
     "why": "스튜디오드래곤 정정 결과 확인"},
    {"id": "20261110-ktw-studiodragon-resell", "op": "resell",
     "person": "김태완", "stock": "스튜디오드래곤", "wrong_rec": "2026-10-19", "right_rec": "2026-10-26",
     "quarter": (26, 4),
     "why": "'스튜디오드래곤 (10/26일자)' 매도를 10/19분으로 처리한 것 정정"},
    # 처리 완료: 20261109-log-gold
    {"id": "20261109-log-gold", "op": "dispatch", "workflow": "listener_log.yml",
     "inputs": {"pattern": "신한|금 선물|금선물|ETN|오류|미매핑|가동|재시도", "lines": "120", "scan": "20000"},
     "why": "11/9 '신한 금 선물 ETN' 추천 미수집 원인 조사"},
    # 처리 완료: 20261102-kdh-wedding (김동환 결혼 보너스)
    {"id": "20261102-kdh-wedding", "op": "bonus",
     "person": "김동환", "label": "결혼 보너스", "date": "2026-11-01", "pct": 10, "quarter": (26, 4),
     "why": "김동환 결혼 기념 +10% (운영자 지시, 11/1자)"},
]
OPS_DONE_FILE = BASE_DIR / "_ops_done.txt"


def run_startup_ops(cache):
    try:
        done = set(OPS_DONE_FILE.read_text().split()) if OPS_DONE_FILE.exists() else set()
    except Exception:
        done = set()
    for op in STARTUP_OPS:
        oid = op.get("id")
        if not oid or oid in done:
            continue
        try:
            if op.get("op") == "dispatch":
                # VM 크론이 쓰는 dispatch PAT로 워크플로를 실행한다(작업 세션 PAT엔 권한 없음)
                pat = Path("/home/won3ho/.hantang_dispatch_pat").read_text().strip()
                r = requests.post(
                    f"https://api.github.com/repos/won3ho-max/go/actions/workflows/{op['workflow']}/dispatches",
                    headers={"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"},
                    json={"ref": "main", "inputs": op.get("inputs", {})}, timeout=20)
                if r.status_code != 204:
                    raise RuntimeError(f"dispatch {r.status_code} {r.text[:200]}")
                msg = f"▶️ [OPS 워크플로 실행] {oid}: {op['workflow']} {op.get('inputs', {})}"
                notify_admin(msg)
                log(msg)
                done.add(oid)
                OPS_DONE_FILE.write_text("\n".join(sorted(done)))
                continue
            if op.get("op") == "resell":
                # 잘못 판 추천분(wrong_rec)을 활성으로 되돌리고, 같은 매도일·같은 실현 행에
                # 맞는 추천분(right_rec)을 판다. 가드: 잘못 판 실현 행 정확히 1건, 맞는 추천분 활성 1건↑.
                ws = cache.tab(tuple(op["quarter"]))
                vals = cache.values(ws, fresh=True)
                person = op["person"]; tb = _split_stock_label(op["stock"])[1]
                blk = next(b for b in find_person_blocks(vals) if person in b["person"])
                rows = [(r, (vals[r - 1] + [""] * 21)[:21]) for r in range(blk["row_start"], blk["row_end"] + 1)
                        if r - 1 < len(vals)]
                wrong = [(r, row) for r, row in rows
                         if row[15] and _split_stock_label(row[15])[1] == tb and op["wrong_rec"] in row[16]]
                right = [r for r, row in rows
                         if row[9] and _split_stock_label(row[9])[1] == tb and op["right_rec"] in row[10]]
                empty_j = next((r for r, row in rows if not row[9].strip()), None)
                if len(wrong) != 1 or not right or empty_j is None:
                    raise RuntimeError(f"가드 불통과 — 잘못 판 행 {len(wrong)}건, 맞는 보유 {len(right)}건, 빈 활성행 {empty_j}")
                pr, row = wrong[0]
                sd = row[17][:10]
                sheet_retry(lambda: ws.batch_update([
                    {"range": f"J{empty_j}:N{empty_j}", "values": [[row[15], row[16], row[18], row[19], f"=(M{empty_j}-L{empty_j})/L{empty_j}"]]},
                    {"range": f"P{pr}:U{pr}", "values": [["", "", "", "", "", ""]]},
                ], value_input_option="USER_ENTERED"), "매도 취소")
                vals = cache.refresh(ws)
                ok, res = sell_stock(ws, vals, person, op["stock"], datetime.date.fromisoformat(sd), cache,
                                     rec_hint=op["right_rec"], p_row_hint=pr)
                cache.refresh(ws)
                msg = (f"{'✅' if ok else '❌'} [OPS 매도 정정] {oid}\n[{ws.title}] {person} {row[15]} "
                       f"{op['wrong_rec']}분 매도 취소(활성 J{empty_j}) → {op['right_rec']}분 {sd} 매도\n{res}")
                notify_admin(msg)
                log(msg.replace("\n", " | "))
                if not ok:
                    raise RuntimeError(res)
                done.add(oid)
                OPS_DONE_FILE.write_text("\n".join(sorted(done)))
                continue
            if op.get("op") == "fix_realized":
                # 실현 행 S(매수 기준가)·T(매도가) 정정. 행마다 가드: 종목·추천일·매도일과
                # 기존 S·T가 시트와 정확히 같을 때만 쓴다. 다르면 그 행만 건너뛰고 보고.
                okn, skip = 0, []
                by_tab = {}
                for yy, q, r, name, rec, sell, oS, oT, nS, nT in op["rows"]:
                    by_tab.setdefault((yy, q), []).append((r, name, rec, sell, oS, oT, nS, nT))
                for key, items in by_tab.items():
                    ws = cache.tab(key)
                    vals = cache.values(ws, fresh=True)
                    ups = []
                    for r, name, rec, sell, oS, oT, nS, nT in items:
                        row = (vals[r - 1] + [""] * 21)[:21] if r - 1 < len(vals) else [""] * 21
                        try:
                            cS = float(str(row[18]).replace(",", "")); cT = float(str(row[19]).replace(",", ""))
                        except Exception:
                            cS = cT = None
                        if row[15].strip() != name or rec not in row[16] or sell not in row[17] \
                                or cS is None or abs(cS - oS) > 0.5 or abs(cT - oT) > 0.5:
                            skip.append(f"{ws.title} P{r} {name}")
                            continue
                        ups.append({"range": f"S{r}:T{r}", "values": [[nS, nT]]})
                        okn += 1
                    if ups:
                        sheet_retry(lambda: ws.batch_update(ups, value_input_option="USER_ENTERED"), "실현 정정")
                        cache.refresh(ws)
                msg = (f"🛠 [OPS 실현 정정] {oid}\n{okn}건 정정" +
                       (f", 건너뜀 {len(skip)}건: " + ", ".join(skip) if skip else ""))
                notify_admin(msg)
                log(msg.replace("\n", " | "))
                done.add(oid)
                OPS_DONE_FILE.write_text("\n".join(sorted(done)))
                continue
            if op.get("op") == "bonus":
                # 실현 칸에 보너스·조정 행 추가(S=100, T=100+pct). 가드: 같은 라벨·같은 날짜가
                # 이미 있으면 쓰지 않는다. 칸이 없으면 블록 마지막 행 앞에 한 줄 끼워 넣는다.
                ws = cache.tab(tuple(op["quarter"]))
                person, label, d, pct = op["person"], op["label"], op["date"], float(op["pct"])
                for attempt in (0, 1):
                    vals = cache.values(ws, fresh=True)
                    blk = next(b for b in find_person_blocks(vals) if person in b["person"])
                    rows = [(r, (vals[r - 1] + [""] * 21)[:21] if r - 1 < len(vals) else [""] * 21)
                            for r in range(blk["row_start"], blk["row_end"] + 1)]
                    if any(row[15].strip() == label and d in row[16] for _, row in rows):
                        raise RuntimeError(f"이미 기록됨: {person} {label} {d}, 쓰기 안 함")
                    pr = next((r for r, row in rows if not row[15].strip()), None)
                    if pr is not None:
                        break
                    at = blk["row_end"]
                    sheet_retry(lambda: ws.insert_rows([[""]], row=at, inherit_from_before=True), "행 추가")
                if pr is None:
                    raise RuntimeError("실현 칸 확보 실패")
                sheet_retry(lambda: ws.batch_update([
                    {"range": f"P{pr}:U{pr}", "values": [[label, d, d, 100, 100 + pct, f"=(T{pr}-S{pr})/S{pr}"]]},
                ], value_input_option="USER_ENTERED"), "보너스 기록")
                cache.refresh(ws)
                msg = f"🎁 [OPS 보너스] {oid}\n[{ws.title}] {person} {label} {d} {pct:+.0f}% (실현 P{pr})"
                notify_admin(msg)
                log(msg.replace("\n", " | "))
                done.add(oid)
                OPS_DONE_FILE.write_text("\n".join(sorted(done)))
                continue
            if op.get("op") == "restore":
                # 잘못 매도된 실현 행을 활성으로 되돌린다. 가드: 실현 칸에서 종목·추천일·매도일이
                # 모두 맞는 행이 정확히 1건이고, 같은 블록에 빈 활성 행이 있을 때만.
                ws = cache.tab(tuple(op["quarter"]))
                vals = cache.values(ws, fresh=True)
                person = op["person"]
                tb = _split_stock_label(op["stock"])[1]
                blk = next(b for b in find_person_blocks(vals) if person in b["person"])
                hits, empty_j = [], None
                for r in range(blk["row_start"], blk["row_end"] + 1):
                    row = (vals[r - 1] + [""] * 21)[:21] if r - 1 < len(vals) else [""] * 21
                    if row[15] and _split_stock_label(row[15])[1] == tb \
                            and op["rec_date"] in row[16] and op["sell_date"] in row[17]:
                        hits.append((r, row))
                    if empty_j is None and not row[9].strip():
                        empty_j = r
                if len(hits) != 1 or empty_j is None:
                    raise RuntimeError(f"가드 불통과 — 실현 매칭 {len(hits)}건, 빈 활성행 {empty_j}. 쓰기 안 함")
                pr, row = hits[0]
                jr = empty_j
                sheet_retry(lambda: ws.batch_update([
                    {"range": f"J{jr}:N{jr}", "values": [[row[15], row[16], row[18], row[19], f"=(M{jr}-L{jr})/L{jr}"]]},
                    {"range": f"P{pr}:U{pr}", "values": [["", "", "", "", "", ""]]},
                ], value_input_option="USER_ENTERED"), "매도 취소")
                cache.refresh(ws)
                msg = (f"↩️ [OPS 매도 취소] {oid}\n[{ws.title}] {person} {row[15]} (추천일 {row[16]}) "
                       f"실현 P{pr} → 활성 J{jr} 복원. 현재가는 다음 데일리가 갱신")
                notify_admin(msg)
                log(msg.replace("\n", " | "))
                done.add(oid)
                OPS_DONE_FILE.write_text("\n".join(sorted(done)))
                continue
            if op.get("op") == "sell_rest":
                # 가드: 직전+현재 분기에 남은 매칭이 정확히 expect_count건일 때만 전부 판다
                person, stock = op["person"], op["stock"]
                cnt, where = 0, []
                for tws, tv in (cache.prev(), cache.ensure()):
                    if tws is None:
                        continue
                    tv = cache.values(tws, fresh=True)
                    hits = [h for h in active_holdings(tv, person) if _split_stock_label(h)[1] == _split_stock_label(stock)[1]]
                    rows = sum(1 for b in find_person_blocks(tv) if person in b["person"]
                               for r in range(b["row_start"], b["row_end"] + 1)
                               if r - 1 < len(tv) and len(tv[r - 1]) > 9 and tv[r - 1][9]
                               and _split_stock_label(tv[r - 1][9])[1] == _split_stock_label(stock)[1])
                    cnt += rows
                    if rows:
                        where.append(f"{tws.title} {rows}건")
                if cnt != op.get("expect_count", 1):
                    raise RuntimeError(f"가드 불통과 — 남은 보유 {cnt}건({', '.join(where) or '없음'}), "
                                       f"기대 {op.get('expect_count', 1)}건. 쓰기 안 함")
                ok, res = sell_across(cache, person, stock,
                                      datetime.date.fromisoformat(op["sell_date"]), sell_all=True)
                if not ok:
                    raise RuntimeError(res)
                msg = f"✅ [OPS 매도] {oid}\n{res}"
                notify_admin(msg)
                log(msg.replace("\n", " | "))
                done.add(oid)
                OPS_DONE_FILE.write_text("\n".join(sorted(done)))
                continue
            ws = cache.tab(tuple(op["quarter"]))
            if ws is None:
                raise RuntimeError(f"{op['quarter']} 탭 없음")
            vals = cache.values(ws, fresh=True)
            person, stock = op["person"], op["stock"]
            if op.get("op") == "buy":
                # 가드: 그 분기 탭에 같은 종목이 이미 활성이면 쓰지 않는다(재실행 대비)
                d = datetime.date.fromisoformat(op["rec_date"])
                dup = find_active_dup(vals, person, stock)
                if dup and (not op.get("allow_dup") or dup_on_date(vals, person, stock, d)):
                    raise RuntimeError(f"이미 기록됨: {dup}, 쓰기 안 함")
                ok, res = add_stock(ws, vals, person, stock, d)
                cache.refresh(ws)
                if not ok:
                    raise RuntimeError(res)
                msg = f"✅ [OPS 매수] {oid}\n[{ws.title}] {res} (추천일 {d})"
                notify_admin(msg)
                log(msg.replace("\n", " | "))
                done.add(oid)
                OPS_DONE_FILE.write_text("\n".join(sorted(done)))
                continue
            sd = datetime.date.fromisoformat(op["sell_date"])
            held = active_holdings(vals, person)
            t_code, t_base = _split_stock_label(stock)
            hits = []
            for b in find_person_blocks(vals):
                if b["person"] == person or person in b["person"]:
                    for r in range(b["row_start"], b["row_end"] + 1):
                        row = vals[r - 1] if r - 1 < len(vals) else []
                        j = row[9] if len(row) > 9 else ""
                        if not j:
                            continue
                        j_code, j_base = _split_stock_label(j)
                        if (t_code and t_code == j_code) or (
                                len(t_base) >= 2 and len(j_base) >= 2
                                and (t_base in j_base or j_base in t_base)):
                            hits.append((r, row[9:14]))
            desc = "; ".join(f"J{r} {v}" for r, v in hits) or "없음"
            if op.get("dry", True):
                msg = (f"🧪 [OPS 드라이런] {oid}\n탭: {ws.title}\n{person} 보유: {', '.join(held) or '없음'}\n"
                       f"매칭: {desc}\n매도일 {sd} (쓰기 없음)")
            else:
                # 드라이런 대체 가드: 매칭이 정확히 1건이고 기대 라벨을 포함해야만 쓴다
                exp = op.get("expect", "")
                if len(hits) != 1 or (exp and exp not in str(hits[0][1][0])):
                    raise RuntimeError(f"가드 불통과 — 매칭 {len(hits)}건: {desc} (기대 '{exp}'), 쓰기 안 함")
                ok, res = sell_stock(ws, vals, person, stock, sd, cache)
                cache.refresh(ws)
                msg = f"{'✅' if ok else '❌'} [OPS 매도] {oid}\n[{ws.title}] {res}"
                if not ok:
                    raise RuntimeError(res)
            notify_admin(msg)
            log(msg.replace("\n", " | "))
            done.add(oid)
            OPS_DONE_FILE.write_text("\n".join(sorted(done)))
        except Exception as e:
            notify_admin(f"⚠️ [OPS 실패] {oid}: {e}")
            log(f"[OPS 실패] {oid}: {e}")


# ── 주기 점검(총무 알림) ──────────────────────────────────────────────────
# 메인 루프가 long-poll 한 번(≤50초)마다 부른다. 각 항목은 하루 한 번만 보내고,
# 보낸 키는 VM 로컬 파일에 남겨 재기동해도 반복하지 않는다. 받는 사람은 운영자뿐(본채널 아님).
PERIODIC_FILE = BASE_DIR / "_periodic_done.txt"
ROUND_REMIND_AT = ("14:00", "21:00")   # 라운드일 미추천자 현황 → 단톡방(빠진 사람이 있을 때만)
DAILY_WATCH_AT = "08:00"               # 07:00 데일리가 돌았는지 확인
DAILY_WATCH_GIVEUP = "10:00"           # 이때까지 진행 중이면 그대로 알린다
_periodic_cache = {"keys": None}


def _periodic_done(key):
    if _periodic_cache["keys"] is None:
        try:
            _periodic_cache["keys"] = set(PERIODIC_FILE.read_text().split())
        except Exception:
            _periodic_cache["keys"] = set()
    return key in _periodic_cache["keys"]


def _periodic_mark(key):
    keys = _periodic_cache["keys"] or set()
    keys.add(key)
    # 2주 넘은 키는 버린다(키에 날짜가 들어 있다)
    cut = str(today_kst() - datetime.timedelta(days=14))
    def _fresh(k):
        m = re.search(r"\d{4}-\d{2}-\d{2}", k)
        return not m or m.group(0) >= cut
    _periodic_cache["keys"] = {k for k in keys if _fresh(k)}
    try:
        PERIODIC_FILE.write_text("\n".join(sorted(_periodic_cache["keys"])))
    except Exception as e:
        log(f"[경고] 주기 점검 기록 실패: {e}")


def now_kst():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def listener_round_day(d: datetime.date) -> datetime.date:
    """그 주(월~일) 첫 KRX 거래일 — update_gsheets.round_day와 같은 정의."""
    monday = d - datetime.timedelta(days=d.weekday())
    for i in range(7):
        x = monday + datetime.timedelta(days=i)
        if is_krx_session(x):
            return x
    return monday


def _is_adjust_label(s: str) -> bool:
    return any(w in str(s) for w in _ADJUST_WORDS)


def unresolved_rows(cache):
    """_unresolved_buy 탭의 [(rec_date, member, reason, status)] (없으면 [])."""
    try:
        wu = cache.get_ss().worksheet(UNRESOLVED_TAB)
        rows = sheet_retry(wu.get_all_values, "미인식 탭 읽기")
    except Exception:
        return []
    out = []
    for r in rows[1:]:
        r = (r + [""] * 6)[:6]
        if r[0] and r[1]:
            out.append((r[0].strip()[:10], r[1].strip(), r[2].strip(), r[5].strip()))
    return out


def round_status(values, day: datetime.date):
    """그날 추천 여부: {멤버: [종목,...]}(추천 안 한 멤버는 빈 목록)."""
    ds = str(day)
    out = {}
    for b in find_person_blocks(values):
        person = b["person"]
        if not person:
            continue
        got = out.setdefault(person, [])
        for r in range(b["row_start"], b["row_end"] + 1):
            if r - 1 >= len(values):
                break
            row = (values[r - 1] + [""] * 21)[:21]
            if row[9].strip() and ds in row[10]:
                got.append(row[9].strip())
            if row[15].strip() and ds in row[16] and not _is_adjust_label(row[15]):
                got.append(row[15].strip())
    return out


def round_reminder(cache, now):
    """라운드일 14시·21시: 미추천자·인식 실패 보류자 현황을 운영자에게 보낸다.
    총무가 마감 전에 단톡방에서 독촉할 수 있게. (패널티는 다음 날 아침 데일리가 매긴다)"""
    today = now.date()
    if not is_krx_session(today) or listener_round_day(today) != today:
        return
    hm = now.strftime("%H:%M")
    for i, at in enumerate(ROUND_REMIND_AT):
        key = f"round-{today}-{at}"
        if hm < at or _periodic_done(key):
            continue
        # 늦게 기동해 두 시각을 다 넘겼으면 마지막 것만 보낸다
        if i + 1 < len(ROUND_REMIND_AT) and hm >= ROUND_REMIND_AT[i + 1]:
            _periodic_mark(key)
            continue
        ws, values = cache.ensure(today)
        if ws is None:
            return
        st = round_status(cache.refresh(ws), today)
        # 인식 실패 건이라도 그 뒤 수동 기록됐으면(추천 완료) 보류 목록에서 뺀다
        held = sorted({m for d, m, _r, s in unresolved_rows(cache)
                       if d == str(today) and s not in ("패널티", "해제") and not st.get(m)})
        done = [f"{m}({', '.join(v)})" for m, v in st.items() if v]
        miss = [m for m, v in st.items() if not v and m not in held]
        _periodic_mark(key)
        if at != ROUND_REMIND_AT[0] and not miss and not held:
            log(f"[라운드 현황] {today} {at} 전원 추천 — 알림 생략")
            continue
        # 단톡방: 현재 기준 미추천자만 짧게. 인식 실패 보류자는 글을 올린 사람이라 넣지 않는다.
        group_txt = ""
        if miss:
            group_txt = (f"📋 이번주 종목 추천 현황 ({today.strftime('%m/%d')} {at} 기준)\n"
                         f"미추천: {', '.join(miss)}\n"
                         f"※ 오늘 안에 #종목추천 올려주세요 (미추천 시 -10%)")
            gid = group_chat_id()
            if gid:
                tg_send(gid, group_txt)
            else:
                group_txt = ("(단톡방 chat_id를 아직 몰라 못 보냄 — 멤버 메시지가 한 번 오면 자동 등록)\n"
                             + group_txt)
        # 운영자: 단톡방에 보낸 내용 + 인식 실패 보류자
        lines = [f"📋 이번주 종목 추천 현황 — {today.strftime('%m/%d')} {at} [{ws.title}] 추천 완료 {len(done)}/{len(st)}"]
        lines.append(("단톡방 발송:\n" + group_txt) if group_txt else "✅ 미추천자 없음 — 단톡방 발송 안 함")
        if held:
            lines.append(f"⚠️ 추천 글은 있는데 인식 실패(패널티 보류): {', '.join(held)} → 종목 수동 확정 필요")
        notify_admin("\n".join(lines))
        log(f"[라운드 현황] {today} {at} 미추천={miss} 보류={held}")


def daily_watch(now):
    """08시: 오늘 07시 데일리(daily_report)가 성공했는지 GitHub에서 확인하고,
    없거나 실패했으면 운영자에게 알린다. 예전엔 크론·dispatch가 실패해도
    단톡방에 카드가 안 올라온 걸 보고서야 알았다(§12-7)."""
    today = now.date()
    key = f"dailywatch-{today}"
    if today.weekday() >= 5 or now.strftime("%H:%M") < DAILY_WATCH_AT or _periodic_done(key):
        return
    try:
        pat = Path("/home/won3ho/.hantang_dispatch_pat").read_text().strip()
    except Exception:
        pat = ""
    since = (datetime.datetime.combine(today, datetime.time()) - datetime.timedelta(hours=9))
    try:
        r = requests.get(
            "https://api.github.com/repos/won3ho-max/go/actions/workflows/daily_report.yml/runs",
            headers={"Accept": "application/vnd.github+json",
                     **({"Authorization": f"token {pat}"} if pat else {})},
            params={"created": f">={since.strftime('%Y-%m-%dT%H:%M:%SZ')}", "per_page": 20},
            timeout=20)
        runs = r.json().get("workflow_runs", []) if r.ok else None
    except Exception as e:
        runs, r = None, None
        log(f"[데일리 감시] 조회 오류: {e}")
    if runs is None:
        return   # 조회 실패 — 다음 루프에 다시 본다
    ok = [x for x in runs if x.get("conclusion") == "success"]
    running = [x for x in runs if x.get("status") != "completed"]
    if ok:
        _periodic_mark(key)
        log(f"[데일리 감시] {today} 성공 run {len(ok)}건 확인")
        return
    if running and now.strftime("%H:%M") < DAILY_WATCH_GIVEUP:
        return   # 아직 도는 중 — 조금 뒤 다시
    _periodic_mark(key)
    if not runs:
        why = "오늘 실행된 run이 없습니다(VM 크론 또는 dispatch 실패 의심)"
    elif running:
        why = f"{DAILY_WATCH_GIVEUP}까지 끝나지 않았습니다"
    else:
        why = "오늘 run이 모두 실패했습니다: " + ", ".join(
            f"{x.get('conclusion')}" for x in runs[:5])
    notify_admin(f"🚨 데일리 감시 — {today} 07시 카드뉴스 확인 필요\n{why}\n"
                 f"→ 작업 세션에서 STARTUP_OPS dispatch(daily_report)로 재실행")
    log(f"[데일리 감시] 경보: {why}")


HOLIDAY_NOTICE_AT = "17:00"   # 휴장 전날(주말 끼면 금요일) 단톡방 안내
_WD = "월화수목금토일"


def _md(d: datetime.date) -> str:
    return f"{d.month}/{d.day}({_WD[d.weekday()]})"


def holiday_notice_text(today: datetime.date) -> str:
    """오늘 저녁 단톡방에 보낼 휴장 안내. 보낼 게 없으면 ''.
    다음 평일(N)이 국내·미국 휴장이면 사유와 함께 알리고, 다음 주로 넘어가는 날이면
    다음주 추천일(그 주 첫 국내 거래일)이 월요일이 아닐 때 함께 알린다."""
    if today.weekday() >= 5:
        return ""
    n = today + datetime.timedelta(days=1)
    while n.weekday() >= 5:
        n += datetime.timedelta(days=1)
    next_week = n.isocalendar()[1] != today.isocalendar()[1]
    when = (f"다음주 {_WD[n.weekday()]}요일({n.month}/{n.day})" if next_week
            else f"내일 {_md(n)}")
    lines = []

    # 국내: N부터 이어지는 평일 휴장 묶음(연휴)
    if not is_krx_session(n):
        run, d = [], n
        while len(run) < 10:
            if d.weekday() < 5:
                if is_krx_session(d):
                    break
                run.append(d)
            d += datetime.timedelta(days=1)
        names = []
        for x in run:
            nm = KRX_HOLIDAY_NAMES.get(str(x), "휴장")
            if nm not in names:
                names.append(nm)
        span = when if len(run) == 1 else f"{when}부터 {_md(run[-1])}까지"
        lines.append(f"🇰🇷 {span} 국내 증시 휴장 — {', '.join(names)}")
    # 미국: N 하루만(미국 연휴는 하루씩)
    if str(n) in US_HOLIDAYS:
        lines.append(f"🇺🇸 {when} 미국 증시 휴장 — {US_HOLIDAYS[str(n)]} (한국시간 그날 밤 장 없음)")
    has_holiday = bool(lines)
    # 휴장 아닌 장 일정(수능 개장 지연, 조기 폐장 등)
    for flag, txt in MARKET_EVENTS.get(str(n), []):
        lines.append(f"{flag} {when} {txt}")
    # 다음주 추천일이 월요일이 아니면
    if next_week:
        rd = listener_round_day(n)
        monday = n - datetime.timedelta(days=n.weekday())
        if rd != monday:
            lines.append(f"📌 다음주 추천일은 {_WD[rd.weekday()]}요일({rd.month}/{rd.day})입니다")
    if not lines:
        return ""
    only_holiday = has_holiday and not MARKET_EVENTS.get(str(n))
    return ("📅 휴장 안내\n" if only_holiday else "📅 증시 일정 안내\n") + "\n".join(lines)


def holiday_notice(now):
    today = now.date()
    key = f"holiday-{today}"
    if now.strftime("%H:%M") < HOLIDAY_NOTICE_AT or _periodic_done(key):
        return
    _periodic_mark(key)
    txt = holiday_notice_text(today)
    if not txt:
        return
    gid = group_chat_id()
    if gid:
        tg_send(gid, txt)
        notify_admin("단톡방 발송:\n" + txt)
    else:
        notify_admin("(단톡방 chat_id를 몰라 못 보냄)\n" + txt)
    log(f"[휴장 안내] {today}: {txt.replace(chr(10), ' / ')}")


def periodic_checks(cache):
    if COLLECT_MODE:
        return
    now = now_kst()
    for fn in (lambda: round_reminder(cache, now), lambda: daily_watch(now),
               lambda: holiday_notice(now)):
        try:
            fn()
        except Exception as e:
            log(f"[주기 점검 오류] {e}")


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
    if not COLLECT_MODE:
        # 분기 첫 라운드 전에 탭을 미리 만들어 둔다(휴장일 밤에 올린 추천도 바로 기록되게).
        try:
            w0, _ = cache.ensure(next_krx_session(today_kst()))
            log(f"기록 대상 탭: {w0.title if w0 else '없음'}")
        except Exception as e:
            log(f"[경고] 분기 탭 준비 실패: {e}")
        run_startup_ops(cache)
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
                    # 마지막 안전망: 개별 시트 호출을 다 감싸도 새로 추가된 경로가
                    # 빠질 수 있다. 일시 장애(5xx)면 캐시를 버리고 통째로 한 번 더.
                    if any(c in str(e) for c in ("[500]", "[502]", "[503]", "[504]",
                                                 "Timeout", "timed out",
                                                 "Connection", "Unavailable")):
                        log(f"[재시도] handle_message 통째 재실행: {e}")
                        time.sleep(5)
                        cache = SheetCache()
                        try:
                            handle_message(msg, cache)
                            continue
                        except Exception as e2:
                            e = e2
                    # 실패한 메시지의 내용을 반드시 남긴다. 예전엔 오류만 찍혀
                    # 무엇이 유실됐는지 알 수 없었다(2026-08-18 11:07 503 건).
                    frm = msg.get("from") or {}
                    who = MEMBER_MAP.get(frm.get("id")) or \
                        f"미매핑(id={frm.get('id')}, {frm.get('first_name', '')})"
                    body = (msg.get("text", "") or msg.get("caption", "") or
                            "").strip().replace("\n", " ")[:200]
                    log(f"[오류] handle_message: {e} | {who} | {body}")
                    held = ""
                    raw = msg.get("text", "") or msg.get("caption", "") or ""
                    if detect_tag(raw) == "buy" and MEMBER_MAP.get(frm.get("id")):
                        rd = next_krx_session(today_kst())
                        if unresolved_add(cache, rd, MEMBER_MAP[frm.get("id")], "처리 오류", body):
                            held = f"\n※ {rd} 미추천 패널티 보류 등록"
                    notify_admin(f"⚠️ 메시지 처리 오류 — {who}\n원문: {body}\n"
                                 f"오류: {e}\n※ 이 건은 기록되지 않았습니다. 수동 확인 필요" + held)
            if updates:
                save_offset(offset)
            periodic_checks(cache)
        except requests.exceptions.RequestException as e:
            log(f"[네트워크] {e}")
            time.sleep(min(backoff, 30)); backoff = min(backoff * 2, 30)
        except Exception as e:
            log(f"[루프오류] {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
