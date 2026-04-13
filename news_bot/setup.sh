#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== 농협 뉴스 봇 설치 ==="

# venv 생성 (없으면)
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "가상환경 생성 완료"
fi

# 패키지 설치
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q
echo "패키지 설치 완료"

# .env 파일 생성 (없으면)
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  .env 파일이 생성되었습니다. 아래 값을 채워주세요:"
    echo "   nano /home/ubuntu/news_bot/.env"
    echo ""
fi

# systemd 서비스 등록
sudo cp news_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable news_bot
echo "systemd 서비스 등록 완료"

echo ""
echo "=== 설치 완료 ==="
echo ""
echo "다음 단계:"
echo "1. .env 파일에 토큰/채팅ID 입력: nano /home/ubuntu/news_bot/.env"
echo "2. 봇 시작: sudo systemctl start news_bot"
echo "3. 상태 확인: sudo systemctl status news_bot"
echo "4. 로그 확인: tail -f /home/ubuntu/news_bot/bot.log"
