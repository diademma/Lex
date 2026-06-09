# --- START OF FILE aiogram_face.py ---

import time
import re
import os
import asyncio
import sqlite3
import uuid
import zipfile
import html
import logging
import base64
import aiohttp
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

import config
import database
from ai_orchestrator import AIOrchestrator          # только resilient_llm_call, без генерации кода
from module_operator import operator_router
from states import AdminState, TagState, LexState, OwnerFSM

bot = Bot(token=config.BOT_TOKEN)
dp  = Dispatcher()
main_router  = Router()
lex_cooldowns = {}

ALLOWED_CHAT_ID = -1002281822286

OWNER_ONLY_COMMANDS = {"backup", "lexlog", "restart", "patch", "update", "patchplug", "plugmenu"}

details_context = {}


# ==========================================
# 0. ГЛОБАЛЬНАЯ ЗАЩИТА И ПРОВЕРКА ДОСТУПА
# ==========================================
class OwnerPresenceMiddleware(BaseMiddleware):
    def __init__(self):
        self.cache      = {}
        self.cache_time = 300

    async def __call__(self, handler, event, data):
        if not getattr(event, "chat", None):
            return await handler(event, data)
        chat_id = event.chat.id
        if chat_id > 0:
            return await handler(event, data)
        now = time.time()
        if chat_id in self.cache and now - self.cache[chat_id] < self.cache_time:
            if not self.cache[chat_id]:
                return
        else:
            try:
                member     = await data["bot"].get_chat_member(chat_id, config.OWNER_ID)
                is_present = member.status not in ["left", "kicked"]
                self.cache[chat_id] = is_present
                if not is_present:
                    return
            except Exception:
                self.cache[chat_id] = False
                return
        return await handler(event, data)


dp.message.middleware(OwnerPresenceMiddleware())


def is_allowed_to_call(user_id: int, chat_id: int) -> bool:
    if chat_id == ALLOWED_CHAT_ID:
        return True
    if database.is_owner(user_id):
        return True
    return False


def is_admin(user_id, username, chat_id):
    if database.is_owner(user_id):
        return True
    admins = database.get_local_admins(chat_id)
    for adm_id, adm_un in admins:
        if adm_id == user_id:
            return True
        if adm_un and username and adm_un.lower() == username.lower():
            return True
    return False


