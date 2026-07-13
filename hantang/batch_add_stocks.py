"""[일회성] 7/13 오염된 종목명 4건 교정 (K=2026-07-13 행의 J열)."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
print("대상:",ws.title)
vals=ws.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
def block(name):
    for h in hdr:
        s=next((r for r in sog if r>h),None)
        if not s: continue
        nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
        if nm==name or name in nm: return h+1,s-1
    return None
FIX={"안병열":"코셈","송지호":"엘티씨","이원호":"KODEX WTI원유선물(H)","조형오":"SOL 조선TOP3플러스레버리지"}
for name,correct in FIX.items():
    b=block(name)
    if not b: print(f"  ❌ {name} 블록없음"); continue
    row=next((r for r in range(b[0],b[1]+1) if r-1<len(vals) and "2026-07-13" in str(vals[r-1][10] if len(vals[r-1])>10 else "")),None)
    if not row: print(f"  ❌ {name} 7/13행 없음"); continue
    before=(vals[row-1][9] if len(vals[row-1])>9 else "")[:30]
    ws.update_cells([gspread.Cell(row,10,correct)],value_input_option="USER_ENTERED")
    print(f"  ✅ {name} R{row}: '{before}...' → '{correct}'")
print("완료")
