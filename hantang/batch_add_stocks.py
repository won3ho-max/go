"""[일회성 진단2] Q2 시트 수식/구조 상세 덤프 — 읽기전용."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=ss.worksheet("한탕(26년 2분기)")
def col(n):
    s=""
    while n>0:
        n,r=divmod(n-1,26); s=chr(65+r)+s
    return s
# values + formulas
vals=ws.get_all_values()
form=ws.get_all_values(value_render_option='FORMULA')
print("=== 1~3행 (제목/헤더) values ===")
for r in range(1,4):
    print(f"R{r}:", [ (col(c+1), vals[r-1][c]) for c in range(len(vals[r-1])) if vals[r-1][c] ][:12])
print("\n=== 안병열 블록: 채워진행 R4, 빈행 R14 — J~U values/formula ===")
for r in [4,14,17,18]:
    print(f"--- R{r} ---")
    for c in range(10,22):  # J(10)~U(21)
        v=vals[r-1][c] if len(vals[r-1])>c else ""
        fo=form[r-1][c] if len(form[r-1])>c else ""
        if v or fo:
            print(f"   {col(c+1)}: val={v!r} formula={fo!r}")
print("\n=== 실현(P~U) 데이터 있는 행 스캔 (안병열 4~17) ===")
for r in range(4,18):
    p=vals[r-1][15] if len(vals[r-1])>15 else ""
    if p: print(f"   R{r} P={p!r}")
print("\n=== 시장지표/하단 패널 위치 추정: '소계' 아래 마지막 블록 끝(234) 이후 행 ===")
for r in range(234,246):
    rowv=vals[r-1] if r-1<len(vals) else []
    nz=[(col(c+1),rowv[c]) for c in range(len(rowv)) if rowv[c]]
    if nz: print(f"   R{r}:", nz[:10])
