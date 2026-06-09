# --- START OF FILE task_bus.py ---
# Шина задач между Лицом (aiogram) и Тенью (Telethon).

import asyncio
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("lex.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

shadow_queue = asyncio.Queue()
face_queue   = asyncio.Queue()
_results_store: dict = {}


class TaskBus:
    """Глобальная шина задач для связи aiogram и Telethon."""

    @staticmethod
    async def send_to_shadow(action: str, payload: dict) -> str:
        task_id = f"task_{int(time.time() * 1000)}"
        task = {"id": task_id, "action": action, "payload": payload}
        await shadow_queue.put(task)
        logging.info(f"[TaskBus] 📥 Задача {task_id} → Тень (action={action})")
        return task_id

    @staticmethod
    async def get_shadow_task():
        return await shadow_queue.get()

    @staticmethod
    def mark_shadow_task_done():
        shadow_queue.task_done()

    @staticmethod
    async def send_to_face(task_id: str, status: str, result):
        _results_store[task_id] = {"status": status, "result": result}
        await face_queue.put({"id": task_id, "status": status, "result": result})
        logging.info(f"[TaskBus] 📤 Результат {task_id} → Лицо (status={status})")

    @staticmethod
    async def wait_for_result(task_id: str, timeout: int = 60) -> dict:
        start = time.time()
        while time.time() - start < timeout:
            if task_id in _results_store:
                return _results_store.pop(task_id)
            await asyncio.sleep(0.5)
        return {"status": "error", "result": "Таймаут: Тень не ответила вовремя."}
