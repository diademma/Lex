# --- START OF FILE reposter_worker.py ---
"""
Репостер-воркер LEX v2 — без Telethon, без юзербота.

Механика:
  - Fetcher  (каждые 20 мин) читает t.me/s/{channel}, чистит рекламу,
    применяет JSON-фильтр и кладёт посты в reposter_queue (max 20 на чат).
    Более свежие посты вытесняют старые при переполнении.

  - Sender (каждые 60 сек) проверяет активные чаты.
    Отправляет пост ТОЛЬКО если в очереди >= 5 постов.
    Под каждым постом — кнопки ❤️ и 🗑️.
    Telegram message_id сохраняется для удаления по реакции 💊.
"""

import asyncio
import json
import logging
import re
import time

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database

# ─────────────────────────────────────────────────────────────────────────────
# КОНСТАНТЫ
# ─────────────────────────────────────────────────────────────────────────────

FETCH_INTERVAL   = 20 * 60   # как часто забираем новые посты (сек)
SEND_TICK        = 60         # как часто проверяем нужно ли слать (сек)
CHANNEL_LIMIT    = 25         # сколько постов парсим с t.me/s за раз
DELETE_VOTES_REQ = 2          # сколько голосов 🗑️ нужно чтобы удалить

AD_PATTERNS = [
    re.compile(r"https?://\S+",          re.IGNORECASE),
    re.compile(r"www\.\S+",             re.IGNORECASE),
    re.compile(r"t\.me/\S*",           re.IGNORECASE),
    re.compile(r"@[A-Za-z0-9_]{3,}"),
    re.compile(r"\+\d[\d\s\-\(\)]{6,}\d"),
]


