"""
텔레그램 "커피 시켜" → 스타벅스 앱 자동 주문 봇
──────────────────────────────────────────────
안드로이드폰의 Termux에서 실행한다. 봇이 ADB로 같은 폰의 스타벅스 앱을 대신 누른다.

사용법 (텔레그램)
  커피 시켜                 기본 메뉴를 가까운 매장에서
  커피 시켜 라떼            별칭 '라떼'를 가까운 매장에서
  커피 시켜 라떼 역삼점     지점 지정 ('점'으로 끝나거나 @로 시작하면 지점)
  /stop   진행 중인 주문 중단
  /dump   현재 화면의 글자 목록 + 스크린샷 (설정 맞출 때)
  /shot   스크린샷만
  /status 연결 상태 확인
  /menus  메뉴 별칭 목록
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent


def _load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(BASE_DIR / ".env")

import device  # noqa: E402  (.env의 ADB_SERIAL을 먼저 읽어야 함)
import flow    # noqa: E402

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API = f"https://api.telegram.org/bot{TOKEN}"
CONFIRM_TIMEOUT = 120

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("coffee_bot")


def load_config():
    path = BASE_DIR / "config.json"
    if not path.exists():
        path = BASE_DIR / "config.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ── 텔레그램 ─────────────────────────────────────────────────────────────
def tg(method, **kw):
    files = kw.pop("files", None)
    try:
        r = requests.post(f"{API}/{method}", data=kw, files=files, timeout=60)
        return r.json()
    except Exception as e:
        log.warning("텔레그램 %s 실패: %s", method, e)
        return {}


def send(text, buttons=None):
    kw = {"chat_id": CHAT_ID, "text": text}
    if buttons:
        kw["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})
    tg("sendMessage", **kw)


def send_photo(png, caption="", buttons=None):
    kw = {"chat_id": CHAT_ID, "caption": caption[:1000]}
    if buttons:
        kw["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})
    tg("sendPhoto", files={"photo": ("screen.png", png, "image/png")}, **kw)


def send_screen(caption, buttons=None):
    try:
        send_photo(device.screenshot(), caption, buttons)
    except Exception as e:
        send(f"{caption}\n(스크린샷 실패: {e})", buttons)


# ── 주문 상태 ────────────────────────────────────────────────────────────
class Order:
    def __init__(self):
        self.lock = threading.Lock()
        self.abort = threading.Event()
        self.decided = threading.Event()
        self.approved = False
        self.waiting_confirm = False

    def busy(self):
        return self.lock.locked()


order = Order()

TRIGGER_RE = re.compile(
    r"^\s*(?:/coffee(?:@\w+)?|커피\s*(?:시켜|주문)\s*(?:해줘|줘)?)(?:\s+(.*))?$")


def parse_request(args, cfg):
    menu, store = "", ""
    rest = []
    for tok in args.split():
        if tok.startswith("@"):
            store = tok[1:]
        elif tok.endswith("점") and len(tok) > 1:
            store = tok[:-1]
        else:
            rest.append(tok)
    key = " ".join(rest).strip() or cfg.get("default_menu", "")
    menu = cfg.get("menus", {}).get(key, key)
    store = store or cfg.get("default_store", "")
    return menu, store


def run_order(menu, store):
    cfg = load_config()
    pkg = cfg.get("package", "com.starbucks.co")
    ret_pkg = cfg.get("return_package", "")
    order.abort.clear()
    order.decided.clear()
    order.approved = False

    def on_checkpoint(step):
        if not cfg.get("confirm_before_pay", True):
            return True
        order.waiting_confirm = True
        send_screen(f"💳 결제 직전입니다\n메뉴: {menu}\n매장: {store or '가까운 매장'}\n"
                    f"{CONFIRM_TIMEOUT}초 안에 눌러 주세요.",
                    [{"text": "✅ 결제", "callback_data": "pay"},
                     {"text": "❌ 취소", "callback_data": "cancel"}])
        if ret_pkg:
            device.launch(ret_pkg)  # 버튼을 누를 수 있게 텔레그램을 앞으로
        ok = order.decided.wait(CONFIRM_TIMEOUT) and order.approved
        order.waiting_confirm = False
        if not ok:
            if not order.decided.is_set():
                send("⏰ 응답이 없어 주문을 취소했습니다. 결제는 되지 않았습니다.")
            return False
        if ret_pkg:
            device.launch(pkg)  # 떠나 있던 결제 화면으로 복귀
            time.sleep(2)
        return True

    try:
        device.check_connection()
        device.wake()
        if device.is_locked():
            send("🔒 폰 화면이 잠겨 있습니다. 잠금을 풀고 다시 시켜 주세요.")
            return
        if not device.is_installed(pkg):
            send(f"스타벅스 앱({pkg})을 찾지 못했습니다. config.json의 package를 확인하세요.")
            return

        send(f"☕ 주문 시작: {menu} / {store or '가까운 매장'}")
        flow.run(cfg["steps"], {"menu": menu, "store": store}, pkg,
                 order.abort, on_checkpoint, log=log.info)
        time.sleep(1)
        send_screen("✅ 결제 버튼까지 눌렀습니다. 앱에서 주문 완료 여부를 확인해 주세요.")
        if ret_pkg:
            device.launch(ret_pkg)

    except flow.FlowAborted:
        send("🛑 주문을 중단했습니다. 결제는 되지 않았습니다.")
    except flow.StepFailed as e:
        log.error("%s", e)
        send_screen(f"⚠️ {e}\n/dump로 화면 글자를 확인해 config.json을 고쳐 주세요.")
    except device.DeviceError as e:
        log.error("기기 오류: %s", e)
        send(f"⚠️ 기기 오류: {e}")
    except Exception as e:
        log.exception("예상 못한 오류")
        send(f"⚠️ 오류: {e}")


def start_order(args):
    if order.busy():
        send("이미 주문이 진행 중입니다. 중단하려면 /stop")
        return
    cfg = load_config()
    menu, store = parse_request(args, cfg)
    if not menu:
        send("메뉴를 알 수 없습니다. config.json의 default_menu를 정해 주세요.")
        return

    def worker():
        with order.lock:
            run_order(menu, store)

    threading.Thread(target=worker, daemon=True).start()


# ── 보조 명령 ────────────────────────────────────────────────────────────
def cmd_dump():
    try:
        nodes = device.dump_nodes()
    except Exception as e:
        send(f"화면 읽기 실패: {e}")
        return
    lines = []
    for n in nodes:
        label = n.text or n.desc
        if not label:
            continue
        mark = "👆" if n.clickable else "  "
        kind = "" if n.text else " (desc)"
        lines.append(f"{mark} {label}{kind}")
    body = "\n".join(lines) or "(글자가 있는 요소 없음)"
    for i in range(0, len(body), 3800):
        send(body[i:i + 3800])
    send_screen(f"현재 앱: {device.foreground_package()}")


def cmd_status():
    try:
        serial = device.check_connection()
        cfg = load_config()
        pkg = cfg.get("package")
        send(f"✅ 연결됨: {serial}\n"
             f"화면 잠김: {'예' if device.is_locked() else '아니오'}\n"
             f"스타벅스 앱({pkg}): {'설치됨' if device.is_installed(pkg) else '없음'}\n"
             f"ADB Keyboard: {'설치됨' if device.is_installed('com.android.adbkeyboard') else '없음 (한글 지점 검색 불가)'}\n"
             f"결제 전 확인: {'켬' if cfg.get('confirm_before_pay', True) else '끔'}")
    except Exception as e:
        send(f"⚠️ {e}")


def cmd_menus():
    cfg = load_config()
    lines = [f"• {k} → {v}" for k, v in cfg.get("menus", {}).items()]
    send("메뉴 별칭\n" + "\n".join(lines) + f"\n\n기본: {cfg.get('default_menu')}")


def handle_text(text):
    text = text.strip()
    m = TRIGGER_RE.match(text)
    if m:
        start_order(m.group(1) or "")
    elif text in ("/stop", "중단", "취소"):
        if order.waiting_confirm:
            order.approved = False
            order.decided.set()
        order.abort.set()
        send("중단 요청을 보냈습니다." if order.busy() else "진행 중인 주문이 없습니다.")
    elif text == "결제" and order.waiting_confirm:
        order.approved = True
        order.decided.set()
    elif text == "/dump":
        cmd_dump()
    elif text == "/shot":
        send_screen("현재 화면")
    elif text == "/status":
        cmd_status()
    elif text == "/menus":
        cmd_menus()
    elif text in ("/start", "/help"):
        send(__doc__.split("사용법 (텔레그램)")[1].strip())


def handle_callback(cb):
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    if not order.waiting_confirm:
        send("확인 대기 중인 주문이 없습니다.")
        return
    order.approved = cb.get("data") == "pay"
    order.decided.set()
    send("결제를 진행합니다." if order.approved else "취소했습니다. 결제는 되지 않았습니다.")


def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit(".env에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID를 넣어 주세요")
    log.info("커피 봇 시작")
    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 50, "offset": offset}, timeout=60).json()
        except Exception as e:
            log.warning("getUpdates 실패: %s", e)
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            cb = u.get("callback_query")
            chat = str((msg.get("chat") or (cb or {}).get("message", {}).get("chat") or {}).get("id", ""))
            if chat != str(CHAT_ID):
                continue  # 내 채팅이 아니면 무시
            try:
                if cb:
                    handle_callback(cb)
                elif msg.get("text"):
                    handle_text(msg["text"])
            except Exception:
                log.exception("업데이트 처리 실패")


if __name__ == "__main__":
    main()
