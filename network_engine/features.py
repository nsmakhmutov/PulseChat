# network_engine/features.py — Soundboard, Nudge, File Transfer, Draw Stroke, Host Mute
#
# Миксин FeaturesMixin: все методы используют self.* атрибуты NetworkClient.

import base64
import io
import os
import threading
import time

import numpy as np
import sounddevice as sd
import soundfile as sf
from PyQt6.QtCore import QSettings

from config import (
    resource_path,
    CMD_NUDGE_VOTE, CMD_PLAY_NUDGE, CMD_NUDGE_TRIGGERED, NUDGE_SOUND_PATH,
    CMD_FILE_OFFER, CMD_FILE_OFFER_ROOM,
    CMD_QUICK_MSG, QUICK_MSG_MAX_LEN,
    CMD_HOST_MUTE, CMD_FORCE_MUTED, CMD_HOST_MOVE,
    CMD_HOST_KICK, CMD_HOST_BAN, CMD_HOST_UNBAN,
    CMD_BAN_LIST_REQ, CMD_BAN_LIST,
    CMD_KICKED, CMD_BANNED,
    CMD_DRAW_STROKE, DRAW_MAX_POINTS,
    CMD_REMOTE_CONTROL_REQUEST, CMD_REMOTE_CONTROL_RESPONSE,
    CMD_REMOTE_CONTROL_EVENT, CMD_REMOTE_CONTROL_STOP,
)


