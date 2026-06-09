# --- START OF FILE module_operator.py ---
"""
Модуль-оператор LEX.

Отвечает за:
  - Управление плагинами (включить / выключить / удалить)
  - Кнопка ⚙️ «Настройки» — открывает настройки конкретного плагина.
    Конвенция: плагин регистрирует callback_query с data=«mod_settings_{stem}»,
    где stem — имя файла без .py (например, mod_reposter → mod_settings_mod_reposter).
  - Патчер кода (текстовый, v2)
  - Приём .py и .zip файлов в песочницу
  - Бэкап, обновления, восстановление
"""

import os
import shutil
import time
import zipfile
import py_compile
import re
import html
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

import config
import database
from states import PatchState, OwnerFSM

operator_router = Router()

MIDDOT = "\u00b7"


# ==========================================
# 1. МЕНЮ ОВНЕРА
# ==========================================

@operator_router.callback_query(F.data == "submenu_owner")
async def cb_submenu_owner(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return await call.answer("Нет прав!", show_alert=True)
    kb = [
        [InlineKeyboardButton(text="📦 Бэкап кода",     callback_data="owner_backup"),
         InlineKeyboardButton(text="🧩 Модули и Патчи",  callback_data="modules_menu")],
        [InlineKeyboardButton(text="📜 Логи (Lex)",      callback_data="owner_lexlog"),
         InlineKeyboardButton(text="➕ Добавить овнера",  callback_data="add_owner_btn")],
        [InlineKeyboardButton(text="🔄 Перезапуск",      callback_data="owner_restart"),
         InlineKeyboardButton(text="📖 Список команд",   callback_data="owner_commands")],
        [InlineKeyboardButton(text="🔙 Назад",           callback_data="menu_main")],
    ]
    try:
        await call.message.edit_text(
            "👑 <b>Секретное меню Создателя:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
    except Exception:
        pass
    await call.answer()


@operator_router.callback_query(F.data == "owner_restart")
async def cb_owner_restart(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    await call.message.edit_text("🔄 <b>Выполняю перезапуск system LEX...</b>", parse_mode="HTML")
    os._exit(0)


@operator_router.callback_query(F.data == "owner_backup")
async def cb_owner_backup(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    m = await call.message.answer("⏳ Собираю бэкап системы...")
    try:
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        backup_path = os.path.join(config.BACKUP_DIR, f"backup_{int(time.time())}.zip")
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk("."):
                dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "backups", "updates")]
                for file in files:
                    if file.endswith((".db-journal", ".log", ".zip", ".session")):
                        continue
                    zipf.write(os.path.join(root, file))
        size_mb = os.path.getsize(backup_path) / (1024 * 1024)
        await call.message.answer_document(
            FSInputFile(backup_path),
            caption=f"📦 Ваш бэкап ({size_mb:.2f} МБ).",
        )
        try:
            await m.delete()
        except Exception:
            pass
    except Exception as e:
        safe_e = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await call.message.answer(f"❌ Ошибка бэкапа:\n<pre>{safe_e}</pre>", parse_mode="HTML")
    await call.answer()


# ==========================================
# 2. ПРИЁМ ФАЙЛОВ
# ==========================================

@operator_router.message(
    F.document
    & (F.chat.type == "private")
    & F.document.file_name.func(lambda n: n.endswith(".py") or n.endswith(".zip"))
)
async def handle_uploaded_files(message: Message):
    if not database.is_owner(message.from_user.id):
        return
    doc = message.document

    if doc.file_name.endswith(".zip"):
        file_path = os.path.join(config.UPDATES_DIR, "restore.zip")
        file = await message.bot.get_file(doc.file_id)
        await message.bot.download_file(file.file_path, file_path)
        try:
            with zipfile.ZipFile(file_path, "r") as zip_ref:
                if "main.py" not in zip_ref.namelist():
                    os.remove(file_path)
                    return await message.answer("❌ В архиве нет main.py — не похоже на бэкап бота!")
                file_count = len(zip_ref.namelist())
        except Exception as e:
            if os.path.exists(file_path):
                os.remove(file_path)
            return await message.answer(f"❌ Ошибка чтения архива:\n<pre>{e}</pre>", parse_mode="HTML")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Откатить к бэкапу", callback_data="confirm_restore")
        ]])
        return await message.answer(
            f"📦 <b>ZIP загружен!</b>\nФайлов: {file_count}\nГотов к распаковке.",
            parse_mode="HTML",
            reply_markup=kb,
        )

    # .py файл
    if doc.file_name.startswith("mod_") or doc.file_name.startswith("plugin_"):
        file_path = os.path.join(config.MODULES_DIR, doc.file_name)
        file = await message.bot.get_file(doc.file_id)
        await message.bot.download_file(file.file_path, file_path)
        try:
            py_compile.compile(file_path, doraise=True)
            database.set_module_state(doc.file_name, False)
            await message.answer(
                f"✅ Модуль <b>{doc.file_name}</b> загружен (выключен по умолчанию).",
                parse_mode="HTML",
            )
        except py_compile.PyCompileError as e:
            if os.path.exists(file_path):
                os.remove(file_path)
            safe_e = str(e).replace("<", "&lt;").replace(">", "&gt;")
            await message.answer(f"❌ <b>Ошибка синтаксиса!</b>\n<pre>{safe_e}</pre>", parse_mode="HTML")
    else:
        file_path = os.path.join(config.UPDATES_DIR, doc.file_name)
        file = await message.bot.get_file(doc.file_id)
        await message.bot.download_file(file.file_path, file_path)
        try:
            py_compile.compile(file_path, doraise=True)
            await message.answer(
                f"✅ Файл <b>{doc.file_name}</b> в песочнице.\n/update — применить.",
                parse_mode="HTML",
            )
        except py_compile.PyCompileError as e:
            if os.path.exists(file_path):
                os.remove(file_path)
            safe_e = str(e).replace("<", "&lt;").replace(">", "&gt;")
            await message.answer(f"❌ <b>Ошибка синтаксиса!</b>\n<pre>{safe_e}</pre>", parse_mode="HTML")


