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
from datetime import datetime, timedelta
import urllib.request
import math
import requests
from bs4 import BeautifulSoup
import json
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, exceptions
from aiogram.types import (
    FSInputFile, Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, URLInputFile, InputMediaPhoto
)
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003334578127"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "723550550"))  # Ваш ID для управления ссылками
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))
DELAY_BETWEEN_UPLOADS = int(os.getenv("DELAY_BETWEEN_UPLOADS", "20"))
MAX_FILE_SIZE_MB = 49  # Лимит Телеграма для ботов (50 МБ минус запас)
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_cookies.txt")

# --- ЛОГИРОВАНИЕ ---
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file = 'bot.log'

file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

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

def is_account_new(account_url):
    """Проверяет, есть ли уже отправленные видео для этого аккаунта"""
    conn = sqlite3.connect("videos.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_videos WHERE account_url = ? LIMIT 1", (account_url,))
    result = cursor.fetchone()
    conn.close()
    return result is None

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
    """Проверить, слишком ли много попыток скачать видео"""
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
        except Exception:
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
        result = subprocess.run(
            cmd,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            w, h = result.stdout.strip().split(',')
            return int(w), int(h)
    except Exception as e:
        logger.error(f"Ошибка получения размеров {file_path}: {e}")
    return None, None

def generate_thumbnail(video_file, thumb_file):
    """
    Генерация JPEG-превью для видео (макс. 320px по длинной стороне).
    Telegram требует thumbnail <= 320x320, JPEG.
    Без thumbnail iOS-клиент часто неправильно определяет aspect ratio.
    """
    try:
        cmd = [
            'ffmpeg', '-i', video_file,
            '-ss', '00:00:01.000',
            '-vframes', '1',
            '-vf', r'scale=if(gt(iw\,ih)\,320\,-2):if(gt(iw\,ih)\,-2\,320)',
            '-q:v', '3',
            '-y',
            thumb_file
        ]
        result = subprocess.run(
            cmd,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            timeout=30
        )
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
    1. Копируем видео- и аудиопотоки без перекодирования (-c copy).
    2. Устанавливаем movflags +faststart для быстрого стриминга.
    3. Если copy-режим не сработал, делаем минимальный re-encode (CRF 17, preset slow).
    """
    try:
        # Попытка 1: Быстрый ремуксинг без перекодировки
        cmd_copy = [
            "ffmpeg",
            "-i", input_file,
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-y",
            output_file,
        ]

        logger.info(f"Запуск ffmpeg ремуксинг (copy): {input_file} -> {output_file}")
        result = subprocess.run(
            cmd_copy,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0 and os.path.exists(output_file):
            logger.info(f"✅ Видео ремукснуто без потерь: {output_file}")
            return True

        logger.warning(f"Copy-режим не удался, пробуем минимальный re-encode: {result.stderr[:300]}")

        # Попытка 2: Минимальный re-encode с сохранением качества
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

        logger.info(f"Запуск ffmpeg re-encode (CRF 17): {input_file} -> {output_file}")
        result = subprocess.run(
            cmd_reencode,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            timeout=600
        )

        if result.returncode == 0:
            logger.info(f"✅ Видео переобработано с высоким качеством: {output_file}")
            return True

        logger.error(f"❌ Ошибка ffmpeg re-encode: {result.stderr}")
        return False

    except Exception as e:
        logger.error(f"Ошибка при переобработке видео: {e}")
        return False

def compress_video_to_size(input_file, output_file, target_mb=45):
    """
    Сжатие видео до требуемого размера (в МБ) через однопроходное битрейтное кодирование.
    Используется, если видео превышает лимит Telegram в 49 МБ.
    """
    try:
        cmd_dur = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'csv=p=0', input_file
        ]
        res = subprocess.run(
            cmd_dur,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            timeout=10
        )
        dur_str = res.stdout.strip()
        if not dur_str:
            return False
        duration = float(dur_str)
        if duration <= 0:
            return False

        # Расчет целевого видеобитрейта: (размер в килобитах) / duration - аудиобитрейт
        target_bitrate_kbps = int((target_mb * 8192) / duration) - 128
        if target_bitrate_kbps < 200:
            target_bitrate_kbps = 200

        cmd_compress = [
            'ffmpeg', '-i', input_file,
            '-c:v', 'libx264',
            '-b:v', f'{target_bitrate_kbps}k',
            '-maxrate', f'{int(target_bitrate_kbps * 1.5)}k',
            '-bufsize', f'{target_bitrate_kbps * 2}k',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            '-y', output_file
        ]
        result = subprocess.run(
            cmd_compress,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            timeout=300
        )
        return result.returncode == 0 and os.path.exists(output_file)
    except Exception as e:
        logger.error(f"Ошибка сжатия {input_file}: {e}")
        return False

# --- ФУНКЦИИ TIKTOK ---
def get_tiktok_avatar(username):
    """Пытаемся вытащить аватарку аккаунта через парсинг страницы"""
    url = f"https://www.tiktok.com/@{username}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        data_script = soup.find('script', id='__UNIVERSAL_DATA_FOR_REHYDRATION__')
        
        if data_script and data_script.string:
            data = json.loads(data_script.string)
            user_detail = data.get('__DEFAULT_SCOPE__', {}).get('webapp.user-detail', {})
            user_info = user_detail.get('userInfo', {}).get('user', {})
            avatar_url = user_info.get('avatarLarger') or user_info.get('avatarMedium') or user_info.get('avatarThumb')
            return avatar_url
    except Exception as e:
        logger.error(f"Ошибка получения аватарки {username}: {e}")
    return None

def download_tiktok_photos(video_url, video_id):
    """
    Скачивание фото для постов-каруселей (photomode / слайдшоу) TikTok.
    Возвращает структуру со списком локальных путей к изображениям или None.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(video_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html)
        if not m:
            return None
        data = json.loads(m.group(1))
        detail = data.get('__DEFAULT_SCOPE__', {}).get('webapp.video-detail', {})
        item_struct = detail.get('itemInfo', {}).get('itemStruct', {})

        image_post = item_struct.get('imagePost') or item_struct.get('imagePostInfo')
        if not image_post:
            return None

        images = image_post.get('images', [])
        if not images:
            return None

        downloaded_images = []
        # Telegram поддерживает до 10 фотографий в одной медиагруппе
        for idx, img in enumerate(images[:10]):
            urls = img.get('imageURL', {}).get('urlList', [])
            if not urls:
                continue
            img_url = urls[0]
            img_path = f"{video_id}_img_{idx}.jpg"
            try:
                img_req = urllib.request.Request(img_url, headers=headers)
                with urllib.request.urlopen(img_req, timeout=15) as img_resp:
                    with open(img_path, 'wb') as f:
                        f.write(img_resp.read())
                if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                    downloaded_images.append(img_path)
            except Exception as e:
                logger.error(f"Не удалось сохранить фото {img_url}: {e}")

        if not downloaded_images:
            return None

        author = (
            item_struct.get('author', {}).get('nickname')
            or item_struct.get('author', {}).get('uniqueId')
            or "TikTok"
        )
        desc = item_struct.get('desc', '')
        ts = item_struct.get('createTime')
        clean_title = clean_hashtags(desc)
        final_date = format_timestamp(ts, None)

        return {
            'images': downloaded_images,
            'title': clean_title,
            'date': final_date,
            'author': author
        }
    except Exception as e:
        logger.error(f"Ошибка загрузки фотопоста {video_id}: {e}")
        return None

def get_channel_videos(url, is_new=False):
    """
    Получение списка видеороликов с канала.
    Для регулярных проверок: ограничение плейлиста 30 видео (быстро, ~2-4 сек).
    Для новых аккаунтов: считываем до 150 видеороликов с увеличенным таймаутом.
    Используется curl_cffi impersonate Chrome, чтобы обойти блокировки TikTok без cookies.
    """
    limit = 150 if is_new else 30
    timeout_sec = 240 if is_new else 60

    cookies_args = []
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        cookies_args = ["--cookies", COOKIES_FILE]

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--dump-json",
        "--flat-playlist",
        "--playlist-end", str(limit),
        "--ignore-errors",
        "--quiet",
        "--impersonate", "chrome",
        *cookies_args,
        url
    ]
    try:
        result = subprocess.run(
            cmd,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            timeout=timeout_sec
        )
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
        logger.error(f"Таймаут ({timeout_sec}с) при получении видео для {url}")
        return []
    except Exception as e:
        logger.error(f"Ошибка выполнения yt-dlp для {url}: {e}")
        return []