def get_main_menu_kb(user_id, chat_id):
    kb = [
        [InlineKeyboardButton(text="Призыв (Теги)", callback_data="submenu_summon"),
         InlineKeyboardButton(text="Админы чата",   callback_data="submenu_admins")],
        [InlineKeyboardButton(text="📡 Репостер",   callback_data="submenu_reposter")],
    ]
    if database.is_owner(user_id):
        kb.append([InlineKeyboardButton(text="👑 Для Создателя", callback_data="submenu_owner")])
    kb.append([InlineKeyboardButton(text="🔙 Скрыть", callback_data="menu_hide")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def collect_commands(router: Router):
    visited = set()

    def _collect(r: Router):
        for handler in r.message.handlers:
            doc          = handler.callback.__doc__ or "Нет описания."
            doc          = doc.strip().split("\n")[0]
            commands_list = handler.flags.get("commands", [])
            for cmd_filter in commands_list:
                commands_attr = getattr(cmd_filter, "commands", None)
                if commands_attr:
                    for cmd_pattern in commands_attr:
                        if isinstance(cmd_pattern, str):
                            visited.add((cmd_pattern, doc))
                        elif hasattr(cmd_pattern, "command"):
                            visited.add((cmd_pattern.command, doc))
            for filter_wrapper in getattr(handler, "filters", []):
                flt = getattr(filter_wrapper, "filter", None)
                if flt and flt.__class__.__name__ == "Command":
                    commands_attr = getattr(flt, "commands", None)
                    if commands_attr:
                        for cmd_pattern in commands_attr:
                            if isinstance(cmd_pattern, str):
                                visited.add((cmd_pattern, doc))
                            elif hasattr(cmd_pattern, "command"):
                                visited.add((cmd_pattern.command, doc))
        for sub_router in r.sub_routers:
            _collect(sub_router)

    _collect(router)
    return sorted(list(visited), key=lambda x: x[0])


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def md_to_html(text: str) -> str:
    """Markdown → безопасный HTML для Telegram."""
    text = html.escape(text)
    text = re.sub(r"```python(.*?)```", r'<pre><code class="language-python">\1</code></pre>', text, flags=re.DOTALL)
    text = re.sub(r"```(.*?)```",       r"<pre>\1</pre>",                                      text, flags=re.DOTALL)
    text = re.sub(r"`(.*?)`",           r"<code>\1</code>",                                    text)
    text = re.sub(r"\*\*(.*?)\*\*",     r"<b>\1</b>",                                          text)
    text = re.sub(r"\*(.*?)\*",         r"<i>\1</i>",                                          text)
    text = re.sub(r"__(.*?)__",         r"<u>\1</u>",                                          text)
    return text


async def ask_lex_llm(query: str, chat_id: int, reply_message: Message = None) -> tuple[str, str, bool, list]:
    """Вызов LLM с контекстной памятью (до 3 обращений в истории)."""
    conversation_id = None
    history         = []
    should_cleanup  = False

    if reply_message:
        reply_id   = reply_message.message_id
        reply_text = reply_message.text or reply_message.caption or ""
        conversation_id, history = database.get_conversation_by_reply(chat_id, reply_id, reply_text)
        if len(history) >= 4:
            should_cleanup = True

    if not conversation_id:
        conversation_id = str(uuid.uuid4())[:8]
        history         = []

    if len(history) > 6:
        history = history[-6:]

    history_str = ""
    for msg in history:
        role = "Пользователь" if msg["role"] == "user" else "Ты (Лекс)"
        history_str += f"{role}: {msg['content']}\n"

    system_prompt = (
        "Ты — Лекс, высокоинтеллектуальный ИИ-помощник.\n"
        "Тебе предоставлена история переписки с пользователем. "
        "Помни всё, о чём говорилось ранее, и используй это для ответа.\n"
        "Правила:\n"
        "- На простые вопросы — кратко и ёмко.\n"
        "- На сложные — развёрнуто и глубоко.\n"
        "- Форматируй ответ через Markdown (**жирный**, *курсив*)."
    )

    user_prompt = ""
    if history_str:
        user_prompt += "КОНТЕКСТ ПРЕДЫДУЩЕЙ БЕСЕДЫ:\n====\n" + history_str + "====\n\n"
    user_prompt += f"НОВЫЙ ВОПРОС: {query}\nДай ответ, опираясь на контекст выше."

    response = await AIOrchestrator.resilient_llm_call(system_prompt, user_prompt)
    return response, conversation_id, should_cleanup, history


async def resilient_vision_call(user_prompt: str, base64_image: str) -> str:
    """Vision API — понимает изображения."""
    import random
    vision_providers = [
        {"id": "github",     "url": "https://models.inference.ai.azure.com/chat/completions", "model": "gpt-4o-mini"},
        {"id": "groq",       "url": "https://api.groq.com/openai/v1/chat/completions",         "model": "llama-3.2-11b-vision-preview"},
        {"id": "openrouter", "url": "https://openrouter.ai/api/v1/chat/completions",            "model": "google/gemini-2.0-flash-001"},
    ]
    random.shuffle(vision_providers)

    async with aiohttp.ClientSession() as session:
        for prov in vision_providers:
            keys = config.API_KEYS.get(prov["id"], [])
            if not keys:
                continue
            random.shuffle(keys)
            for key in keys:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                payload = {
                    "model": prov["model"],
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                        ],
                    }],
                    "max_tokens": 1024,
                }
                try:
                    async with session.post(prov["url"], headers=headers, json=payload, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["choices"][0]["message"]["content"]
                        else:
                            logging.warning(f"[Vision] Ошибка {resp.status} на {prov['id']}")
                except Exception as e:
                    logging.warning(f"[Vision] Тайм-аут на {prov['id']}: {e}")
                    continue

    return "❌ Ошибка: Все Vision-провайдеры недоступны."


def get_details_kb(query_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✦ Подробнее ✦", callback_data=f"details_{query_id}")
    ]])


# ==========================================
# 1. СЕКРЕТНЫЙ ПРОТОКОЛ ОВНЕРА
# ==========================================
@main_router.message(Command("start"))
async def cmd_start(message: Message):
    """Приветствие и выдача базовых команд."""
    if message.chat.type != "private":
        return
    if database.is_owner(message.from_user.id):
        return await message.answer(
            "👑 <b>С возвращением, Создатель!</b>\n\n"
            "<code>/lexset</code> - Меню системы\n"
            "<code>/lexlog 30</code> - Логи ошибок\n"
            "<code>/backup</code> - Бэкап кода",
            parse_mode="HTML",
        )
    await message.answer("Я система Лекс.\n<tg-spoiler>/getowner</tg-spoiler>", parse_mode="HTML")