# ==========================================
# 3. УПРАВЛЕНИЕ МОДУЛЯМИ
# ==========================================

def _module_stem(name: str) -> str:
    """mod_reposter.py → mod_reposter"""
    return name[:-3] if name.endswith(".py") else name


@operator_router.callback_query(F.data == "modules_menu")
async def cb_modules_menu(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    modules = database.get_all_modules()
    kb = []
    for mod in modules:
        stem   = _module_stem(mod["name"])
        status = "✅" if mod["enabled"] else "❌"
        kb.append([
            InlineKeyboardButton(
                text=f"{status} {mod['name']}",
                callback_data=f"mod_toggle:{mod['name']}",
            ),
            InlineKeyboardButton(
                text="⚙️ Настройки",
                callback_data=f"mod_settings:{stem}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"mod_del:{mod['name']}",
            ),
        ])
    kb.append([InlineKeyboardButton(text="🔄 Песочница патчей", callback_data="owner_update")])
    kb.append([InlineKeyboardButton(text="🔙 Назад",            callback_data="submenu_owner")])

    await call.message.edit_text(
        "🧩 <b>Управление Модулями</b>\n"
        "<i>Кидай .py файлы с именем mod_* чтобы добавить модуль.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await call.answer()


@operator_router.callback_query(F.data.startswith("mod_toggle:"))
async def cb_mod_toggle(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    mod_name = call.data.split(":", 1)[1]
    current  = database.is_module_enabled(mod_name)
    database.set_module_state(mod_name, not current)
    label = "ВКЛЮЧЁН" if not current else "ВЫКЛЮЧЕН"
    await call.answer(f"Модуль {label}. Рестарт для применения.", show_alert=True)
    await cb_modules_menu(call)


@operator_router.callback_query(F.data.startswith("mod_settings:"))
async def cb_mod_settings(call: CallbackQuery, state: FSMContext):
    """
    Открывает настройки модуля.
    Пробует вызвать callback «mod_settings_{stem}» — каждый модуль сам регистрирует его.
    Если модуль не зарегистрировал — сообщаем об этом.
    """
    stem = call.data.split(":", 1)[1]

    # Меняем data и пересылаем в диспетчер — aiogram сам найдёт нужный handler
    # Делаем это через явное изменение call.data
    call.__dict__["data"] = f"mod_settings_{stem}"

    # Находим зарегистрированный handler в dp через event-re-dispatch
    # Простой способ: просто отправляем callback с нужным data заново через message.bot
    # Хак: напрямую ищем в роутерах dp
    from aiogram_face import dp as _dp
    handled = False
    for r in _dp.sub_routers + [_dp]:
        for h in r.callback_query.handlers:
            for flt in getattr(h, "filters", []):
                flt_inner = getattr(flt, "filter", None)
                if flt_inner and hasattr(flt_inner, "text"):
                    if getattr(flt_inner, "text", None) == f"mod_settings_{stem}":
                        try:
                            await h.callback(call, state=state)
                            handled = True
                        except Exception:
                            pass
                        break

    if not handled:
        # Простой fallback: ищем callback через update
        try:
            await call.message.edit_text(
                f"⚙️ <b>Настройки: {stem}</b>\n\n"
                "Этот модуль не предоставляет UI настроек.\n"
                "Настройки можно открыть командой, указанной в документации модуля.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔙 К модулям", callback_data="modules_menu")
                ]]),
            )
        except Exception:
            pass
        await call.answer()


