# --- START OF FILE mod_reposter.py ---
"""
Модуль Репостер для LEX v2.

Подключение:
  1. Положи файл в modules/ как mod_reposter.py
  2. Включи в модуль-операторе — router подхватится автоматически.
  3. Открыть настройки: /reposter  или  Меню → 📡 Репостер

Работает без Telethon — только Bot API + t.me/s HTTP-скрапинг.

═══════════════════════════════════════════════════════════════
JSON-ФИЛЬТР — ПОЛНАЯ СХЕМА
═══════════════════════════════════════════════════════════════

{
  "comment":   "Описание фильтра (не влияет на логику)",

  // ── Ключевые слова ────────────────────────────────────────
  "require_any":  ["слово1", "слово2"],  // ≥1 слова должны быть в тексте
  "require_all":  ["слово1", "слово2"],  // ВСЕ слова должны быть (AND)
  "exclude_any":  ["спам", "реклама"],   // НИЧЕГО из списка не должно быть
  "exclude_all":  ["слово1", "слово2"],  // выбросить только если ВСЕ присутствуют

  // ── Группы ────────────────────────────────────────────────
  // Пост проходит если ХОТЯ БЫ ОДНА группа целиком совпала
  "require_group_any": [
    ["означает", "слово"],        // группа 1: оба слова в тексте
    ["расшифровыва"],             // группа 2: одно слово
    ["аббревиатур", "переводится"]// группа 3: оба
  ],

  // ── Регулярные выражения (Python re, IGNORECASE по умолчанию) ─
  "require_regex":  "паттерн",   // текст ДОЛЖЕН совпасть
  "exclude_regex":  "паттерн",   // текст НЕ ДОЛЖЕН совпасть

  // ── Длина ────────────────────────────────────────────────
  "min_chars":  80,    // мин. символов (после очистки рекламы)
  "max_chars":  5000,  // макс. символов
  "min_words":  10,    // мин. слов
  "max_words":  600,   // макс. слов

  // ── Структура ─────────────────────────────────────────────
  "require_newlines":     1,    // мин. кол-во переносов строки
  "max_uppercase_ratio":  0.4,  // макс. доля ЗАГЛАВНЫХ букв (0.0–1.0)

  // ── Медиа ─────────────────────────────────────────────────
  "require_media": null,   // null=любой, true=с медиа, false=без медиа

  // ── Учёт регистра ─────────────────────────────────────────
  "case_sensitive": false,  // по умолчанию false (регистр игнорируется)

  // ── Скоринг (набери >= min_score баллов) ─────────────────
  "score_rules": [
    {"contains":   "означает",  "score": 3},
    {"contains":   "расшифров", "score": 3},
    {"min_chars":  150,         "score": 2},
    {"has_media":  true,        "score": 1},
    {"min_words":  20,          "score": 1},
    {"regex_match":"^[А-ЯЁ]",  "score": 1}
  ],
  "min_score": 3
}
"""

import html
import json
import logging
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, MessageReactionUpdated,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReactionTypeEmoji,
)
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command

import database
from reposter_worker import (
    fetch_tme_posts, clean_text, passes_json_filter,
    validate_json_filter, _vote_markup, DELETE_VOTES_REQ,
)
from states import ReposterFSM

MODULE_STEM       = "mod_reposter"
SETTINGS_CALLBACK = f"mod_settings_{MODULE_STEM}"

router = Router()

INTERVALS = [
    (5,   "5 мин"),
    (10,  "10 мин"),
    (15,  "15 мин"),
    (30,  "30 мин"),
    (60,  "1 час"),
    (90,  "1.5 ч"),
    (120, "2 часа"),
    (180, "3 часа"),
]

# Emoji реакции для быстрого удаления поста овнером
OWNER_DELETE_REACTION = "💊"


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _is_admin(user_id: int, username: str | None, chat_id: int) -> bool:
    if database.is_owner(user_id):
        return True
    return any(
        aid == user_id or (aun and username and aun.lower() == username.lower())
        for aid, aun in database.get_local_admins(chat_id)
    )


def _back_kb(callback: str, label: str = "🔙 Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=callback)
    ]])


def _fmt_interval(minutes: int) -> str:
    return next((l for m, l in INTERVALS if m == minutes), f"{minutes} мин")