@main_router.message(Command("getowner"))
async def cmd_getowner(message: Message, state: FSMContext):
    """Секретный протокол авторизации Овнера."""
    if message.chat.type != "private":
        return
    if database.is_owner(message.from_user.id):
        return await message.answer("Вы уже овнер!")
    await state.set_state(OwnerFSM.q1)
    await message.answer("🔒 <b>Секретный протокол.</b>\nВопрос 1: Каких цифр не хватает: 619#################1005 ?", parse_mode="HTML")


@main_router.message(OwnerFSM.q1)
async def process_q1(message: Message, state: FSMContext):
    if message.text.strip() == "103692210061003":
        await state.set_state(OwnerFSM.q2)
        await message.answer("Вопрос 2: Вести код \"звезда\"")
    else:
        await fail_owner(message, state)


@main_router.message(OwnerFSM.q2)
async def process_q2(message: Message, state: FSMContext):
    if message.text.strip() == "18349276":
        await state.set_state(OwnerFSM.q3)
        await message.answer("Вопрос 3: Вести код \"обратная звезда\"")
    else:
        await fail_owner(message, state)


@main_router.message(OwnerFSM.q3)
async def process_q3(message: Message, state: FSMContext):
    if message.text.strip() == "67294381":
        database.add_owner(message.from_user.id, message.from_user.username)
        await state.clear()
        await message.answer("✅ <b>ДОСТУП РАЗРЕШЕН!</b> Вы получили права Овнера.", parse_mode="HTML")
    else:
        await fail_owner(message, state)


async def fail_owner(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ <b>Неверно!</b>", parse_mode="HTML")


@main_router.callback_query(F.data == "add_owner_btn")
async def cb_add_owner_btn(call: CallbackQuery, state: FSMContext):
    if not database.is_owner(call.from_user.id):
        return
    await state.set_state(OwnerFSM.waiting_for_owner_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="submenu_owner")]])
    await call.message.edit_text(
        "👑 <b>Добавление овнера</b>\nОтправьте ID пользователя.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@main_router.message(OwnerFSM.waiting_for_owner_id)
async def process_add_owner(message: Message, state: FSMContext):
    if message.text.lower() == "отмена":
        return await state.clear()
    if not message.text.isdigit():
        return await message.answer("❌ ID должен состоять только из цифр.")
    database.add_owner(int(message.text), None)
    await state.clear()
    await message.answer(f"✅ Пользователь <code>{message.text}</code> теперь Овнер!", parse_mode="HTML")


@main_router.message(Command("backup"))
async def cmd_backup(message: Message):
    """Создать полный бэкап исходного кода бота (.zip)."""
    if not database.is_owner(message.from_user.id):
        return
    m = await message.answer("⏳ Собираю бэкап...")
    try:
        backup_path = os.path.join(config.BACKUP_DIR, f"backup_{int(time.time())}.zip")
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk("."):
                dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "backups", "updates")]
                for file in files:
                    if file.endswith((".db-journal", ".log", ".zip")):
                        continue
                    zipf.write(os.path.join(root, file))
        size_mb = os.path.getsize(backup_path) / (1024 * 1024)
        await message.answer_document(FSInputFile(backup_path), caption=f"📦 Ваш бэкап ({size_mb:.2f} МБ).")
        try:
            await m.delete()
        except Exception:
            pass
    except Exception as e:
        safe_e = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await message.answer(f"❌ Ошибка бэкапа:\n<pre>{safe_e}</pre>", parse_mode="HTML")


@main_router.message(Command("lexlog"))
async def cmd_lexlog(message: Message):
    """Просмотр системного лога (lex.log)."""
    if not database.is_owner(message.from_user.id):
        return
    try:
        n = int(message.text.split()[1])
    except Exception:
        n = 30
    await send_logs(message, n)


