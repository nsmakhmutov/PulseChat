# server/chat_db.py — SQLite-хранилище чата для хоста сервера
#
# Хост хранит историю на диске. При перезапуске — история сохраняется.
# При миграции сервера — история остаётся у старого хоста (новая сессия).
# Клиенты получают историю через CMD_CHAT_HISTORY при подключении.

import json
import os
import sqlite3
import threading
import time

from config import CHAT_DB_PATH, CHAT_HISTORY_MAX


class ChatDB:
    """Потокобезопасный SQLite wrapper для серверного чата."""

    def __init__(self, db_path: str = CHAT_DB_PATH):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads
        self._conn.execute("PRAGMA synchronous=NORMAL")  # fast writes
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                uid         INTEGER NOT NULL,
                nick        TEXT NOT NULL,
                avatar      TEXT DEFAULT '',
                text        TEXT DEFAULT '',
                room        TEXT DEFAULT '',
                ts          REAL NOT NULL,
                file_name   TEXT DEFAULT '',
                file_type   TEXT DEFAULT '',
                file_data_b64 TEXT DEFAULT ''
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_room_ts
            ON chat_messages(room, ts)
        """)
        self._conn.commit()
        print(f"[ChatDB] Инициализирован: {self._db_path}")

    def add_message(self, entry: dict) -> None:
        """Сохраняет сообщение (текст или медиа)."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO chat_messages
                   (uid, nick, avatar, text, room, ts, file_name, file_type, file_data_b64)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(entry.get('uid', 0)),
                    str(entry.get('nick', '?')),
                    str(entry.get('avatar', '')),
                    str(entry.get('text', '')),
                    str(entry.get('room', '')),
                    float(entry.get('ts', time.time())),
                    str(entry.get('file_name', '')),
                    str(entry.get('file_type', '')),
                    str(entry.get('file_data_b64', '')),
                ),
            )
            self._conn.commit()

        # Trim старые сообщения
        self._trim()

    def get_history(self, room: str = '', limit: int = CHAT_HISTORY_MAX) -> list[dict]:
        """Возвращает последние N сообщений для комнаты (или все если room='')."""
        with self._lock:
            if room:
                rows = self._conn.execute(
                    """SELECT uid, nick, avatar, text, room, ts,
                              file_name, file_type, file_data_b64
                       FROM chat_messages
                       WHERE room = ?
                       ORDER BY ts DESC LIMIT ?""",
                    (room, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT uid, nick, avatar, text, room, ts,
                              file_name, file_type, file_data_b64
                       FROM chat_messages
                       ORDER BY ts DESC LIMIT ?""",
                    (limit,),
                ).fetchall()

        messages = []
        for row in reversed(rows):  # reversed: от старых к новым
            entry = {
                'uid': row[0], 'nick': row[1], 'avatar': row[2],
                'text': row[3], 'room': row[4], 'ts': row[5],
            }
            if row[6]:  # file_name — медиа-вложение
                entry['file_name'] = row[6]
                entry['file_type'] = row[7]
                entry['file_data_b64'] = row[8]
            messages.append(entry)

        return messages

    def _trim(self) -> None:
        """Удаляем сообщения сверх лимита."""
        with self._lock:
            self._conn.execute(
                """DELETE FROM chat_messages
                   WHERE id NOT IN (
                       SELECT id FROM chat_messages
                       ORDER BY ts DESC LIMIT ?
                   )""",
                (CHAT_HISTORY_MAX * 2,),  # запас x2
            )
            self._conn.commit()

    def clear(self) -> int:
        """Удаляет ВСЕ сообщения из БД. Возвращает число удалённых строк."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM chat_messages")
            self._conn.commit()
            deleted = cur.rowcount
            # VACUUM вне транзакции — освобождает место на диске
            try:
                self._conn.execute("VACUUM")
            except Exception:
                pass
            print(f"[ChatDB] Очищена: {deleted} сообщений удалено")
            return deleted

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None