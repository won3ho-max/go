"""[일회성] 실수로 지운 어정윤 RAM(6/29) 복원. R112 J/K만 복구 → 데일리가 L/M/N 재계산."""
import os, json
import gspread
from google.oauth2.service_account import Credentials
SCOPES=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
info=json.loads(os.environ["GSHEETS_CREDENTIALS"])
gc=gspread.authorize(Credentials.from_service_account_info(info,scopes=SCOPES))
ss=gc.open_by_key(os.environ["GSHEETS_ID"])
ws=ss.worksheet("한탕(26년 3분기)")
NAME="Roundhill T-REX 2X Long DRAM Daily Target ETF(RAM)"
cur=ws.row_values(112)
j=cur[9] if len(cur)>9 else ''
print(f"복원 전 R112 J열: {j!r}")
if j.strip():
    print("이미 값이 있음 — 중단"); raise SystemExit(0)
ws.batch_update([
    {"range":"J112","values":[[NAME]]},
    {"range":"K112","values":[["2026-06-29"]]},
], value_input_option="USER_ENTERED")
print("✅ 복원: R112 =", NAME, "/ 2026-06-29 (L·M·N은 데일리가 재계산)")
# 검증
for r in (112,113,114,115):
    row=ws.row_values(r)
    jj=row[9] if len(row)>9 else ''
    kk=row[10] if len(row)>10 else ''
    print(f"  R{r}: {jj!r} ({kk})")