def process_and_download(video_url, video_id):
    """
    Скачивание и обработка публикации TikTok (видео или фото-карусель).
    """
    cleanup_files(video_id)
    filename_template = f"{video_id}.%(ext)s"
    temp_file = f"{video_id}_temp.mp4"
    final_file = f"{video_id}.mp4"
    thumb_file = f"{video_id}_thumb.jpg"

    cookies_args = []
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        cookies_args = ["--cookies", COOKIES_FILE]

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo+bestaudio/best",
        "-o", filename_template,
        "--merge-output-format", "mp4",
        "--quiet",
        "--no-playlist",
        "--impersonate", "chrome",
        *cookies_args,
        "--dump-json",
        "--no-simulate",
        video_url
    ]

    try:
        result = subprocess.run(
            cmd,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            capture_output=True,
            text=True,
            timeout=300
        )

        info = {}
        for line in result.stdout.strip().split('\n'):
            if line.startswith('{'):
                try:
                    info = json.loads(line)
                except json.JSONDecodeError:
                    pass

        # Вариант 1: Успешно скачалось видео
        if os.path.exists(final_file):
            os.rename(final_file, temp_file)

            if reprocess_video_ffmpeg(temp_file, final_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
            else:
                logger.warning(f"ffmpeg не сработал, отправляем оригинальное видео {video_id}")
                os.rename(temp_file, final_file)

            generate_thumbnail(final_file, thumb_file)

            raw_title = info.get('title', '') or info.get('description', '')
            clean_title = clean_hashtags(raw_title)
            author = (
                info.get('uploader') or info.get('channel') or "TikTok"
            )
            ts = info.get('timestamp')
            d_str = info.get('upload_date')
            final_date = format_timestamp(ts, d_str)

            return {
                'type': 'video',
                'file_path': final_file,
                'title': clean_title,
                'date': final_date,
                'author': author
            }

        # Вариант 2: Проверяем на фотопост (фотокарусель / слайдшоу)
        logger.info(f"Видео {video_id} не обнаружено, проверяем на фотопост (photomode)...")
        photo_data = download_tiktok_photos(video_url, video_id)
        if photo_data:
            logger.info(f"✅ Фотопост {video_id} успешно загружен ({len(photo_data['images'])} фото)")
            return {
                'type': 'photos',
                'images': photo_data['images'],
                'title': photo_data['title'],
                'date': photo_data['date'],
                'author': photo_data['author']
            }

        logger.error(f"Публикация {video_id} не была загружена. Stderr: {result.stderr[:200]}")

    except subprocess.TimeoutExpired:
        logger.error(f"Таймаут (300с) при скачивании публикации {video_url}")
    except Exception as e:
        logger.error(f"Ошибка скачивания {video_url}: {e}")

    return None

# --- ЛОГИКА СКАЧИВАНИЯ И ОТПРАВКИ ПУБЛИКАЦИЙ ---
async def process_single_account(bot: Bot, account: str, is_new: bool = False):
    """
    Обработать один аккаунт:
    получить список видео, отфильтровать уже отправленные, скачать и отправить в канал.
    """
    logger.info(f"Проверяем: {account} (новый={is_new})")
    videos = await asyncio.to_thread(get_channel_videos, account, is_new)

    if not videos:
        fails, notified = await asyncio.to_thread(record_account_failure, account)
        logger.warning(
            f"Нет видео для {account} (подряд неудач: {fails}, уведомлено: {notified})"
        )
        if fails >= 10 and not notified:
            await notify_admin_account_down(bot, account)
            await asyncio.to_thread(mark_account_notified, account)
        return

    # Видео получены — сбрасываем счётчик неудач для аккаунта
    await asyncio.to_thread(record_account_success, account)

    # Хронологический порядок: от старых к новым
    videos_sorted = list(reversed(videos))

    for entry in videos_sorted:
        video_id = entry.get("id")
        video_url = entry.get("url")

        if not video_id or not video_url:
            continue

        if is_video_sent(video_id):
            continue

        # Пропускаем видео, которые ранее не скачались 3+ раза
        if is_video_failed(video_id):
            continue

        logger.info(f"Обрабатываю публикацию: {video_id}")

        result = await asyncio.to_thread(process_and_download, video_url, video_id)

        if result:
            post_type = result.get('type')
            title = result.get('title', '')
            date_str = result.get('date', '')
            author = result.get('author', 'TikTok')

            caption = (
                f"📅 <b>{date_str}</b>\n"
                f"{title}\n\n"
                f"👤 Канал: <b>{author}</b>\n"
                f"Источник: <a href='{video_url}'>TikTok</a>"
            )

            try:
                if post_type == 'video':
                    file_path = result.get('file_path')
                    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

                    if file_size_mb > MAX_FILE_SIZE_MB:
                        logger.warning(
                            f"Файл {file_path} ({file_size_mb:.2f} MB) превышает {MAX_FILE_SIZE_MB} MB. Сжимаем видео..."
                        )
                        compressed_file = f"{video_id}_comp.mp4"
                        if compress_video_to_size(file_path, compressed_file, target_mb=45):
                            try:
                                os.remove(file_path)
                            except OSError:
                                pass
                            file_path = compressed_file
                            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                            logger.info(f"✅ Видео сжато до {file_size_mb:.2f} MB")
                        else:
                            logger.warning(f"Не удалось сжать {file_path}. Пропускаем.")
                            cleanup_files(video_id)
                            mark_video_sent(video_id, account)
                            continue

                    video_input = FSInputFile(file_path)
                    w, h = await asyncio.to_thread(get_video_dimensions, file_path)
                    kwargs = {}
                    if w and h:
                        kwargs["width"] = w
                        kwargs["height"] = h

                    thumb_path = f"{video_id}_thumb.jpg"
                    if os.path.exists(thumb_path):
                        kwargs["thumbnail"] = FSInputFile(thumb_path)

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

                elif post_type == 'photos':
                    images = result.get('images', [])
                    if len(images) == 1:
                        await bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=FSInputFile(images[0]),
                            caption=caption,
                            parse_mode="HTML"
                        )
                    else:
                        media = [
                            InputMediaPhoto(
                                media=FSInputFile(img),
                                caption=caption if idx == 0 else None,
                                parse_mode="HTML"
                            )
                            for idx, img in enumerate(images[:10])
                        ]
                        await bot.send_media_group(chat_id=CHANNEL_ID, media=media)

                    mark_video_sent(video_id, account)
                    logger.info(f"Фотопост {video_id} отправлен.")

            except exceptions.TelegramRetryAfter as e:
                logger.warning(f"Лимит Telegram. Ждем {e.retry_after} сек.")
                await asyncio.sleep(e.retry_after)
                cleanup_files(video_id)
                continue
            except Exception as e:
                logger.error(f"Ошибка отправки публикации {video_id}: {e}")
            finally:
                cleanup_files(video_id)
                await asyncio.sleep(DELAY_BETWEEN_UPLOADS)
        else:
            logger.error(f"Не удалось скачать публикацию {video_id}. Отмечаем как неудачную.")
            mark_video_failed(video_id)
            cleanup_files(video_id)