class FeaturesMixin:
    """Методы фич: soundboard, nudge, file transfer, draw stroke, host mute."""

    # ------------------------------------------------------------------
    # Soundboard
    # ------------------------------------------------------------------
    def play_soundboard_file(self, filename, data_b64=None, from_nick=None,
                             src_uid=None):
        """
        Воспроизвести soundboard-файл через sounddevice.
        Разрешён одновременный запуск нескольких звуков.
        """
        try:
            _gs = getattr(self.audio, 'global_settings', None)
            if _gs is None:
                _gs = QSettings("MyVoiceChat", "GlobalSettings")

            # Звук при входе (__joinsound__) — это персональный звук входа
            # пользователя. Громкость берём от «Системные звуки», тост автора
            # НЕ показываем (это не обычный soundboard).
            is_join_sound = bool(filename and filename.startswith("__joinsound__:"))

            # Свой собственный звук входа НЕ проигрываем у себя — его слышат
            # только собеседники (сервер рассылает CMD_SOUNDBOARD всем, включая
            # отправителя, поэтому отсекаем по src_uid == my_uid).
            if is_join_sound and src_uid is not None:
                try:
                    if int(src_uid) == int(getattr(self.audio, 'my_uid', 0)):
                        return
                except (TypeError, ValueError):
                    pass

            if is_join_sound:
                raw = int(_gs.value("system_sound_volume", 30)) / 100.0
            else:
                raw = int(_gs.value("soundboard_volume", 40)) / 100.0
            vol = raw ** 2

            if data_b64:
                try:
                    audio_bytes  = base64.b64decode(data_b64)
                    audio_source = io.BytesIO(audio_bytes)
                except Exception as e:
                    print(f"[Net] Soundboard base64 decode error: {e}")
                    return
            else:
                if filename and (filename.startswith("__custom__:")
                                 or filename.startswith("__joinsound__:")):
                    print("[Net] Soundboard: кастомный звук без data_b64 — пропущен")
                    return
                path = resource_path(os.path.join("assets/panel", filename))
                if not os.path.exists(path):
                    print(f"[Net] Soundboard file not found: {path}")
                    return
                audio_source = path

            if from_nick and not is_join_sound:
                self.soundboard_played.emit(from_nick)

            # Уведомляем UI, что от другого пользователя пришёл «звук при входе».
            # UI использует это, чтобы не проигрывать дефолтный user_join.wav
            # для этого uid (наш кастомный звук уже выполняет эту роль).
            if is_join_sound and src_uid is not None:
                try:
                    _su = int(src_uid)
                    if _su != int(getattr(self.audio, 'my_uid', 0) or 0):
                        self.join_sound_received.emit(_su)
                except (TypeError, ValueError):
                    pass

            def _play():
                try:
                    data, sr = sf.read(audio_source, dtype='float32')
                    if hasattr(self.audio, 'play_internal_sound') and self.audio.stream:
                        self.audio.play_internal_sound(data, sr, vol)
                        duration = len(data) / sr
                        time.sleep(duration)
                    else:
                        sd.play(data * vol, sr)
                        sd.wait()
                except Exception as e:
                    print(f"[Net] Soundboard playback error: {e}")

            threading.Thread(
                target=_play, daemon=True, name="soundboard-play"
            ).start()
            print(
                f"[Net] Playing soundboard: {filename} "
                f"(vol={vol:.3f}, custom={bool(data_b64)}, by={from_nick!r})"
            )
        except Exception as e:
            print(f"[Net] Soundboard error: {e}")

    def broadcast_join_sound(self) -> bool:
        """
        Транслируем свой «звук при входе» всем в текущей комнате через
        существующий механизм soundboard-broadcast (CMD_SOUNDBOARD). На стороне
        получателей сработает play_soundboard_file по маркеру __joinsound__:
        играется на громкости системных звуков, без тоста. Свой собственный
        звук у себя не играем (фильтр по src_uid в receive-пути).

        Возвращает True, если файл валиден и широковещание отправлено —
        вызывающая сторона использует этот флаг для подавления дефолтного
        user_join.wav (его роль выполнит наш кастомный звук).
        """
        try:
            from PyQt6.QtCore import QSettings
            gs = QSettings("MyVoiceChat", "GlobalSettings")
            path = gs.value("join_sound_path", "") or ""
            if not path or not os.path.exists(path):
                return False
            size = os.path.getsize(path)
            # Тот же лимит, что у кастомных soundboard-звуков: 1 МБ.
            if size <= 0 or size > 1 * 1024 * 1024:
                return False
            with open(path, 'rb') as f:
                raw_bytes = f.read()
            data_b64 = base64.b64encode(raw_bytes).decode('ascii')
            my_uid = int(getattr(self.audio, 'my_uid', 0) or 0)
            self.send_json({
                "action":   "play_soundboard",
                "file":     f"__joinsound__:{os.path.basename(path)}",
                "data_b64": data_b64,
                "src_uid":  my_uid,
            })
            print(f"[Net] broadcast_join_sound: отправлен ({size} bytes)")
            return True
        except Exception as e:
            print(f"[Net] broadcast_join_sound error: {e}")
            return False

    # ------------------------------------------------------------------
    # File Transfer P2P — только сигнализация через сервер
    # ------------------------------------------------------------------
    def send_file_offer(
        self, target_uid: int, filename: str,
        filesize: int, sender_port: int, token: str
    ) -> None:
        self.send_json({
            "action":      "file_offer",
            "target_uid":  target_uid,
            "filename":    filename,
            "filesize":    filesize,
            "sender_port": sender_port,
            "token":       token,
        })

    def send_file_offer_room(
        self, filename: str, filesize: int,
        sender_port: int, token: str
    ) -> None:
        self.send_json({
            "action":      "file_offer_room",
            "filename":    filename,
            "filesize":    filesize,
            "sender_port": sender_port,
            "token":       token,
        })

    # ------------------------------------------------------------------
    # Nudge: голосование и воспроизведение
    # ------------------------------------------------------------------
    def send_nudge_vote(self, target_uid: int):
        self.send_json({
            'action':     CMD_NUDGE_VOTE,
            'target_uid': target_uid,
        })
        print(f"[Net] Nudge vote sent → target_uid={target_uid}")

    def _nudge_get_endpoint_vol(self):
        """
        Возвращает IAudioEndpointVolume дефолтного устройства воспроизведения.
        Поддерживает ОБЕ версии pycaw.
        """
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from comtypes import CLSCTX_ALL
            from ctypes import cast, POINTER

            device = AudioUtilities.GetSpeakers()
            if hasattr(device, 'Activate'):
                raw_dev = device
            elif hasattr(device, '_dev'):
                raw_dev = device._dev
            else:
                raise RuntimeError(f"Неизвестный тип GetSpeakers(): {type(device).__name__}")

            iface = raw_dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return cast(iface, POINTER(IAudioEndpointVolume))

        except ImportError:
            print("[Nudge] pycaw не установлен — пробуем comtypes напрямую")
        except Exception as e:
            print(f"[Nudge] pycaw get_endpoint_vol error: {e}")

        # ── Попытка 2: comtypes напрямую ──────────────────────────────────
        try:
            import comtypes
            import comtypes.client
            from ctypes import cast, POINTER, c_float, c_int, c_uint, HRESULT

            CLSID_MMDeviceEnumerator = comtypes.GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
            IID_IMMDeviceEnumerator  = comtypes.GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
            IID_IMMDevice            = comtypes.GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
            IID_IAudioEndpointVolume = comtypes.GUID('{5CDF2C82-841E-4546-9722-0CF74078229A}')

            class IMMDevice(comtypes.IUnknown):
                _iid_ = IID_IMMDevice
                _methods_ = [
                    comtypes.COMMETHOD([], HRESULT, 'Activate',
                        (['in'],  comtypes.GUID,              'iid'),
                        (['in'],  c_uint,                     'dwClsCtx'),
                        (['in'],  comtypes.c_void_p,          'pActivationParams'),
                        (['out'], POINTER(comtypes.c_void_p), 'ppInterface'),
                    ),
                    comtypes.COMMETHOD([], HRESULT, 'OpenPropertyStore',
                        (['in'],  c_uint, 'stgmAccess'),
                        (['out'], POINTER(comtypes.IUnknown), 'ppProperties'),
                    ),
                    comtypes.COMMETHOD([], HRESULT, 'GetId',
                        (['out'], POINTER(comtypes.c_wchar_p), 'ppstrId'),
                    ),
                    comtypes.COMMETHOD([], HRESULT, 'GetState',
                        (['out'], POINTER(c_uint), 'pdwState'),
                    ),
                ]

            class IMMDeviceEnumerator(comtypes.IUnknown):
                _iid_ = IID_IMMDeviceEnumerator
                _methods_ = [
                    comtypes.COMMETHOD([], HRESULT, 'EnumAudioEndpoints',
                        (['in'],  c_uint, 'dataFlow'),
                        (['in'],  c_uint, 'dwStateMask'),
                        (['out'], POINTER(comtypes.IUnknown), 'ppDevices'),
                    ),
                    comtypes.COMMETHOD([], HRESULT, 'GetDefaultAudioEndpoint',
                        (['in'],  c_uint,             'dataFlow'),
                        (['in'],  c_uint,             'role'),
                        (['out'], POINTER(IMMDevice), 'ppEndpoint'),
                    ),
                ]

            class IAudioEndpointVolumeDirect(comtypes.IUnknown):
                _iid_ = IID_IAudioEndpointVolume
                _methods_ = [
                    comtypes.COMMETHOD([], HRESULT, 'RegisterControlChangeNotify',
                        (['in'], comtypes.IUnknown, 'pNotify')),
                    comtypes.COMMETHOD([], HRESULT, 'UnregisterControlChangeNotify',
                        (['in'], comtypes.IUnknown, 'pNotify')),
                    comtypes.COMMETHOD([], HRESULT, 'GetChannelCount',
                        (['out'], POINTER(c_uint), 'pnChannelCount')),
                    comtypes.COMMETHOD([], HRESULT, 'SetMasterVolumeLevel',
                        (['in'], c_float, 'fLevelDB'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'SetMasterVolumeLevelScalar',
                        (['in'], c_float, 'fLevel'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'GetMasterVolumeLevel',
                        (['out'], POINTER(c_float), 'pfLevelDB')),
                    comtypes.COMMETHOD([], HRESULT, 'GetMasterVolumeLevelScalar',
                        (['out'], POINTER(c_float), 'pfLevel')),
                    comtypes.COMMETHOD([], HRESULT, 'SetChannelVolumeLevel',
                        (['in'], c_uint, 'nChannel'),
                        (['in'], c_float, 'fLevelDB'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'SetChannelVolumeLevelScalar',
                        (['in'], c_uint, 'nChannel'),
                        (['in'], c_float, 'fLevel'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'GetChannelVolumeLevel',
                        (['in'],  c_uint, 'nChannel'),
                        (['out'], POINTER(c_float), 'pfLevelDB')),
                    comtypes.COMMETHOD([], HRESULT, 'GetChannelVolumeLevelScalar',
                        (['in'],  c_uint, 'nChannel'),
                        (['out'], POINTER(c_float), 'pfLevel')),
                    comtypes.COMMETHOD([], HRESULT, 'SetMute',
                        (['in'], c_int, 'bMute'),
                        (['in'], comtypes.c_void_p, 'pguidEventContext')),
                    comtypes.COMMETHOD([], HRESULT, 'GetMute',
                        (['out'], POINTER(c_int), 'pbMute')),
                ]

            comtypes.CoInitialize()
            enumerator = comtypes.client.CreateObject(
                CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator,
            )
            device = enumerator.GetDefaultAudioEndpoint(0, 0)
            iface  = device.Activate(IID_IAudioEndpointVolume, 0x17, None)
            return cast(iface, POINTER(IAudioEndpointVolumeDirect))

        except Exception as e:
            print(f"[Nudge] comtypes direct error: {e}")

        return None

    def _nudge_boost_volume(self) -> tuple:
        NUDGE_MIN_VOL   = 0.30
        NUDGE_BOOST_VOL = 0.80
        prev_scalar = -1.0
        was_muted   = False

        vol = self._nudge_get_endpoint_vol()
        if vol is None:
            print("[Nudge] IAudioEndpointVolume недоступен — громкость не изменена")
            return prev_scalar, was_muted

        try:
            prev_scalar = float(vol.GetMasterVolumeLevelScalar())
            was_muted   = bool(vol.GetMute())
            if was_muted:
                vol.SetMute(False, None)
                print("[Nudge] Системный мьют снят")
            if prev_scalar < NUDGE_MIN_VOL:
                vol.SetMasterVolumeLevelScalar(NUDGE_BOOST_VOL, None)
                print(f"[Nudge] Громкость {prev_scalar:.0%} → {NUDGE_BOOST_VOL:.0%}")
        except Exception as e:
            print(f"[Nudge] boost error: {e}")

        return prev_scalar, was_muted

    def _nudge_restore_volume(self, prev_scalar: float, was_muted: bool):
        if prev_scalar < 0:
            return
        vol = self._nudge_get_endpoint_vol()
        if vol is None:
            return
        try:
            vol.SetMasterVolumeLevelScalar(prev_scalar, None)
            if was_muted:
                vol.SetMute(True, None)
            print(f"[Nudge] Громкость восстановлена → {prev_scalar:.0%}"
                  + (" + мьют" if was_muted else ""))
        except Exception as e:
            print(f"[Nudge] restore error: {e}")

    def _play_nudge_sound(self):
        """Воспроизвести Danger.mp3 + системный писк — НЕЗАВИСИМО от deaf/mute."""
        import winsound as _ws

        prev_scalar, was_muted = self._nudge_boost_volume()

        try:
            try:
                _ws.MessageBeep(0x30)
            except Exception as e:
                print(f"[Nudge] MessageBeep error: {e}")

            try:
                _ws.Beep(1200, 400)
            except Exception as e:
                print(f"[Nudge] Beep error: {e}")

            sound_path = NUDGE_SOUND_PATH if os.path.exists(NUDGE_SOUND_PATH) else None
            if sound_path is None:
                print(f"[Nudge] Danger.mp3 не найден: {NUDGE_SOUND_PATH}")
                return

            try:
                data, sr = sf.read(sound_path, dtype='float32')
                if hasattr(self.audio, 'play_internal_sound') and self.audio.stream:
                    self.audio.play_internal_sound(data, sr, 1.0)
                    time.sleep(len(data) / sr)
                else:
                    sd.play(data, sr)
                    sd.wait()
                print("[Nudge] Danger.mp3 воспроизведён успешно")
            except Exception as e:
                print(f"[Nudge] playback error: {e}")

        finally:
            self._nudge_restore_volume(prev_scalar, was_muted)

    # ------------------------------------------------------------------
    # Draw Stroke
    # ------------------------------------------------------------------
    def send_draw_stroke(self, streamer_uid: int, nick: str,
                         color: str, points: list, width: int) -> None:
        if not points:
            return
        if len(points) > DRAW_MAX_POINTS:
            points = points[:DRAW_MAX_POINTS]
        self.send_json({
            'action':       CMD_DRAW_STROKE,
            'streamer_uid': streamer_uid,
            'nick':         nick[:32],
            'color':        color[:16],
            'points':       points,
            'width':        max(1, min(8, width)),
        })

    # ------------------------------------------------------------------
    # Remote Control
    # ------------------------------------------------------------------
    def send_remote_control_request(self, streamer_uid: int, nick: str) -> None:
        """Зритель просит у стримера разрешение на управление мышью/клавиатурой."""
        self.send_json({
            'action':       CMD_REMOTE_CONTROL_REQUEST,
            'streamer_uid': int(streamer_uid),
            'nick':         str(nick)[:32],
        })

    def send_remote_control_response(self, viewer_uid: int, granted: bool) -> None:
        """Стример отвечает зрителю: True — разрешить, False — отклонить."""
        self.send_json({
            'action':     CMD_REMOTE_CONTROL_RESPONSE,
            'viewer_uid': int(viewer_uid),
            'granted':    bool(granted),
        })

    def send_remote_control_event(self, streamer_uid: int, event: dict) -> None:
        """
        Зритель отправляет событие мыши/клавиатуры стримеру.
        event = {
          'type': 'mouse_move'|'mouse_press'|'mouse_release'|'mouse_scroll'
                  |'key_press'|'key_release',
          'x': float,      # 0.0–1.0 нормализованные по экрану
          'y': float,
          'button': int,   # Qt.MouseButton (для mouse_*)
          'key': int,      # Qt.Key (для key_*)
          'modifiers': int,
          'text': str,     # символ (для key_press)
          'delta': int,    # для mouse_scroll
        }
        """
        self.send_json({
            'action':       CMD_REMOTE_CONTROL_EVENT,
            'streamer_uid': int(streamer_uid),
            'event':        event,
        })

    def send_remote_control_stop(self, streamer_uid: int) -> None:
        """Любая из сторон останавливает управление."""
        self.send_json({
            'action':       CMD_REMOTE_CONTROL_STOP,
            'streamer_uid': int(streamer_uid),
        })

    # ------------------------------------------------------------------
    # Host Mute
    # ------------------------------------------------------------------
    def send_host_mute(self, target_uid: int) -> None:
        self.send_json({'action': CMD_HOST_MUTE, 'target_uid': int(target_uid)})

    def send_host_move(self, target_uid: int, room: str) -> None:
        """Хост → сервер: переместить участника в другой канал."""
        self.send_json({
            'action':     CMD_HOST_MOVE,
            'target_uid': int(target_uid),
            'room':       str(room),
        })

    # ------------------------------------------------------------------
    # Host Kick / Ban / Unban
    # ------------------------------------------------------------------
    def send_host_kick(self, target_uid: int) -> None:
        """Хост → сервер: кикнуть участника (без записи в банлист)."""
        self.send_json({'action': CMD_HOST_KICK, 'target_uid': int(target_uid)})

    def send_host_ban(self, target_uid: int, reason: str = '') -> None:
        """Хост → сервер: кикнуть + занести IP в банлист."""
        self.send_json({
            'action':     CMD_HOST_BAN,
            'target_uid': int(target_uid),
            'reason':     str(reason)[:120],
        })

    def send_host_unban(self, ip: str, nick: str = '') -> None:
        """Хост → сервер: убрать (IP, nick) из банлиста. В ответ прилетит CMD_BAN_LIST."""
        self.send_json({
            'action': CMD_HOST_UNBAN,
            'ip':     str(ip)[:45],
            'nick':   str(nick)[:32],
        })

    def request_ban_list(self) -> None:
        """Хост → сервер: запрос текущего снимка банлиста для UI."""
        self.send_json({'action': CMD_BAN_LIST_REQ})

    # ------------------------------------------------------------------
    # process_message dispatch для фич
    # ------------------------------------------------------------------
    def _process_features_message(self, msg: dict, act: str) -> bool:
        """Обрабатывает сообщения фич. Возвращает True если обработано."""

        if act == 'play_soundboard':
            self.play_soundboard_file(
                msg.get('file'), msg.get('data_b64'), msg.get('from_nick'),
                src_uid=msg.get('src_uid')
            )
            return True

        elif act == CMD_PLAY_NUDGE:
            threading.Thread(
                target=self._play_nudge_sound,
                daemon=True,
                name="nudge-sound",
            ).start()
            self.nudge_received.emit()
            return True

        elif act == CMD_NUDGE_TRIGGERED:
            target_nick = msg.get('target_nick', '?')
            voter_nick  = msg.get('voter_nick',  '?')
            self.nudge_triggered.emit(target_nick, voter_nick)
            return True

        elif act in ('file_offer', 'file_offer_room'):
            self.file_offer_received.emit(msg)
            return True

        elif act == CMD_FORCE_MUTED:
            self.force_muted.emit()
            return True

        elif act == CMD_KICKED:
            # Сервер уведомил что нас кикнули. Останавливаем сеть СИНХРОННО
            # прямо здесь — до того как tcp_listen увидит EOF и попытается
            # стартовать recovery. Сигнал emit попадёт в Qt-очередь и UI
            # обработает его позже, но к этому моменту running=False и
            # любой reconnect-путь уже отсечён.
            self._kicked_flag    = True
            self.running         = False
            self._is_connected   = False
            self._shutdown_event.set()
            self.kicked.emit(str(msg.get('reason', '')))
            return True

        elif act == CMD_BANNED:
            # Аналогично CMD_KICKED — блокируем сеть синхронно, эмитим сигнал.
            self._kicked_flag    = True
            self.running         = False
            self._is_connected   = False
            self._shutdown_event.set()
            self.banned.emit(str(msg.get('reason', '')))
            return True

        elif act == CMD_BAN_LIST:
            entries = msg.get('entries', [])
            if isinstance(entries, list):
                self.ban_list_updated.emit(entries)
            return True

        elif act == CMD_DRAW_STROKE:
            sender_uid_dr = int(msg.get('sender_uid', 0))
            nick_dr       = str(msg.get('nick', '?'))
            color_dr      = str(msg.get('color', '#FF6B6B'))
            points_dr     = msg.get('points', [])
            width_dr      = int(msg.get('width', 3))
            if isinstance(points_dr, list) and points_dr:
                self.draw_stroke_received.emit(
                    sender_uid_dr, nick_dr, color_dr, points_dr, width_dr
                )
            return True

        elif act == CMD_REMOTE_CONTROL_REQUEST:
            viewer_uid  = int(msg.get('viewer_uid', 0))
            viewer_nick = str(msg.get('nick', '?'))
            self.remote_control_requested.emit(viewer_uid, viewer_nick)
            return True

        elif act == CMD_REMOTE_CONTROL_RESPONSE:
            self.remote_control_response.emit(
                bool(msg.get('granted', False)),
                str(msg.get('reason', '')),
            )
            return True

        elif act == CMD_REMOTE_CONTROL_EVENT:
            event = msg.get('event', {})
            if isinstance(event, dict):
                _et = event.get('type')
                if _et != 'mouse_move':
                    try:
                        print(f"[RC-NET] received {_et} from server: {event}")
                    except Exception:
                        pass
                self.remote_control_event.emit(event)
            return True

        elif act == CMD_REMOTE_CONTROL_STOP:
            self.remote_control_stopped.emit()
            return True

        return False
