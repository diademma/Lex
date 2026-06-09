# --- START OF FILE safe_mode.py ---

import sys
import os
import shutil
import zipfile
import traceback
import logging
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command
import config
import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("lex.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

LAST_CRASH = ""


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        return sys.__excepthook__(exc_type, exc_value, exc_traceback)
    global LAST_CRASH
    LAST_CRASH = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    logging.critical(f"КРИТИЧЕСКАЯ ОШИБКА ЯДРА!\n{LAST_CRASH}")
    database.log_error(LAST_CRASH)


sys.excepthook = handle_exception

safe_router = Router()


@safe_router.message(Command("start", "error", "rollback", "rollback_step"))
async def safe_commands(message: Message):
    if not database.is_owner(message.from_user.id):
        return await message.answer("🛠 Система на техобслуживании.")

    if message.text.startswith("/rollback_step"):
        files = os.listdir(config.MICRO_BACKUP_DIR)
        if not files:
            return await message.answer("❌ Микро-бэкапов нет. Используй ZIP-бэкап.")
        for f in files:
            dest_path = f.replace("__SLSH__", "/")
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copyfile(os.path.join(config.MICRO_BACKUP_DIR, f), dest_path)
        await message.answer("✅ Откат последнего патча завершен! Перезапускаю...")
        os._exit(0)

    safe_text = (
        "🚨 <b>КРИТИЧЕСКАЯ ОШИБКА! СИСТЕМА В БЕЗОПАСНОМ РЕЖИМЕ.</b>\n\n"
        "1. <code>/rollback_step</code> — Откатить последний патч.\n"
        "2. Отправь исправленный <code>.zip</code> бэкап, и я распакую его.\n\n"
        f"<b>Ошибка Python:</b>\n<pre>{LAST_CRASH[-3000:]}</pre>"
    )
    await message.answer(safe_text, parse_mode="HTML")


@safe_router.message(F.document)
async def safe_doc(message: Message):
    if not database.is_owner(message.from_user.id):
        return
    if not message.document.file_name.endswith(".zip"):
        return await message.answer("В Safe Mode принимаются только .zip бэкапы!")

    zip_path = "rollback_safe.zip"
    file = await message.bot.get_file(message.document.file_id)
    await message.bot.download_file(file.file_path, zip_path)
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(".")
        os.remove(zip_path)
        await message.answer("✅ Откат из ZIP завершен! Перезапускаю...")
        os._exit(0)
    except Exception as e:
        await message.answer(f"❌ Ошибка распаковки: {e}")
