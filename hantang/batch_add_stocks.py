"""[일회성 조회] _collect 시트 덤프 — 읽기전용."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
try:
    wc=ss.worksheet("_collect")
except gspread.WorksheetNotFound:
    print("_collect 시트 없음"); raise SystemExit(0)
rows=wc.get_all_values()
print(f"총 {len(rows)}행 (헤더 포함)")
# 중복 제거: user_id별 최신 1건
seen={}
for r in rows[1:]:
    if len(r)<4: continue
    uid=r[1].strip()
    if not uid.isdigit(): continue
    seen[uid]={"username":r[2],"name":r[3],"sample":r[4] if len(r)>4 else "","ts":r[0]}
print(f"고유 글쓴이 {len(seen)}명:")
for uid,d in seen.items():
    print(f"  id={uid} | name={d['name']!r} | @{d['username']} | 예:{d['sample']!r}")