def _fmt_filter_short(json_str: str | None) -> str:
    """Краткое описание фильтра для списка каналов."""
    if not json_str:
        return "⚪ нет фильтра"
    try:
        f = json.loads(json_str)
        parts = []
        if f.get("comment"):
            return f"🔵 {f['comment'][:40]}"
        if f.get("require_any"):
            parts.append(f"нужно: {', '.join(f['require_any'][:3])}")
        if f.get("require_group_any"):
            parts.append(f"группы: {len(f['require_group_any'])}")
        if f.get("score_rules"):
            parts.append(f"скоринг: {len(f['score_rules'])} правил")
        return "🔵 " + " · ".join(parts) if parts else "🔵 фильтр активен"
    except Exception:
        return "⚠️ повреждён"


def _fmt_filter_full(json_str: str | None) -> str:
    """Подробное отображение фильтра."""
    if not json_str:
        return "<i>Нет фильтра — берём всё (только вырезаем рекламу)</i>"
    try:
        f    = json.loads(json_str)
        rows = []
        if f.get("comment"):
            rows.append(f"💬 <b>{html.escape(f['comment'])}</b>")
        if f.get("require_any"):
            rows.append("🔍 Хотя бы одно: " + " / ".join(f"<code>{html.escape(w)}</code>" for w in f["require_any"]))
        if f.get("require_all"):
            rows.append("✅ Все должны быть: " + " + ".join(f"<code>{html.escape(w)}</code>" for w in f["require_all"]))
        if f.get("exclude_any"):
            rows.append("🚫 Запрещено: " + ", ".join(f"<code>{html.escape(w)}</code>" for w in f["exclude_any"]))
        if f.get("exclude_all"):
            rows.append("⛔ Выбросить если ВСЕ: " + " + ".join(f"<code>{html.escape(w)}</code>" for w in f["exclude_all"]))
        if f.get("require_group_any"):
            grps = f["require_group_any"]
            grp_strs = [" &amp; ".join(f"<code>{html.escape(w)}</code>" for w in g) for g in grps[:4]]
            rows.append("🗂 Группы (одна из): " + " | ".join(grp_strs))
        if f.get("require_regex"):
            rows.append(f"🔎 Regex (есть): <code>{html.escape(f['require_regex'])}</code>")
        if f.get("exclude_regex"):
            rows.append(f"🔇 Regex (нет): <code>{html.escape(f['exclude_regex'])}</code>")
        if f.get("min_chars"):
            rows.append(f"📏 Мин. символов: <b>{f['min_chars']}</b>")
        if f.get("max_chars"):
            rows.append(f"📐 Макс. символов: <b>{f['max_chars']}</b>")
        if f.get("min_words"):
            rows.append(f"📝 Мин. слов: <b>{f['min_words']}</b>")
        if f.get("max_words"):
            rows.append(f"📝 Макс. слов: <b>{f['max_words']}</b>")
        if f.get("require_newlines"):
            rows.append(f"↵ Мин. абзацев: <b>{f['require_newlines']}</b>")
        if f.get("max_uppercase_ratio") is not None:
            rows.append(f"🔠 Макс. ЗАГЛАВНЫХ: <b>{int(f['max_uppercase_ratio']*100)}%</b>")
        rm = f.get("require_media")
        if rm is True:
            rows.append("📷 Только с медиа")
        elif rm is False:
            rows.append("📝 Только текст")
        cs = f.get("case_sensitive", False)
        if cs:
            rows.append("🔤 Учёт регистра: <b>включён</b>")
        if f.get("score_rules"):
            rows.append(f"🎯 Скоринг: {len(f['score_rules'])} правил, мин. балл = {f.get('min_score', 0)}")
        return "\n".join(rows) if rows else "<i>Пустой объект {}</i>"
    except Exception:
        return f"<i>Повреждён: {html.escape(json_str[:80])}</i>"


async def _edit_or_answer(event, text: str, markup=None, parse_mode="HTML"):
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, parse_mode=parse_mode, reply_markup=markup)
            return
        except Exception:
            pass
        await event.message.answer(text, parse_mode=parse_mode, reply_markup=markup)
    else:
        await event.answer(text, parse_mode=parse_mode, reply_markup=markup)


