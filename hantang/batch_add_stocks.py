"""[일회성] 김동환 7/13 업스타트 교정 + 7/13 미추천자 -10% 패널티."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
REC="2026-07-13"
print("대상:",ws.title)
vals=ws.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
def blk(name):
    for h in hdr:
        s=next((r for r in sog if r>h),None)
        if not s: continue
        nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
        if nm and (nm==name or name in nm): return nm,h+1,s-1
    return None

# 1) 김동환 7/13 교정
b=blk("김동환")
row=next((r for r in range(b[1],b[2]+1) if REC in str(vals[r-1][10] if len(vals[r-1])>10 else "")),None)
if row:
    before=(vals[row-1][9] if len(vals[row-1])>9 else "")[:25]
    ws.update_cells([gspread.Cell(row,10,"업스타트 홀딩스(UPST)")],value_input_option="USER_ENTERED")
    print(f"  ✅ 김동환 R{row}: '{before}...' → '업스타트 홀딩스(UPST)'")

# 2) 7/13 추천자 판별
MEMBERS=["안병열","김동환","이광훈","송지호","조형오","어정윤","이원호","김태완"]
recommenders=set()
for name in MEMBERS:
    bb=blk(name)
    if not bb: continue
    for r in range(bb[1],bb[2]+1):
        j=vals[r-1][9] if len(vals[r-1])>9 else ""
        k=vals[r-1][10] if len(vals[r-1])>10 else ""
        if j and REC in str(k): recommenders.add(name); break
missing=[m for m in MEMBERS if m not in recommenders]
print("  추천자:",sorted(recommenders))
print("  미추천(패널티 대상):",missing)

# 3) 미추천자 -10% 패널티 (실현 섹션)
for name in missing:
    bb=blk(name)
    if not bb: print(f"  ❌ {name} 블록없음"); continue
    # 중복 체크
    dup=any((vals[r-1][15] if len(vals[r-1])>15 else '') and "미추천" in vals[r-1][15]
            and REC in str(vals[r-1][16] if len(vals[r-1])>16 else '')
            for r in range(bb[1],bb[2]+1))
    if dup: print(f"  ⚠️ {name} 7/13 패널티 이미 존재 — 스킵"); continue
    prow=next((r for r in range(bb[1],bb[2]+1) if not (vals[r-1][15] if len(vals[r-1])>15 else '')),None)
    if not prow: print(f"  ❌ {name} 실현 빈행없음"); continue
    ws.update_cells([
        gspread.Cell(prow,16,"미추천(패널티)"),gspread.Cell(prow,17,REC),
        gspread.Cell(prow,18,REC),gspread.Cell(prow,19,"100"),
        gspread.Cell(prow,20,"90"),gspread.Cell(prow,21,f"=(T{prow}-S{prow})/S{prow}"),
    ],value_input_option="USER_ENTERED")
    print(f"  ✅ {name} 미추천 -10% → 실현 R{prow}")
print("완료")
