"""[일회성 검증] 3분기 활성 종목 현황 — 읽기전용."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
print("시트들:", [w.title for w in ss.worksheets()])
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
print("대상(현재 분기):", ws.title)
vals=ws.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
total=0
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if not s: continue
    nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
    rows=[(r,vals[r-1][9],vals[r-1][10] if len(vals[r-1])>10 else '') for r in range(h+1,s) if len(vals[r-1])>9 and vals[r-1][9]]
    if nm or rows:
        total+=len(rows)
        print(f"  {nm}: {[(rr[1],rr[2]) for rr in rows]}")
print("활성 종목 총:", total, "건")
