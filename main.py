import sys
import logging
import asyncio
import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
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
import requests
from bs4 import BeautifulSoup
import json

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003334578127"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "723550550"))  # Ваш ID для управления ссылками
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))
DELAY_BETWEEN_UPLOADS = int(os.getenv("DELAY_BETWEEN_UPLOADS", "20"))
MAX_FILE_SIZE_MB = 49  # Лимит телеграма для ботов (оставляем запас 1 МБ)
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_cookies.txt")

from logging.handlers import RotatingFileHandler

# --- ЛОГИРОВАНИЕ ---
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file = 'bot.log'

# Ротация логов: 5 МБ на файл, храним до 5 старых файлов
file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# Вывод в консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

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
    """Получить все аккаунты из базы"""
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM tiktok_accounts")
    accounts = [row[0] for row in cursor.fetchall()]
    conn.close()
    return accounts

def get_stats():
    """Получить статистику базы данных"""
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM tiktok_accounts")
    total_accounts = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM sent_videos")
    total_sent = cursor.fetchone()[0]
    conn.close()
    return total_accounts, total_sent

def add_account(url):
    """Добавить новый аккаунт"""
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
    """Удалить аккаунт"""
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tiktok_accounts WHERE url = ?", (url,))
    cursor.execute("DELETE FROM account_failures WHERE url = ?", (url,))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0

def is_video_failed(video_id):
    """Проверить слишком ли много попыток скачать видео"""
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT fail_count FROM failed_videos WHERE video_id = ?", (video_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] >= 3  # Пропускаем видео после 3 неудачных попыток

def mark_video_failed(video_id):
    """Отметить видео как не скачиваемое"""
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
    """Сбросить счётчик неудач для аккаунта (видео найдены)."""
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE account_failures SET consecutive_fails = 0, notified = 0 WHERE url = ?",
        (url,),
    )
    conn.commit()
    conn.close()


def record_account_failure(url):
    """
    Увеличить счётчик последовательных неудач для аккаунта.
    Возвращает (consecutive_fails, already_notified).
    """
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO account_failures (url, consecutive_fails, notified)
        VALUES (?, 1, 0)
        ON CONFLICT(url) DO UPDATE SET
            consecutive_fails = consecutive_fails + 1,
            last_fail_time = CURRENT_TIMESTAMP
        """,
        (url,),
    )
    conn.commit()
    cursor.execute(
        "SELECT consecutive_fails, notified FROM account_failures WHERE url = ?",
        (url,),
    )
    row = cursor.fetchone()
    conn.close()
    return row if row else (0, 0)


def mark_account_notified(url):
    """Отметить, что уведомление об аккаунте уже отправлено."""
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE account_failures SET notified = 1 WHERE url = ?", (url,)
    )
    conn.commit()
    conn.close()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def clean_hashtags(text):
    if not text:
        return ""
    text = re.sub(r'#\S+', '', text).strip()
    return re.sub(r'\n\s*\n', '\n', text)

def format_timestamp(timestamp, date_str):
    if timestamp:
        try:
            dt = datetime.fromtimestamp(timestamp)
            return dt.strftime("%d.%m.%Y")
        except:
            pass
    if date_str and len(date_str) == 8:
        return f"{date_str[6:8]}.{date_str[4:6]}.{date_str[0:4]}"
    return datetime.now().strftime("%d.%m.%Y")

def cleanup_files(video_id):
    """Удаление всех временных файлов, связанных с video_id."""
    files = glob.glob(f"{video_id}*")
    for f in files:
        try:
            os.remove(f)
        except Exception as e:
            logger.error(f"Не удалось удалить {f}: {e}")

def get_video_dimensions(file_path):
    """Получить реальные размеры видео через ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0', file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            w, h = result.stdout.strip().split(',')
            return int(w), int(h)
    except Exception as e:
        logger.error(f"Ошибка получения размеров {file_path}: {e}")
    return None, None


