"""[일회성] 안병열 7/20 마이크론 → KODEX SK하이닉스단일종목레버리지 교정.
APPLY 모드에서만 실제 수정. 먼저 대상 행을 확인 출력."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=[w for w in ss.worksheets() if not w.title.startswith("_")][-1]
APPLY = os.environ.get("STOCKS","")=="APPLY"
vals=ws.get_all_values()
hdr=[i+1 for i,r in enumerate(vals) if (r[9] if len(r)>9 else '')=='종목명']
sog=[i+1 for i,r in enumerate(vals) if '실현수익률 소계' in str(r[15] if len(r)>15 else '')]
print("대상:",ws.title,"/ 모드:", "적용" if APPLY else "드라이런")
target=None
for h in hdr:
    s=next((r for r in sog if r>h),None)
    if not s: continue
    nm=(vals[h][8] if len(vals[h])>8 else '').replace('\n','')
    if nm!="안병열": continue
    print("안병열 활성:")
    for r in range(h+1,s):
        row=vals[r-1]; j=row[9] if len(row)>9 else ''; k=row[10] if len(row)>10 else ''
        if j:
            print(f"  R{r}: {j} ({k})")
            if "2026-07-20" in str(k) and "마이크론" in j:
                target=r
if not target:
    print("❌ 7/20 마이크론 행 못 찾음 (이미 고쳐졌거나 다름)")
else:
    print(f"\n교정 대상: R{target} '마이크론 테크놀로지' → 'KODEX SK하이닉스단일종목레버리지'")
    if APPLY:
        ws.update_cells([gspread.Cell(target,10,"KODEX SK하이닉스단일종목레버리지")],
                        value_input_option="USER_ENTERED")
        print("✅ 교정 완료 (기준가·현재가는 데일리가 재계산)")
    else:
        print("(드라이런 — 실제 수정 안 함)")
