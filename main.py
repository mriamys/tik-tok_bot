import sys
import logging
import asyncio
import os
from dotenv import load_dotenv
import sqlite3
import glob
import re
import subprocess
from datetime import datetime
from aiogram import Bot, Dispatcher, exceptions
from aiogram.types import FSInputFile, Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, URLInputFile
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp
import math
import json
from telethon import TelegramClient
from logging.handlers import RotatingFileHandler

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003334578127"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "723550550"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))
DELAY_BETWEEN_UPLOADS = int(os.getenv("DELAY_BETWEEN_UPLOADS", "20"))
MAX_FILE_SIZE_MB = 49
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file = 'bot.log'
file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger(__name__)

# Telethon Userbot Client
user_client = TelegramClient('user', API_ID, API_HASH)

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_videos (
            video_id TEXT PRIMARY KEY,
            account_url TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tiktok_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS failed_videos (
            video_id TEXT PRIMARY KEY,
            fail_count INTEGER DEFAULT 1,
            last_fail_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_failures (
            url TEXT PRIMARY KEY,
            consecutive_fails INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0,
            last_fail_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_video_sent(video_id):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT video_id FROM sent_videos WHERE video_id = ?", (video_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_video_sent(video_id, account_url):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO sent_videos (video_id, account_url) VALUES (?, ?)", (video_id, account_url))
    conn.commit()
    conn.close()

def get_all_accounts():
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM tiktok_accounts")
    accounts = [row[0] for row in cursor.fetchall()]
    conn.close()
    return accounts

def get_stats():
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM tiktok_accounts")
    total_accounts = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM sent_videos")
    total_sent = cursor.fetchone()[0]
    conn.close()
    return total_accounts, total_sent

def add_account(url):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO tiktok_accounts (url) VALUES (?)", (url,))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def delete_account(url):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tiktok_accounts WHERE url = ?", (url,))
    cursor.execute("DELETE FROM account_failures WHERE url = ?", (url,))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0

def is_video_failed(video_id):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT fail_count FROM failed_videos WHERE video_id = ?", (video_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] >= 3

def mark_video_failed(video_id):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO failed_videos (video_id, fail_count) 
        VALUES (?, 1)
        ON CONFLICT(video_id) DO UPDATE SET 
            fail_count = fail_count + 1,
            last_fail_time = CURRENT_TIMESTAMP
    """, (video_id,))
    conn.commit()
    conn.close()

def record_account_success(url):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE account_failures SET consecutive_fails = 0, notified = 0 WHERE url = ?", (url,))
    conn.commit()
    conn.close()

def record_account_failure(url):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO account_failures (url, consecutive_fails, notified)
        VALUES (?, 1, 0)
        ON CONFLICT(url) DO UPDATE SET
            consecutive_fails = consecutive_fails + 1,
            last_fail_time = CURRENT_TIMESTAMP
        """, (url,))
    conn.commit()
    cursor.execute("SELECT consecutive_fails, notified FROM account_failures WHERE url = ?", (url,))
    row = cursor.fetchone()
    conn.close()
    return row if row else (0, 0)

def mark_account_notified(url):
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE account_failures SET notified = 1 WHERE url = ?", (url,))
    conn.commit()
    conn.close()

def cleanup_files(video_id):
    files = glob.glob(f"{video_id}*")
    for f in files:
        try:
            os.remove(f)
        except:
            pass

# --- ФУНКЦИИ TIKTOK ---
def get_channel_videos(url):
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--dump-json",
        "--flat-playlist",
        "--ignore-errors",
        "--quiet",
        url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        entries = []
        for line in result.stdout.strip().split('\n'):
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries
    except Exception as e:
        logger.error(f"Ошибка выполнения yt-dlp (получение списка) для {url}: {e}")
        return []

async def download_tiktok_via_userbot(video_url, video_id):
    logger.info(f"Запрос на скачивание {video_url} через @ttsavebot...")
    try:
        await user_client.send_message('ttsavebot', video_url)
    except Exception as e:
        logger.error(f"Ошибка отправки боту: {e}")
        return None

    video_file = None
    for _ in range(30):
        await asyncio.sleep(2)
        try:
            messages = await user_client.get_messages('ttsavebot', limit=2)
            for msg in messages:
                if msg.video:
                    logger.info("Видео получено от @ttsavebot! Сохраняем...")
                    video_file = await msg.download_media(file=f"{video_id}.mp4")
                    break
            if video_file:
                break
        except Exception as e:
            logger.error(f"Ошибка получения сообщений: {e}")
    
    return video_file