# ─────────────────────────────────────────────────────────────────────────────
# ОЧИСТКА РЕКЛАМЫ
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    if not text:
        return ""
    for pat in AD_PATTERNS:
        text = pat.sub("", text)
    text = re.sub(r"[ \t]+",  " ",    text)
    text = re.sub(r"\n{3,}",  "\n\n", text)
    text = re.sub(r"[ \t\n\r:;.\-|👇⬇️➡️]+$", "", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# JSON-ФИЛЬТР (мощная версия)
# ─────────────────────────────────────────────────────────────────────────────

_FILTER_ALLOWED_KEYS = {
    # Ключевые слова
    "require_any", "require_all", "exclude_any", "exclude_all",
    # Группы (хотя бы одна группа целиком совпадает)
    "require_group_any",
    # Регулярные выражения
    "require_regex", "exclude_regex",
    # Длина
    "min_chars", "max_chars", "min_words", "max_words",
    # Структура
    "require_newlines", "max_uppercase_ratio",
    # Медиа
    "require_media",
    # Скоринг (список правил + минимальный балл)
    "score_rules", "min_score",
    # Флаги
    "case_sensitive",
    # Мета
    "comment",
}

_FILTER_SCHEMA_EXAMPLE = """{
  "comment":             "Описание фильтра (игнорируется логикой)",

  // Ключевые слова (проверяются без учёта регистра по умолчанию)
  "require_any":         ["слово1", "слово2"],   // хотя бы ОДНО должно быть
  "require_all":         ["слово1", "слово2"],   // ВСЕ должны быть (AND)
  "exclude_any":         ["спам", "реклама"],    // НИЧЕГО из этих не должно быть
  "exclude_all":         ["слово1", "слово2"],   // отбросить если ВСЕ присутствуют

  // Группы: проходит если ХОТЯ БЫ ОДНА группа полностью совпала
  "require_group_any": [
    ["означает", "слово"],        // группа 1: оба слова в тексте
    ["расшифровыва"],             // группа 2: достаточно одного
    ["аббревиатур", "переводится"]// группа 3
  ],

  // Регулярные выражения (Python re, IGNORECASE)
  "require_regex":       "^[А-ЯЁ]",             // текст начинается с заглавной рус. буквы
  "exclude_regex":       "http|@\\w+",           // нет ссылок и упоминаний

  // Длина
  "min_chars":           80,      // минимум символов (после очистки рекламы)
  "max_chars":           5000,    // максимум символов
  "min_words":           10,      // минимум слов
  "max_words":           600,     // максимум слов

  // Структура
  "require_newlines":    1,       // минимум N символов переноса строки
  "max_uppercase_ratio": 0.4,     // макс. доля ЗАГЛАВНЫХ букв (0.0–1.0)

  // Медиа: null=любой, true=только с медиа, false=только текст
  "require_media":       null,

  // Учитывать регистр при проверке ключевых слов
  "case_sensitive":      false,

  // Скоринг: пост проходит если набрал >= min_score баллов
  "score_rules": [
    {"contains":  "означает",    "score": 3},
    {"contains":  "расшифров",   "score": 3},
    {"min_chars": 150,           "score": 2},
    {"has_media": true,          "score": 1}
  ],
  "min_score": 3
}"""


def validate_json_filter(raw: str) -> tuple[bool, str]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return False, f"Невалидный JSON: {e}"

    unknown = set(obj.keys()) - _FILTER_ALLOWED_KEYS
    if unknown:
        return False, f"Неизвестные ключи: {unknown}. Допустимы: {_FILTER_ALLOWED_KEYS}"

    for key in ("require_any", "require_all", "exclude_any", "exclude_all"):
        if key in obj:
            if not isinstance(obj[key], list):
                return False, f"'{key}' должен быть массивом строк."
            if not all(isinstance(x, str) for x in obj[key]):
                return False, f"'{key}' должен содержать только строки."

    if "require_group_any" in obj:
        rg = obj["require_group_any"]
        if not isinstance(rg, list):
            return False, "'require_group_any' — список списков строк."
        for grp in rg:
            if not isinstance(grp, list) or not all(isinstance(x, str) for x in grp):
                return False, "Каждая группа в 'require_group_any' — список строк."

    for key in ("require_regex", "exclude_regex"):
        if key in obj and obj[key] is not None:
            if not isinstance(obj[key], str):
                return False, f"'{key}' должен быть строкой-regex."
            try:
                re.compile(obj[key], re.IGNORECASE)
            except re.error as e:
                return False, f"Невалидный regex в '{key}': {e}"

    for key in ("min_chars", "max_chars", "min_words", "max_words", "require_newlines"):
        if key in obj and obj[key] is not None:
            if not isinstance(obj[key], int) or obj[key] < 0:
                return False, f"'{key}' — целое число >= 0."

    if "max_uppercase_ratio" in obj and obj["max_uppercase_ratio"] is not None:
        v = obj["max_uppercase_ratio"]
        if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
            return False, "'max_uppercase_ratio' — число от 0.0 до 1.0."

    if "require_media" in obj and obj["require_media"] not in (True, False, None):
        return False, "'require_media' — true, false или null."

    if "score_rules" in obj:
        if not isinstance(obj["score_rules"], list):
            return False, "'score_rules' — массив объектов."
        for rule in obj["score_rules"]:
            if not isinstance(rule, dict):
                return False, "Каждое правило в 'score_rules' — объект."
            if "score" not in rule:
                return False, "Каждое правило должно иметь поле 'score'."

    if "min_score" in obj and not isinstance(obj["min_score"], (int, float)):
        return False, "'min_score' — число."

    return True, ""


def passes_json_filter(text: str, has_media: bool, json_filter_str: str | None) -> bool:
    if not json_filter_str:
        return True
    try:
        f = json.loads(json_filter_str)
    except Exception:
        return True

    case_sensitive = f.get("case_sensitive", False)
    cmp_text = text if case_sensitive else text.lower()

    def kw(s: str) -> str:
        return s if case_sensitive else s.lower()

    # require_any
    req_any = f.get("require_any", [])
    if req_any and not any(kw(w) in cmp_text for w in req_any):
        return False

    # require_all
    req_all = f.get("require_all", [])
    if req_all and not all(kw(w) in cmp_text for w in req_all):
        return False

    # exclude_any
    exc_any = f.get("exclude_any", [])
    if any(kw(w) in cmp_text for w in exc_any):
        return False

    # exclude_all (отбросить только если ВСЕ присутствуют)
    exc_all = f.get("exclude_all", [])
    if exc_all and all(kw(w) in cmp_text for w in exc_all):
        return False

    # require_group_any (хотя бы одна группа целиком совпадает)
    req_grp = f.get("require_group_any", [])
    if req_grp:
        if not any(all(kw(w) in cmp_text for w in grp) for grp in req_grp):
            return False

    # require_regex
    req_rx = f.get("require_regex")
    if req_rx:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            if not re.search(req_rx, text, flags):
                return False
        except re.error:
            pass

    # exclude_regex
    exc_rx = f.get("exclude_regex")
    if exc_rx:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            if re.search(exc_rx, text, flags):
                return False
        except re.error:
            pass

    # min/max chars
    min_c = f.get("min_chars")
    if min_c and len(text) < min_c:
        return False
    max_c = f.get("max_chars")
    if max_c and len(text) > max_c:
        return False

    # min/max words
    words = text.split()
    min_w = f.get("min_words")
    if min_w and len(words) < min_w:
        return False
    max_w = f.get("max_words")
    if max_w and len(words) > max_w:
        return False

    # require_newlines
    req_nl = f.get("require_newlines")
    if req_nl and text.count("\n") < req_nl:
        return False

    # max_uppercase_ratio
    max_up = f.get("max_uppercase_ratio")
    if max_up is not None and text:
        letters = [c for c in text if c.isalpha()]
        if letters:
            ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            if ratio > max_up:
                return False

    # require_media
    req_media = f.get("require_media")
    if req_media is True and not has_media:
        return False
    if req_media is False and has_media:
        return False

    # score_rules
    score_rules = f.get("score_rules", [])
    if score_rules:
        score = 0
        for rule in score_rules:
            rule_score = rule.get("score", 1)
            if "contains" in rule and kw(rule["contains"]) in cmp_text:
                score += rule_score
            if "min_chars" in rule and len(text) >= rule["min_chars"]:
                score += rule_score
            if "max_chars" in rule and len(text) <= rule["max_chars"]:
                score += rule_score
            if "has_media" in rule and rule["has_media"] == has_media:
                score += rule_score
            if "min_words" in rule and len(words) >= rule["min_words"]:
                score += rule_score
            if "regex_match" in rule:
                try:
                    flags = 0 if case_sensitive else re.IGNORECASE
                    if re.search(rule["regex_match"], text, flags):
                        score += rule_score
                except re.error:
                    pass
        min_score = f.get("min_score", 0)
        if score < min_score:
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# ПАРСЕР t.me/s
# ─────────────────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LexReposterBot/2.0; +https://t.me)",
    "Accept-Language": "ru,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

_RE_MSG_ID    = re.compile(r'data-post="[^/]+/(\d+)"')
_RE_MSG_TEXT  = re.compile(
    r'class="[^"]*tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_RE_HAS_MEDIA = re.compile(
    r'tgme_widget_message_photo_wrap|tgme_widget_message_video|tgme_widget_message_document_wrap',
    re.IGNORECASE,
)
_RE_SERVICE   = re.compile(r'tgme_widget_message_service', re.IGNORECASE)


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
             .strip())