@main_router.callback_query(F.data == "owner_lexlog")
async def cb_owner_lexlog(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    await send_logs(call, 30)
    await call.answer()


async def send_logs(msg_or_call, n):
    if not os.path.exists("lex.log"):
        target = msg_or_call if isinstance(msg_or_call, Message) else msg_or_call.message
        return await target.answer("Пусто.")
    with open("lex.log", "r", encoding="utf-8") as f:
        lines = f.readlines()
    tail = "".join(lines[-n:])
    if not tail:
        target = msg_or_call if isinstance(msg_or_call, Message) else msg_or_call.message
        return await target.answer("Пусто.")

    if len(tail) < 3500:
        safe_tail = tail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text   = f"📜 Последние {n} строк:\n<blockquote expandable><tg-spoiler>{safe_tail}</tg-spoiler></blockquote>"
        target = msg_or_call if isinstance(msg_or_call, Message) else msg_or_call.message
        return await target.answer(text, parse_mode="HTML")

    with open("lex_logs.py", "w", encoding="utf-8") as f:
        f.write(tail)
    doc    = FSInputFile("lex_logs.py")
    target = msg_or_call if isinstance(msg_or_call, Message) else msg_or_call.message
    await target.answer_document(doc)


@main_router.message(Command("restart"))
async def cmd_restart(message: Message):
    """Принудительная перезагрузка бота."""
    if not database.is_owner(message.from_user.id):
        return
    await message.answer("🔄 <b>Выполняю перезапуск системы LEX...</b>", parse_mode="HTML")
    os._exit(0)


@main_router.callback_query(F.data == "owner_commands")
async def cb_owner_commands(call: CallbackQuery, dispatcher: Dispatcher):
    if not database.is_owner(call.from_user.id):
        return await call.answer("Нет прав!")

    commands  = collect_commands(dispatcher)
    owner_cmds = []
    user_cmds  = []
    for cmd, desc in commands:
        if cmd.lower() in OWNER_ONLY_COMMANDS:
            owner_cmds.append(f"• <code>/{cmd}</code> — {desc}")
        else:
            user_cmds.append(f"• <code>/{cmd}</code> — {desc}")

    text  = "📖 <b>Справочник команд системы LEX:</b>\n\n<blockquote expandable>"
    if owner_cmds:
        text += "👑 <b>КОМАНДЫ СОЗДАТЕЛЯ:</b>\n" + "\n".join(owner_cmds) + "\n\n"
    if user_cmds:
        text += "👥 <b>ОБЩИЕ КОМАНДЫ:</b>\n" + "\n".join(user_cmds) + "\n\n"

    text += (
        "⚡️ <b>ТЕКСТОВЫЕ ТРИГГЕРЫ:</b>\n"
        "• <code>лекс, удали</code> — Удалить сообщение реплаем\n"
        "• <code>лекс, скажи [текст]</code> — Отправить фразу от имени бота\n"
        "• <code>лекс, [вопрос]</code> — Задать вопрос ИИ-помощнику Лекс\n"
        "</blockquote>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="submenu_owner")]])
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await call.answer()


# ==========================================
# 2. ИИ-ПОМОЩНИК (ОТВЕТЫ НА ВОПРОСЫ)
# ==========================================
async def process_orchestrator(message: Message, query_text: str):
    """Отвечает на вопросы пользователя через LLM. Не пишет и не исполняет код."""
    if not query_text:
        return
    if not is_allowed_to_call(message.from_user.id, message.chat.id):
        if message.chat.type == "private":
            await message.answer("🛠 Лекс активен только в чате Onyx или для Создателя.")
        return

    initial_text = '⚡ <b>ЛЕКС:</b> Готовлю ответ <a href="tg://emoji?id=5386367538735104399">⌛</a>'
    m            = await message.answer(initial_text, parse_mode="HTML")
    chat_id      = message.chat.id
    message_id   = m.message_id
    start_time   = time.time()

    reply_msg   = message.reply_to_message if message.reply_to_message else None
    photo       = None
    if reply_msg:
        if reply_msg.photo:
            photo = reply_msg.photo[-1]
        elif reply_msg.document and reply_msg.document.mime_type and reply_msg.document.mime_type.startswith("image/"):
            photo = reply_msg.document

    base64_image = None
    if photo:
        try:
            file_info = await bot.get_file(photo.file_id)
            temp_path = f"temp_{uuid.uuid4().hex[:8]}.jpg"
            await bot.download_file(file_info.file_path, temp_path)
            with open(temp_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode("utf-8")
            os.remove(temp_path)
        except Exception as e:
            logging.error(f"[Vision] Ошибка скачивания фото: {e}")

    try:
        if base64_image:
            response_text   = await resilient_vision_call(query_text, base64_image)
            conversation_id = str(uuid.uuid4())[:8]
            should_cleanup  = False
            messages_list   = []
        else:
            response_text, conversation_id, should_cleanup, messages_list = await ask_lex_llm(query_text, chat_id, reply_msg)

        if response_text and len(response_text) > 3300:
            response_text = response_text[:3300] + "\n\n<i>(...текст сокращен из-за лимитов Telegram...)</i>"

        query_id = uuid.uuid4().hex[:8]
        details_context[query_id] = {
            "query":            query_text,
            "conversation_id":  conversation_id,
            "assistant_msg_id": message_id,
            "chat_id":          chat_id,
        }
    except Exception as e:
        response_text   = f"❌ Ошибка ИИ: {e}"
        conversation_id = str(uuid.uuid4())[:8]
        query_id        = None
        should_cleanup  = False
        messages_list   = []

    elapsed = time.time() - start_time
    if elapsed < 2.0:
        await asyncio.sleep(2.0 - elapsed)

    if conversation_id:
        if should_cleanup:
            database.delete_conversation(conversation_id)
        else:
            messages_list.append({"role": "user",      "content": query_text})
            messages_list.append({"role": "assistant",  "content": response_text})
            database.save_conversation(conversation_id, chat_id, messages_list, message_id, response_text)

    formatted_response = md_to_html(response_text)
    safe_query         = html.escape(query_text)
    formatted_text     = (
        f"🔍 <b>Запрос:</b> <i>{safe_query}</i>\n\n"
        f"✨ <b>Ответ Лекса:</b>\n"
        f"<blockquote expandable>{formatted_response}</blockquote>"
    )
    kb = get_details_kb(query_id) if query_id else None

    try:
        await bot.edit_message_text(
            text=formatted_text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as e:
        logging.error(f"[Orchestrator] Ошибка публикации ответа: {e}")


@main_router.message(F.text.func(lambda t: t and re.match(r"^(лекс|lex),?\s+удали", t.lower())))
async def cmd_lex_delete(message: Message):
    if not is_admin(message.from_user.id, message.from_user.username, message.chat.id):
        return await message.answer("❌ У вас нет прав для этой команды.")
    if not message.reply_to_message:
        return await message.answer("⚠️ Сделай реплай на сообщение, которое нужно удалить.")
    try:
        await message.bot.delete_message(message.chat.id, message.reply_to_message.message_id)
        await message.delete()
    except TelegramBadRequest:
        pass
    except Exception:
        pass


@main_router.message(F.text.func(lambda t: t and re.match(r"^(лекс|lex),?\s+скажи\s+", t.lower())))
async def cmd_lex_say(message: Message):
    if not is_admin(message.from_user.id, message.from_user.username, message.chat.id):
        return await message.answer("❌ У вас нет прав для этой команды.")
    text_to_say = re.sub(r"^(лекс|lex),?\s+скажи\s+", "", message.text, flags=re.IGNORECASE)
    safe_text   = text_to_say.replace("<", "&lt;").replace(">", "&gt;")
    try:
        await message.delete()
    except Exception:
        pass
    if safe_text:
        await message.answer(f"<b><i>{safe_text}</i></b>", parse_mode="HTML")


@main_router.message(F.text.func(lambda t: t and re.match(r"^(лекс|lex),?\s+(.+)", t.lower())))
async def cmd_lex_direct_task(message: Message):
    """Задать вопрос Лексу: «лекс, [вопрос]»"""
    match = re.match(r"^(лекс|lex),?\s+(.+)", message.text, re.IGNORECASE)
    if match:
        await process_orchestrator(message, match.group(2).strip())


@main_router.message(F.reply_to_message)
async def cmd_lex_reply_task(message: Message):
    if not message.text:
        return
    me = await message.bot.get_me()
    if message.reply_to_message.from_user.id == me.id:
        await process_orchestrator(message, message.text)


@main_router.message(F.text.func(lambda t: t and t.strip().lower() in ["лекс", "lex", "лекс,", "lex,"]))
async def cmd_lex_listen(message: Message, state: FSMContext):
    if not is_allowed_to_call(message.from_user.id, message.chat.id):
        if message.chat.type == "private":
            await message.answer("🛠 Лекс активен только в чате Onyx или для Создателя.")
        return

    is_own = database.is_owner(message.from_user.id)
    if not is_own:
        if time.time() - lex_cooldowns.get(message.from_user.id, 0) < 5:
            return
    lex_cooldowns[message.from_user.id] = time.time()
    await state.set_state(LexState.listening)
    await state.update_data(listen_start=time.time())
    reply_text = "<b>Слушаю, Создатель</b>" if is_own else "К вашим услугам"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✦", callback_data="menu_main")]])
    await message.answer(reply_text, parse_mode="HTML", reply_markup=kb)


@main_router.message(LexState.listening)
async def process_listened_task(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if time.time() - data.get("listen_start", 0) > 60:
        return
    if message.text:
        await process_orchestrator(message, message.text)


# ==========================================
# 3. МЕНЮ И АДМИНИСТРАТОРЫ
# ==========================================
@main_router.message(Command("lexset"))
async def cmd_lexset(message: Message):
    """Открыть главное меню администрирования бота."""
    if not is_admin(message.from_user.id, message.from_user.username, message.chat.id):
        return
    await message.answer(
        "⚙️ <b>Главное меню системы LEX:</b>",
        parse_mode="HTML",
        reply_markup=get_main_menu_kb(message.from_user.id, message.chat.id),
    )


@main_router.callback_query(F.data == "menu_main")
async def cb_menu_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text(
            "⚙️ <b>Главное меню:</b>",
            parse_mode="HTML",
            reply_markup=get_main_menu_kb(call.from_user.id, call.message.chat.id),
        )
    except Exception:
        pass
    await call.answer()


@main_router.callback_query(F.data == "menu_hide")
async def cb_menu_hide(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


@main_router.callback_query(F.data == "submenu_admins")
async def cb_submenu_admins(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(call.from_user.id, call.from_user.username, call.message.chat.id):
        return await call.answer("Нет прав!")
    admins = database.get_local_admins(call.message.chat.id)
    text   = "👑 <b>Администраторы этого чата:</b>\n\n"
    for adm_id, adm_name in admins:
        text += f"• <a href='tg://user?id={adm_id}'><b><i>{adm_name or 'Участник'}</i></b></a> (<code>{adm_id}</code>)\n"
    kb = []
    if database.is_owner(call.from_user.id):
        kb.append([
            InlineKeyboardButton(text="➕ Добавить", callback_data="admin_add"),
            InlineKeyboardButton(text="➖ Удалить",  callback_data="admin_del"),
        ])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main")])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@main_router.callback_query(F.data.in_(["admin_add", "admin_del"]))
async def cb_admin_manage(call: CallbackQuery, state: FSMContext):
    if not database.is_owner(call.from_user.id):
        return await call.answer("Только Создатель!")
    await state.set_state(AdminState.waiting_for_add if call.data == "admin_add" else AdminState.waiting_for_del)
    text = "➕ Реплай или ID для добавления." if call.data == "admin_add" else "➖ Отправьте ID для удаления."
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Отмена", callback_data="submenu_admins")
        ]]),
    )


@main_router.message(AdminState.waiting_for_add)
async def process_admin_add(message: Message, state: FSMContext):
    target_id, target_name = None, "Участник"
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id, target_name = message.reply_to_message.from_user.id, message.reply_to_message.from_user.first_name
    elif message.text and message.text.isdigit():
        target_id = int(message.text)
    if not target_id:
        return await message.answer("⚠️ Нужен ID или реплай!")
    success, text = database.add_local_admin(message.chat.id, target_id, target_name)
    await state.clear()
    await message.answer(text)


@main_router.message(AdminState.waiting_for_del)
async def process_admin_del(message: Message, state: FSMContext):
    if database.remove_local_admin(message.chat.id, message.text.strip()):
        await message.answer("🗑 Админ удален.")
    else:
        await message.answer("⚠️ Админ не найден.")
    await state.clear()


@main_router.callback_query(F.data == "submenu_summon")
async def cb_submenu_summon(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(call.from_user.id, call.from_user.username, call.message.chat.id):
        return await call.answer("Нет прав!")
    conn = sqlite3.connect(config.DB_NAME)
    res  = conn.cursor().execute("SELECT tag FROM chat_tags WHERE chat_id = ?", (call.message.chat.id,)).fetchall()
    conn.close()
    tags = [r[0] for r in res]
    text = "📢 <b>Призыв:</b>\n\n" + ("\n".join([f"• <code>{t}</code>" for t in tags]) if tags else "Список пуст.")
    kb = [
        [InlineKeyboardButton(text="Позвать всех", callback_data="do_summon")],
        [InlineKeyboardButton(text="➕ Добавить",  callback_data="tag_add"),
         InlineKeyboardButton(text="➖ Удалить",   callback_data="tag_del")],
        [InlineKeyboardButton(text="🔙 Назад",     callback_data="menu_main")],
    ]
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@main_router.callback_query(F.data.in_(["tag_add", "tag_del"]))
async def cb_tag_manage(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id, call.from_user.username, call.message.chat.id):
        return await call.answer("Нет прав!")
    await state.set_state(TagState.waiting_for_add if call.data == "tag_add" else TagState.waiting_for_del)
    text = "📝 Напишите юзернеймы (с @) или ID." if call.data == "tag_add" else "🗑 Напишите юзернеймы или ID для удаления."
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Отмена", callback_data="submenu_summon")
        ]]),
    )


