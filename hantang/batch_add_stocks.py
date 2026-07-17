"""[일회성 진단] 불일치 7건: 매도일 전후 종가 스캔 — 읽기전용."""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_gsheets as U
import yfinance as yf

CASES=[("조형오","SGC에너지","2026-06-03",50100.0),
       ("김동환","카르만 홀딩스(KRMN)","2026-05-05",65.32),
       ("송지호","아이씨티케이","2026-06-03",28900.0),
       ("이광훈","나노신소재","2026-06-03",62500.0),
       ("김동환","SK텔레콤","2026-05-12",103400.0),
       ("김태완","POSCO홀딩스","2026-06-03",399000.0),
       ("조형오","롯데쇼핑","2026-05-26",158500.0),
       ("조형오","동성화인텍","2026-05-12",None)]
for person,name,sd,rec in CASES:
    d=datetime.date.fromisoformat(sd)
    market,code=U.parse_stock(name)
    if not market or not code:
        print(f"{person}/{name}: 코드 미인식"); continue
    tick = code + (".KQ" if code in U.KOSDAQ_CODES else ".KS") if market=="KR" else code
    try:
        h=yf.Ticker(tick).history(start=str(d-datetime.timedelta(days=6)),
                                  end=str(d+datetime.timedelta(days=6)), prepost=False)
    except Exception as e:
        print(f"{person}/{name}: 조회오류 {e}"); continue
    print(f"\n[{person} / {name}] 기록매도가={rec} 매도일={sd} ({tick})")
    for ts,row in h.iterrows():
        day=ts.date()
        mark=" ←매도일" if day==d else ""
        hit=""
        if rec is not None and abs(float(row['Close'])-rec)/max(rec,1) < 0.005: hit="  ★기록값과 일치"
        print(f"   {day} 종가 {float(row['Close']):,.2f}{mark}{hit}")