@operator_router.callback_query(F.data.startswith("mod_del:"))
async def cb_mod_del(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    mod_name = call.data.split(":", 1)[1]
    database.delete_module_state(mod_name)
    mod_path = os.path.join(config.MODULES_DIR, mod_name)
    if os.path.exists(mod_path):
        os.remove(mod_path)
    await call.answer(f"Модуль {mod_name} удалён!", show_alert=True)
    await cb_modules_menu(call)


# ==========================================
# 4. ПЕСОЧНИЦА ОБНОВЛЕНИЙ
# ==========================================

@operator_router.callback_query(F.data == "owner_update")
async def cb_owner_update(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    py_files = []
    for root, dirs, files in os.walk(config.UPDATES_DIR):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(root, f), config.UPDATES_DIR))
    text = (
        f"🛠 <b>Песочница:</b>\nОжидают: <code>{', '.join(py_files)}</code>"
        if py_files else "🛠 Песочница пуста."
    )
    kb = [
        [InlineKeyboardButton(text="🔄 Применить", callback_data="apply_updates")],
        [InlineKeyboardButton(text="🗑 Очистить",  callback_data="clear_updates")],
        [InlineKeyboardButton(text="🔙 К модулям", callback_data="modules_menu")],
    ]
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()


@operator_router.callback_query(F.data == "apply_updates")
async def cb_apply_updates(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    has = any(
        f.endswith(".py")
        for root, _, files in os.walk(config.UPDATES_DIR)
        if "__pycache__" not in root
        for f in files
    )
    if not has:
        return await call.answer("Песочница пуста!", show_alert=True)
    await call.answer("Применяю...")
    _apply_sandbox()
    await call.message.edit_text("✅ Обновления применены! Перезапускаю...", parse_mode="HTML")
    os._exit(0)


@operator_router.callback_query(F.data == "clear_updates")
async def cb_clear_updates(call: CallbackQuery):
    for f in os.listdir(config.UPDATES_DIR):
        p = os.path.join(config.UPDATES_DIR, f)
        if os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)
    await call.answer("Очищено!")
    await cb_owner_update(call)