@main_router.message(TagState.waiting_for_add)
async def process_tag_add(message: Message, state: FSMContext):
    if not message.text:
        return
    conn  = sqlite3.connect(config.DB_NAME)
    added = 0
    for word in message.text.split():
        if word.strip().startswith("@") or word.strip().isdigit():
            try:
                conn.cursor().execute("INSERT INTO chat_tags (chat_id, tag) VALUES (?, ?)", (message.chat.id, word.strip()))
                added += 1
            except sqlite3.IntegrityError:
                pass
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Добавлено <b>{added}</b> тегов.", parse_mode="HTML")


@main_router.message(TagState.waiting_for_del)
async def process_tag_del(message: Message, state: FSMContext):
    if not message.text:
        return
    conn    = sqlite3.connect(config.DB_NAME)
    deleted = 0
    for word in message.text.split():
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_tags WHERE chat_id = ? AND tag = ?", (message.chat.id, word.strip()))
        deleted += cur.rowcount
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"🗑 Удалено <b>{deleted}</b> тегов.", parse_mode="HTML")


@main_router.callback_query(F.data == "do_summon")
async def cb_do_summon(call: CallbackQuery):
    if not is_admin(call.from_user.id, call.from_user.username, call.message.chat.id):
        return await call.answer("Нет прав!")
    conn = sqlite3.connect(config.DB_NAME)
    res  = conn.cursor().execute("SELECT tag FROM chat_tags WHERE chat_id = ?", (call.message.chat.id,)).fetchall()
    conn.close()
    if not res:
        return await call.answer("⚠️ Список пуст.")
    await call.answer("Призыв!")
    for tag in [r[0] for r in res]:
        mention = f'<a href="tg://user?id={tag}">Участник</a>' if tag.isdigit() else f"{tag}"
        await call.message.answer(f"{mention} вызываю! ⚡️\n<tg-spoiler>/lexdelme</tg-spoiler>", parse_mode="HTML")
        await asyncio.sleep(1.0)


