import os
import re
import uuid
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile
)
from aiogram.enums import ChatAction, ParseMode
from aiogram.utils.chat_action import ChatActionSender
from aiogram.client.default import DefaultBotProperties
from aiohttp import web
import yt_dlp

# ==========================================
# ⚙️ НАСТРОЙКИ КОНФИГУРАЦИИ
# ==========================================
# На Render токен берется из Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "8662035278:AAHCLiCaQl8ssoJ8TBiDPWsh_PXt9WgGAuA")

# Лимит Telegram Bot API на отправку файлов (50 МБ)
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

# Базовая директория скрипта
BASE_DIR = Path(__file__).resolve().parent

# Папка для временных загрузок
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Файл куки (если есть, используется для обхода блокировок Instagram и YouTube)
COOKIES_FILE = BASE_DIR / "cookies.txt"

# Автоматическая загрузка куки из переменной окружения (для хостинга Render / Docker)
env_cookies = os.getenv("INSTAGRAM_COOKIES")
if env_cookies:
    COOKIES_FILE.write_text(env_cookies.strip(), encoding="utf-8")

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ==========================================
# 🔍 РЕГУЛЯРНЫЕ ВЫРАЖЕНИЯ И НОРМАЛИЗАЦИЯ
# ==========================================
YOUTUBE_REGEX = re.compile(
    r'(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w-]+',
    re.IGNORECASE
)
INSTAGRAM_REGEX = re.compile(
    r'(https?://)?(www\.)?(instagram\.com|instagr\.am)/(reel|reels|p|tv|share/(?:reel|r|p))/([a-zA-Z0-9_\-]+)',
    re.IGNORECASE
)
TIKTOK_REGEX = re.compile(
    r'(https?://)?((www|vm|vt)\.)?tiktok\.com/(@[\w.-]+/video/\d+|t/[\w-]+|[\w-]+)',
    re.IGNORECASE
)


def normalize_instagram_url(text: str) -> str:
    """
    Извлекает ID публикации и приводит любую ссылку Instagram
    (включая mobile share /reels/, /share/reel/ и параметры ?igsh=)
    к стандартному виду, который гарантированно распознает yt-dlp.
    """
    match = INSTAGRAM_REGEX.search(text)
    if match:
        code = match.group(5)
        return f"https://www.instagram.com/reel/{code}/"
    return text

# Кэш ссылок для inline-кнопок (так как лимит callback_data в Telegram — 64 байта)
PENDING_URLS: Dict[str, str] = {}


# ==========================================
# 📥 МОДУЛЬ ЗАГРУЗКИ (YT-DLP & FFMPEG)
# ==========================================
class DownloadError(Exception):
    """Исключение при ошибках парсинга и загрузки."""
    pass


class FileTooLargeError(Exception):
    """Исключение при превышении лимита 50 МБ."""
    pass


def get_ydl_options(output_template: str, format_type: str = "best") -> Dict[str, Any]:
    """Генерация параметров для yt-dlp с адаптацией под наличие ffmpeg."""
    base_opts: Dict[str, Any] = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        # Заголовки реального браузера для предотвращения блокировок Instagram/Meta
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Sec-Fetch-Mode": "navigate",
        },
    }

    # Проверяем, установлен ли ffmpeg в системе или лежит ffmpeg.exe в папке с ботом
    local_ffmpeg = BASE_DIR / "ffmpeg.exe"
    has_ffmpeg = bool(shutil.which("ffmpeg") or local_ffmpeg.exists())
    if local_ffmpeg.exists():
        base_opts["ffmpeg_location"] = str(local_ffmpeg)

    # Если рядом со скриптом лежит cookies.txt, автоматически используем его
    if COOKIES_FILE.exists():
        base_opts["cookiefile"] = str(COOKIES_FILE)

    if format_type == "mp3":
        if has_ffmpeg:
            base_opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            # Если ffmpeg не установлен, скачиваем нативную аудиодорожку (m4a/aac) без конвертации
            base_opts.update({
                "format": "bestaudio[ext=m4a]/bestaudio/best",
            })
    elif format_type == "720p":
        if has_ffmpeg:
            base_opts.update({
                "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
                "merge_output_format": "mp4",
            })
        else:
            base_opts.update({
                "format": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
            })
    elif format_type == "1080p":
        if has_ffmpeg:
            base_opts.update({
                "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
                "merge_output_format": "mp4",
            })
        else:
            base_opts.update({
                "format": "best[ext=mp4]/best",
            })
    else:
        # Для TikTok и Instagram: скачиваем готовое видео со звуком напрямую БЕЗ запроса склейки через ffmpeg
        base_opts.update({
            "format": "best[ext=mp4]/best",
        })

    return base_opts


