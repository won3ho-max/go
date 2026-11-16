import os
import sys
import json
import logging
import fcntl
import asyncio
from datetime import time, datetime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
from collector import fetch_new_articles, format_article, LLM_STATE, NAVER_ENABLED, NAVER_STATE, AI_REJECTED

KST = ZoneInfo('Asia/Seoul')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(BASE_DIR, '.bot.lock')


def _acquire_lock():
    """단일 인스턴스 보장 — 이미 봇이 실행 중이면 즉시 종료"""
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd  # 프로세스 종료 시 자동 해제
    except BlockingIOError:
        print(f"[BLOCKED] 봇이 이미 실행 중입니다. 중복 실행을 차단합니다. (lock: {LOCK_FILE})")
        sys.exit(1)
PENDING_FILE = os.path.join(BASE_DIR, 'pending_articles.json')


def load_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_pending(articles):
    with open(PENDING_FILE, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False)


load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
# ADMIN_CHAT_ID — 본인 1:1 채팅 (하트비트·에러 알림 등 운영자만 받는 정보)
# .env의 TELEGRAM_CHAT_ID(=1633958343, deploy.yml이 매번 덮어씀)에서 읽음
ADMIN_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
# BROADCAST_CHAT_ID — 공개 채널 (다른 사람도 볼 수 있도록 뉴스를 발송하는 대상)
# 채널 ID는 deploy.yml로 이전하려면 PAT workflow scope 필요하므로 우선 코드에 하드코딩
BROADCAST_CHAT_ID = '-1003717850867'  # 금융 뉴스 모니터링(MTN)
# 하위 호환용 별칭 — 기존 코드의 CHAT_ID 참조를 채널로 통합
CHAT_ID = BROADCAST_CHAT_ID
# 폴링 주기 (분). 2026-05-23부터 30→15 단축.
# .env의 CHECK_INTERVAL_MINUTES 값에 관계없이 코드 기본값 우선 적용 (서버 .env 직접 수정 불필요)
CHECK_INTERVAL = 15


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏦 <b>금융권 뉴스 모니터링 봇</b>\n\n"
        f"⏱ 확인 주기: {CHECK_INTERVAL}분마다\n\n"
        "명령어:\n"
        "/status - 봇 상태 확인\n"
        "/news - 지금 바로 뉴스 확인\n"
        "/start - 도움말",
        parse_mode='HTML'
    )


def _llm_status_text() -> str:
    if LLM_STATE['ok'] is None:
        return '아직 호출 전'
    return '정상' if LLM_STATE['ok'] else f"고장 — {LLM_STATE['error']}"


def _naver_status_text() -> str:
    if not NAVER_ENABLED:
        return '꺼짐 (키 없음)'
    if NAVER_STATE['ok'] is None:
        return '켜짐 (아직 호출 전)'
    return '정상' if NAVER_STATE['ok'] else f"실패 — {NAVER_STATE['error']}"


_reported = {'llm': None, 'naver': False, 'naver_ok': None}


async def report_filter_state(context):
    """AI 판정·네이버 수집 상태를 관리자에게 알린다.

    둘 다 고장 나도 봇은 계속 돌기 때문에, 알리지 않으면 아무도 모른 채 몇 달이 간다.
    AI 판정은 상태가 바뀔 때마다, 네이버 수집 꺼짐은 봇이 켜질 때 한 번 알린다.
    """
    notes = []
    if not NAVER_ENABLED and not _reported['naver']:
        _reported['naver'] = True
        notes.append("ℹ️ 네이버 수집이 꺼져 있습니다 (NAVER_CLIENT_ID·SECRET 없음). 구글·언론사 피드만 읽습니다.")
    naver_ok = NAVER_STATE['ok']
    if naver_ok is not None and naver_ok != _reported['naver_ok']:
        _reported['naver_ok'] = naver_ok
        notes.append("✅ 네이버 수집이 정상 작동합니다." if naver_ok else
                     "⚠️ 네이버 수집이 실패하고 있습니다. 구글·언론사 피드만 읽습니다.\n" + NAVER_STATE['error'])
    ok = LLM_STATE['ok']
    if ok is not None and ok != _reported['llm']:
        _reported['llm'] = ok
        notes.append("✅ AI 판정이 정상 작동합니다." if ok else
                     "⚠️ AI 판정이 작동하지 않습니다. 복구될 때까지 [단독]·허용어 기사만 보냅니다.\n" + LLM_STATE['error'])
    for text in notes:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        except Exception as e:
            logger.error(f"상태 알림 전송 오류: {e}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"✅ <b>봇 정상 작동 중</b>\n"
        f"⏱ 확인 주기: {CHECK_INTERVAL}분마다\n"
        f"📢 뉴스 발송 채널: <code>{BROADCAST_CHAT_ID}</code>\n"
        f"🛠 관리자 채팅: <code>{ADMIN_CHAT_ID}</code>\n"
        f"🤖 AI 판정: {_llm_status_text()}\n"
        f"🔎 네이버 수집: {_naver_status_text()}",
        parse_mode='HTML'
    )


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 뉴스 확인 중...")
    articles = fetch_new_articles()
    if not articles:
        await update.message.reply_text("새로운 뉴스가 없습니다.")
        return
    for i, article in enumerate(articles[:30]):
        if i > 0:
            await asyncio.sleep(1)
        await update.message.reply_text(
            format_article(article),
            parse_mode='HTML',
            disable_web_page_preview=True
        )