async def process_and_send_video(bot: Bot, video_url: str, video_id: str, account_url: str):
    logger.info(f"Обрабатываю видео: {video_id}")
    file_path = await download_tiktok_via_userbot(video_url, video_id)
    
    if file_path:
        try:
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if file_size_mb > MAX_FILE_SIZE_MB:
                logger.warning(f"Файл слишком большой ({file_size_mb:.2f} MB)")
                cleanup_files(video_id)
                mark_video_sent(video_id, account_url)
                return True

            video_input = FSInputFile(file_path)
            caption = f"Источник: <a href='{video_url}'>Видео</a>"
            
            await bot.send_video(
                chat_id=CHANNEL_ID,
                video=video_input,
                caption=caption,
                parse_mode="HTML",
                supports_streaming=True
            )
            mark_video_sent(video_id, account_url)
            logger.info(f"Видео {video_id} отправлено.")
            return True

        except exceptions.TelegramRetryAfter as e:
            logger.warning(f"Лимит Telegram. Ждем {e.retry_after} сек.")
            await asyncio.sleep(e.retry_after)
            cleanup_files(video_id)
            return False
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
            return False
        finally:
            cleanup_files(video_id)
            await asyncio.sleep(DELAY_BETWEEN_UPLOADS)
    else:
        logger.error(f"Не удалось скачать видео {video_id}")
        mark_video_failed(video_id)
        cleanup_files(video_id)
        return False

