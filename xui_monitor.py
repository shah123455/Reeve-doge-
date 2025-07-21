#!/usr/bin/env python3
import os
import sqlite3
import re
import json
from collections import defaultdict
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update
from telegram.ext import Updater, CallbackQueryHandler, Dispatcher, CallbackContext, CommandHandler
from datetime import datetime, timedelta
import time
import subprocess
import threading
from dotenv import load_dotenv

# بارگذاری متغیرهای محیطی
load_dotenv()

# ========== تنظیمات ==========
LOG_FILE = "/var/log/x-ui/access.log"
DB_FILE = "/etc/x-ui/x-ui.db"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
SLEEP_INTERVAL = 600
LOG_WINDOW_MIN = 3
DEFAULT_LIMIT_PER_USER = 2
SERVICE_PATH = "/etc/systemd/system/xui-monitor.service"
USER_LIMITS_FILE = "/etc/x-ui/user_limits.json"

LOG_PATTERN = re.compile(
    r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\.\d+\s+from\s+(?:tcp:)?([\d\.]+):\d+.*email:\s*([^\s]+)'
)

def load_user_limits_file():
    try:
        if os.path.exists(USER_LIMITS_FILE):
            with open(USER_LIMITS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_user_limits_file(user_limits):
    try:
        with open(USER_LIMITS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_limits, f, ensure_ascii=False)
    except Exception as e:
        print(f"❌ خطا در ذخیره user_limits.json: {e}")

def sync_user_limits_to_default():
    limits = load_user_limits()
    user_limits_dict = load_user_limits_file()
    for email in limits:
        k = str(email)
        if k not in user_limits_dict:
            user_limits_dict[k] = DEFAULT_LIMIT_PER_USER
    save_user_limits_file(user_limits_dict)

def create_systemd_service():
    if os.path.exists(SERVICE_PATH):
        return
    python_path = subprocess.getoutput("which python3")
    script_path = os.path.abspath(__file__)
    service_content = f"""[Unit]
Description=X-UI Monitor Script
After=network.target

[Service]
Type=simple
ExecStart={python_path} {script_path}
WorkingDirectory={os.path.dirname(script_path)}
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(SERVICE_PATH, "w") as f:
            f.write(service_content)
        print(f"✅ فایل سرویس ساخته شد: {SERVICE_PATH}")
        print("برای فعال‌سازی:")
        print("systemctl daemon-reload && systemctl enable xui-monitor && systemctl start xui-monitor")
    except Exception as e:
        print(f"❌ خطا در ساخت سرویس: {e}")

def send_telegram_message(email, ip_count, ip_list, disabled=False, enabled=True):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    status_line = ""
    if not enabled:
        status_line = "❌ کاربر غیرفعال است\n"
    elif disabled:
        status_line = "⛔️ کاربر غیرفعال شد!\n"
    else:
        status_line = "📊 گزارش وضعیت کاربر\n"

    user_limits_dict = load_user_limits_file()
    per_user_limit = user_limits_dict.get(str(email), DEFAULT_LIMIT_PER_USER)

    message = (
        f"{status_line}"
        f"ایمیل: `{email}`\n"
        f"تعداد آی‌پی فعال: {ip_count}\n"
        f"محدودیت: {per_user_limit}\n"
        f"آی‌پی‌ها:\n" + "\n".join(f"`{ip}`" for ip in ip_list)
    )
    if disabled:
        message += "\n❌ دسترسی غیرفعال شد."

    keyboard = [
        [
            InlineKeyboardButton("⛔️ غیرفعال‌سازی کاربر" if enabled else "✅ فعال‌سازی کاربر", callback_data=f"{'disable' if enabled else 'enable'}|{email}"),
            InlineKeyboardButton("🔢 تنظیم محدودیت آی‌پی", callback_data=f"set_ip_limit_menu|{email}")
        ],
        [
            InlineKeyboardButton("🗂️ لیست کاربران غیرفعال", callback_data=f"list_deactivated|{email}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        print(f"❌ خطای ربات تلگرام: {e}")

def get_email_ip_map():
    m = defaultdict(set)
    now = datetime.now()
    window_start = now - timedelta(minutes=LOG_WINDOW_MIN)
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f:
                match = LOG_PATTERN.match(line)
                if match:
                    log_time = datetime.strptime(match.group(1), "%Y/%m/%d %H:%M:%S")
                    if log_time < window_start:
                        continue
                    ip = match.group(2)
                    email = match.group(3)
                    m[str(email)].add(ip)
    except FileNotFoundError:
        print(f"❌ فایل لاگ یافت نشد: {LOG_FILE}")
    return m

def load_user_limits():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    limits = {}
    cur.execute("SELECT id, settings FROM inbounds")
    rows = cur.fetchall()
    for inbound_id, settings_json in rows:
        try:
            cfg = json.loads(settings_json)
            for client in cfg.get("clients", []):
                email = client.get("email")
                enabled = client.get("enable", True)
                if email is not None:
                    limits[str(email)] = (inbound_id, enabled)
        except Exception:
            continue
    conn.close()
    return limits

def clear_user_ips_from_log(email):
    if not os.path.exists(LOG_FILE):
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                if f"email: {email}" not in line:
                    f.write(line)
    except Exception as e:
        print(f"❌ خطا در پاک‌کردن آی‌پی‌های کاربر از لاگ: {e}")

def disable_user(inbound_id, email):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT settings FROM inbounds WHERE id = ?", (inbound_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    try:
        cfg = json.loads(row[0])
        updated = False
        for client in cfg.get("clients", []):
            if str(client.get("email")) == str(email) and client.get("enable", True):
                client["enable"] = False
                updated = True
        if updated:
            new_json = json.dumps(cfg, ensure_ascii=False)
            cur.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_json, inbound_id))
            conn.commit()
            conn.close()
            print(f"✅ کاربر {email} غیرفعال شد.")
            return True
    except Exception as e:
        print(f"❌ خطا در غیرفعال‌سازی {email}: {e}")
    conn.close()
    return False

def enable_user(inbound_id, email):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT settings FROM inbounds WHERE id = ?", (inbound_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    try:
        cfg = json.loads(row[0])
        updated = False
        for client in cfg.get("clients", []):
            if str(client.get("email")) == str(email) and not client.get("enable", True):
                client["enable"] = True
                updated = True
        if updated:
            new_json = json.dumps(cfg, ensure_ascii=False)
            cur.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_json, inbound_id))
            conn.commit()
            clear_user_ips_from_log(email)
            conn.close()
            print(f"✅ کاربر {email} فعال شد.")
            return True
    except Exception as e:
        print(f"❌ خطا در فعال‌سازی {email}: {e}")
    conn.close()
    return False

def enable_user_by_email(email):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, settings FROM inbounds")
    found = False
    rows = cur.fetchall()
    for inbound_id, settings_json in rows:
        cfg = json.loads(settings_json)
        updated = False
        for client in cfg.get("clients", []):
            if str(client.get("email")) == str(email) and not client.get("enable", True):
                client["enable"] = True
                updated = True
                found = True
        if updated:
            new_json = json.dumps(cfg, ensure_ascii=False)
            cur.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_json, inbound_id))
            conn.commit()
            clear_user_ips_from_log(email)
    conn.close()
    if found:
        restart_xui()
        return True
    return False

def disable_user_by_email(email):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, settings FROM inbounds")
    found = False
    rows = cur.fetchall()
    for inbound_id, settings_json in rows:
        cfg = json.loads(settings_json)
        updated = False
        for client in cfg.get("clients", []):
            if str(client.get("email")) == str(email) and client.get("enable", True):
                client["enable"] = False
                updated = True
                found = True
        if updated:
            new_json = json.dumps(cfg, ensure_ascii=False)
            cur.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_json, inbound_id))
            conn.commit()
    conn.close()
    if found:
        restart_xui()
        return True
    return False

def restart_xui():
    try:
        subprocess.run(['x-ui', 'restart'], check=True)
        print("✅ سرویس x-ui ری‌استارت شد.")
    except Exception as e:
        print("❌ خطا در اجرای x-ui restart:", e)

def activation_menu():
    limits = load_user_limits()
    deact_users = [email for email, val in limits.items() if val[1] is False]
    if not deact_users:
        print("هیچ کاربر غیفعالی وجود ندارد.")
        return
    print("\n===== فعال‌سازی کاربر =====")
    print("لیست کاربران غیرفعال:")
    for i, email in enumerate(deact_users, 1):
        print(f"{i}. {email}")
    print("شماره یا ایمیل کاربر را وارد کن (یا q برای خروج):")
    choice = input("> ").strip()
    if choice.lower() == "q":
        return
    if choice.isdigit():
        idx = int(choice) - 1
        if idx < 0 or idx >= len(deact_users):
            print("شماره نامعتبر است.")
            return
        email = deact_users[idx]
    else:
        email = choice
        if email not in deact_users:
            print("ایمیل یافت نشد.")
            return
    inbound_id, _ = limits[email]
    if enable_user(inbound_id, email):
        restart_xui()
        print(f"کاربر {email} فعال شد و x-ui ری‌استارت شد.")
    else:
        print("فعال‌سازی انجام نشد یا قبلاً فعال بوده.")
    time.sleep(2)

def monitor_loop():
    print("🔄 مانیتورینگ شروع شد ...")
    while True:
        sync_user_limits_to_default()
        email_ips = get_email_ip_map()
        limits = load_user_limits()
        user_limits_dict = load_user_limits_file()
        need_restart = False
        for email, ips in email_ips.items():
            ip_count = len(ips)
            ip_list = sorted(ips)
            if email in limits:
                inbound_id, enabled = limits[email]
                per_user_limit = user_limits_dict.get(str(email), DEFAULT_LIMIT_PER_USER)
                if enabled and ip_count > per_user_limit:
                    if disable_user(inbound_id, email):
                        send_telegram_message(email, ip_count, ip_list, disabled=True, enabled=False)
                        need_restart = True
                else:
                    send_telegram_message(email, ip_count, ip_list, disabled=False, enabled=enabled)
        if need_restart:
            restart_xui()
        time.sleep(SLEEP_INTERVAL)

def menu_loop():
    while True:
        print("\n======== منو ========")
        print("1. فعال‌سازی یک کاربر غیرفعال")
        print("2. خروج از منو")
        print("=====================")
        choice = input("شماره را وارد کنید: ").strip()
        if choice == "1":
            activation_menu()
        elif choice == "2":
            print("خروج از منو.")
            break
        else:
            print("گزینه نامعتبر!")

def arrange_buttons(items, buttons_per_row):
    rows = []
    for i in range(0, len(items), buttons_per_row):
        rows.append(items[i:i + buttons_per_row])
    return rows

def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    if str(query.message.chat_id) != str(TELEGRAM_CHAT_ID):
        query.answer("اجازه نداری.", show_alert=True)
        return
    data = query.data
    if data.startswith("enable|"):
        email = data.split("|", 1)[1]
        if enable_user_by_email(email):
            query.edit_message_text(f"✅ کاربر `{email}` فعال شد و x-ui ریستارت شد.", parse_mode="Markdown")
        else:
            query.answer("فعال‌سازی انجام نشد یا کاربر قبلاً فعال بوده.", show_alert=True)
    elif data.startswith("disable|"):
        email = data.split("|", 1)[1]
        if disable_user_by_email(email):
            query.edit_message_text(f"⛔️ کاربر `{email}` غیرفعال شد و x-ui ریستارت شد.", parse_mode="Markdown")
        else:
            query.answer("غیرفعال‌سازی انجام نشد یا قبلاً غیرفعال بوده.", show_alert=True)
    elif data.startswith("list_deactivated|"):
        last_email = data.split("|", 1)[1]
        limits = load_user_limits()
        deact_users = [email for email, val in limits.items() if val[1] is False]
        msg = "🗂️ لیست کاربران غیرفعال:\n\n"
        user_buttons = [
            InlineKeyboardButton(f"✅ فعال‌سازی {email}", callback_data=f"enable|{email}")
            for email in deact_users
        ]
        keyboard = arrange_buttons(user_buttons, 2)
        keyboard.append([InlineKeyboardButton("⬅️ برگشت", callback_data=f"back_to_status|{last_email}")])
        if not deact_users:
            msg += "🎉 همه کاربران فعال هستند."
        else:
            for email in deact_users:
                msg += f"- {email}\n"
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            query.edit_message_text(msg, reply_markup=reply_markup)
        except Exception as e:
            query.answer("خطا در ارسال لیست!", show_alert=True)
    elif data.startswith("back_to_status|"):
        email = data.split("|", 1)[1]
        email_ips = get_email_ip_map()
        limits = load_user_limits()
        user_limits_dict = load_user_limits_file()
        ip_list = sorted(email_ips.get(email, []))
        ip_count = len(ip_list)
        if email in limits:
            inbound_id, enabled = limits[email]
            per_user_limit = user_limits_dict.get(str(email), DEFAULT_LIMIT_PER_USER)
            status_line = ""
            if not enabled:
                status_line = "❌ کاربر غیرفعال است\n"
            else:
                status_line = "📊 گزارش وضعیت کاربر\n"
            message = (
                f"{status_line}"
                f"ایمیل: `{email}`\n"
                f"تعداد آی‌پی فعال: {ip_count}\n"
                f"محدودیت: {per_user_limit}\n"
                f"آی‌پی‌ها:\n" + "\n".join(f"`{ip}`" for ip in ip_list)
            )
            keyboard = [
                [
                    InlineKeyboardButton("⛔️ غیرفعال‌سازی کاربر" if enabled else "✅ فعال‌سازی کاربر", callback_data=f"{'disable' if enabled else 'enable'}|{email}"),
                    InlineKeyboardButton("🔢 تنظیم محدودیت آی‌پی", callback_data=f"set_ip_limit_menu|{email}")
                ],
                [
                    InlineKeyboardButton("🗂️ لیست کاربران غیرفعال", callback_data=f"list_deactivated|{email}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")
            except Exception as e:
                query.answer("خطا در ارسال گزارش!", show_alert=True)
        else:
            query.answer("کاربر یافت نشد.", show_alert=True)
    elif data.startswith("set_ip_limit_menu|"):
        email = data.split("|", 1)[1]
        msg = f"🔢 محدودیت آی‌پی برای `{email}` را انتخاب کنید:"
        number_buttons = [
            InlineKeyboardButton(str(i), callback_data=f"set_ip_limit|{email}|{i}")
            for i in range(1, 11)
        ]
        keyboard = arrange_buttons(number_buttons, 5)
        keyboard.append([InlineKeyboardButton("⬅️ برگشت", callback_data=f"back_to_status|{email}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="Markdown")
    elif data.startswith("set_ip_limit|"):
        parts = data.split("|")
        if len(parts) == 3:
            email = parts[1]
            limit = parts[2]
            try:
                limit = int(limit)
                if limit < 1 or limit > 100:
                    query.answer("محدودیت باید بین 1 تا 100 باشد.", show_alert=True)
                    return
            except Exception:
                query.answer("مقدار محدودیت نامعتبر است.", show_alert=True)
                return
            user_limits_dict = load_user_limits_file()
            user_limits_dict[str(email)] = limit
            save_user_limits_file(user_limits_dict)
            query.answer(f"✅ محدودیت آی‌پی برای {email} روی {limit} تنظیم شد.", show_alert=True)
            # بروزرسانی وضعیت کاربر پس از تنظیم محدودیت
            email_ips = get_email_ip_map()
            limits = load_user_limits()
            ip_list = sorted(email_ips.get(email, []))
            ip_count = len(ip_list)
            if email in limits:
                inbound_id, enabled = limits[email]
                per_user_limit = user_limits_dict.get(str(email), DEFAULT_LIMIT_PER_USER)
                status_line = ""
                if not enabled:
                    status_line = "❌ کاربر غیرفعال است\n"
                else:
                    status_line = "📊 گزارش وضعیت کاربر\n"
                message = (
                    f"{status_line}"
                    f"ایمیل: `{email}`\n"
                    f"تعداد آی‌پی فعال: {ip_count}\n"
                    f"محدودیت: {per_user_limit}\n"
                    f"آی‌پی‌ها:\n" + "\n".join(f"`{ip}`" for ip in ip_list)
                )
                keyboard = [
                    [
                        InlineKeyboardButton("⛔️ غیرفعال‌سازی کاربر" if enabled else "✅ فعال‌سازی کاربر", callback_data=f"{'disable' if enabled else 'enable'}|{email}"),
                        InlineKeyboardButton("🔢 تنظیم محدودیت آی‌پی", callback_data=f"set_ip_limit_menu|{email}")
                    ],
                    [
                        InlineKeyboardButton("🗂️ لیست کاربران غیرفعال", callback_data=f"list_deactivated|{email}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                query.answer("کاربر یافت نشد.", show_alert=True)

def start_handler(update: Update, context: CallbackContext):
    limits = load_user_limits()
    deact_users = [email for email, val in limits.items() if val[1] is False]
    msg = "🗂️ لیست کاربران غیرفعال:\n\n"
    user_buttons = [
        InlineKeyboardButton(f"✅ فعال‌سازی {email}", callback_data=f"enable|{email}")
        for email in deact_users
    ]
    keyboard = arrange_buttons(user_buttons, 5)
    reply_markup = InlineKeyboardMarkup(keyboard)
    if not deact_users:
        msg += "🎉 همه کاربران فعال هستند."
    else:
        for email in deact_users:
            msg += f"- {email}\n"
    update.message.reply_text(msg, reply_markup=reply_markup)

def setlimit_handler(update: Update, context: CallbackContext):
    if str(update.message.chat_id) != str(TELEGRAM_CHAT_ID):
        update.message.reply_text("اجازه نداری.")
        return
    parts = update.message.text.strip().split()
    if len(parts) != 3:
        update.message.reply_text("فرمت دستور:\n`/setlimit email@example.com 3`", parse_mode="Markdown")
        return
    _, email, limit = parts
    try:
        limit = int(limit)
        if limit < 1 or limit > 100:
            update.message.reply_text("محدودیت باید بین 1 تا 100 باشد.")
            return
    except ValueError:
        update.message.reply_text("مقدار محدودیت باید عدد صحیح باشد.")
        return
    user_limits_dict = load_user_limits_file()
    user_limits_dict[str(email)] = limit
    save_user_limits_file(user_limits_dict)
    update.message.reply_text(f"✅ محدودیت آی‌پی برای `{email}` روی {limit} تنظیم شد.", parse_mode="Markdown")

def start_telegram_bot():
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp: Dispatcher = updater.dispatcher
    dp.add_handler(CallbackQueryHandler(button_handler))
    dp.add_handler(CommandHandler("start", start_handler))
    dp.add_handler(CommandHandler("setlimit", setlimit_handler))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    sync_user_limits_to_default()
    create_systemd_service()
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    start_telegram_bot()
