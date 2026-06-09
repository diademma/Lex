# --- START OF FILE telethon_shadow.py ---
# Тень LEX — Telethon userbot.
# Здесь НЕТ выполнения кода ИИ. Тень читает каналы и выполняет конкретные
# задачи, которые ставят зарегистрированные модули через TaskBus.

import asyncio
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ChannelPrivateError, UsernameNotOccupiedError
import config
from task_bus import TaskBus

client = TelegramClient(StringSession(config.STRING_SESSION), config.API_ID, config.API_HASH)


async def shadow_worker():
    """
    Основной воркер Тени — читает задачи из TaskBus и выполняет их.
    Поддерживаемые action:
      - fetch_channel_messages: прочитать N последних сообщений из канала.
    """
    logging.info("[Shadow] Тень проснулась и готова к работе...")
    if not client.is_connected():
        await client.connect()

    while True:
        task = await TaskBus.get_shadow_task()
        task_id = task["id"]
        action  = task["action"]
        payload = task["payload"]

        try:
            if action == "fetch_channel_messages":
                result = await _fetch_channel_messages(
                    channel=payload["channel"],
                    limit=payload.get("limit", 20),
                )
                await TaskBus.send_to_face(task_id, "success", result)

            else:
                await TaskBus.send_to_face(task_id, "error", f"Неизвестный action: {action}")

        except FloodWaitError as e:
            logging.warning(f"[Shadow] FloodWait {e.seconds}s для задачи {task_id}")
            await asyncio.sleep(e.seconds)
            await TaskBus.send_to_face(task_id, "error", f"FloodWait: {e.seconds}s")

        except (ChannelPrivateError, UsernameNotOccupiedError) as e:
            logging.warning(f"[Shadow] Недоступный канал в задаче {task_id}: {e}")
            await TaskBus.send_to_face(task_id, "error", f"Канал недоступен: {e}")

        except Exception as e:
            logging.error(f"[Shadow] Ошибка задачи {task_id}: {e}")
            await TaskBus.send_to_face(task_id, "error", str(e))

        finally:
            TaskBus.mark_shadow_task_done()


async def _fetch_channel_messages(channel: str, limit: int = 20) -> list[dict]:
    """
    Получить последние `limit` сообщений из канала.
    Возвращает список словарей, совместимых с reposter_worker.
    """
    entity = await client.get_entity(channel)
    messages = []

    async for msg in client.iter_messages(entity, limit=limit):
        # Пропускаем сервисные и пустые
        if msg.action is not None:
            continue

        # Собираем entities для очистки рекламы
        entities_raw = []
        if msg.entities:
            for e in msg.entities:
                entities_raw.append({
                    "type": type(e).__name__,
                    "offset": e.offset,
                    "length": e.length,
                })

        # Медиа
        media_info = None
        if msg.photo:
            media_info = {"kind": "photo", "id": msg.photo.id, "msg_id": msg.id, "channel": channel}
        elif msg.video:
            media_info = {"kind": "video", "id": msg.video.id, "msg_id": msg.id, "channel": channel}
        elif msg.document:
            media_info = {"kind": "document", "id": msg.document.id, "msg_id": msg.id, "channel": channel}

        messages.append({
            "id":       msg.id,
            "text":     msg.text or msg.message or "",
            "date":     msg.date.timestamp() if msg.date else 0,
            "entities": entities_raw,
            "media":    media_info,
            "grouped":  msg.grouped_id,
        })

    return messages


async def forward_message_as_copy(channel: str, msg_id: int, target_chat_id: int, caption: str = "") -> bool:
    """
    Переслать медиа-сообщение из канала в целевой чат от имени бота.
    Используется reposter_worker напрямую (без TaskBus — оба в одном процессе).
    """
    try:
        entity = await client.get_entity(channel)
        msg = await client.get_messages(entity, ids=msg_id)
        if msg is None:
            return False
        # Пересылаем без forward-шапки
        await client.forward_messages(target_chat_id, msg.id, entity, drop_author=True)
        return True
    except Exception as e:
        logging.error(f"[Shadow.forward] Ошибка пересылки {channel}#{msg_id}: {e}")
        return False