def generate_thumbnail(video_file, thumb_file):
    """
    Генерация JPEG-превью для видео (макс. 320px по длинной стороне).
    Telegram требует thumbnail ≤ 320x320, JPEG.
    Без thumbnail iOS-клиент часто неправильно определяет aspect ratio.
    """
    try:
        cmd = [
            'ffmpeg', '-i', video_file,
            '-ss', '00:00:01.000',
            '-vframes', '1',
            '-vf', 'scale=if(gt(iw\,ih)\,320\,-2):if(gt(iw\,ih)\,-2\,320)',
            '-q:v', '3',
            '-y',
            thumb_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and os.path.exists(thumb_file):
            logger.info(f"✅ Thumbnail создан: {thumb_file}")
            return True
        else:
            logger.warning(f"Не удалось создать thumbnail: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"Ошибка создания thumbnail: {e}")
        return False


def reprocess_video_ffmpeg(input_file, output_file):
    """
    Ремуксинг видео для совместимости с Telegram без потери качества.

    Стратегия:
    1. Копируем видео- и аудиопотоки без перекодирования (-c copy).
    2. Устанавливаем movflags +faststart для быстрого стриминга.
    3. Фиксим SAR=1 через bsf, чтобы Telegram/iOS корректно определял
       пропорции.
    4. Если copy-режим не сработал (редкий кодек), делаем минимальный
       re-encode с максимальным качеством (CRF 17, preset slow).
    """
    try:
        # --- Попытка 1: Быстрый ремуксинг без перекодировки ---
        cmd_copy = [
            "ffmpeg",
            "-i", input_file,
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-y",
            output_file,
        ]

        logger.info(
            f"Запуск ffmpeg ремуксинг (copy): {input_file} -> {output_file}"
        )
        result = subprocess.run(
            cmd_copy, capture_output=True, text=True, timeout=120
        )

        if result.returncode == 0 and os.path.exists(output_file):
            logger.info(f"✅ Видео ремукснуто без потерь: {output_file}")
            return True

        logger.warning(
            f"Copy-режим не удался, пробуем минимальный re-encode: "
            f"{result.stderr[:300]}"
        )

        # --- Попытка 2: Минимальный re-encode с сохранением качества ---
        cmd_reencode = [
            "ffmpeg",
            "-i", input_file,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "17",
            "-preset", "slow",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-y",
            output_file,
        ]

        logger.info(
            f"Запуск ffmpeg re-encode (CRF 17): {input_file} -> {output_file}"
        )
        result = subprocess.run(
            cmd_reencode, capture_output=True, text=True, timeout=600
        )

        if result.returncode == 0:
            logger.info(
                f"✅ Видео переобработано с высоким качеством: {output_file}"
            )
            return True

        logger.error(f"❌ Ошибка ffmpeg re-encode: {result.stderr}")
        return False

    except Exception as e:
        logger.error(f"Ошибка при переобработке видео: {e}")
        return False

# --- ФУНКЦИИ TIKTOK ---
def get_tiktok_avatar(username):
    """Пытаемся вытащить аватарку аккаунта через парсинг страницы"""
    url = f"https://www.tiktok.com/@{username}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        data_script = soup.find('script', id='__UNIVERSAL_DATA_FOR_REHYDRATION__')
        
        if data_script:
            data = json.loads(data_script.string)
            user_detail = data.get('__DEFAULT_SCOPE__', {}).get('webapp.user-detail', {})
            user_info = user_detail.get('userInfo', {}).get('user', {})
            avatar_url = user_info.get('avatarLarger') or user_info.get('avatarMedium') or user_info.get('avatarThumb')
            return avatar_url
    except Exception as e:
        logger.error(f"Ошибка получения аватарки {username}: {e}")
    return None

def get_channel_videos(url):
    cookies_args = ["--cookies", COOKIES_FILE, "--impersonate", "chrome"] if os.path.exists(COOKIES_FILE) else ["--impersonate", "chrome"]
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--dump-json",
        "--flat-playlist",
        "--playlist-end", "30",
        "--ignore-errors",
        "--quiet",
        *cookies_args,
        url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if result.returncode != 0 and not result.stdout.strip():
            logger.error(f"Ошибка yt-dlp для {url}: {result.stderr[:200]}")
            return []
        
        entries = []
        for line in result.stdout.strip().split('\n'):
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries
    except subprocess.TimeoutExpired:
        logger.error(f"Таймаут (90с) при получении видео для {url}")
        return []
    except Exception as e:
        logger.error(f"Ошибка выполнения yt-dlp для {url}: {e}")
        return []

def process_and_download(video_url, video_id):
    """Скачивание и переобработка видео для Telegram."""
    cleanup_files(video_id)
    filename_template = f"{video_id}.%(ext)s"
    temp_file = f"{video_id}_temp.mp4"
    final_file = f"{video_id}.mp4"
    thumb_file = f"{video_id}_thumb.jpg"

    cookies_args = ["--cookies", COOKIES_FILE, "--impersonate", "chrome"] if os.path.exists(COOKIES_FILE) else ["--impersonate", "chrome"]
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo+bestaudio/best",
        "-o", filename_template,
        "--merge-output-format", "mp4",
        "--quiet",
        "--no-playlist",
        *cookies_args,
        "--dump-json",
        "--no-simulate",
        video_url
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        info = {}
        for line in result.stdout.strip().split('\n'):
            if line.startswith('{'):
                try:
                    info = json.loads(line)
                except json.JSONDecodeError:
                    pass

        if os.path.exists(final_file):
            # Переименовываем в temp и переобрабатываем через ffmpeg
            os.rename(final_file, temp_file)

            if reprocess_video_ffmpeg(temp_file, final_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
            else:
                # Если ffmpeg не сработал — используем оригинал
                logger.warning(
                    f"ffmpeg не сработал, отправляем оригинальное видео {video_id}"
                )
                os.rename(temp_file, final_file)

            # Генерируем thumbnail из итогового файла
            generate_thumbnail(final_file, thumb_file)

            raw_title = info.get('title', '') or info.get('description', '')
            clean_title = clean_hashtags(raw_title)
            author = (
                info.get('uploader') or info.get('channel') or "TikTok"
            )
            ts = info.get('timestamp')
            d_str = info.get('upload_date')
            final_date = format_timestamp(ts, d_str)

            return final_file, clean_title, final_date, author
        else:
            logger.error(f"Файл {final_file} не был создан yt-dlp. Stderr: {result.stderr}")

    except subprocess.TimeoutExpired:
        logger.error(f"Таймаут (300с) при скачивании видео {video_url}")
    except Exception as e:
        logger.error(f"Ошибка обработки {video_url}: {e}")

    return None, None, None, None

# --- ОСНОВНОЙ ЦИКЛ ---
async def get_main_keyboard():
    kb = [
        [KeyboardButton(text="➕ Добавить ссылку"), KeyboardButton(text="📋 Список")],
        [KeyboardButton(text="📊 Статистика")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

async def handle_manage_links(message_or_callback, page: int = 0, force_new_message: bool = False):
    """Показать кнопки для управления ссылками с пагинацией"""
    if isinstance(message_or_callback, Message):
        user_id = message_or_callback.from_user.id
        event = message_or_callback
    else:
        user_id = message_or_callback.from_user.id
        event = message_or_callback.message
    
    logger.info(f"handle_manage_links: user_id={user_id}, page={page}")
    
    if user_id != ADMIN_ID:
        # Если это сообщение от самого бота (через callback.message), не ругаемся на доступ
        # так как это внутренний вызов после успешного подтверждения админа
        if user_id == (await message_or_callback.bot.get_me()).id:
            logger.info("Внутренний вызов от имени бота, проверка ADMIN_ID пропущена")
        else:
            if isinstance(message_or_callback, Message):
                await message_or_callback.answer("❌ Доступ запрещен.")
            else:
                await message_or_callback.answer("❌ Доступ запрещен.", show_alert=True)
            return
    
    accounts = get_all_accounts()
    
    ITEMS_PER_PAGE = 10
    total_pages = math.ceil(len(accounts) / ITEMS_PER_PAGE) if accounts else 1
    
    # Защита от выхода за пределы
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1
    
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_accounts = accounts[start_idx:end_idx]
    
    keyboard = InlineKeyboardBuilder()
    
    if accounts:
        for acc in current_accounts:
            # Обрезаем ссылку для отображения
            display_text = acc.replace("https://www.tiktok.com/@", "")
            keyboard.button(text=f"❌ {display_text}", callback_data=f"confirm_del_{display_text}")
        
        # Делаем по 2 кнопки в ряд для компактности
        keyboard.adjust(2)
        
        # Кнопки пагинации
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardBuilder().button(text="⬅️ Назад", callback_data=f"page_{page-1}").as_markup().inline_keyboard[0][0])
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardBuilder().button(text="Вперед ➡️", callback_data=f"page_{page+1}").as_markup().inline_keyboard[0][0])
            
        if pagination_row:
            keyboard.row(*pagination_row)
    
    text = f"📋 <b>Управление аккаунтами TikTok (Страница {page+1} из {total_pages})</b>\n\n"
    if accounts:
        text += "Нажмите на кнопку с крестиком, чтобы удалить аккаунт из отслеживания:"
    else:
        text += "📝 Список аккаунтов пуст."
    
    if isinstance(message_or_callback, Message) or force_new_message:
        await event.answer(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")
    else:
        # Если это колбэк и не просили новое сообщение, обновляем текущее
        await message_or_callback.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")

async def handle_confirm_delete(callback: CallbackQuery):
    """Показать аватарку и запросить подтверждение удаления"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    
    username = callback.data.replace("confirm_del_", "")
    await callback.message.edit_text("⏳ Загрузка информации об аккаунте...")
    
    avatar_url = await asyncio.to_thread(get_tiktok_avatar, username)
    text = f"❓ <b>Вы уверены, что хотите удалить аккаунт из отслеживания?</b>\n\n👤 <a href='https://www.tiktok.com/@{username}'>@{username}</a>"
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Да, удалить", callback_data=f"do_del_{username}")
    keyboard.button(text="❌ Отмена", callback_data="cancel_del")
    keyboard.adjust(2)
    
    # Удаляем старое текстовое сообщение
    await callback.message.delete()
    
    if avatar_url:
        try:
            photo = URLInputFile(avatar_url)
            await callback.message.answer_photo(
                photo=photo,
                caption=text,
                reply_markup=keyboard.as_markup(),
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.error(f"Не удалось отправить фото {avatar_url}: {e}")
            
    # Если аватарку найти не удалось или не отправилась
    await callback.message.answer(
        text, 
        reply_markup=keyboard.as_markup(), 
        parse_mode="HTML"
    )

async def handle_delete_callback(callback: CallbackQuery):
    """Обработка окончательного удаления ссылки через кнопку"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    
    username = callback.data.replace("do_del_", "")
    url = f"https://www.tiktok.com/@{username}"
    
    if delete_account(url):
        await callback.answer(f"✅ Удалено: @{username}", show_alert=True)
        logger.info(f"Удалена ссылка: {url}")
    else:
        await callback.answer("❌ Ошибка при удалении", show_alert=True)
        
    # Удаляем сообщение с фото/подтверждением
    await callback.message.delete()
    
    # Возвращаемся к списку (отправляем новое сообщение, так как старое удалено)
    await handle_manage_links(callback.message, page=0, force_new_message=True)

async def handle_cancel_delete(callback: CallbackQuery):
    """Отмена удаления"""
    await callback.message.delete()
    await handle_manage_links(callback.message, page=0, force_new_message=True)

async def handle_add_request(message: Message):
    """Запрос на добавление ссылки"""
    if message.from_user.id != ADMIN_ID:
        return
    
    await message.answer(
        "📝 Отправьте ссылку на аккаунт TikTok в формате:\n"
        "https://www.tiktok.com/@username"
    )

async def handle_text_input(message: Message):
    """Обработка ввода текста"""
    if message.from_user.id != ADMIN_ID:
        return
    
    text = message.text.strip()
    
    if text == "➕ Добавить ссылку":
        await handle_add_request(message)
        return
        
    if text == "📋 Список":
        await handle_manage_links(message)
        return
        
    if text == "📊 Статистика":
        total_accs, total_sent = get_stats()
        stats_text = (
            "📊 <b>Статистика работы бота</b>\n\n"
            f"👥 В базе аккаунтов: <b>{total_accs}</b>\n"
            f"🎬 Успешно отправлено видео: <b>{total_sent}</b>"
        )
        await message.answer(stats_text, parse_mode="HTML")
        return
    
    # Пропускаем команды
    if text.startswith('/'):
        return
    
    # Проверяем формат
    if not text.startswith('https://www.tiktok.com/@'):
        # Если это не ссылка и не команда меню, можно игнорировать или сказать про формат
        # Но лучше не спамить, если пользователь просто пишет что-то другое
        return
    
    if add_account(text):
        await message.answer(f"✅ Ссылка добавлена: {text}")
        logger.info(f"Добавлена новая ссылка: {text}")
    else:
        await message.answer(f"⚠️ Ссылка уже существует: {text}")

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    # Регистрируем обработчики для ПМ
    @dp.message(Command("manage"))
    async def manage_cmd(message: Message):
        await handle_manage_links(message)
    
    @dp.message(Command("start"))
    async def start_cmd(message: Message):
        logger.info(f"Получена команда /start от {message.from_user.id}")
        await message.answer("👋 Бот управления TikTok ссылками", reply_markup=await get_main_keyboard())
        await handle_manage_links(message)
    
    @dp.callback_query(lambda c: c.data.startswith("confirm_del_"))
    async def confirm_delete_callback(callback: CallbackQuery):
        await handle_confirm_delete(callback)

    @dp.callback_query(lambda c: c.data.startswith("do_del_"))
    async def delete_callback(callback: CallbackQuery):
        await handle_delete_callback(callback)
        
    @dp.callback_query(lambda c: c.data == "cancel_del")
    async def cancel_delete_callback(callback: CallbackQuery):
        await handle_cancel_delete(callback)
        
    @dp.callback_query(lambda c: c.data.startswith("page_"))
    async def page_callback(callback: CallbackQuery):
        page = int(callback.data.split("_")[1])
        await handle_manage_links(callback, page=page)
        await callback.answer()
    
    @dp.message()
    async def text_handler(message: Message):
        logger.info(f"Получено текстовое сообщение от {message.from_user.id}: {message.text[:50]}")
        await handle_text_input(message)
    
    init_db()
    logger.info("Бот запущен v7.5 (Без сжатия качества + Уведомления о недоступных ссылках)")
    
    # Запускаем polling и основной цикл одновременно
    polling_task = asyncio.create_task(dp.start_polling(bot))
    video_check_task = asyncio.create_task(check_videos(bot, dp))
    backup_task = asyncio.create_task(daily_db_backup(bot))
    
    try:
        # Ждем завершения polling (это произойдет при SIGINT/Ctrl+C)
        await polling_task
    finally:
        # Отменяем задачу проверки видео
        logger.info("Останавливаем задачи...")
        video_check_task.cancel()
        backup_task.cancel()
        try:
            await asyncio.gather(video_check_task, backup_task)
        except asyncio.CancelledError:
            logger.info("Задачи остановлены.")

async def notify_admin_account_down(bot: Bot, account_url: str):
    """Отправить уведомление админу в ЛС о недоступной ссылке."""
    username = account_url.replace("https://www.tiktok.com/@", "")
    text = (
        f"⚠️ <b>Ссылка недоступна</b>\n\n"
        f"Аккаунт <a href='{account_url}'>@{username}</a> "
        f"не отвечает 10 проверок подряд.\n\n"
        f"Возможные причины:\n"
        f"• Аккаунт заблокирован или удалён\n"
        f"• Изменился URL\n"
        f"• Временные проблемы TikTok\n\n"
        f"Проверьте ссылку и удалите, если она больше не нужна."
    )
    try:
        await bot.send_message(
            chat_id=ADMIN_ID, text=text, parse_mode="HTML"
        )
        logger.info(f"Уведомление админу: аккаунт {account_url} недоступен")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админу: {e}")


async def daily_db_backup(bot: Bot):
    """Отправляет бэкап базы данных каждый день в 5 утра."""
    from datetime import datetime, timedelta
    import asyncio
    try:
        while True:
            now = datetime.now()
            # Устанавливаем время на 5:00 текущего дня
            target_time = now.replace(hour=5, minute=0, second=0, microsecond=0)
            
            # Если 5 утра уже прошло сегодня, планируем на завтра
            if now >= target_time:
                target_time += timedelta(days=1)
                
            sleep_seconds = (target_time - now).total_seconds()
            logger.info(f"Запланирована отправка бэкапа БД через {sleep_seconds/3600:.2f} часов (в {target_time.strftime('%Y-%m-%d %H:%M:%S')})")
            
            await asyncio.sleep(sleep_seconds)
            
            # Отправка файла
            db_path = "videos.db"
            if os.path.exists(db_path):
                try:
                    document = FSInputFile(db_path)
                    await bot.send_document(
                        chat_id=ADMIN_ID,
                        document=document,
                        caption=f"📦 Ежедневный бэкап базы данных (5:00)\nДата: {datetime.now().strftime('%Y-%m-%d')}"
                    )
                    logger.info("Бэкап базы данных успешно отправлен админу.")
                except Exception as e:
                    logger.error(f"Ошибка при отправке бэкапа: {e}")
            else:
                logger.warning("Файл videos.db не найден для бэкапа.")
                
            # Пауза чтобы не сработать дважды за одну минуту
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("Задача бэкапа остановлена.")


async def check_videos(bot: Bot, dp: Dispatcher):
    """Основной цикл скачивания видео"""
    try:
        while True:
            accounts = await asyncio.to_thread(get_all_accounts)

            for account in accounts:
                logger.info(f"Проверяем: {account}")
                videos = await asyncio.to_thread(get_channel_videos, account)

                if not videos:
                    # Отслеживаем последовательные неудачи получения списка
                    fails, notified = await asyncio.to_thread(
                        record_account_failure, account
                    )
                    logger.warning(
                        f"Нет видео для {account} "
                        f"(подряд неудач: {fails}, уведомлено: {notified})"
                    )
                    if fails >= 10 and not notified:
                        await notify_admin_account_down(bot, account)
                        await asyncio.to_thread(
                            mark_account_notified, account
                        )
                    continue

                # Видео получены — сбрасываем счётчик неудач
                await asyncio.to_thread(record_account_success, account)

                videos_sorted = list(reversed(videos))

                for entry in videos_sorted:
                    video_id = entry.get("id")
                    video_url = entry.get("url")

                    if not video_id or not video_url:
                        continue

                    if is_video_sent(video_id):
                        continue

                    # Пропускаем видео которые не скачиваются
                    if is_video_failed(video_id):
                        logger.warning(
                            f"Видео {video_id} не скачивается "
                            f"(более 3 попыток). Пропускаем."
                        )
                        continue

                    logger.info(f"Обрабатываю новое видео: {video_id}")

                    file_path, title, date_str, author = (
                        await asyncio.to_thread(
                            process_and_download, video_url, video_id
                        )
                    )

                    if file_path:
                        try:
                            # --- ПРОВЕРКА РАЗМЕРА ФАЙЛА ---
                            file_size_mb = os.path.getsize(file_path) / (
                                1024 * 1024
                            )
                            if file_size_mb > MAX_FILE_SIZE_MB:
                                logger.warning(
                                    f"Файл {file_path} слишком большой "
                                    f"({file_size_mb:.2f} MB). Пропускаем."
                                )
                                cleanup_files(video_id)
                                mark_video_sent(video_id, account)
                                continue

                            video_input = FSInputFile(file_path)
                            caption = (
                                f"📅 <b>{date_str}</b>\n"
                                f"{title}\n\n"
                                f"👤 Канал: <b>{author}</b>\n"
                                f"Источник: <a href='{video_url}'>Видео</a>"
                            )

                            # Размеры для корректного отображения на iOS
                            w, h = await asyncio.to_thread(
                                get_video_dimensions, file_path
                            )
                            kwargs = {}
                            if w and h:
                                kwargs["width"] = w
                                kwargs["height"] = h

                            # Thumbnail — критично для iOS aspect ratio
                            thumb_path = f"{video_id}_thumb.jpg"
                            if os.path.exists(thumb_path):
                                kwargs["thumbnail"] = FSInputFile(
                                    thumb_path
                                )

                            await bot.send_video(
                                chat_id=CHANNEL_ID,
                                video=video_input,
                                caption=caption,
                                parse_mode="HTML",
                                supports_streaming=True,
                                **kwargs,
                            )

                            mark_video_sent(video_id, account)
                            logger.info(f"Видео {video_id} отправлено.")

                        except exceptions.TelegramRetryAfter as e:
                            logger.warning(
                                f"Лимит Telegram. Ждем {e.retry_after} сек."
                            )
                            await asyncio.sleep(e.retry_after)
                            cleanup_files(video_id)
                            continue
                        except Exception as e:
                            logger.error(f"Ошибка отправки: {e}")
                        finally:
                            cleanup_files(video_id)
                            await asyncio.sleep(DELAY_BETWEEN_UPLOADS)
                    else:
                        logger.error(
                            f"Не удалось скачать видео {video_id}. "
                            f"Отмечаем как неудачное."
                        )
                        mark_video_failed(video_id)
                        cleanup_files(video_id)

            logger.info(f"Пауза {CHECK_INTERVAL} сек.")
            await asyncio.sleep(CHECK_INTERVAL)
    except asyncio.CancelledError:
        logger.info("Цикл скачивания видео остановлен.")

if __name__ == "__main__":
    import sys
    
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
    
    # Всегда выходим с кодом 1, чтобы start.sh перезапустил бота
    # Для полной остановки используйте kill (SIGTERM)
    logger.info("Перезапуск бота...")
    sys.exit(1)
