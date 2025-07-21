#!/bin/bash

set -e

if [ "$(id -u)" != "0" ]; then
  echo "این اسکریپت باید با دسترسی root اجرا شود."
  exit 1
fi

read -p "توکن ربات تلگرام را وارد کنید: " bot_token
read -p "آیدی عددی مدیر (chat_id) را وارد کنید: " chat_id

echo "TELEGRAM_BOT_TOKEN=$bot_token" > .env
echo "TELEGRAM_CHAT_ID=$chat_id" >> .env

echo "⏳ نصب وابستگی‌های لازم..."
apt update
apt install -y python3 python3-pip sqlite3

echo "⏳ نصب کتابخانه‌های پایتون..."
pip3 install python-telegram-bot python-dotenv

echo "⏳ کپی فایل سرویس systemd..."
cp xui-monitor.service /etc/systemd/system/

echo "⏳ راه‌اندازی سرویس..."
systemctl daemon-reload
systemctl enable xui-monitor
systemctl restart xui-monitor

echo "✅ نصب و راه‌اندازی کامل شد!"
echo "وضعیت سرویس:"
systemctl status xui-monitor --no-pager
