import os
import sys
import json
import logging
import fcntl
from datetime import time, datetime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
from collector import fetch_new_articles, format_article

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
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL_MINUTES', '30'))


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


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"✅ <b>봇 정상 작동 중</b>\n"
        f"⏱ 확인 주기: {CHECK_INTERVAL}분마다\n"
        f"📢 뉴스 발송 채널: <code>{BROADCAST_CHAT_ID}</code>\n"
        f"🛠 관리자 채팅: <code>{ADMIN_CHAT_ID}</code>",
        parse_mode='HTML'
    )


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 뉴스 확인 중...")
    articles = fetch_new_articles()
    if not articles:
        await update.message.reply_text("새로운 뉴스가 없습니다.")
        return
    for article in articles[:5]:
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
        if not articles:
            return
        # 수면시간(22:00~06:00 KST): 큐에 저장
        if hour >= 22 or hour < 6:
            pending = load_pending()
            pending.extend(articles)
            save_pending(pending)
            return
        for article in articles[:10]:
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
