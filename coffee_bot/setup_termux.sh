#!/data/data/com.termux/files/usr/bin/bash
# Termux에서 한 번만 실행: bash setup_termux.sh
set -e
cd "$(dirname "$0")"

pkg update -y
pkg install -y python android-tools termux-api
pip install --quiet requests

[ -f .env ] || { cp .env.example .env; echo "→ .env를 만들었습니다. 토큰과 채팅 ID를 채워 주세요 (nano .env)"; }
[ -f config.json ] || { cp config.example.json config.json; echo "→ config.json을 만들었습니다"; }

echo
echo "다음 단계: README의 'ADB 연결' 절차대로 adb connect 후 bash start.sh"