# ==========================================
# 4. ГОСТЕВОЙ РЕЖИМ
# ==========================================
@main_router.guest_message()
async def cb_guest_message(message: Message):
    """Прием гостевого сообщения."""
    if not is_allowed_to_call(message.from_user.id, message.chat.id):
        if message.chat.type == "private":
            await message.answer("🛠 Лекс активен только в чате Onyx или для Создателя.")
        return

    query_text = message.text or ""
    me         = await message.bot.get_me()
    clean_query = re.sub(rf"^@{me.username}\s*", "", query_text, flags=re.IGNORECASE).strip()
    if not clean_query:
        return

    result_id    = f"guest_call_{uuid.uuid4().hex[:8]}"
    loading_html = '⚡ <b>ЛЕКС:</b> Готовлю ответ <a href="tg://emoji?id=5386367538735104399">⌛</a>'
    result       = InlineQueryResultArticle(
        id=result_id,
        title="Ответ Лекса",
        input_message_content=InputTextMessageContent(message_text=loading_html, parse_mode="HTML"),
    )
    try:
        sent_guest        = await message.answer_guest_query(result=result)
        inline_message_id = sent_guest.inline_message_id
    except Exception as e:
        logging.error(f"[Guest] Ошибка answer_guest_query: {e}")
        return

    reply_msg = message.reply_to_message if message.reply_to_message else None
    asyncio.create_task(
        process_guest_llm(inline_message_id, clean_query, message.chat.id, reply_msg, message.message_id)
    )


