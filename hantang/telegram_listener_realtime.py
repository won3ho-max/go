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

    cell_j = ws.cell(empty_row, 10)
    cell_k = ws.cell(empty_row, 11)
    cell_j.value = stock_name
    cell_k.value = str(rec_date)
    ws.update_cells([cell_j, cell_k], value_input_option="USER_ENTERED")
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


def sell_stock(ws, all_values, person_name, stock_name, sell_date, cache):
    """가장 과거(위쪽) 매칭 활성 종목을 실현 섹션으로 이동. manual_sell과 동일 규칙."""
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
        if t_code and j_code and t_code == j_code:
            stock_row = r
            break
        if (len(t_base) >= 2 and len(j_base) >= 2
                and (t_base == j_base or t_base in j_base or j_base in t_base)):
            stock_row = r
            break
    if stock_row is None:
        return False, f"'{stock_name}' 활성 종목을 찾을 수 없음"

    row = all_values[stock_row - 1]
    orig     = row[9]  if len(row) > 9  else ""
    rec_date = row[10] if len(row) > 10 else ""
    base     = row[11] if len(row) > 11 else ""
    cur      = row[12] if len(row) > 12 else ""

    p_row = None
    for r in range(block["row_start"], block["row_end"] + 1):
        ri = r - 1
        if ri >= len(all_values):
            break
        pv = all_values[ri][15] if len(all_values[ri]) > 15 else ""
        if not pv or not pv.strip():
            p_row = r
            break
    if p_row is None:
        return False, "실현 섹션에 빈 행 없음"

    try:
        base_f = float(str(base).replace(",", ""))
    except Exception:
        return False, f"기준가 파싱 실패: {base!r}"
    try:
        price = float(str(cur).replace(",", ""))
    except Exception:
        price = base_f

    ws.batch_update([
        {"range": f"P{p_row}", "values": [[orig]]},
        {"range": f"Q{p_row}", "values": [[rec_date]]},
        {"range": f"R{p_row}", "values": [[str(sell_date)]]},
        {"range": f"S{p_row}", "values": [[base_f]]},
        {"range": f"T{p_row}", "values": [[price]]},
        {"range": f"U{p_row}", "values": [[f"=(T{p_row}-S{p_row})/S{p_row}"]]},
        {"range": f"J{stock_row}:N{stock_row}", "values": [["", "", "", "", ""]]},
    ], value_input_option="USER_ENTERED")

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


def _norm(s: str) -> str:
    return re.sub(r"[\s\(\)\[\]{}·\-_/\.]", "", s or "").upper()


def _split_stock_label(label: str):
    """'코히런트(COHR)' → ('COHR', '코히런트'). 괄호 티커가 없으면 코드는 ''."""
    s = str(label or "").strip()
    m = re.search(r"\(([A-Za-z0-9]{1,7})\)\s*$", s)
    code = m.group(1).upper() if m else ""
    base = _norm(re.sub(r"\([^)]*\)\s*$", "", s))
    return code, base


# 원문에 거래소와 함께 명시된 티커 (예: NYSE: COHR, NASDAQ:NVDA, 나스닥 TSLA)
_EXCH_TICKER_RE = re.compile(
    r"(?:NYSE\s*American|NYSE|NASDAQ|AMEX|ARCA|CBOE|나스닥|뉴욕증권거래소|뉴욕거래소)"
    r"\s*[:：]?\s*([A-Z]{1,5})\b")
# 괄호 안 단독 티커 (예: (COHR))
_PAREN_TICKER_RE = re.compile(r"\(\s*([A-Z]{2,5})\s*\)")


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
                    if not is_kr and strict_foreign:
                        # 해외는 완전일치만. 단 ADR은 국내 상장분으로 대체될 수 있어 통과
                        # (대체 실패 시 호출부에서 채택하지 않음).
                        if not _is_adr(nm):
                            continue
                    elif not (a in b or b in a):
                        continue
                    # 후보가 종목명의 절반도 설명하지 못하면 우연한 부분일치로 본다.
                    #   'SOL' → 'SOL AI반도체TOP2플러스' 같은 오탐 차단.
                    # ADR은 예외 — 뒤이어 국내 상장분 대체 검증을 통과해야만 채택되므로
                    # 오탐 위험이 낮고, 이 가드가 'SKT'→SK텔레콤 경로를 막고 있었다.
                    if len(a) * 2 < len(b) and not _is_adr(nm):
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


