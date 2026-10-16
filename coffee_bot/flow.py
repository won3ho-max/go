"""
config.json의 steps를 순서대로 실행하는 모듈
──────────────────────────────────────────────
스타벅스 앱 화면은 업데이트마다 바뀌므로 버튼 이름을 코드에 박지 않고
config.json에 적어 둔다. 앱이 바뀌면 /dump로 화면을 보고 설정만 고치면 된다.

지원 action
  launch      앱 실행 (restart: true면 강제 종료 후 실행)
  tap         글자/설명/ID로 요소를 찾아 누름 (scroll: true면 내려가며 찾음)
  tap_xy      좌표를 직접 누름 (x, y는 0~1 비율 또는 픽셀)
  type        글자 입력
  wait        특정 요소가 나타날 때까지 대기
  sleep       초 단위 대기
  back / key  뒤로가기, 키 입력
  checkpoint  결제 직전 멈춤 → 텔레그램으로 확인 요청

공통 옵션
  if / unless 변수(store, menu)가 있을 때만 / 없을 때만 실행
  optional    못 찾아도 다음 단계로 진행
  timeout     요소를 기다릴 최대 초 (기본 10)
  contains    부분 일치 / regex  정규식 일치 / index  여러 개면 몇 번째(0부터)
"""

import threading
import time

import device


class FlowAborted(Exception):
    pass


class StepFailed(Exception):
    def __init__(self, step_no, step, reason):
        self.step_no, self.step, self.reason = step_no, step, reason
        super().__init__(f"{step_no}단계 실패 ({describe(step)}): {reason}")


def describe(step):
    a = step.get("action")
    target = step.get("text") or step.get("desc") or step.get("id") or ""
    return f"{a} {target}".strip()


def _fill(value, vars_):
    if isinstance(value, str):
        for k, v in vars_.items():
            value = value.replace("{" + k + "}", v or "")
    return value


def _target(step, vars_):
    for by in ("text", "desc", "id"):
        if step.get(by):
            return by, _fill(step[by], vars_)
    return "any", ""


def _wait_for(step, vars_, abort, scroll=False):
    by, target = _target(step, vars_)
    contains = step.get("contains", False)
    index = step.get("index", 0)
    timeout = step.get("timeout", 10)
    max_scrolls = step.get("max_scrolls", 8)
    deadline = time.time() + timeout
    scrolls = 0
    while True:
        if abort.is_set():
            raise FlowAborted()
        node = device.find(device.dump_nodes(), target, by, contains, index,
                           regex=step.get("regex", False))
        if node:
            return node
        if scroll and scrolls < max_scrolls:
            device.swipe_up()
            scrolls += 1
            time.sleep(0.8)
            continue
        if time.time() > deadline:
            return None
        time.sleep(0.8)


def _xy(v, full):
    return int(v * full) if isinstance(v, float) and v <= 1 else int(v)


def run(steps, vars_, package, abort: threading.Event, on_checkpoint, log=print):
    """
    on_checkpoint(step) -> bool: True면 계속(결제), False면 중단.
    """
    for i, step in enumerate(steps, 1):
        if abort.is_set():
            raise FlowAborted()
        if step.get("if") and not vars_.get(step["if"]):
            continue
        if step.get("unless") and vars_.get(step["unless"]):
            continue

        a = step.get("action")
        log(f"[{i}] {describe(step)}")

        if a == "launch":
            if step.get("restart"):
                device.force_stop(package)
                time.sleep(0.5)
            device.launch(package)

        elif a in ("tap", "wait"):
            node = _wait_for(step, vars_, abort, scroll=step.get("scroll", False))
            if not node:
                if step.get("optional"):
                    log(f"    ↳ 없음, 건너뜀")
                    continue
                raise StepFailed(i, step, f"'{_target(step, vars_)[1]}'을(를) 화면에서 찾지 못함")
            if a == "tap":
                x, y = node.center
                dx, dy = step.get("offset", [0, 0])
                device.tap(x + dx, y + dy)

        elif a == "tap_xy":
            w, h = device.screen_size()
            device.tap(_xy(step["x"], w), _xy(step["y"], h))

        elif a == "type":
            device.type_text(_fill(step["text"], vars_))

        elif a == "sleep":
            time.sleep(step.get("sec", 1))

        elif a == "back":
            device.back()

        elif a == "key":
            device.key(step["code"])

        elif a == "checkpoint":
            if not on_checkpoint(step):
                raise FlowAborted()

        else:
            raise StepFailed(i, step, f"알 수 없는 action: {a}")

        time.sleep(step.get("after", 0.8))