async def heartbeat(context):
    """하트비트는 운영자 1:1 채팅으로만 — 채널 노이즈 방지"""
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="✅ 정상 작동 중입니다",
        )
    except Exception as e:
        logger.error(f"하트비트 전송 오류: {e}")
    await send_rejected_digest(context)


async def send_rejected_digest(context):
    """AI가 거른 기사 제목을 관리자에게 보여준다. 잘못 거른 기사가 있으면 판정 기준을 고칠 근거가 된다."""
    if not AI_REJECTED:
        return
    titles = AI_REJECTED[:]
    AI_REJECTED.clear()
    lines = [f"🤖 AI가 거른 기사 {len(titles)}건 (잘못 거른 것이 있으면 알려주세요)"] + [f"· {t[:70]}" for t in titles]
    chunk = ''
    try:
        for line in lines:
            if len(chunk) + len(line) > 3500:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=chunk)
                chunk = ''
            chunk += line + '\n'
        if chunk:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=chunk)
    except Exception as e:
        logger.error(f"AI 차단 목록 전송 오류: {e}")


async def send_pending(context):
    """06:00 KST — 수면시간 동안 쌓인 기사 일괄 발송 (공개 채널)"""
    pending = load_pending()
    if not pending:
        return
    save_pending([])
    try:
        await context.bot.send_message(
            chat_id=BROADCAST_CHAT_ID,
            text=f"🌅 <b>수면시간 동안 수집된 뉴스 {len(pending)}건</b>",
            parse_mode='HTML',
        )
    except Exception as e:
        logger.error(f"pending 헤더 전송 오류: {e}")
    for article in pending:
        try:
            await context.bot.send_message(
                chat_id=BROADCAST_CHAT_ID,
                text=format_article(article),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"pending 기사 전송 오류: {e}")


async def scheduled_check(context):
    hour = datetime.now(KST).hour
    try:
        articles = fetch_new_articles()
        await report_filter_state(context)
        if not articles:
            return
        # 수면시간(22:00~06:00 KST): 큐에 저장
        if hour >= 22 or hour < 6:
            pending = load_pending()
            pending.extend(articles)
            save_pending(pending)
            return
        # 회당 최대 30건 발송 + 1초 sleep (텔레그램 rate-limit·도배 방지)
        for i, article in enumerate(articles[:30]):
            if i > 0:
                await asyncio.sleep(1)
            try:
                await context.bot.send_message(
                    chat_id=BROADCAST_CHAT_ID,
                    text=format_article(article),
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"메시지 전송 오류: {e}")
    except Exception as e:
        logger.error(f"뉴스 수집 오류: {e}")


def main():
    # 단일 인스턴스 잠금 — 구봇 부활 원천 차단
    _lock = _acquire_lock()

    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN 환경변수가 없습니다. .env 파일을 확인하세요.")
    if not ADMIN_CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID(ADMIN_CHAT_ID) 환경변수가 없습니다.")
    if not BROADCAST_CHAT_ID:
        raise ValueError("BROADCAST_CHAT_ID가 비어 있습니다. main.py 상단을 확인하세요.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("news", news_command))

    app.job_queue.run_repeating(
        scheduled_check,
        interval=CHECK_INTERVAL * 60,
        first=10
    )

    app.job_queue.run_daily(
        heartbeat,
        time=time(15, 0, 0, tzinfo=KST),
    )

    app.job_queue.run_daily(
        send_pending,
        time=time(6, 0, 0, tzinfo=KST),
    )

    logger.info(f"봇 시작 - {CHECK_INTERVAL}분마다 금융권 뉴스 모니터링")
    app.run_polling()


if __name__ == '__main__':
    main()