async def process_guest_llm(
    inline_message_id: str,
    query_text: str,
    chat_id: int,
    reply_message: Message = None,
    incoming_msg_id: int = 0,
):
    start_time = time.time()
    photo = None
    if reply_message:
        if reply_message.photo:
            photo = reply_message.photo[-1]
        elif reply_message.document and reply_message.document.mime_type and reply_message.document.mime_type.startswith("image/"):
            photo = reply_message.document

    base64_image = None
    if photo:
        try:
            file_info = await bot.get_file(photo.file_id)
            temp_path = f"temp_{uuid.uuid4().hex[:8]}.jpg"
            await bot.download_file(file_info.file_path, temp_path)
            with open(temp_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode("utf-8")
            os.remove(temp_path)
        except Exception as e:
            logging.error(f"[Guest Vision] Ошибка: {e}")

    try:
        if base64_image:
            response_text   = await resilient_vision_call(query_text, base64_image)
            conversation_id = str(uuid.uuid4())[:8]
            should_cleanup  = False
            messages_list   = []
        else:
            response_text, conversation_id, should_cleanup, messages_list = await ask_lex_llm(query_text, chat_id, reply_message)

        if response_text and len(response_text) > 3300:
            response_text = response_text[:3300] + "\n\n<i>(...текст сокращен...)</i>"

        dummy_msg_id = -int(time.time() % 1000000)
        if conversation_id:
            if should_cleanup:
                database.delete_conversation(conversation_id)
            else:
                messages_list.append({"role": "user",     "content": query_text})
                messages_list.append({"role": "assistant", "content": response_text})
                database.save_conversation(conversation_id, chat_id, messages_list, dummy_msg_id, response_text)

        query_id = uuid.uuid4().hex[:8]
        details_context[query_id] = {
            "query":            query_text,
            "conversation_id":  conversation_id,
            "assistant_msg_id": dummy_msg_id,
            "chat_id":          chat_id,
        }
    except Exception as e:
        response_text = f"❌ Ошибка ИИ: {e}"
        query_id      = None
        should_cleanup = False
        messages_list  = []

    elapsed = time.time() - start_time
    if elapsed < 2.0:
        await asyncio.sleep(2.0 - elapsed)

    formatted_response = md_to_html(response_text)
    safe_query         = html.escape(query_text)
    formatted_text     = (
        f"🔍 <b>Запрос:</b> <i>{safe_query}</i>\n\n"
        f"✨ <b>Ответ Лекса:</b>\n"
        f"<blockquote expandable>{formatted_response}</blockquote>"
    )
    kb = get_details_kb(query_id) if query_id else None

    try:
        await bot.edit_message_text(
            text=formatted_text,
            inline_message_id=inline_message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as e:
        logging.error(f"[Guest] Ошибка обновления: {e}")


# ==========================================
# 5. КНОПКА "✦ ПОДРОБНЕЕ ✦"
# ==========================================
@main_router.callback_query(F.data.startswith("details_"))
async def cb_more_details(call: CallbackQuery):
    if not is_allowed_to_call(call.from_user.id, call.message.chat.id if call.message else 0):
        return await call.answer("🛠 Доступ ограничен.", show_alert=True)

    query_id = call.data.split("_")[1]
    if query_id not in details_context:
        return await call.answer("⚠️ Информация стерлась.", show_alert=True)

    await call.answer("⚡ Провожу детальный анализ...")

    ctx              = details_context[query_id]
    query_text       = ctx["query"]
    assistant_msg_id = ctx["assistant_msg_id"]
    chat_id          = ctx.get("chat_id") or (call.message.chat.id if call.message else None)
    message_id       = call.message.message_id if call.message else None
    inline_msg_id    = call.inline_message_id

    if not inline_msg_id and not message_id:
        return await call.answer("❌ Сообщение не найдено.", show_alert=True)

    loading_text = '⚡ <b>ЛЕКС:</b> Готовлю развернутый ответ <a href="tg://emoji?id=5386367538735104399">⌛</a>'
    try:
        if inline_msg_id:
            await bot.edit_message_text(text=loading_text, inline_message_id=inline_msg_id, parse_mode="HTML")
        else:
            await bot.edit_message_text(text=loading_text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
    except Exception:
        pass

    start_time = time.time()
    try:
        system_prompt = (
            "Ты — Лекс, глубокий ИИ-аналитик. "
            "Дай развернутый детальный разбор запроса. "
            "Структурируй текст через Markdown (**жирный**, *курсив*)."
        )
        response_text = await AIOrchestrator.resilient_llm_call(system_prompt, f"Дай подробности к: {query_text}")
        if response_text and len(response_text) > 3300:
            response_text = response_text[:3300] + "\n\n<i>(...сокращено из-за лимитов...)</i>"
    except Exception as e:
        response_text = f"❌ Не удалось провести разбор: {e}"

    elapsed = time.time() - start_time
    if elapsed < 2.0:
        await asyncio.sleep(2.0 - elapsed)

    if assistant_msg_id and chat_id:
        database.update_history_message(assistant_msg_id, chat_id, response_text)

    formatted_response = md_to_html(response_text)
    safe_query         = html.escape(query_text)
    formatted_text     = (
        f"🔍 <b>Запрос:</b> <i>{safe_query}</i>\n\n"
        f"📚 <b>Детальный разбор Лекса:</b>\n"
        f"<blockquote expandable>{formatted_response}</blockquote>"
    )

    try:
        if inline_msg_id:
            await bot.edit_message_text(text=formatted_text, inline_message_id=inline_msg_id, parse_mode="HTML")
        else:
            await bot.edit_message_text(text=formatted_text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
    except Exception as e:
        logging.error(f"[Details] Ошибка: {e}")


# ==========================================
# 6. РЕГИСТРАЦИЯ РОУТЕРОВ
# ==========================================
@main_router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == LexState.listening.state:
        return

# Подключение модулей
dp.include_router(operator_router)
dp.include_router(main_router)