# ─────────────────────────────────────────────────────────────────────────────
# 1. ГОЛОСОВАНИЕ ❤️ / 🗑️
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "rp_noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("rp_like:"))
async def cb_like(call: CallbackQuery, bot: Bot):
    _, chat_id_s, msg_id_s = call.data.split(":")
    chat_id    = int(chat_id_s)
    message_id = int(msg_id_s)

    if not database.reposter_is_sent_msg(chat_id, message_id):
        return await call.answer("Это сообщение уже не актуально.", show_alert=True)

    likes = database.reposter_like_toggle(chat_id, message_id, call.from_user.id)
    _, del_votes = database.reposter_get_vote_counts(chat_id, message_id)

    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=_vote_markup(chat_id, message_id, likes, del_votes),
        )
    except Exception:
        pass

    await call.answer("❤️")


@router.callback_query(F.data.startswith("rp_del:"))
async def cb_del_vote(call: CallbackQuery, bot: Bot):
    _, chat_id_s, msg_id_s = call.data.split(":")
    chat_id    = int(chat_id_s)
    message_id = int(msg_id_s)

    if not database.reposter_is_sent_msg(chat_id, message_id):
        return await call.answer("Сообщение уже удалено.", show_alert=True)

    # Овнер удаляет сразу без голосования
    if database.is_owner(call.from_user.id):
        await _delete_reposter_msg(bot, chat_id, message_id)
        return await call.answer("🗑 Удалено.")

    del_votes = database.reposter_delete_vote(chat_id, message_id, call.from_user.id)
    likes, _  = database.reposter_get_vote_counts(chat_id, message_id)

    if del_votes >= DELETE_VOTES_REQ:
        await _delete_reposter_msg(bot, chat_id, message_id)
        return await call.answer("🗑 Удалено по голосованию!")

    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=_vote_markup(chat_id, message_id, likes, del_votes),
        )
    except Exception:
        pass

    await call.answer(f"Голос засчитан ({del_votes}/{DELETE_VOTES_REQ})")


async def _delete_reposter_msg(bot: Bot, chat_id: int, message_id: int):
    """Удалить сообщение и пометить в БД."""
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        logging.warning(f"[Reposter] Не удалось удалить {message_id} в {chat_id}: {e}")
    database.reposter_mark_deleted(chat_id, message_id)


# ─────────────────────────────────────────────────────────────────────────────
# 2. РЕАКЦИЯ 💊 ОТ ОВНЕРА → НЕМЕДЛЕННОЕ УДАЛЕНИЕ
# ─────────────────────────────────────────────────────────────────────────────

