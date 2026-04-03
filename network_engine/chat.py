# network_engine/chat.py — Чат, Quick Messages, Chat Media, Chat History

import time

from config import (
    CMD_QUICK_MSG, QUICK_MSG_MAX_LEN,
    CMD_CHAT_MSG, CMD_CHAT_HISTORY, CMD_CHAT_HISTORY_REQ,
    CHAT_MSG_MAX_LEN, CHAT_HISTORY_MAX,
    CMD_CHAT_MEDIA, CHAT_MEDIA_MAX_B64,
    CMD_TYPING, TYPING_THROTTLE_MS,
)


class ChatMixin:
    """Методы чата: сообщения, медиа, история."""

    # ------------------------------------------------------------------
    # Отправка
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------
    _last_typing_ts: float = 0.0

    def send_typing(self) -> None:
        """Отправить typing indicator (throttled, не чаще TYPING_THROTTLE_MS)."""
        import time
        now = time.time()
        if now - self._last_typing_ts < TYPING_THROTTLE_MS / 1000.0:
            return
        self._last_typing_ts = now
        self.send_json({'action': CMD_TYPING})

    def send_quick_msg(self, text: str) -> None:
        text = text.strip()[:QUICK_MSG_MAX_LEN]
        if not text:
            return
        self.send_json({'action': CMD_QUICK_MSG, 'text': text})

    def send_chat_msg(self, text: str) -> None:
        text = text.strip()[:CHAT_MSG_MAX_LEN]
        if not text:
            return
        self.send_json({'action': CMD_CHAT_MSG, 'text': text})

    def send_chat_media(
        self, file_name: str, file_type: str, file_data_b64: str
    ) -> None:
        if not file_data_b64 or len(file_data_b64) > CHAT_MEDIA_MAX_B64:
            print(f"[Net] send_chat_media: файл слишком большой или пустой")
            return
        self.send_json({
            'action':       CMD_CHAT_MEDIA,
            'file_name':    file_name,
            'file_type':    file_type,
            'file_data_b64': file_data_b64,
        })

    # ------------------------------------------------------------------
    # process_message dispatch для чата
    # ------------------------------------------------------------------
    def _process_chat_message(self, msg: dict, act: str) -> bool:
        """Обрабатывает сообщения чата. Возвращает True если обработано."""

        if act == CMD_QUICK_MSG:
            sender_uid  = int(msg.get('uid', 0))
            from_nick   = str(msg.get('from_nick', '?'))
            text        = str(msg.get('text', ''))
            if text:
                self.quick_msg_received.emit(sender_uid, from_nick, text)
            return True

        elif act == CMD_CHAT_MSG:
            sender_uid = int(msg.get('uid', 0))
            from_nick  = str(msg.get('from_nick', '?'))
            text       = str(msg.get('text', ''))
            avatar     = msg.get('avatar', '')
            ts         = float(msg.get('ts', time.time()))
            room       = str(msg.get('room', ''))
            if text:
                entry = {
                    'uid':    sender_uid,
                    'nick':   from_nick,
                    'avatar': avatar,
                    'text':   text,
                    'ts':     ts,
                    'room':   room,
                }
                self._chat_history.append(entry)
                if len(self._chat_history) > CHAT_HISTORY_MAX:
                    del self._chat_history[0]
                self.chat_msg_received.emit(entry)
            return True

        elif act == CMD_CHAT_HISTORY:
            messages = msg.get('messages', [])
            if isinstance(messages, list) and messages:
                existing = {(m.get('uid', 0), m.get('ts', 0))
                            for m in self._chat_history}
                for m in messages:
                    key = (m.get('uid', 0), m.get('ts', 0))
                    if key not in existing:
                        self._chat_history.append(m)
                        existing.add(key)
                self._chat_history.sort(key=lambda m: m.get('ts', 0))
                if len(self._chat_history) > CHAT_HISTORY_MAX:
                    self._chat_history = self._chat_history[-CHAT_HISTORY_MAX:]
                self.chat_history_received.emit(list(self._chat_history))
            return True

        elif act == CMD_CHAT_MEDIA:
            entry = {
                'uid':          int(msg.get('uid', 0)),
                'nick':         str(msg.get('from_nick', '?')),
                'avatar':       msg.get('avatar', ''),
                'ts':           float(msg.get('ts', time.time())),
                'room':         str(msg.get('room', '')),
                'text':         '',
                'file_name':    msg.get('file_name', 'file'),
                'file_type':    msg.get('file_type', 'file'),
                'file_data_b64': msg.get('file_data_b64', ''),
            }
            if entry['file_data_b64']:
                self._chat_history.append(entry)
                if len(self._chat_history) > CHAT_HISTORY_MAX:
                    del self._chat_history[0]
                self.chat_media_received.emit(entry)
            return True

        elif act == CMD_CHAT_HISTORY_REQ:
            requester_uid = int(msg.get('requester_uid', 0))
            if requester_uid and self._chat_history:
                history_slice = self._chat_history[-100:]
                self.send_json({
                    'action':     CMD_CHAT_HISTORY,
                    'target_uid': requester_uid,
                    'messages':   history_slice,
                })
                print(f"[Net] chat_history → uid={requester_uid}: "
                      f"{len(history_slice)} сообщений")
            return True

        elif act == CMD_TYPING:
            sender_uid = int(msg.get('uid', 0))
            sender_nick = str(msg.get('nick', '?'))
            if sender_uid:
                self.typing_received.emit(sender_uid, sender_nick)
            return True

        return False
