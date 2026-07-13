"""[일회성 조회] 특정 추천일 기록 조회 — 읽기전용."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
TARGET=os.environ.get("REC_DATE","2026-07-13")
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
print("대상 시트:", ws.title, "/ 추천일:", TARGET)
vals=ws.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
found=0
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if not s: continue
    nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
    for r in range(h+1,s):
        j=vals[r-1][9] if len(vals[r-1])>9 else ''
        k=vals[r-1][10] if len(vals[r-1])>10 else ''
        if j and TARGET in str(k):
            print(f"  {nm}: {j}  (R{r})"); found+=1
print(f"총 {found}건")