async def fetch_tme_posts(channel: str, limit: int = CHANNEL_LIMIT) -> list[dict]:
    channel = channel.lstrip("@")
    url     = f"https://t.me/s/{channel}"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    logging.warning(f"[Reposter.fetch] t.me/s/{channel} → {resp.status}")
                    return []
                html = await resp.text()
    except Exception as e:
        logging.error(f"[Reposter.fetch] Ошибка запроса для {channel}: {e}")
        return []

    posts  = []
    chunks = re.split(r"(?=data-post=\"[^\"]+/\d+\")", html)
    for chunk in chunks:
        id_m = _RE_MSG_ID.search(chunk)
        if not id_m:
            continue
        if _RE_SERVICE.search(chunk):
            continue
        msg_id    = int(id_m.group(1))
        has_media = bool(_RE_HAS_MEDIA.search(chunk))
        text_m    = _RE_MSG_TEXT.search(chunk)
        raw_text  = _strip_html(text_m.group(1)) if text_m else ""
        posts.append({"msg_id": msg_id, "raw_text": raw_text, "has_media": has_media})

    return posts[-limit:]


# ─────────────────────────────────────────────────────────────────────────────
# КНОПКИ ГОЛОСОВАНИЯ
# ─────────────────────────────────────────────────────────────────────────────

def _vote_markup(chat_id: int, message_id: int, likes: int = 0, del_votes: int = 0) -> InlineKeyboardMarkup:
    heart_label = f"❤️  {likes}" if likes else "❤️"
    del_label   = f"🗑  {del_votes}/{DELETE_VOTES_REQ}" if del_votes else "🗑  Лишнее"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=heart_label, callback_data=f"rp_like:{chat_id}:{message_id}"),
        InlineKeyboardButton(text=del_label,   callback_data=f"rp_del:{chat_id}:{message_id}"),
    ]])


# ─────────────────────────────────────────────────────────────────────────────
# ВОРКЕР-СБОРЩИК
# ─────────────────────────────────────────────────────────────────────────────