# --- UI И ЦИКЛ ---
async def get_main_keyboard():
    kb = [
        [KeyboardButton(text="➕ Добавить ссылку"), KeyboardButton(text="📋 Список")],
        [KeyboardButton(text="📊 Статистика")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

async def handle_manage_links(message_or_callback, page: int = 0, force_new_message: bool = False):
    event = message_or_callback if isinstance(message_or_callback, Message) else message_or_callback.message
    accounts = get_all_accounts()
    ITEMS_PER_PAGE = 10
    total_pages = math.ceil(len(accounts) / ITEMS_PER_PAGE) if accounts else 1
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1
    
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_accounts = accounts[start_idx:end_idx]
    
    keyboard = InlineKeyboardBuilder()
    if accounts:
        for acc in current_accounts:
            display_text = acc.replace("https://www.tiktok.com/@", "")
            keyboard.button(text=f"❌ {display_text}", callback_data=f"confirm_del_{display_text}")
        keyboard.adjust(2)
        
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardBuilder().button(text="⬅️ Назад", callback_data=f"page_{page-1}").as_markup().inline_keyboard[0][0])
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardBuilder().button(text="Вперед ➡️", callback_data=f"page_{page+1}").as_markup().inline_keyboard[0][0])
        if pagination_row:
            keyboard.row(*pagination_row)
    
    text = f"📋 <b>Управление аккаунтами TikTok (Страница {page+1} из {total_pages})</b>\n\n"
    if accounts: text += "Нажмите на кнопку с крестиком, чтобы удалить аккаунт из отслеживания:"
    else: text += "📝 Список аккаунтов пуст."
    
    if isinstance(message_or_callback, Message) or force_new_message:
        await event.answer(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")
    else:
        await event.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")

async def notify_admin_account_down(bot: Bot, account_url: str):
    username = account_url.replace("https://www.tiktok.com/@", "")
    text = (f"⚠️ <b>Ссылка недоступна / Ошибка получения видео</b>\n\n"
            f"Аккаунт <a href='{account_url}'>@{username}</a> не отвечает 10 проверок подряд.\n"
            f"Внимание: без Proxy сервер не может проверить наличие новых видео из-за блокировок TikTok.\n"
            f"Проверьте ссылку и удалите, если она больше не нужна.")
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except: pass

async def check_videos(bot: Bot):
    try:
        while True:
            accounts = await asyncio.to_thread(get_all_accounts)
            for account in accounts:
                logger.info(f"Проверяем: {account}")
                videos = await asyncio.to_thread(get_channel_videos, account)
                if not videos:
                    fails, notified = await asyncio.to_thread(record_account_failure, account)
                    if fails >= 10 and not notified:
                        # await notify_admin_account_down(bot, account)
                        await asyncio.to_thread(mark_account_notified, account)
                    continue
                
                await asyncio.to_thread(record_account_success, account)
                videos_sorted = list(reversed(videos))
                for entry in videos_sorted:
                    video_id = entry.get("id")
                    video_url = entry.get("url")
                    if not video_id or not video_url or is_video_sent(video_id) or is_video_failed(video_id):
                        continue
                    
                    await process_and_send_video(bot, video_url, video_id, account)

            logger.info(f"Пауза {CHECK_INTERVAL} сек.")
            await asyncio.sleep(CHECK_INTERVAL)
    except asyncio.CancelledError:
        logger.info("Цикл скачивания остановлен.")

async def main():
    # Запускаем Userbot сессию
    await user_client.start()
    logger.info("Userbot (Telethon) успешно запущен!")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    @dp.message(Command("start"))
    async def start_cmd(message: Message):
        if message.from_user.id != ADMIN_ID: return
        await message.answer("👋 Бот управления TikTok ссылками", reply_markup=await get_main_keyboard())
        await handle_manage_links(message)
    
    @dp.callback_query(lambda c: c.data.startswith("confirm_del_"))
    async def confirm_delete_callback(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID: return
        username = callback.data.replace("confirm_del_", "")
        text = f"❓ <b>Вы уверены, что хотите удалить аккаунт из отслеживания?</b>\n\n👤 <a href='https://www.tiktok.com/@{username}'>@{username}</a>"
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="✅ Да, удалить", callback_data=f"do_del_{username}")
        keyboard.button(text="❌ Отмена", callback_data="cancel_del")
        keyboard.adjust(2)
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")

    @dp.callback_query(lambda c: c.data.startswith("do_del_"))
    async def delete_callback(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID: return
        username = callback.data.replace("do_del_", "")
        url = f"https://www.tiktok.com/@{username}"
        if delete_account(url): await callback.answer(f"✅ Удалено: @{username}", show_alert=True)
        else: await callback.answer("❌ Ошибка при удалении", show_alert=True)
        await callback.message.delete()
        await handle_manage_links(callback.message, page=0, force_new_message=True)
        
    @dp.callback_query(lambda c: c.data == "cancel_del")
    async def cancel_delete_callback(callback: CallbackQuery):
        await callback.message.delete()
        await handle_manage_links(callback.message, page=0, force_new_message=True)
        
    @dp.callback_query(lambda c: c.data.startswith("page_"))
    async def page_callback(callback: CallbackQuery):
        page = int(callback.data.split("_")[1])
        await handle_manage_links(callback, page=page)
        await callback.answer()
    
    @dp.message()
    async def text_handler(message: Message):
        if message.from_user.id != ADMIN_ID: return
        text = message.text.strip()
        
        if text == "➕ Добавить ссылку":
            await message.answer("📝 Отправьте ссылку на аккаунт TikTok (https://www.tiktok.com/@username) или прямую ссылку на видео для скачивания.")
            return
        if text == "📋 Список":
            await handle_manage_links(message)
            return
        if text == "📊 Статистика":
            total_accs, total_sent = get_stats()
            stats_text = (f"📊 <b>Статистика работы бота</b>\n\n"
                          f"👥 В базе аккаунтов: <b>{total_accs}</b>\n"
                          f"🎬 Успешно отправлено видео: <b>{total_sent}</b>")
            await message.answer(stats_text, parse_mode="HTML")
            return
            
        if text.startswith('https://'):
            if '/video/' in text:
                video_id = text.split('/video/')[1].split('?')[0]
                await message.answer(f"⏳ Скачиваю видео через Userbot: {video_id}...")
                success = await process_and_send_video(bot, text, video_id, "direct_link")
                if success:
                    await message.answer("✅ Видео успешно опубликовано в канал.")
                else:
                    await message.answer("❌ Ошибка скачивания видео.")
            else:
                if text.startswith('https://www.tiktok.com/@'):
                    if add_account(text):
                        await message.answer(f"✅ Ссылка добавлена: {text}")
                    else:
                        await message.answer(f"⚠️ Ссылка уже существует: {text}")
                else:
                    await message.answer("❌ Неверный формат ссылки.")
    
    init_db()
    logger.info("Бот запущен с поддержкой Userbot (Telethon)")
    
    polling_task = asyncio.create_task(dp.start_polling(bot))
    video_check_task = asyncio.create_task(check_videos(bot))
    
    try:
        await polling_task
    finally:
        video_check_task.cancel()

if __name__ == "__main__":
    import sys
    try:
        # Телетон требует свой loop, asyncio.run() иногда конфликтует
        user_client.loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
    sys.exit(1)