@operator_router.callback_query(F.data == "confirm_restore")
async def cb_confirm_restore(call: CallbackQuery):
    if not database.is_owner(call.from_user.id):
        return
    zip_path = os.path.join(config.UPDATES_DIR, "restore.zip")
    if not os.path.exists(zip_path):
        return await call.answer("❌ Архив не найден.", show_alert=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(".")
        os.remove(zip_path)
        await call.message.edit_text("✅ Бэкап распакован! Перезапускаю...")
        os._exit(0)
    except Exception as e:
        safe_e = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await call.message.edit_text(f"❌ Ошибка:\n<pre>{safe_e}</pre>", parse_mode="HTML")


@operator_router.message(Command("update"))
async def cmd_update(message: Message):
    """Применить патчи из песочницы и перезапустить бота."""
    if not database.is_owner(message.from_user.id):
        return
    has = any(
        f.endswith(".py")
        for root, _, files in os.walk(config.UPDATES_DIR)
        if "__pycache__" not in root
        for f in files
    )
    if not has:
        return await message.answer("🛠 Песочница пуста!")
    _apply_sandbox()
    await message.answer("✅ Обновления применены! Перезапускаю...", parse_mode="HTML")
    os._exit(0)


def _apply_sandbox():
    for root, dirs, files in os.walk(config.UPDATES_DIR):
        if "__pycache__" in root:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            rel_path  = os.path.relpath(os.path.join(root, f), config.UPDATES_DIR)
            dest_path = os.path.join(".", rel_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            os.makedirs(config.MICRO_BACKUP_DIR, exist_ok=True)
            if os.path.exists(dest_path):
                flat = rel_path.replace("/", "__SLSH__").replace("\\", "__SLSH__")
                shutil.copyfile(dest_path, os.path.join(config.MICRO_BACKUP_DIR, flat))
            shutil.copyfile(os.path.join(root, f), dest_path)
    for f in os.listdir(config.UPDATES_DIR):
        p = os.path.join(config.UPDATES_DIR, f)
        if os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)


# ==========================================
# 5. ТЕКСТОВЫЙ ПАТЧЕР V2
# ==========================================

def _decode_middot(text: str) -> str:
    return text.replace(MIDDOT, " ")


def get_patch_text(message: Message) -> str:
    text = message.text or message.caption or ""
    return (text
            .replace("§§§", "```").replace("§", "`")
            .replace("∆∆", "__").replace("∆", "_")
            .replace("¢¢", "**").replace("¢", "*"))


def _find_block_in_file(content_lines, old_lines):
    while old_lines and not old_lines[0].strip():
        old_lines.pop(0)
    while old_lines and not old_lines[-1].strip():
        old_lines.pop()
    if not old_lines:
        return -1, -1

    def cmp(s):
        return s.strip()

    first_pat = cmp(old_lines[0])
    if not first_pat:
        return -1, -1

    for i, line in enumerate(content_lines):
        if cmp(line) != first_pat:
            continue
        # Проверяем последовательность
        pi, fi = 0, i
        while fi < len(content_lines) and pi < len(old_lines):
            if not cmp(old_lines[pi]):
                pi += 1
                continue
            if not cmp(content_lines[fi]):
                fi += 1
                continue
            if cmp(content_lines[fi]) == cmp(old_lines[pi]):
                pi += 1
                fi += 1
            else:
                break
        if pi == len(old_lines):
            return i, fi - 1

    return -1, -1


def _find_block_fuzzy(content_lines, old_lines):
    while old_lines and not old_lines[0].strip():
        old_lines.pop(0)
    while old_lines and not old_lines[-1].strip():
        old_lines.pop()
    if not old_lines:
        return -1, -1

    def cmp(s):
        s = re.sub(r"[^\x00-\x7F]", "", s)
        return re.sub(r"\s+", "", s).lower()

    first_pat = cmp(old_lines[0])
    if not first_pat:
        return -1, -1

    for i, line in enumerate(content_lines):
        if cmp(line) != first_pat:
            continue
        pi, fi = 0, i
        while fi < len(content_lines) and pi < len(old_lines):
            if not cmp(old_lines[pi]):
                pi += 1
                continue
            if not cmp(content_lines[fi]):
                fi += 1
                continue
            if cmp(content_lines[fi]) == cmp(old_lines[pi]):
                pi += 1
                fi += 1
            else:
                break
        if pi == len(old_lines):
            return i, fi - 1

    return -1, -1


def _find_block_anchor(content_lines, old_lines):
    non_empty = [l.strip() for l in old_lines if l.strip()]
    if len(non_empty) < 2:
        return -1, -1
    first, last = non_empty[0], non_empty[-1]
    for si in range(len(content_lines)):
        if content_lines[si].strip() != first:
            continue
        for ei in range(si + 1, len(content_lines)):
            if content_lines[ei].strip() == last:
                return si, ei
    return -1, -1


@operator_router.message(Command("patch"))
async def cmd_patch(message: Message, state: FSMContext):
    """Текстовый патчер ядра бота V2."""
    if message.chat.type != "private":
        return
    if not database.is_owner(message.from_user.id):
        return

    raw_text = get_patch_text(message)
    parts    = raw_text.split("\n", 1)

    if len(parts[0].split()) < 2:
        return await message.answer(
            "📋 <b>Формат патча:</b>\n"
            "<code>/patch имя_файла.py\n&lt;&lt;&lt;&lt;\nстарый·код\n====\nновый·код\n&gt;&gt;&gt;&gt;</code>",
            parse_mode="HTML",
        )

    target_file = parts[0].split()[1]
    patch_body  = parts[1] if len(parts) > 1 else ""

    if ">>>>" not in patch_body and "PATCH_END:" not in patch_body:
        await state.set_state(PatchState.collecting)
        await state.update_data(target_file=target_file, patch_buffer=patch_body)
        return await message.answer("⏳ Сообщение разорвано. Жду продолжения (до >>>> )...")

    await _apply_patch(message, state, target_file, patch_body)


@operator_router.message(PatchState.collecting)
async def process_patch_chunk(message: Message, state: FSMContext):
    if message.text and message.text.strip().lower() == "/cancel":
        await state.clear()
        return await message.answer("❌ Патчинг отменён.")

    data   = await state.get_data()
    buffer = data.get("patch_buffer", "") + "\n" + get_patch_text(message)
    await state.update_data(patch_buffer=buffer)

    if ">>>>" in buffer or "PATCH_END:" in buffer:
        await _apply_patch(message, state, data["target_file"], buffer)
    else:
        await message.answer("⏳ Принято. Жду завершения...")


async def _apply_patch(message: Message, state: FSMContext, target_file: str, patch_body: str):
    await state.clear()
    patch_body = _decode_middot(patch_body)

    if "<<<<" in patch_body and "====" in patch_body:
        try:
            body     = patch_body.split("<<<<", 1)[1].split(">>>>", 1)[0]
            old_part, new_part = body.split("====", 1)
        except ValueError:
            return await message.answer("❌ Не могу разобрать блоки <<<< ==== >>>>")
    elif "PATCH_OLD:" in patch_body and "PATCH_NEW:" in patch_body:
        try:
            old_part = patch_body.split("PATCH_OLD:", 1)[1].split("PATCH_NEW:", 1)[0]
            new_part = patch_body.split("PATCH_NEW:", 1)[1].split("PATCH_END:", 1)[0]
        except ValueError:
            return await message.answer("❌ Не могу разобрать PATCH_OLD/PATCH_NEW/PATCH_END")
    else:
        return await message.answer("❌ Нет разделителей патча.")

    old_lines = old_part.strip("\r\n").split("\n")
    new_lines = new_part.strip("\r\n").split("\n")
    for lst in (old_lines, new_lines):
        if lst and lst[0].strip().startswith("```"):
            lst.pop(0)
        if lst and lst[-1].strip() == "```":
            lst.pop()

    file_path  = None
    candidates = [
        target_file,
        os.path.join(config.UPDATES_DIR, target_file),
        os.path.join(config.UPDATES_DIR, os.path.basename(target_file)),
    ]
    for c in candidates:
        if os.path.exists(c):
            file_path = c
            break

    if not file_path:
        return await message.answer(
            f"❌ Файл <code>{html.escape(target_file)}</code> не найден!",
            parse_mode="HTML",
        )

    with open(file_path, "r", encoding="utf-8") as f:
        content_lines = f.read().splitlines()

    start_line, end_line = _find_block_in_file(content_lines, old_lines.copy())
    used_fuzzy = False

    if start_line == -1:
        start_line, end_line = _find_block_fuzzy(content_lines, old_lines.copy())
        if start_line != -1:
            used_fuzzy = True

    if start_line == -1:
        start_line, end_line = _find_block_anchor(content_lines, old_lines.copy())
        if start_line != -1:
            used_fuzzy = True

    if start_line == -1:
        return await message.answer("❌ <b>Старый код не найден!</b>", parse_mode="HTML")

    content_lines = content_lines[:start_line] + new_lines + content_lines[end_line + 1:]

    out_path = os.path.join(config.UPDATES_DIR, target_file)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(content_lines) + "\n")

    try:
        py_compile.compile(out_path, doraise=True)
        note = "\n⚠️ <i>(Нечёткий поиск)</i>" if used_fuzzy else ""
        await message.answer(
            f"✅ Патч применён: <b>{html.escape(target_file)}</b>{note}\nНапиши /update",
            parse_mode="HTML",
        )
    except Exception as e:
        if os.path.exists(out_path):
            os.remove(out_path)
        safe_e = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await message.answer(f"❌ <b>Ошибка компиляции!</b>\n<pre>{safe_e}</pre>", parse_mode="HTML")
