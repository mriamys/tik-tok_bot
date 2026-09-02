#!/usr/bin/env python3
"""
Скрипт для получения cookies от TikTok через реальный браузер Chromium (Playwright).
Cookies сохраняются в формате Netscape для передачи в yt-dlp через --cookies.
"""
import sys
import asyncio
import logging
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

COOKIES_FILE = Path(__file__).parent / "tiktok_cookies.txt"


def write_netscape_cookies(cookies: list, filepath: Path):
    """Сохраняет cookies в формате Netscape (совместим с yt-dlp и curl)."""
    lines = ["# Netscape HTTP Cookie File", "# https://curl.haxx.se/rfc/cookie_spec.html", ""]
    for c in cookies:
        domain = c.get("domain", "")
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expires = int(c.get("expires", 0) or 0)
        if expires <= 0:
            expires = int(time.time()) + 86400 * 365
        name = c.get("name", "")
        value = c.get("value", "")
        if name:
            lines.append(f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    
    filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"Сохранено cookies в {filepath}")


async def fetch_tiktok_cookies():
    from playwright.async_api import async_playwright
    
    logger.info("Запуск браузера Chromium...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-gpu",
            ]
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        
        page = await context.new_page()
        
        logger.info("Открываем TikTok...")
        try:
            await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        
        logger.info("Ждём загрузки JS и cookies (15 сек)...")
        await asyncio.sleep(15)
        
        await page.evaluate("window.scrollBy(0, 300)")
        await asyncio.sleep(2)
        await page.evaluate("window.scrollBy(0, 200)")
        await asyncio.sleep(1)
        
        cookies = await context.cookies()
        await browser.close()
        
        if not cookies:
            logger.error("Cookies не получены!")
            return False
        
        logger.info(f"Получено {len(cookies)} cookies")
        tiktok_cookies = [c for c in cookies if "tiktok" in c.get("domain", "").lower()]
        logger.info(f"TikTok cookies: {len(tiktok_cookies)}")
        
        write_netscape_cookies(tiktok_cookies if tiktok_cookies else cookies, COOKIES_FILE)
        return True


def main():
    try:
        result = asyncio.run(fetch_tiktok_cookies())
        if result:
            logger.info(f"Cookies сохранены: {COOKIES_FILE}")
            sys.exit(0)
        else:
            logger.error("Не удалось получить cookies")
            sys.exit(1)
    except ImportError:
        logger.error("Playwright не установлен.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