def sync_download_media(url: str, format_type: str = "best") -> Path:
    """
    Синхронная функция скачивания через yt-dlp.
    Запускается в пуле потоков через asyncio.to_thread.
    """
    unique_id = uuid.uuid4().hex
    out_tmpl = str(DOWNLOAD_DIR / f"{unique_id}_%(title).50s.%(ext)s")
    opts = get_ydl_options(output_template=out_tmpl, format_type=format_type)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

            downloaded_files = list(DOWNLOAD_DIR.glob(f"{unique_id}_*"))
            if not downloaded_files:
                raise DownloadError("Файл не найден на диске после завершения загрузки.")

            file_path = downloaded_files[0]
            file_size = file_path.stat().st_size

            if file_size > MAX_FILE_SIZE_BYTES:
                if file_path.exists():
                    file_path.unlink()
                raise FileTooLargeError(
                    f"Размер файла ({file_size // (1024 * 1024)} МБ) превышает лимит Telegram (50 МБ)."
                )

            return file_path

    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e).lower()
        if "private" in err_msg or "login" in err_msg or "401" in err_msg or "checkpoint" in err_msg:
            raise DownloadError(
                "Видео приватное или требует авторизации Instagram.\n\n"
                "💡 <i>Instagram (Meta) часто блокирует анонимные запросы. "
                "Если это публичное видео, положите файл cookies.txt от Instagram в папку с ботом для обхода блокировок.</i>"
            )
        elif "copyright" in err_msg:
            raise DownloadError("Видео заблокировано правообладателем.")
        elif "max-filesize" in err_msg:
            raise FileTooLargeError("Размер видео превышает лимит в 50 МБ.")
        else:
            raise DownloadError(f"Ошибка yt-dlp: {e}")
    except Exception as e:
        if isinstance(e, (DownloadError, FileTooLargeError)):
            raise
        raise DownloadError(f"Ошибка при обработке медиа: {e}")


# ==========================================
# 🤖 ЛОГИКА TELEGRAM БОТА (AIOGRAM 3.X)
# ==========================================
dp = Dispatcher()


def get_youtube_keyboard(url_id: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора качества для YouTube."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 720p", callback_data=f"yt:720p:{url_id}"),
                InlineKeyboardButton(text="🎬 1080p", callback_data=f"yt:1080p:{url_id}")
            ],
            [
                InlineKeyboardButton(text="🎵 Аудио (MP3)", callback_data=f"yt:mp3:{url_id}")
            ]
        ]
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = (
        "👋 <b>Привет! Я бот для загрузки медиа.</b>\n\n"
        "Поддерживаемые платформы:\n"
        "• <b>YouTube</b> (видео, Shorts, аудио в MP3)\n"
        "• <b>Instagram</b> (Reels, посты)\n"
        "• <b>TikTok</b> (видео без водяного знака)\n\n"
        "🔗 <i>Отправьте ссылку на видео для начала скачивания.</i>"
    )
    await message.answer(welcome_text)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>Инструкция:</b>\n"
        "1. Скопируйте ссылку на YouTube, Instagram или TikTok.\n"
        "2. Отправьте её в чат.\n"
        "3. Дождитесь загрузки и отправки файла.\n\n"
        "⚠️ <i>Максимальный размер видео для отправки ботом — 50 МБ (ограничение Telegram).</i>"
    )
    await message.answer(help_text)