def _heuristic_names(text: str):
    """앞 5줄에서 종목명 후보 토큰열 생성(기존 폴백 로직)."""
    out = []
    t = (text or "").replace("#", " ")
    for line in t.split("\n")[:5]:
        line = re.sub(r"[·:;,/|_\-\*\"\'`!?()\[\]]", " ", line)
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


def resolve_stock(text: str, holdings=None):
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
        for hd in holdings:
            _, base = _split_stock_label(hd)
            if len(base) >= 2 and base in tn:
                cands.append(("name", re.sub(r"\([^)]*\)\s*$", "", hd).strip(), "보유 종목 대조"))

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

    paren = sorted({m.group(1).upper() for m in _PAREN_TICKER_RE.finditer(text)})
    if len(paren) == 1 and not exch:
        cands.append(("code", paren[0], "원문 괄호 티커"))

    for nm in _heuristic_names(text):
        cands.append(("name", nm, "원문 토큰"))

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
    return None, "; ".join(notes[:4]) or "후보 없음"


def today_kst():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date()


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
    body = text.strip().replace("\n", " ")[:200]

    if tag == "buy":
        if not member:
            notify_admin(f"🟢 매수 감지(미매핑) — id={sid} {sender}\n원문: {body}\n"
                         f"※ 멤버 매핑 안 됨 → 자동기록 안 함")
            log(f"[매수-미매핑] id={sid}")
            return
        stock, why = resolve_stock(text)
        if not stock:
            notify_admin(f"🟢 매수 감지 — {member}\n원문: {body}\n"
                         f"※ 종목명 자동인식 실패 → 자동기록 안 함(수동 확정 필요)\n"
                         f"사유: {why}")
            log(f"[매수-미인식] {member}: {why}")
            return
        ws, values = cache.ensure()
        dup = find_active_dup(values, member, stock)
        if dup:
            notify_admin(f"⚠️ 매수 감지 — {member} / {stock}\n원문: {body}\n"
                         f"※ 이미 활성 보유 중: {dup}\n"
                         f"→ 중복 의심으로 자동기록 보류(정당한 재추천이면 "
                         f"batch_add allow_dup=true로 수동 추가)\n근거: {why}")
            log(f"[매수-중복보류] {member}/{stock}: {dup}")
            return
        ok, result = add_stock(ws, values, member, stock, today_kst())
        if ok:
            cache.refresh()
        icon = "✅" if ok else "❌"
        notify_admin(f"{icon} 매수 자동기록 — {member} / {stock}\n원문: {body}\n{result}\n근거: {why}")
        log(f"[매수-{'기록' if ok else '실패'}] {member}/{stock}: {result}")
    else:  # sell
        if not member:
            notify_admin(f"🔴 매도 감지(미매핑) — id={sid} {sender}\n원문: {body}\n"
                         f"※ 멤버 매핑 안 됨 → 자동처리 안 함")
            log(f"[매도-미매핑] id={sid}")
            return
        # 매도는 보유 종목 중에서 고르는 일이므로 후보를 보유분으로 좁힌다.
        ws, values = cache.ensure()
        held = active_holdings(values, member)
        stock, why = resolve_stock(text, holdings=held)
        if not stock:
            notify_admin(f"🔴 매도 감지 — {member}\n원문: {body}\n"
                         f"※ 종목명 자동인식 실패 → 자동처리 안 함(수동 확정 필요)\n"
                         f"사유: {why}\n"
                         f"보유 중: {', '.join(held) if held else '없음'}")
            log(f"[매도-미인식] {member}: {why}")
            return
        ok, result = sell_stock(ws, values, member, stock, today_kst(), cache)
        if ok:
            cache.refresh()
        icon = "✅" if ok else "❌"
        notify_admin(f"{icon} 매도 자동처리 — {member} / {stock}\n원문: {body}\n{result}\n근거: {why}")
        log(f"[매도-{'처리' if ok else '실패'}] {member}/{stock}: {result}")


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
