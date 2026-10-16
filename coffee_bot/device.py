"""
ADB로 같은 폰의 화면을 읽고 누르는 모듈
────────────────────────────────────────
Termux 안에서 `adb connect localhost:<포트>`로 폰 자기 자신에 붙어 동작한다.
화면 구조는 `uiautomator dump`로 받은 XML에서 읽고, 탭은 `input tap`으로 한다.
"""

import base64
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

ADB_SERIAL = os.environ.get("ADB_SERIAL", "").strip()
DUMP_PATH = "/sdcard/coffee_bot_window.xml"
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


class DeviceError(RuntimeError):
    pass


@dataclass
class Node:
    text: str
    desc: str
    res_id: str
    clickable: bool
    bounds: tuple  # (x1, y1, x2, y2)

    @property
    def center(self):
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def label(self):
        return self.text or self.desc or self.res_id


def _adb(*args, timeout=20, binary=False):
    cmd = ["adb"]
    if ADB_SERIAL:
        cmd += ["-s", ADB_SERIAL]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise DeviceError("adb가 설치돼 있지 않습니다 (pkg install android-tools)")
    except subprocess.TimeoutExpired:
        raise DeviceError(f"adb 응답 없음: {' '.join(args)}")
    if r.returncode != 0:
        err = r.stderr.decode(errors="ignore").strip()
        raise DeviceError(f"adb 실패 ({' '.join(args)}): {err}")
    return r.stdout if binary else r.stdout.decode(errors="ignore")


def shell(cmd, timeout=20):
    return _adb("shell", cmd, timeout=timeout)


# ── 상태 확인 ────────────────────────────────────────────────────────────
def check_connection():
    out = _adb("devices")
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    online = [l for l in lines if l.endswith("\tdevice")]
    if not online:
        raise DeviceError("연결된 기기가 없습니다. `adb connect localhost:<포트>`를 먼저 실행하세요")
    if len(online) > 1 and not ADB_SERIAL:
        raise DeviceError("기기가 여러 개입니다. .env에 ADB_SERIAL을 지정하세요")
    return online[0].split("\t")[0] if not ADB_SERIAL else ADB_SERIAL


def wake():
    shell("input keyevent KEYCODE_WAKEUP")
    time.sleep(0.5)


def is_locked():
    out = shell("dumpsys window")
    return any(k in out for k in (
        "mShowingLockscreen=true",
        "isKeyguardShowing=true",
        "mDreamingLockscreen=true",
    ))


def is_installed(package):
    return f"package:{package}" in shell(f"pm list packages {package}")


def foreground_package():
    out = shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    m = re.search(r"([a-zA-Z0-9_.]+)/", out)
    return m.group(1) if m else ""


# ── 앱 조작 ──────────────────────────────────────────────────────────────
def launch(package):
    shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")


def force_stop(package):
    shell(f"am force-stop {package}")


def back():
    shell("input keyevent KEYCODE_BACK")


def key(code):
    shell(f"input keyevent {code}")


def tap(x, y):
    shell(f"input tap {x} {y}")


def screen_size():
    out = shell("wm size")
    m = re.findall(r"(\d+)x(\d+)", out)
    if not m:
        return 1080, 2340
    w, h = m[-1]  # Override size가 있으면 마지막 값이 실제 값
    return int(w), int(h)


def swipe_up(ratio=0.45):
    w, h = screen_size()
    x = w // 2
    y1 = int(h * 0.75)
    y2 = int(h * (0.75 - ratio))
    shell(f"input swipe {x} {y1} {x} {y2} 350")


def type_text(text):
    """영문·숫자는 input text로, 한글이 섞이면 ADB Keyboard로 입력한다."""
    if text.isascii():
        escaped = text.replace(" ", "%s").replace("'", "\\'")
        shell(f"input text '{escaped}'")
        return

    if not is_installed("com.android.adbkeyboard"):
        raise DeviceError("한글 입력에는 ADB Keyboard 앱이 필요합니다 (README 참고)")

    prev = shell("settings get secure default_input_method").strip()
    shell("ime enable " + ADB_KEYBOARD_IME)
    shell("ime set " + ADB_KEYBOARD_IME)
    time.sleep(0.5)
    try:
        b64 = base64.b64encode(text.encode()).decode()
        shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}")
        time.sleep(0.5)
    finally:
        if prev and prev != "null" and prev != ADB_KEYBOARD_IME:
            shell(f"ime set {prev}")


def screenshot():
    """PNG 바이트. 결제 화면처럼 캡처를 막는 화면은 검게 나올 수 있다."""
    return _adb("exec-out", "screencap", "-p", binary=True, timeout=30)


# ── 화면 읽기 ────────────────────────────────────────────────────────────
def dump_nodes(retries=3):
    last = ""
    for _ in range(retries):
        out = shell(f"uiautomator dump {DUMP_PATH}", timeout=30)
        if "dumped to" in out:
            xml = shell(f"cat {DUMP_PATH}")
            return parse_nodes(xml)
        last = out.strip()
        time.sleep(0.7)  # 애니메이션 중이면 idle 대기 실패가 난다
    raise DeviceError(f"화면 구조를 읽지 못했습니다: {last}")


def parse_nodes(xml):
    xml = xml[xml.find("<"):]  # 앞에 붙는 잡음 제거
    root = ET.fromstring(xml)
    nodes = []
    for el in root.iter("node"):
        m = _BOUNDS_RE.match(el.get("bounds", ""))
        if not m:
            continue
        b = tuple(int(v) for v in m.groups())
        if b[2] <= b[0] or b[3] <= b[1]:
            continue  # 화면 밖·크기 0
        nodes.append(Node(
            text=(el.get("text") or "").strip(),
            desc=(el.get("content-desc") or "").strip(),
            res_id=el.get("resource-id") or "",
            clickable=el.get("clickable") == "true",
            bounds=b,
        ))
    return nodes


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def match(node, target, by="any", contains=False, regex=False):
    t = _norm(target)
    if regex:
        fields = {"text": [node.text], "desc": [node.desc]}.get(by, [node.text, node.desc])
        return any(f and re.search(target, f) for f in fields)
    if by == "id":
        fields = [node.res_id]
        return any(f == target or f.endswith(":id/" + target) for f in fields)
    fields = {"text": [node.text], "desc": [node.desc]}.get(by, [node.text, node.desc])
    for f in fields:
        f = _norm(f)
        if not f:
            continue
        if f == t or (contains and t in f):
            return True
    return False


def find(nodes, target, by="any", contains=False, index=0, regex=False):
    hits = [n for n in nodes if match(n, target, by, contains, regex)]
    return hits[index] if len(hits) > index else None