async def process_and_send_media(
    bot: Bot,
    chat_id: int,
    url: str,
    format_type: str,
    status_message: Message
):
    """
    Асинхронная обработка: скачивание в отдельном потоке и отправка.
    Локальный файл гарантированно удаляется в блоке finally.
    """
    file_path = None
    try:
        action = ChatAction.UPLOAD_DOCUMENT if format_type == "mp3" else ChatAction.UPLOAD_VIDEO
        async with ChatActionSender(bot=bot, chat_id=chat_id, action=action):
            await status_message.edit_text("⏳ <i>Скачиваю и обрабатываю файл... Пожалуйста, подождите.</i>")

            # Скачивание в отдельном потоке без блокировки Event Loop
            file_path = await asyncio.to_thread(sync_download_media, url, format_type)

            await status_message.edit_text("📤 <i>Отправляю файл в Telegram...</i>")
            input_file = FSInputFile(str(file_path))

            if format_type == "mp3":
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=input_file,
                    caption="🎵 <b>Ваше аудио готово!</b>"
                )
            else:
                await bot.send_video(
                    chat_id=chat_id,
                    video=input_file,
                    caption="🎬 <b>Ваше видео готово!</b>",
                    supports_streaming=True
                )

        await status_message.delete()

    except FileTooLargeError as e:
        await status_message.edit_text(f"❌ <b>Файл слишком большой!</b>\n\n{e}")
    except DownloadError as e:
        await status_message.edit_text(f"❌ <b>Ошибка загрузки:</b>\n\n{e}")
    except Exception as e:
        logger.exception("Непредвиденная ошибка при отправке")
        await status_message.edit_text(f"❌ <b>Произошла ошибка:</b>\n{e}")
    finally:
        # Гарантированное удаление файла с диска
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Файл {file_path} успешно удален с диска.")
            except OSError as err:
                logger.error(f"Не удалось удалить файл {file_path}: {err}")


@dp.callback_query(F.data.startswith("yt:"))
async def handle_youtube_callback(callback: CallbackQuery, bot: Bot):
    """Обработка выбора качества YouTube."""
    await callback.answer()
    _, quality, url_id = callback.data.split(":")
    url = PENDING_URLS.get(url_id)

    if not url:
        await callback.message.edit_text("⚠️ <i>Срок действия ссылки истек. Отправьте её заново.</i>")
        return

    await process_and_send_media(
        bot=bot,
        chat_id=callback.message.chat.id,
        url=url,
        format_type=quality,
        status_message=callback.message
    )
    PENDING_URLS.pop(url_id, None)


@dp.message(F.text)
async def handle_links(message: Message, bot: Bot):
    """Маршрутизация ссылок по платформам."""
    text = message.text.strip()

    # YouTube (включая Shorts)
    yt_match = YOUTUBE_REGEX.search(text)
    if yt_match:
        url = yt_match.group(0)
        url_id = uuid.uuid4().hex[:10]
        PENDING_URLS[url_id] = url
        await message.reply(
            "📹 <b>Найдено видео YouTube!</b>\nВыберите качество:",
            reply_markup=get_youtube_keyboard(url_id)
        )
        return

    # Instagram (Reels, посты)
    insta_match = INSTAGRAM_REGEX.search(text)
    if insta_match:
        url = normalize_instagram_url(text)
        status_msg = await message.reply("📸 <b>Instagram:</b> Скачиваю...")
        await process_and_send_media(
            bot=bot,
            chat_id=message.chat.id,
            url=url,
            format_type="best",
            status_message=status_msg
        )
        return

    # TikTok (без водяных знаков)
    tt_match = TIKTOK_REGEX.search(text)
    if tt_match:
        url = tt_match.group(0)
        status_msg = await message.reply("🎵 <b>TikTok:</b> Скачиваю без водяного знака...")
        await process_and_send_media(
            bot=bot,
            chat_id=message.chat.id,
            url=url,
            format_type="best",
            status_message=status_msg
        )
        return


async def start_health_check_server():
    """
    Легковесный HTTP-сервер для прохождения проверки Render (Health Check).
    Render требует, чтобы веб-сервис слушал порт $PORT.
    """
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running! 🚀"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health-check сервер успешно слушает порт {port} (для Render)")


# ==========================================
# 🚀 ТОЧКА ВХОДА
# ==========================================
async def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВАШ_ТОКЕН_ОТ_BOTFATHER":
        print("❌ ОШИБКА: Укажите токен бота в переменной BOT_TOKEN или в файле .env!")
        return

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    # Запускаем фоновый веб-сервер для Render Health Check
    try:
        await start_health_check_server()
    except Exception as e:
        logger.warning(f"Не удалось запустить health-check сервер: {e}")

    logger.info("Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
