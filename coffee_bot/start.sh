#!/data/data/com.termux/files/usr/bin/bash
# 봇 실행. 죽으면 5초 뒤 자동 재시작
cd "$(dirname "$0")"
termux-wake-lock 2>/dev/null || true

SERIAL=$(grep -E '^ADB_SERIAL=' .env | cut -d= -f2)
while true; do
  if [ -n "$SERIAL" ] && ! adb devices | grep -q "^$SERIAL[[:space:]]*device"; then
    adb connect "$SERIAL" >/dev/null 2>&1 || true
  fi
  python bot.py
  echo "봇 종료됨, 5초 뒤 재시작"; sleep 5
done