async def _fetcher(bot: Bot):
    while True:
        sources = database.reposter_get_all_sources()
        logging.info(f"[Reposter.fetcher] Тик — источников: {len(sources)}")

        for src in sources:
            chat_id     = src["chat_id"]
            channel     = src["channel"]
            src_id      = src["id"]
            json_filter = src["json_filter"]
            last_seen   = src["last_seen_msg_id"]

            try:
                posts = await fetch_tme_posts(channel)
            except Exception as e:
                logging.error(f"[Reposter.fetcher] Ошибка для {channel}: {e}")
                continue

            new_max_id = last_seen
            added      = 0

            for post in posts:
                msg_id    = post["msg_id"]
                raw_text  = post["raw_text"]
                has_media = post["has_media"]

                if msg_id <= last_seen:
                    continue

                clean = clean_text(raw_text)

                # Пустой текст без медиа → реклама, пропускаем
                if not clean and not has_media:
                    new_max_id = max(new_max_id, msg_id)
                    continue

                if not passes_json_filter(clean, has_media, json_filter):
                    new_max_id = max(new_max_id, msg_id)
                    continue

                # enqueue автоматически вытесняет старый пост при переполнении
                ok = database.reposter_enqueue(chat_id, channel, msg_id, clean, has_media)
                if ok:
                    added += 1

                new_max_id = max(new_max_id, msg_id)

            if new_max_id > last_seen:
                database.reposter_update_last_seen(src_id, new_max_id)

            if added > 0:
                logging.info(f"[Reposter.fetcher] {channel} → chat {chat_id}: +{added} постов")

            await asyncio.sleep(1.5)

        database.reposter_clear_old_sent(days=3)
        database.reposter_clean_sent(days=7)
        await asyncio.sleep(FETCH_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# ВОРКЕР-ОТПРАВЩИК
# ─────────────────────────────────────────────────────────────────────────────

async def _sender(bot: Bot):
    while True:
        await asyncio.sleep(SEND_TICK)
        now    = time.time()
        active = database.reposter_get_all_active()

        for entry in active:
            chat_id  = entry["chat_id"]
            interval = entry["interval_minutes"]
            last_at  = entry["last_post_at"]

            if now - last_at < interval * 60:
                continue

            # Не отправляем если постов меньше 5 — ждём накопления
            queue_n = database.reposter_queue_count(chat_id)
            if queue_n < database.QUEUE_MIN_SEND:
                logging.info(
                    f"[Reposter.sender] chat {chat_id}: "
                    f"очередь {queue_n}/{database.QUEUE_MIN_SEND} — ждём."
                )
                continue

            post = database.reposter_dequeue(chat_id)
            if not post:
                continue

            sent_msg_id = await _send_post(bot, chat_id, post)
            if sent_msg_id:
                database.reposter_record_sent(chat_id, sent_msg_id, post["channel"])
                database.reposter_mark_sent(post["id"])
                database.reposter_update_last_post(chat_id)
                logging.info(
                    f"[Reposter.sender] ✅ chat {chat_id} ← "
                    f"{post['channel']}#{post['msg_id']} (msg_id={sent_msg_id})"
                )
            else:
                # Пост не отправлен — помечаем отправленным чтобы не застрять
                database.reposter_mark_sent(post["id"])
                database.reposter_update_last_post(chat_id)

            await asyncio.sleep(1.0)


async def _send_post(bot: Bot, chat_id: int, post: dict) -> int | None:
    """
    Отправить пост и вернуть telegram message_id отправленного сообщения.
    Возвращает None если не удалось отправить.
    """
    channel      = post["channel"]
    msg_id       = post["msg_id"]
    cleaned_text = post["cleaned_text"]
    has_media    = post["has_media"]

    # Временная заглушка-клавиатура (без реального message_id)
    # Настоящая клавиатура с правильным message_id редактируется сразу после отправки
    placeholder_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❤️",          callback_data="rp_noop"),
        InlineKeyboardButton(text="🗑  Лишнее",   callback_data="rp_noop"),
    ]])

    try:
        if not has_media:
            if not cleaned_text:
                return None
            sent = await bot.send_message(chat_id, cleaned_text, reply_markup=placeholder_kb)
        else:
            caption = cleaned_text[:1020] if cleaned_text else None
            sent    = await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=f"@{channel}",
                message_id=msg_id,
                caption=caption,
                reply_markup=placeholder_kb,
            )

        # Обновляем кнопки с реальным message_id
        real_kb = _vote_markup(chat_id, sent.message_id)
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=sent.message_id,
                reply_markup=real_kb,
            )
        except Exception:
            pass

        return sent.message_id

    except TelegramForbiddenError:
        logging.warning(f"[Reposter.sender] Бот заблокирован в chat {chat_id}")
        database.reposter_set_active(chat_id, False)
        return None
    except TelegramBadRequest as e:
        logging.warning(f"[Reposter.sender] BadRequest ({chat_id}): {e}")
        return None
    except Exception as e:
        logging.error(f"[Reposter.sender] Ошибка ({chat_id}): {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ТОЧКА ВХОДА
# ─────────────────────────────────────────────────────────────────────────────

async def reposter_worker(bot: Bot):
    logging.info("[Reposter] Воркер запущен (Bot API, без юзербота).")
    await asyncio.gather(
        _fetcher(bot),
        _sender(bot),
    )
