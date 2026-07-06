"""[일회성] 7/6 매수 7건 + 어정윤 미추천 패널티(-10%)."""
import os, json, datetime
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
print("대상 시트:", ws.title)
REC="2026-07-06"

def blocks(vals):
    hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
    sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
    out=[]
    for h in hdr:
        s=next((r for r in sog if r>h),None)
        if not s: continue
        nm=(vals[h][8] if len(vals[h])>8 else '').strip().replace('\n','')
        out.append({"person":nm,"start":h+1,"end":s-1})
    return out

def find(vals,name):
    return next((b for b in blocks(vals) if b["person"]==name or name in b["person"]),None)

BUYS=[("조형오","코오롱티슈진"),("송지호","삼성물산"),("김태완","삼성전자"),
      ("김동환","하나금융지주"),("이원호","SK스퀘어"),("이광훈","GS피앤엘"),("안병열","코셈")]

vals=ws.get_all_values()
for person,stock in BUYS:
    b=find(vals,person)
    if not b: print(f"  ❌ {person} 블록없음"); continue
    row=next((r for r in range(b["start"],b["end"]+1) if r-1<len(vals) and not (vals[r-1][9] if len(vals[r-1])>9 else '')),None)
    if not row: print(f"  ❌ {person} 빈행없음"); continue
    ws.update_cells([gspread.Cell(row,10,stock),gspread.Cell(row,11,REC)],value_input_option="USER_ENTERED")
    print(f"  ✅ 매수 {person} / {stock} → R{row}")
    vals=ws.get_all_values()

# 어정윤 미추천 패널티 (실현 섹션)
b=find(vals,"어정윤")
# 중복 체크: 같은 날짜 미추천 이미 있으면 스킵
dup=any((vals[r-1][15] if len(vals[r-1])>15 else '') and "미추천" in vals[r-1][15]
        and REC in (vals[r-1][16] if len(vals[r-1])>16 else '')
        for r in range(b["start"],b["end"]+1))
if dup:
    print("  ⚠️ 어정윤 미추천 패널티 이미 존재 — 스킵")
else:
    prow=next((r for r in range(b["start"],b["end"]+1) if r-1<len(vals) and not (vals[r-1][15] if len(vals[r-1])>15 else '')),None)
    if not prow: print("  ❌ 어정윤 실현 빈행없음")
    else:
        ws.update_cells([
            gspread.Cell(prow,16,"미추천(패널티)"),
            gspread.Cell(prow,17,REC),
            gspread.Cell(prow,18,REC),
            gspread.Cell(prow,19,"100"),
            gspread.Cell(prow,20,"90"),
            gspread.Cell(prow,21,f"=(T{prow}-S{prow})/S{prow}"),
        ], value_input_option="USER_ENTERED")
        print(f"  ✅ 어정윤 미추천 패널티(-10%) → 실현 R{prow}")
print("완료")
