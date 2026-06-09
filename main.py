# --- START OF FILE main.py ---

import asyncio
import logging
import sys
import os
import importlib.util
import config
import database

# 1. База данных — первым делом
database.init_db()

# 2. Safe Mode
import safe_mode

# 3. Ядро
try:
    from aiogram_face import dp, bot
    from telethon_shadow import client, shadow_worker
    from reposter_worker import reposter_worker
except Exception as e:
    logging.critical(f"Ошибка при импорте модулей ядра: {e}")
    raise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("lex.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


async def main():
    logging.info("Подключение Тени к серверам Telegram...")
    try:
        await client.connect()
    except Exception as e:
        logging.error(f"Не удалось подключить юзербот: {e}")

    # ── Динамическая загрузка плагинов ────────────────────────────────────
    modules = database.get_all_modules()
    for mod in modules:
        if mod["enabled"]:
            mod_path = os.path.join(config.MODULES_DIR, mod["name"])
            if os.path.exists(mod_path):
                try:
                    spec   = importlib.util.spec_from_file_location(mod["name"][:-3], mod_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if hasattr(module, "router"):
                        dp.include_router(module.router)
                        logging.info(f"✅ Плагин {mod['name']} загружен.")
                except Exception as e:
                    logging.error(f"❌ Ошибка загрузки плагина {mod['name']}: {e}")

    logging.info("🚀 ЗАПУСК ЯДРА LEX...")

    try:
        await bot.send_message(
            config.OWNER_ID,
            "✅ <b>Система LEX успешно перезагружена!</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Уведомление о запуске не отправлено: {e}")

    # ── Запускаем 4 корутины параллельно ──────────────────────────────────
    # - dp.start_polling        → aiogram (Лицо): обрабатывает команды/кнопки
    # - client.run_until_disconnected → Telethon: держит юзер-сессию живой
    #   (нужен для задач shadow_worker, но репостер его не использует)
    # - shadow_worker           → Тень: читает каналы по заданиям TaskBus
    # - reposter_worker(bot)    → Репостер: Bot API, без юзербота
    await asyncio.gather(
        dp.start_polling(bot),
        client.run_until_disconnected(),
        shadow_worker(),
        reposter_worker(bot),
    )


if __name__ == "__main__":
    try:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Система LEX остановлена.")