async def check_videos(bot: Bot, dp: Dispatcher):
    """Основной периодический цикл проверки всех аккаунтов"""
    try:
        while True:
            accounts = await asyncio.to_thread(get_all_accounts)

            for account in accounts:
                is_new = await asyncio.to_thread(is_account_new, account)
                await process_single_account(bot, account, is_new=is_new)

            logger.info(f"Пауза {CHECK_INTERVAL} сек.")
            await asyncio.sleep(CHECK_INTERVAL)
    except asyncio.CancelledError:
        logger.info("Цикл скачивания видео остановлен.")

async def daily_db_backup(bot: Bot):
    """Отправляет бэкап базы данных каждый день в 5 утра."""
    try:
        while True:
            now = datetime.now()
            target_time = now.replace(hour=5, minute=0, second=0, microsecond=0)
            if now >= target_time:
                target_time += timedelta(days=1)

            sleep_seconds = (target_time - now).total_seconds()
            logger.info(f"Запланирована отправка бэкапа БД через {sleep_seconds/3600:.2f} часов (в {target_time.strftime('%Y-%m-%d %H:%M:%S')})")

            await asyncio.sleep(sleep_seconds)

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

            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("Задача бэкапа остановлена.")

# --- КЛАВИАТУРА И МЕНЮ БОТА ---
async def get_main_keyboard():
    kb = [
        [KeyboardButton(text="➕ Добавить ссылку"), KeyboardButton(text="📋 Список")],
        [KeyboardButton(text="📊 Статистика")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

async def handle_manage_links(message_or_callback, page: int = 0, force_new_message: bool = False):
    """Показать список для управления ссылками с пагинацией."""
    if isinstance(message_or_callback, Message):
        user_id = message_or_callback.from_user.id
        event = message_or_callback
    else:
        user_id = message_or_callback.from_user.id
        event = message_or_callback.message

    if user_id != ADMIN_ID:
        me = await message_or_callback.bot.get_me()
        if user_id != me.id:
            if isinstance(message_or_callback, Message):
                await message_or_callback.answer("❌ Доступ запрещен.")
            else:
                await message_or_callback.answer("❌ Доступ запрещен.", show_alert=True)
            return

    accounts = get_all_accounts()
    ITEMS_PER_PAGE = 10
    total_pages = math.ceil(len(accounts) / ITEMS_PER_PAGE) if accounts else 1

    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1

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
    if accounts:
        text += "Нажмите на кнопку с крестиком, чтобы удалить аккаунт из отслеживания:"
    else:
        text += "📝 Список аккаунтов пуст."

    if isinstance(message_or_callback, Message) or force_new_message:
        await event.answer(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")
    else:
        await message_or_callback.message.edit_text(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")

async def handle_confirm_delete(callback: CallbackQuery):
    """Показать аватарку и запросить подтверждение удаления."""
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

    await callback.message.answer(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")

async def handle_delete_callback(callback: CallbackQuery):
    """Обработка окончательного удаления аккаунта."""
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

    await callback.message.delete()
    await handle_manage_links(callback.message, page=0, force_new_message=True)

async def handle_cancel_delete(callback: CallbackQuery):
    """Отмена удаления."""
    await callback.message.delete()
    await handle_manage_links(callback.message, page=0, force_new_message=True)

async def handle_add_request(message: Message):
    """Запрос на добавление ссылки."""
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📝 Отправьте ссылку на аккаунт TikTok в формате:\n"
        "https://www.tiktok.com/@username"
    )

async def handle_text_input(message: Message):
    """Обработка ввода текста админом."""
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
            f"🎬 Успешно отправлено публикаций: <b>{total_sent}</b>"
        )
        await message.answer(stats_text, parse_mode="HTML")
        return

    if text.startswith('/'):
        return

    if not text.startswith('https://www.tiktok.com/@'):
        return

    if add_account(text):
        await message.answer(
            f"✅ Ссылка добавлена: {text}\n"
            "🚀 Запускаю скачивание последних публикаций в канал..."
        )
        logger.info(f"Добавлена новая ссылка: {text}. Запуск немедленной обработки...")
        asyncio.create_task(process_single_account(message.bot, text, is_new=True))
    else:
        await message.answer(f"⚠️ Ссылка уже существует: {text}")

async def notify_admin_account_down(bot: Bot, account_url: str):
    """Отправить уведомление админу в ЛС о недоступной ссылке."""
    username = account_url.replace("https://www.tiktok.com/@", "")
    text = (
        f"⚠️ <b>Ссылка недоступна</b>\n\n"
        f"Аккаунт <a href='{account_url}'>@{username}</a> "
        f"не отвечает 10 проверок подряд.\n\n"
        f"Возможные причины:\n"
        f"• Аккаунт заблокирован или удален\n"
        f"• Неверный URL\n"
        f"• Временные ограничения TikTok\n\n"
        f"Проверьте ссылку и удалите через меню бота, если она больше не актуальна."
    )
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
        logger.info(f"Уведомление админу: аккаунт {account_url} недоступен")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админу: {e}")

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

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
        await handle_text_input(message)

    init_db()
    logger.info("Бот запущен v8.0 (Impersonate Chrome + Фотопосты/карусели + Сжатие видео + Ускоренный опрос)")

    polling_task = asyncio.create_task(dp.start_polling(bot))
    video_check_task = asyncio.create_task(check_videos(bot, dp))
    backup_task = asyncio.create_task(daily_db_backup(bot))

    try:
        await polling_task
    finally:
        logger.info("Останавливаем задачи...")
        video_check_task.cancel()
        backup_task.cancel()
        try:
            await asyncio.gather(video_check_task, backup_task)
        except asyncio.CancelledError:
            logger.info("Задачи остановлены.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")

    logger.info("Перезапуск бота...")
    sys.exit(1)
