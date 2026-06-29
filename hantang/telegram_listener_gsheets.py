"""[일회성 진단] 봇 수신 상태 점검 — 읽기전용. 시트/발송 없음."""
import os, json, requests
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
def call(m, **p):
    try:
        return requests.get(f"https://api.telegram.org/bot{TOKEN}/{m}", params=p, timeout=20).json()
    except Exception as e:
        return {"ok": False, "err": str(e)}
print("=== getMe ===");          print(json.dumps(call("getMe"), ensure_ascii=False))
print("=== getWebhookInfo ==="); print(json.dumps(call("getWebhookInfo"), ensure_ascii=False))
r = call("getUpdates", timeout=0, limit=100)
print("=== getUpdates(ok) ==="); print(r.get("ok"), "/ count:", len(r.get("result", [])))
for u in r.get("result", []):
    m = u.get("message") or u.get("channel_post") or {}
    ch = m.get("chat", {})
    print(f"upd={u.get('update_id')} chat_id={ch.get('id')} type={ch.get('type')} "
          f"title={ch.get('title','')} from={(m.get('from') or {}).get('username','')} "
          f"text={(m.get('text','') or '')[:60]!r}")
print("=== keys present in updates ===")
print(sorted({k for u in r.get('result', []) for k in u.keys() if k!='update_id'}))
