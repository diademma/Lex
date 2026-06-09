# --- START OF FILE database.py ---

import sqlite3
import time
import json
import config


def init_db():
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS bot_owners (user_id INTEGER PRIMARY KEY, username TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS modules_state (module_name TEXT PRIMARY KEY, is_enabled INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS system_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, error_text TEXT, timestamp REAL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_admins (chat_id INTEGER, admin_id INTEGER, admin_username TEXT, UNIQUE(chat_id, admin_id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_tags (chat_id INTEGER, tag TEXT, UNIQUE(chat_id, tag))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS conversation_history_v2 (
        conversation_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        messages_json TEXT,
        last_message_id INTEGER,
        last_message_text TEXT,
        timestamp REAL
    )''')

    # ─── МОДУЛЬ РЕПОСТЕР ────────────────────────────────────────────────────
    cursor.execute('''CREATE TABLE IF NOT EXISTS reposter_chats (
        chat_id INTEGER PRIMARY KEY,
        is_active INTEGER DEFAULT 0,
        interval_minutes INTEGER DEFAULT 60,
        last_post_at REAL DEFAULT 0
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS reposter_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        channel TEXT NOT NULL,
        label TEXT DEFAULT '',
        json_filter TEXT DEFAULT NULL,
        last_seen_msg_id INTEGER DEFAULT 0,
        UNIQUE(chat_id, channel)
    )''')

    # Очередь: накапливаем отфильтрованные посты, max 20 на чат.
    # Старые вытесняются новыми при переполнении.
    cursor.execute('''CREATE TABLE IF NOT EXISTS reposter_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        channel TEXT NOT NULL,
        msg_id INTEGER NOT NULL,
        cleaned_text TEXT DEFAULT '',
        has_media INTEGER DEFAULT 0,
        queued_at REAL DEFAULT 0,
        sent INTEGER DEFAULT 0,
        UNIQUE(chat_id, channel, msg_id)
    )''')

    # Отправленные сообщения (для редактирования кнопок и удаления)
    cursor.execute('''CREATE TABLE IF NOT EXISTS reposter_sent (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,   -- telegram message_id в чате назначения
        channel TEXT NOT NULL,
        sent_at REAL DEFAULT 0,
        deleted INTEGER DEFAULT 0,
        UNIQUE(chat_id, message_id)
    )''')

    # Лайки (❤️) на постах
    cursor.execute('''CREATE TABLE IF NOT EXISTS reposter_likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        UNIQUE(chat_id, message_id, user_id)
    )''')

    # Голоса за удаление (🗑)
    cursor.execute('''CREATE TABLE IF NOT EXISTS reposter_delete_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        UNIQUE(chat_id, message_id, user_id)
    )''')

    conn.commit()
    cursor.execute("INSERT OR IGNORE INTO bot_owners (user_id, username) VALUES (?, ?)", (config.OWNER_ID, "Creator"))
    conn.commit()
    conn.close()


# ───────────────────────────────────────────────────────────────────────────
# ВЛАДЕЛЬЦЫ
# ───────────────────────────────────────────────────────────────────────────

def is_owner(user_id):
    if user_id == config.OWNER_ID:
        return True
    conn = sqlite3.connect(config.DB_NAME)
    res = conn.cursor().execute("SELECT 1 FROM bot_owners WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return bool(res)


def add_owner(user_id, username):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("INSERT OR REPLACE INTO bot_owners (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()


def log_error(error_text):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("INSERT INTO system_logs (error_text, timestamp) VALUES (?, ?)", (error_text, time.time()))
    conn.commit()
    conn.close()


# ───────────────────────────────────────────────────────────────────────────
# АДМИНИСТРАТОРЫ ЧАТОВ
# ───────────────────────────────────────────────────────────────────────────

def get_local_admins(chat_id):
    conn = sqlite3.connect(config.DB_NAME)
    res = conn.cursor().execute("SELECT admin_id, admin_username FROM chat_admins WHERE chat_id = ?", (chat_id,)).fetchall()
    conn.close()
    return res


def add_local_admin(chat_id, user_id, username):
    conn = sqlite3.connect(config.DB_NAME)
    try:
        conn.cursor().execute("INSERT INTO chat_admins (chat_id, admin_id, admin_username) VALUES (?, ?, ?)", (chat_id, user_id, username))
        conn.commit()
        return True, "✅ Admin added!"
    except sqlite3.IntegrityError:
        return False, "❌ Already admin."
    finally:
        conn.close()


def remove_local_admin(chat_id, identifier):
    conn = sqlite3.connect(config.DB_NAME)
    cur = conn.cursor()
    search = str(identifier).replace("@", "")
    if search.isdigit():
        cur.execute("DELETE FROM chat_admins WHERE chat_id = ? AND admin_id = ?", (chat_id, int(search)))
    else:
        cur.execute("DELETE FROM chat_admins WHERE chat_id = ? AND admin_username = ?", (chat_id, search))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted > 0


# ───────────────────────────────────────────────────────────────────────────
# МОДУЛИ
# ───────────────────────────────────────────────────────────────────────────

def get_all_modules():
    conn = sqlite3.connect(config.DB_NAME)
    res = conn.cursor().execute("SELECT module_name, is_enabled FROM modules_state").fetchall()
    conn.close()
    return [{"name": r[0], "enabled": bool(r[1])} for r in res]


def is_module_enabled(module_name):
    conn = sqlite3.connect(config.DB_NAME)
    res = conn.cursor().execute("SELECT is_enabled FROM modules_state WHERE module_name = ?", (module_name,)).fetchone()
    conn.close()
    return bool(res[0]) if res else False


def set_module_state(module_name, is_enabled: bool):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("INSERT OR REPLACE INTO modules_state (module_name, is_enabled) VALUES (?, ?)", (module_name, int(is_enabled)))
    conn.commit()
    conn.close()


def delete_module_state(module_name):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("DELETE FROM modules_state WHERE module_name = ?", (module_name,))
    conn.commit()
    conn.close()


# ───────────────────────────────────────────────────────────────────────────
# КОНТЕКСТНАЯ ПАМЯТЬ V2
# ───────────────────────────────────────────────────────────────────────────

def save_conversation(conversation_id, chat_id, messages, last_message_id, last_message_text):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute(
        "INSERT OR REPLACE INTO conversation_history_v2 "
        "(conversation_id, chat_id, messages_json, last_message_id, last_message_text, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(conversation_id), int(chat_id), json.dumps(messages, ensure_ascii=False),
         int(last_message_id), str(last_message_text), time.time()),
    )
    conn.commit()
    conn.close()


def delete_conversation(conversation_id):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("DELETE FROM conversation_history_v2 WHERE conversation_id = ?", (str(conversation_id),))
    conn.commit()
    conn.close()


def get_conversation_by_reply(chat_id, reply_message_id=None, reply_text=None):
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    conversation_id = None
    messages_json = None

    if reply_message_id is not None:
        res = cursor.execute(
            "SELECT conversation_id, messages_json FROM conversation_history_v2 WHERE last_message_id = ? AND chat_id = ?",
            (int(reply_message_id), int(chat_id)),
        ).fetchone()
        if res:
            conversation_id, messages_json = res[0], res[1]

    if not conversation_id and reply_text:
        one_hour_ago = time.time() - 3600
        candidates = cursor.execute(
            "SELECT conversation_id, messages_json, last_message_text FROM conversation_history_v2 "
            "WHERE chat_id = ? AND timestamp > ? ORDER BY timestamp DESC",
            (int(chat_id), one_hour_ago),
        ).fetchall()
        for conv_id, msg_json, last_text in candidates:
            cleaned_last  = last_text.strip()
            cleaned_reply = reply_text.strip()
            if (cleaned_last in cleaned_reply or cleaned_reply in cleaned_last
                    or (len(cleaned_last) > 10 and cleaned_last[:100] in cleaned_reply)):
                conversation_id, messages_json = conv_id, msg_json
                break

    conn.close()
    if conversation_id and messages_json:
        return conversation_id, json.loads(messages_json)
    return None, []


def update_history_message(message_id, chat_id, new_content):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute(
        "UPDATE conversation_history_v2 SET last_message_text = ? WHERE last_message_id = ? AND chat_id = ?",
        (str(new_content), int(message_id), int(chat_id)),
    )
    conn.commit()
    conn.close()


# ───────────────────────────────────────────────────────────────────────────
# РЕПОСТЕР — настройки чата
# ───────────────────────────────────────────────────────────────────────────

def reposter_get_chat(chat_id: int) -> dict | None:
    conn = sqlite3.connect(config.DB_NAME)
    row = conn.cursor().execute(
        "SELECT chat_id, is_active, interval_minutes, last_post_at FROM reposter_chats WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    if row:
        return {"chat_id": row[0], "is_active": bool(row[1]), "interval_minutes": row[2], "last_post_at": row[3]}
    return None


def reposter_ensure_chat(chat_id: int):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("INSERT OR IGNORE INTO reposter_chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()


def reposter_set_active(chat_id: int, active: bool):
    reposter_ensure_chat(chat_id)
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("UPDATE reposter_chats SET is_active = ? WHERE chat_id = ?", (int(active), chat_id))
    conn.commit()
    conn.close()


def reposter_set_interval(chat_id: int, minutes: int):
    reposter_ensure_chat(chat_id)
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("UPDATE reposter_chats SET interval_minutes = ? WHERE chat_id = ?", (minutes, chat_id))
    conn.commit()
    conn.close()


def reposter_update_last_post(chat_id: int):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("UPDATE reposter_chats SET last_post_at = ? WHERE chat_id = ?", (time.time(), chat_id))
    conn.commit()
    conn.close()


def reposter_get_all_active() -> list[dict]:
    conn = sqlite3.connect(config.DB_NAME)
    rows = conn.cursor().execute(
        "SELECT chat_id, interval_minutes, last_post_at FROM reposter_chats WHERE is_active = 1"
    ).fetchall()
    conn.close()
    return [{"chat_id": r[0], "interval_minutes": r[1], "last_post_at": r[2]} for r in rows]


# ───────────────────────────────────────────────────────────────────────────
# РЕПОСТЕР — источники (каналы)
# ───────────────────────────────────────────────────────────────────────────

def reposter_add_source(chat_id: int, channel: str, label: str = "") -> bool:
    channel = channel.strip().lstrip("@")
    conn    = sqlite3.connect(config.DB_NAME)
    try:
        conn.cursor().execute(
            "INSERT INTO reposter_sources (chat_id, channel, label) VALUES (?, ?, ?)",
            (chat_id, channel, label),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def reposter_remove_source(source_id: int):
    conn = sqlite3.connect(config.DB_NAME)
    row  = conn.cursor().execute(
        "SELECT chat_id, channel FROM reposter_sources WHERE id = ?", (source_id,)
    ).fetchone()
    conn.cursor().execute("DELETE FROM reposter_sources WHERE id = ?", (source_id,))
    if row:
        conn.cursor().execute(
            "DELETE FROM reposter_queue WHERE chat_id = ? AND channel = ?", row
        )
    conn.commit()
    conn.close()


def reposter_get_sources(chat_id: int) -> list[dict]:
    conn = sqlite3.connect(config.DB_NAME)
    rows = conn.cursor().execute(
        "SELECT id, channel, label, json_filter, last_seen_msg_id FROM reposter_sources WHERE chat_id = ? ORDER BY id",
        (chat_id,),
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "channel": r[1], "label": r[2], "json_filter": r[3], "last_seen_msg_id": r[4]}
        for r in rows
    ]


def reposter_get_all_sources() -> list[dict]:
    conn = sqlite3.connect(config.DB_NAME)
    rows = conn.cursor().execute(
        """SELECT rs.id, rs.chat_id, rs.channel, rs.label, rs.json_filter, rs.last_seen_msg_id
           FROM reposter_sources rs
           JOIN reposter_chats rc ON rs.chat_id = rc.chat_id
           WHERE rc.is_active = 1""",
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "chat_id": r[1], "channel": r[2], "label": r[3],
         "json_filter": r[4], "last_seen_msg_id": r[5]}
        for r in rows
    ]


def reposter_set_json_filter(chat_id: int, channel: str, json_str: str | None):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute(
        "UPDATE reposter_sources SET json_filter = ? WHERE chat_id = ? AND channel = ?",
        (json_str, chat_id, channel),
    )
    conn.commit()
    conn.close()


def reposter_update_last_seen(source_id: int, msg_id: int):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute(
        "UPDATE reposter_sources SET last_seen_msg_id = ? WHERE id = ?",
        (msg_id, source_id),
    )
    conn.commit()
    conn.close()


# ───────────────────────────────────────────────────────────────────────────
# РЕПОСТЕР — очередь постов (max 20 на чат)
# ───────────────────────────────────────────────────────────────────────────

QUEUE_HARD_MAX  = 20   # абсолютный максимум постов в очереди
QUEUE_MIN_SEND  = 5    # минимум постов чтобы начать отправку


def reposter_queue_count(chat_id: int) -> int:
    conn = sqlite3.connect(config.DB_NAME)
    n    = conn.cursor().execute(
        "SELECT COUNT(*) FROM reposter_queue WHERE chat_id = ? AND sent = 0", (chat_id,)
    ).fetchone()[0]
    conn.close()
    return n


def reposter_enqueue(chat_id: int, channel: str, msg_id: int, cleaned_text: str, has_media: bool) -> bool:
    """
    Добавить пост в очередь.
    Если очередь >= QUEUE_HARD_MAX — выталкиваем один самый старый пост.
    Возвращает True если пост новый (не дубликат).
    """
    conn = sqlite3.connect(config.DB_NAME)
    cur  = conn.cursor()

    # Ротация: если очередь полная — выкидываем самый старый пост
    count = cur.execute(
        "SELECT COUNT(*) FROM reposter_queue WHERE chat_id = ? AND sent = 0", (chat_id,)
    ).fetchone()[0]
    if count >= QUEUE_HARD_MAX:
        cur.execute(
            "DELETE FROM reposter_queue WHERE id = ("
            "  SELECT id FROM reposter_queue WHERE chat_id = ? AND sent = 0 "
            "  ORDER BY queued_at ASC LIMIT 1"
            ")",
            (chat_id,),
        )

    try:
        cur.execute(
            "INSERT INTO reposter_queue (chat_id, channel, msg_id, cleaned_text, has_media, queued_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, channel, msg_id, cleaned_text, int(has_media), time.time()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()


def reposter_dequeue(chat_id: int) -> dict | None:
    """Взять первый (самый старый) неотправленный пост из очереди."""
    conn = sqlite3.connect(config.DB_NAME)
    row  = conn.cursor().execute(
        "SELECT id, channel, msg_id, cleaned_text, has_media FROM reposter_queue "
        "WHERE chat_id = ? AND sent = 0 ORDER BY queued_at ASC LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    if row:
        return {"id": row[0], "channel": row[1], "msg_id": row[2],
                "cleaned_text": row[3], "has_media": bool(row[4])}
    return None


def reposter_mark_sent(queue_id: int):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute(
        "UPDATE reposter_queue SET sent = 1 WHERE id = ?", (queue_id,)
    )
    conn.commit()
    conn.close()


def reposter_clear_old_sent(days: int = 3):
    cutoff = time.time() - days * 86400
    conn   = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute(
        "DELETE FROM reposter_queue WHERE sent = 1 AND queued_at < ?", (cutoff,)
    )
    conn.commit()
    conn.close()


# ───────────────────────────────────────────────────────────────────────────
# РЕПОСТЕР — отправленные сообщения (для кнопок и удаления)
# ───────────────────────────────────────────────────────────────────────────

def reposter_record_sent(chat_id: int, message_id: int, channel: str):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute(
        "INSERT OR IGNORE INTO reposter_sent (chat_id, message_id, channel, sent_at) VALUES (?, ?, ?, ?)",
        (chat_id, message_id, channel, time.time()),
    )
    conn.commit()
    conn.close()


def reposter_is_sent_msg(chat_id: int, message_id: int) -> bool:
    conn = sqlite3.connect(config.DB_NAME)
    row  = conn.cursor().execute(
        "SELECT deleted FROM reposter_sent WHERE chat_id = ? AND message_id = ? AND deleted = 0",
        (chat_id, message_id),
    ).fetchone()
    conn.close()
    return bool(row)


def reposter_mark_deleted(chat_id: int, message_id: int):
    conn = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute(
        "UPDATE reposter_sent SET deleted = 1 WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    conn.cursor().execute(
        "DELETE FROM reposter_likes WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    conn.cursor().execute(
        "DELETE FROM reposter_delete_votes WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    conn.commit()
    conn.close()


def reposter_clean_sent(days: int = 7):
    cutoff = time.time() - days * 86400
    conn   = sqlite3.connect(config.DB_NAME)
    conn.cursor().execute("DELETE FROM reposter_sent WHERE sent_at < ?", (cutoff,))
    conn.commit()
    conn.close()


# ───────────────────────────────────────────────────────────────────────────
# РЕПОСТЕР — голосование (❤️ и 🗑)
# ───────────────────────────────────────────────────────────────────────────

def reposter_like_toggle(chat_id: int, message_id: int, user_id: int) -> int:
    """
    Переключить лайк пользователя. Возвращает текущее количество лайков.
    """
    conn = sqlite3.connect(config.DB_NAME)
    cur  = conn.cursor()
    exists = cur.execute(
        "SELECT 1 FROM reposter_likes WHERE chat_id = ? AND message_id = ? AND user_id = ?",
        (chat_id, message_id, user_id),
    ).fetchone()
    if exists:
        cur.execute(
            "DELETE FROM reposter_likes WHERE chat_id = ? AND message_id = ? AND user_id = ?",
            (chat_id, message_id, user_id),
        )
    else:
        try:
            cur.execute(
                "INSERT INTO reposter_likes (chat_id, message_id, user_id) VALUES (?, ?, ?)",
                (chat_id, message_id, user_id),
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    count = cur.execute(
        "SELECT COUNT(*) FROM reposter_likes WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()[0]
    conn.close()
    return count


def reposter_delete_vote(chat_id: int, message_id: int, user_id: int) -> int:
    """
    Добавить голос за удаление (уникальный per user).
    Возвращает текущее кол-во голосов.
    """
    conn = sqlite3.connect(config.DB_NAME)
    cur  = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO reposter_delete_votes (chat_id, message_id, user_id) VALUES (?, ?, ?)",
            (chat_id, message_id, user_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # уже голосовал
    count = cur.execute(
        "SELECT COUNT(*) FROM reposter_delete_votes WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()[0]
    conn.close()
    return count


def reposter_get_vote_counts(chat_id: int, message_id: int) -> tuple[int, int]:
    """Возвращает (likes, delete_votes)."""
    conn        = sqlite3.connect(config.DB_NAME)
    likes       = conn.cursor().execute(
        "SELECT COUNT(*) FROM reposter_likes WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()[0]
    del_votes   = conn.cursor().execute(
        "SELECT COUNT(*) FROM reposter_delete_votes WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()[0]
    conn.close()
    return likes, del_votes