@router.message_reaction()
async def on_message_reaction(event: MessageReactionUpdated, bot: Bot):
    """
    Если овнер ставит реакцию 💊 на пост репостера — удалить его немедленно.
    """
    if not database.is_owner(event.user.id if event.user else 0):
        return

    new_reactions = event.new_reaction or []
    has_pill = any(
        isinstance(r, ReactionTypeEmoji) and r.emoji == OWNER_DELETE_REACTION
        for r in new_reactions
    )
    if not has_pill:
        return

    chat_id    = event.chat.id
    message_id = event.message_id

    if not database.reposter_is_sent_msg(chat_id, message_id):
        return  # Это не пост репостера

    await _delete_reposter_msg(bot, chat_id, message_id)
    logging.info(f"[Reposter] 💊 Удалено по реакции овнера: chat={chat_id} msg={message_id}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. ТОЧКИ ВХОДА В НАСТРОЙКИ
# ─────────────────────────────────────────────────────────────────────────────

@router.message(Command("reposter"))
async def cmd_reposter(message: Message, state: FSMContext):
    await state.clear()
    if not _is_admin(message.from_user.id, message.from_user.username, message.chat.id):
        return await message.answer("❌ Нет прав.")
    await _show_main_menu(message, message.chat.id, edit=False)


@router.callback_query(F.data == "submenu_reposter")
async def cb_submenu_reposter(call: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = call.message.chat.id
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    await _show_main_menu(call, chat_id, edit=True)
    await call.answer()


@router.callback_query(F.data == SETTINGS_CALLBACK)
async def cb_settings_entry(call: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = call.message.chat.id
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    await _show_main_menu(call, chat_id, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("reposter_menu:"))
async def cb_reposter_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(call.data.split(":")[1])
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    await _show_main_menu(call, chat_id, edit=True)
    await call.answer()


# ─────────────────────────────────────────────────────────────────────────────
# 4. ГЛАВНОЕ МЕНЮ
# ─────────────────────────────────────────────────────────────────────────────

async def _show_main_menu(event, chat_id: int, edit: bool = True):
    cfg     = database.reposter_get_chat(chat_id)
    sources = database.reposter_get_sources(chat_id)
    queue_n = database.reposter_queue_count(chat_id)
    min_q   = database.QUEUE_MIN_SEND
    max_q   = database.QUEUE_HARD_MAX

    if cfg and cfg["is_active"]:
        status_ln = f"✅ включён · каждые {_fmt_interval(cfg['interval_minutes'])}"
    elif cfg:
        status_ln = "❌ выключен"
    else:
        status_ln = "⭕ не настроен"

    queue_status = f"{queue_n}/{max_q}"
    if queue_n < min_q:
        queue_status += f" ⏳ (нужно ≥{min_q} для старта)"

    toggle_text   = "❌ Выключить" if cfg and cfg["is_active"] else "✅ Включить"
    interval_label = _fmt_interval(cfg["interval_minutes"]) if cfg else "Интервал"

    text = (
        f"📡 <b>Репостер</b>\n\n"
        f"Статус: {status_ln}\n"
        f"Каналов: <b>{len(sources)}</b>  |  В очереди: <b>{queue_status}</b>\n\n"
        f"Посты накапливаются (до {max_q} шт.), отправка начинается\n"
        f"когда в запасе ≥{min_q} штук. Новые вытесняют старые.\n\n"
        f"<i>💊 реакция от овнера — немедленно удаляет пост.</i>"
    )

    kb = [
        [InlineKeyboardButton(text=toggle_text,           callback_data=f"reposter_toggle:{chat_id}")],
        [InlineKeyboardButton(text=f"⏱ {interval_label}", callback_data=f"reposter_interval:{chat_id}")],
        [InlineKeyboardButton(text="📋 Каналы-источники",  callback_data=f"reposter_sources:{chat_id}")],
        [InlineKeyboardButton(text="🔬 Тест поста",        callback_data=f"reposter_test:{chat_id}")],
        [InlineKeyboardButton(text="🔙 Главное меню",      callback_data="menu_main")],
    ]
    await _edit_or_answer(event, text, InlineKeyboardMarkup(inline_keyboard=kb))


# ─────────────────────────────────────────────────────────────────────────────
# 5. ВКЛЮЧЕНИЕ / ВЫКЛЮЧЕНИЕ
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("reposter_toggle:"))
async def cb_toggle(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    cfg    = database.reposter_get_chat(chat_id)
    is_now = cfg["is_active"] if cfg else False
    database.reposter_set_active(chat_id, not is_now)
    await call.answer("✅ Включён!" if not is_now else "❌ Выключен!")
    await _show_main_menu(call, chat_id, edit=True)


# ─────────────────────────────────────────────────────────────────────────────
# 6. ИНТЕРВАЛ
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("reposter_interval:"))
async def cb_interval(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    cfg     = database.reposter_get_chat(chat_id)
    current = cfg["interval_minutes"] if cfg else 60

    kb, row = [], []
    for minutes, label in INTERVALS:
        mark = "✔️ " if minutes == current else ""
        row.append(InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"reposter_set_iv:{chat_id}:{minutes}",
        ))
        if len(row) == 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    kb.append([InlineKeyboardButton(
        text="✏️ Ввести своё (мин, 5–180)",
        callback_data=f"reposter_custom_iv:{chat_id}",
    )])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"reposter_menu:{chat_id}")])

    await call.message.edit_text(
        "⏱ <b>Интервал публикации</b>\n<i>От 5 до 180 минут.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await call.answer()


@router.callback_query(F.data.startswith("reposter_set_iv:"))
async def cb_set_iv(call: CallbackQuery):
    _, _, chat_id_s, mins_s = call.data.split(":")
    chat_id = int(chat_id_s)
    minutes = int(mins_s)
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    database.reposter_set_interval(chat_id, minutes)
    await call.answer(f"✅ Интервал: {_fmt_interval(minutes)}")
    await _show_main_menu(call, chat_id, edit=True)


@router.callback_query(F.data.startswith("reposter_custom_iv:"))
async def cb_custom_iv(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    await state.set_state(ReposterFSM.waiting_interval)
    await state.update_data(chat_id=chat_id)
    await call.message.edit_text(
        "✏️ Введите интервал в <b>минутах</b> (5–180):",
        parse_mode="HTML",
        reply_markup=_back_kb(f"reposter_interval:{chat_id}", "❌ Отмена"),
    )
    await call.answer()


@router.message(ReposterFSM.waiting_interval)
async def fsm_interval(message: Message, state: FSMContext):
    data    = await state.get_data()
    chat_id = data["chat_id"]
    val     = (message.text or "").strip()
    if not val.isdigit() or not (5 <= int(val) <= 180):
        return await message.answer("❌ Введите число от 5 до 180.")
    database.reposter_set_interval(chat_id, int(val))
    await state.clear()
    await message.answer(
        f"✅ Интервал: <b>{val} мин</b>",
        parse_mode="HTML",
        reply_markup=_back_kb(f"reposter_menu:{chat_id}"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. КАНАЛЫ-ИСТОЧНИКИ
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("reposter_sources:"))
async def cb_sources(call: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = int(call.data.split(":")[1])
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    await _show_sources(call, chat_id)
    await call.answer()


async def _show_sources(event, chat_id: int):
    sources = database.reposter_get_sources(chat_id)
    text    = "📋 <b>Каналы-источники</b>\n\n"
    if sources:
        for src in sources:
            label = src["label"] or src["channel"]
            flt   = _fmt_filter_short(src["json_filter"])
            text += f"• <code>@{html.escape(src['channel'])}</code>  <i>{html.escape(label)}</i>\n  {flt}\n\n"
    else:
        text += "<i>Нет каналов. Добавь первый!</i>\n"

    kb = []
    for src in sources:
        label = src["label"] or src["channel"]
        kb.append([InlineKeyboardButton(
            text=f"⚙️ {label[:30]}",
            callback_data=f"reposter_src:{chat_id}:{src['id']}",
        )])

    kb.append([InlineKeyboardButton(text="➕ Добавить канал",  callback_data=f"reposter_add_src:{chat_id}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад",            callback_data=f"reposter_menu:{chat_id}")])

    await _edit_or_answer(event, text, InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("reposter_add_src:"))
async def cb_add_src(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    await state.set_state(ReposterFSM.waiting_source)
    await state.update_data(chat_id=chat_id)
    await call.message.edit_text(
        "➕ <b>Добавить канал</b>\n\nВведи username канала (с @ или без).\n"
        "<i>Канал должен быть публичным!</i>",
        parse_mode="HTML",
        reply_markup=_back_kb(f"reposter_sources:{chat_id}", "❌ Отмена"),
    )
    await call.answer()


@router.message(ReposterFSM.waiting_source)
async def fsm_source(message: Message, state: FSMContext):
    data    = await state.get_data()
    chat_id = data["chat_id"]
    text    = (message.text or "").strip().lstrip("@")
    if not text:
        return await message.answer("❌ Нужен username канала.")
    await state.set_state(ReposterFSM.waiting_label)
    await state.update_data(channel=text)
    await message.answer(
        f"✅ Канал: <code>@{html.escape(text)}</code>\n\n"
        "Введи короткое название (или <code>-</code> чтобы пропустить):",
        parse_mode="HTML",
    )


@router.message(ReposterFSM.waiting_label)
async def fsm_label(message: Message, state: FSMContext):
    data    = await state.get_data()
    chat_id = data["chat_id"]
    channel = data["channel"]
    label   = (message.text or "").strip()
    if label == "-":
        label = ""
    added = database.reposter_add_source(chat_id, channel, label)
    await state.clear()
    if added:
        await message.answer(
            f"✅ Канал <code>@{html.escape(channel)}</code> добавлен.\n\n"
            "Теперь зайди в его настройки и добавь JSON-фильтр если нужно.",
            parse_mode="HTML",
            reply_markup=_back_kb(f"reposter_sources:{chat_id}", "🔙 К каналам"),
        )
    else:
        await message.answer(
            "⚠️ Этот канал уже добавлен.",
            reply_markup=_back_kb(f"reposter_sources:{chat_id}", "🔙 К каналам"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. МЕНЮ ОТДЕЛЬНОГО КАНАЛА
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("reposter_src:"))
async def cb_src_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, chat_id_s, src_id_s = call.data.split(":")
    chat_id = int(chat_id_s)
    src_id  = int(src_id_s)
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    await _show_src_menu(call, chat_id, src_id)
    await call.answer()


async def _show_src_menu(event, chat_id: int, src_id: int):
    sources = database.reposter_get_sources(chat_id)
    src     = next((s for s in sources if s["id"] == src_id), None)
    if not src:
        await _edit_or_answer(event, "❌ Канал не найден.", _back_kb(f"reposter_sources:{chat_id}"))
        return

    channel = src["channel"]
    label   = src["label"] or channel
    flt_str = _fmt_filter_full(src["json_filter"])

    text = (
        f"⚙️ <b>@{html.escape(channel)}</b>  <i>({html.escape(label)})</i>\n\n"
        f"<b>JSON-фильтр:</b>\n{flt_str}"
    )

    kb = [[InlineKeyboardButton(
        text="📝 Установить / заменить JSON-фильтр",
        callback_data=f"reposter_set_filter:{chat_id}:{src_id}",
    )]]
    if src["json_filter"]:
        kb.append([InlineKeyboardButton(
            text="🗑 Удалить фильтр",
            callback_data=f"reposter_del_filter:{chat_id}:{src_id}",
        )])
    kb.append([InlineKeyboardButton(
        text="🗑 Удалить канал",
        callback_data=f"reposter_del_src:{chat_id}:{src_id}",
    )])
    kb.append([InlineKeyboardButton(text="🔙 К каналам", callback_data=f"reposter_sources:{chat_id}")])

    await _edit_or_answer(event, text, InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("reposter_del_src:"))
async def cb_del_src(call: CallbackQuery):
    _, _, chat_id_s, src_id_s = call.data.split(":")
    chat_id = int(chat_id_s)
    src_id  = int(src_id_s)
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    database.reposter_remove_source(src_id)
    await call.answer("🗑 Канал удалён!")
    await _show_sources(call, chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# 9. JSON-ФИЛЬТР
# ─────────────────────────────────────────────────────────────────────────────

_FILTER_INPUT_PROMPT = (
    "📋 <b>JSON-фильтр для канала</b>\n\n"
    "Вставь JSON. Доступные параметры:\n\n"
    "<code>require_any</code>   — хотя бы одно слово\n"
    "<code>require_all</code>   — все слова\n"
    "<code>exclude_any</code>   — ни одного\n"
    "<code>exclude_all</code>   — выброс если ВСЕ есть\n"
    "<code>require_group_any</code> — группы (одна группа целиком)\n"
    "<code>require_regex</code> — regex должен совпасть\n"
    "<code>exclude_regex</code> — regex НЕ должен совпасть\n"
    "<code>min_chars / max_chars</code> — длина текста\n"
    "<code>min_words / max_words</code> — кол-во слов\n"
    "<code>require_newlines</code> — мин. переносов строк\n"
    "<code>max_uppercase_ratio</code> — макс. доля ЗАГЛАВНЫХ (0.0–1.0)\n"
    "<code>require_media</code> — null/true/false\n"
    "<code>case_sensitive</code> — учёт регистра (false по умолчанию)\n"
    "<code>score_rules + min_score</code> — скоринговая система\n"
    "<code>comment</code>   — твой комментарий (игнорируется ботом)\n\n"
    "Можешь скинуть мне 10 постов канала и сказать какие хорошие — "
    "я напишу фильтр сам.\n\n"
    "<b>Вставь JSON:</b>"
)


@router.callback_query(F.data.startswith("reposter_set_filter:"))
async def cb_set_filter(call: CallbackQuery, state: FSMContext):
    _, _, chat_id_s, src_id_s = call.data.split(":")
    chat_id = int(chat_id_s)
    src_id  = int(src_id_s)
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)
    await state.set_state(ReposterFSM.waiting_json_filter)
    await state.update_data(chat_id=chat_id, src_id=src_id)
    await call.message.edit_text(
        _FILTER_INPUT_PROMPT,
        parse_mode="HTML",
        reply_markup=_back_kb(f"reposter_src:{chat_id}:{src_id}", "❌ Отмена"),
    )
    await call.answer()


@router.message(ReposterFSM.waiting_json_filter)
async def fsm_json_filter(message: Message, state: FSMContext):
    data    = await state.get_data()
    chat_id = data["chat_id"]
    src_id  = data["src_id"]
    raw     = (message.text or "").strip()

    # Убираем code-блоки если есть
    raw = raw.strip("`").strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()

    ok, err = validate_json_filter(raw)
    if not ok:
        return await message.answer(
            f"❌ <b>Ошибка в фильтре:</b>\n<code>{html.escape(err)}</code>\n\nПопробуй ещё раз:",
            parse_mode="HTML",
        )

    sources = database.reposter_get_sources(chat_id)
    src     = next((s for s in sources if s["id"] == src_id), None)
    if not src:
        await state.clear()
        return await message.answer("❌ Канал не найден.")

    pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    database.reposter_set_json_filter(chat_id, src["channel"], pretty)
    await state.clear()

    await message.answer(
        f"✅ <b>JSON-фильтр сохранён</b> для <code>@{html.escape(src['channel'])}</code>\n\n"
        f"<pre>{html.escape(pretty[:1200])}</pre>",
        parse_mode="HTML",
        reply_markup=_back_kb(f"reposter_src:{chat_id}:{src_id}", "🔙 К каналу"),
    )


@router.callback_query(F.data.startswith("reposter_del_filter:"))
async def cb_del_filter(call: CallbackQuery):
    _, _, chat_id_s, src_id_s = call.data.split(":")
    chat_id = int(chat_id_s)
    src_id  = int(src_id_s)
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)

    sources = database.reposter_get_sources(chat_id)
    src     = next((s for s in sources if s["id"] == src_id), None)
    if src:
        database.reposter_set_json_filter(chat_id, src["channel"], None)
    await call.answer("🗑 Фильтр удалён!")
    await _show_src_menu(call, chat_id, src_id)


# ─────────────────────────────────────────────────────────────────────────────
# 10. ТЕСТ
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("reposter_test:"))
async def cb_test(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    if not _is_admin(call.from_user.id, call.from_user.username, chat_id):
        return await call.answer("Нет прав!", show_alert=True)

    sources = database.reposter_get_sources(chat_id)
    if not sources:
        return await call.answer("Нет каналов!", show_alert=True)

    await call.answer("⏳ Загружаю посты...")

    import random
    src     = random.choice(sources)
    channel = src["channel"]
    flt_str = src["json_filter"]

    try:
        posts = await fetch_tme_posts(channel, limit=25)
    except Exception as e:
        return await call.message.answer(
            f"❌ Не удалось прочитать <code>@{html.escape(channel)}</code>:\n"
            f"<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(f"reposter_menu:{chat_id}"),
        )

    if not posts:
        return await call.message.answer(
            f"⚠️ Канал <code>@{html.escape(channel)}</code> пуст или закрыт.",
            parse_mode="HTML",
            reply_markup=_back_kb(f"reposter_menu:{chat_id}"),
        )

    passed_posts = []
    rejected     = 0
    for p in posts:
        clean = clean_text(p["raw_text"])
        if not clean and not p["has_media"]:
            rejected += 1
            continue
        if not passes_json_filter(clean, p["has_media"], flt_str):
            rejected += 1
            continue
        passed_posts.append({"text": clean, "has_media": p["has_media"], "msg_id": p["msg_id"]})

    total  = len(posts)
    passed = len(passed_posts)

    if not passed_posts:
        await call.message.answer(
            f"🔬 <b>Тест: @{html.escape(channel)}</b>\n\n"
            f"Загружено: {total} · прошло рекламочистку: {total - rejected} · "
            f"прошло JSON-фильтр: <b>0</b>\n\n"
            f"❌ Нет подходящих постов.\n"
            f"{'Попробуй ослабить фильтр.' if flt_str else 'Все посты — реклама?'}",
            parse_mode="HTML",
            reply_markup=_back_kb(f"reposter_menu:{chat_id}"),
        )
        return

    sample  = random.choice(passed_posts[:10])
    preview = sample["text"][:800] + ("…" if len(sample["text"]) > 800 else "")
    media   = "📷 с медиа" if sample["has_media"] else "📝 только текст"

    await call.message.answer(
        f"🔬 <b>Тест: @{html.escape(channel)}</b>\n"
        f"Всего: {total} · отсеяно рекламой: {rejected} · "
        f"прошло фильтр: <b>{passed}</b> · {media}\n\n"
        f"<blockquote>{html.escape(preview) if preview else '<i>пустой текст (только медиа)</i>'}</blockquote>",
        parse_mode="HTML",
        reply_markup=_back_kb(f"reposter_menu:{chat_id}"),
    )
