# network_engine/core.py — NetworkClient: основной сетевой клиент InPulse
#
# ── Исправления v2 ─────────────────────────────────────────────────────────
#
#   FIX #47 (КРИТИЧНО): send_json thread-safe. Ранее sendall() вызывался
#     из 6+ потоков одновременно — JSON-сообщения могли перемешаться в TCP
#     stream (TCP сохраняет последовательность байтов, но не атомарность
#     нескольких sendall). Теперь все TCP-отправки сериализованы через
#     _tcp_send_lock.
#
#   FIX #48 (ВАЖНО): ping_loop/udp_keepalive_loop просыпаются на событие
#     остановки вместо time.sleep(). Раньше при stop() потоки спали до 3с
#     и вызывали sendto() на уже закрытый сокет → OSError: WinError 10038.
#
#   FIX #49 (ВАЖНО): UTF-8 incremental decoder в tcp_listen. Аналогично
#     server.py, строковое raw_data через errors='ignore' теряло частичные
#     байты кириллицы/эмоджи на границе chunk.

import codecs
import json
import platform
import socket
import threading
import time
import ctypes

from PyQt6.QtCore import QObject, pyqtSignal

from config import (
    DEFAULT_PORT_TCP, DEFAULT_PORT_UDP, BUFFER_SIZE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE,
    FLAG_STREAM_VOICES,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    FLAG_WHISPER,
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER,
    CMD_SERVER_MIGRATE,
    CMD_MIGRATE_PREPARE, CMD_MIGRATE_READY,
    CMD_CHAT_HISTORY_REQ,
    DISCOVERY_PORT,
    MIGRATION_ANNOUNCE_WAIT_SEC, MIGRATION_TCP_RETRY_SEC,
    MIGRATION_TOTAL_DEADLINE,
    RECONNECT_SAME_HOST_WINDOW_SEC, DISCOVERY_WINDOW_SEC,
    BECOME_HOST_POS0_DELAY_SEC, BECOME_HOST_POS_N_DELAY_SEC,
    RECONNECT_SAME_HOST_TCP_ATTEMPTS,
)

from .webrtc import WebRTCMixin
from .chat import ChatMixin
from .features import FeaturesMixin

MAX_SILENT_RECONNECT_ATTEMPTS = 2
RECONNECT_DELAY               = 1.0

# ── Фазы recovery ────────────────────────────────────────────────────────────
_PHASE_IDLE       = 'idle'
_PHASE_MIGRATING  = 'migrating'
_PHASE_RECOVERING = 'recovering'

# Точность таймера Windows
if platform.system() == "Windows":
    try:
        winmm = ctypes.WinDLL('winmm')
        winmm.timeBeginPeriod(1)
    except Exception:
        pass


# FIX #49: размер TCP-буфера клиента. Меньше, чем у сервера, потому что
# клиент получает в основном sync_users (небольшие) и chat_history.
_TCP_BUFFER_MAX = 32 * 1024 * 1024


class NetworkClient(WebRTCMixin, ChatMixin, FeaturesMixin, QObject):
    # ── Сигналы ───────────────────────────────────────────────────────────
    connected           = pyqtSignal(dict)
    global_state_update = pyqtSignal(dict)
    error_occurred      = pyqtSignal(str)

    connection_lost     = pyqtSignal()
    connection_restored = pyqtSignal()
    reconnect_failed    = pyqtSignal()

    soundboard_played   = pyqtSignal(str)

    nudge_received  = pyqtSignal()
    nudge_triggered = pyqtSignal(str, str)

    bitrate_adjusted = pyqtSignal(int)

    file_offer_received = pyqtSignal(dict)

    quick_msg_received  = pyqtSignal(int, str, str)

    chat_msg_received     = pyqtSignal(dict)
    chat_history_received = pyqtSignal(list)
    chat_media_received   = pyqtSignal(dict)

    become_host      = pyqtSignal()
    server_migrating = pyqtSignal(str)

    channel_created      = pyqtSignal(str)
    channel_deleted      = pyqtSignal(str)
    join_room_denied     = pyqtSignal(str, str)
    channel_auth_ok      = pyqtSignal(str)
    channel_list_updated = pyqtSignal(list)

    force_muted = pyqtSignal()

    # ── Хост: кик / бан / обновление банлиста ─────────────────────────
    # kicked/banned — сервер выгнал этого клиента. Строка — reason (UI).
    # ban_list_updated — снимок банлиста для хоста (UI во вкладке «Главное»).
    kicked            = pyqtSignal(str)
    banned            = pyqtSignal(str)
    ban_list_updated  = pyqtSignal(list)

    draw_stroke_received = pyqtSignal(int, str, str, list, int)

    typing_received = pyqtSignal(int, str)

    def __init__(self, audio):
        super().__init__()
        self.audio  = audio
        self.video  = None

        self.server_addr  = None
        self.running      = False
        self.current_ping = 0
        self.packets_sent      = 0
        self.packets_received  = 0

        self._ip     = None
        self._nick   = None
        self._avatar = None

        self._is_connected       = False
        self._reconnecting       = False
        self._reconnect_attempts = 0

        # Флаг «нас кикнули/забанили сервером» — блокирует авто-reconnect.
        # Устанавливается в features._process_features_message при CMD_KICKED
        # или CMD_BANNED. Проверяется в reconnect-логике.
        self._kicked_flag: bool = False

        # FIX #47: сериализация TCP-отправок между потоками.
        # Вызывающие send_json потоки: UI (главный), ping_loop, chat mixin,
        # webrtc mixin (answer/ice callbacks), features mixin (nudge/file_offer),
        # audio thread (update_status), recovery-потоки.
        # Без лока один sendall мог попасть в середину байтов другого.
        self._tcp_send_lock: threading.Lock = threading.Lock()

        # FIX #48: единое событие остановки для ping_loop / udp_keepalive_loop.
        # Вместо time.sleep(3) используем .wait(3) → при stop() потоки
        # просыпаются мгновенно и не пытаются sendto на закрытый сокет.
        self._shutdown_event: threading.Event = threading.Event()

        self._sb_playing = threading.Event()

        self._chat_history: list[dict] = []

        # ── Встроенный сервер: миграция ─────────────────────────
        self._host_order:       list[int]      = []
        self._server_host_uid:  int            = 0
        self._host_order_ips:   dict[int, str] = {}

        # ── Recovery state machine ─────────────────────────────
        self._recovery_state:     str              = _PHASE_IDLE
        self._recovery_lock:      threading.Lock   = threading.Lock()
        self._recovery_event:     threading.Event  = threading.Event()
        self._recovery_target_ip: str              = ''

        self._init_webrtc_attrs()

        self._init_sockets()

    # ------------------------------------------------------------------
    # Backward-compat
    # ------------------------------------------------------------------
    @property
    def _migration_pending(self) -> bool:
        return self._recovery_state == _PHASE_MIGRATING

    @_migration_pending.setter
    def _migration_pending(self, value: bool) -> None:
        if not value:
            with self._recovery_lock:
                if self._recovery_state == _PHASE_MIGRATING:
                    self._recovery_state     = _PHASE_IDLE
                    self._recovery_target_ip = ''

    # ------------------------------------------------------------------
    # Сокеты
    # ------------------------------------------------------------------
    def _init_sockets(self):
        for attr in ('tcp_sock', 'udp_sock'):
            old = getattr(self, attr, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
        try:
            self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.tcp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket_bound = False
        except Exception as e:
            print(f"[Net] Socket init error: {e}")

    # ------------------------------------------------------------------
    # Подключение
    # ------------------------------------------------------------------
    def connect_to_server(self, ip, nick, avatar):
        self._ip     = ip
        self._nick   = nick
        self._avatar = avatar
        self._reconnect_attempts = 0
        self._reconnecting       = False
        # Свежий коннект — снимаем флаг кика (пользователь явно идёт на
        # новый сервер либо повторно на тот же — решение за UI).
        self._kicked_flag        = False
        self._shutdown_event.clear()  # FIX #48: сбрасываем событие при старте
        threading.Thread(target=self._connect_initial, daemon=True).start()

    def _connect_initial(self):
        try:
            self._do_connect()
        except Exception as e:
            print(f"[Net] Initial connection failed: {e}")
            self.connection_lost.emit()
            self._start_recovery(_PHASE_RECOVERING, target_ip=self._ip)

    def _do_connect(self):
        self.server_addr = (self._ip, DEFAULT_PORT_UDP)

        self.tcp_sock.settimeout(5.0)
        self.tcp_sock.connect((self._ip, DEFAULT_PORT_TCP))
        self.tcp_sock.settimeout(None)

        if not self.udp_socket_bound:
            try:
                self.udp_sock.bind(('0.0.0.0', 0))
                self.udp_socket_bound = True
                local_port = self.udp_sock.getsockname()[1]
                print(f"[Net] UDP socket bound to local port {local_port}")
            except Exception as e:
                print(f"[Net] CRITICAL: UDP bind failed: {e}")
                raise

        try:
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)
            print("[Net] UDP buffers: 512 KB (low-latency mode)")
        except Exception:
            pass

        self.send_json({"action": "login", "nick": self._nick, "avatar": self._avatar})
        self.running       = True
        self._is_connected = True
        self._shutdown_event.clear()

        threading.Thread(target=self.tcp_listen,         daemon=True, name="net-tcp").start()
        threading.Thread(target=self.udp_sender_loop,    daemon=True, name="net-udp-send").start()
        threading.Thread(target=self.udp_keepalive_loop, daemon=True, name="net-udp-keep").start()
        threading.Thread(target=self.udp_receive_loop,   daemon=True, name="net-udp-recv").start()
        threading.Thread(target=self.ping_loop,          daemon=True, name="net-ping").start()

        self._start_webrtc_loop()

        print("[Net] Connected to server")

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------
    def _start_recovery(self, phase: str, target_ip: str = '') -> bool:
        # Если нас кикнули/забанили — блокируем ЛЮБОЙ recovery на корню.
        # Проверка стоит здесь (а не только в _on_connection_lost) потому что
        # trigger_migration / fast_switch_to зовут _start_recovery напрямую,
        # минуя _on_connection_lost.
        if self._kicked_flag:
            print(f"[Recovery] skip {phase}: _kicked_flag активен")
            return False
        with self._recovery_lock:
            if self._recovery_state != _PHASE_IDLE:
                print(f"[Recovery] skip {phase}: уже идёт {self._recovery_state}")
                return False
            self._recovery_state     = phase
            self._recovery_target_ip = target_ip
            self._recovery_event.clear()
            self.running       = False
            self._is_connected = False
            # Сигнализируем ping/keepalive потокам о необходимости остановиться
            self._shutdown_event.set()

        threading.Thread(
            target=self._recovery_loop,
            daemon=True,
            name=f"recovery-{phase}",
        ).start()
        return True

    def _finish_recovery(self) -> None:
        with self._recovery_lock:
            self._recovery_state     = _PHASE_IDLE
            self._recovery_target_ip = ''
            self._reconnecting       = False
            self._reconnect_attempts = 0
        self._recovery_event.set()

    def _on_connection_lost(self) -> None:
        # Если нас кикнули/забанили — никаких reconnect-попыток.
        # UI получит сигнал kicked/banned и покажет экран выбора сервера.
        if self._kicked_flag:
            print("[Net] connection lost после kick/ban — reconnect заблокирован")
            return
        self._start_recovery(_PHASE_RECOVERING, target_ip=self._ip)

    def manual_reconnect(self) -> None:
        if not self._ip:
            return
        if self._kicked_flag:
            print("[Net] manual_reconnect заблокирован: kicked/banned")
            return
        self._start_recovery(_PHASE_RECOVERING, target_ip=self._ip)

    def trigger_migration(self, new_host_ip: str) -> None:
        if not new_host_ip:
            return
        self._start_recovery(_PHASE_MIGRATING, target_ip=new_host_ip)

    def fast_switch_to(self, new_ip: str) -> None:
        if not new_ip:
            return
        if self._kicked_flag:
            print(f"[Net] fast_switch_to({new_ip}) заблокирован: kicked/banned")
            return
        with self._recovery_lock:
            self._recovery_state     = _PHASE_IDLE
            self._recovery_target_ip = ''
        self._start_recovery(_PHASE_MIGRATING, target_ip=new_ip)

    def _tear_down_before_recovery(self) -> None:
        try:
            if getattr(self, '_viewer_pc', None) is not None:
                self._run_in_webrtc_loop(self._close_pc_coro(self._viewer_pc))
                self._viewer_pc = None
        except Exception as e:
            print(f"[Recovery] close viewer_pc error: {e}")

        try:
            if hasattr(self.audio, 'stop_stream_playback'):
                self.audio.stop_stream_playback()
        except Exception:
            pass

        for attr in ('tcp_sock', 'udp_sock'):
            s = getattr(self, attr, None)
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

        try:
            if hasattr(self.audio, 'reset_voice_state'):
                self.audio.reset_voice_state()
        except Exception as e:
            print(f"[Recovery] reset_voice_state error: {e}")

    def _attempt_full_connect(self, ip: str) -> bool:
        if not ip:
            return False
        try:
            self._ip = ip
            self._init_sockets()
            self._do_connect()
            return True
        except Exception:
            return False

    def _listen_for_announce(
            self,
            timeout: float,
            stop_evt: threading.Event,
            exclude_ip: str = '',
    ) -> str | None:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            sock.bind(('', DISCOVERY_PORT))
            sock.settimeout(0.25)

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not stop_evt.is_set():
                try:
                    data, _addr = sock.recvfrom(4096)
                    msg = json.loads(data.decode('utf-8'))
                    if msg.get('action') != 'server_announce':
                        continue
                    ip = msg.get('ip', '')
                    if not ip or ip == exclude_ip:
                        continue
                    return ip
                except socket.timeout:
                    continue
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
        except Exception as e:
            print(f"[Recovery] listen_announce error: {e}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        return None

    def _try_connect_migration(self, target_ip: str, deadline: float) -> bool:
        result_ip: list[str] = []
        stop_evt = threading.Event()

        def _listen_announce():
            ip = self._listen_for_announce(
                timeout=MIGRATION_ANNOUNCE_WAIT_SEC,
                stop_evt=stop_evt,
            )
            if ip and not result_ip:
                result_ip.append(ip)

        t_listen = threading.Thread(target=_listen_announce, daemon=True,
                                    name="mig-listen")
        t_listen.start()

        while time.monotonic() < deadline:
            if result_ip and result_ip[0] != target_ip:
                print(f"[Recovery] migration: анонс указывает на {result_ip[0]}, "
                      f"меняем target с {target_ip}")
                target_ip = result_ip[0]

            if self._attempt_full_connect(target_ip):
                stop_evt.set()
                return True
            time.sleep(MIGRATION_TCP_RETRY_SEC)

        stop_evt.set()
        return False

    def _try_connect_recovery(self, deadline: float) -> bool:
        phase1_end = time.monotonic() + RECONNECT_SAME_HOST_WINDOW_SEC
        old_ip     = self._ip

        print(f"[Recovery] Фаза 1: ретраим старый хост {old_ip} "
              f"в течение {RECONNECT_SAME_HOST_WINDOW_SEC}с")
        attempt = 0
        while time.monotonic() < phase1_end and time.monotonic() < deadline:
            attempt += 1
            if self._attempt_full_connect(old_ip):
                return True
            sleep_left = min(0.4, phase1_end - time.monotonic())
            if sleep_left > 0:
                time.sleep(sleep_left)
            if attempt >= RECONNECT_SAME_HOST_TCP_ATTEMPTS:
                break

        print(f"[Recovery] Фаза 2: слушаем UDP-анонс новых хостов "
              f"{DISCOVERY_WINDOW_SEC}с (+ ретраим {old_ip} в фоне)")

        result_ip: list[str] = []
        stop_evt  = threading.Event()

        def _listen_announce():
            ip = self._listen_for_announce(
                timeout=DISCOVERY_WINDOW_SEC,
                stop_evt=stop_evt,
                exclude_ip=old_ip,
            )
            if ip:
                result_ip.append(ip)

        threading.Thread(target=_listen_announce, daemon=True,
                         name="rec-listen").start()

        while time.monotonic() < deadline:
            if result_ip:
                new_ip = result_ip[0]
                print(f"[Recovery] услышали анонс нового хоста: {new_ip}")
                if self._attempt_full_connect(new_ip):
                    stop_evt.set()
                    return True
            else:
                if self._attempt_full_connect(old_ip):
                    stop_evt.set()
                    return True
            time.sleep(MIGRATION_TCP_RETRY_SEC)

        stop_evt.set()
        return False

    def _should_become_host_now(self) -> bool:
        my_uid   = getattr(self.audio, 'my_uid', 0)
        old_host = self._server_host_uid
        remaining = [uid for uid in self._host_order if uid != old_host]

        if not remaining:
            print("[Recovery] host_order пуст — становимся хостом (singleton)")
            return True

        try:
            my_pos = remaining.index(my_uid)
        except ValueError:
            print("[Recovery] нас нет в host_order — НЕ становимся хостом")
            return False

        wait = (BECOME_HOST_POS0_DELAY_SEC if my_pos == 0
                else BECOME_HOST_POS_N_DELAY_SEC * my_pos)
        print(f"[Recovery] my_pos={my_pos}, финальное ожидание {wait:.1f}с")

        stop_evt = threading.Event()
        ip = self._listen_for_announce(timeout=wait, stop_evt=stop_evt,
                                       exclude_ip=self._ip)
        if ip:
            print(f"[Recovery] услышали анонс {ip} — пробуем подключиться вместо "
                  f"become_host")
            # SENIOR FIX: раньше было безусловное return False, даже если
            # _attempt_full_connect упал. Теперь: возвращаем False ТОЛЬКО
            # если подключение к анонсированному хосту реально удалось.
            # Иначе продолжаем по ветке "pos>0 listen" или "pos==0 become_host".
            if self._attempt_full_connect(ip):
                return False
            print(f"[Recovery] connect к анонсированному {ip} упал — "
                  f"продолжаем решать становиться ли хостом")

        if my_pos == 0:
            return True

        # FIX: раньше было time.sleep(1.0) + _listen_for_announce(timeout=1.5).
        # sleep(1.0) блокировал _recovery_loop без пользы — за это время мы
        # всё равно не слушали анонсы. Объединяем в один listen(2.5):
        # если за 2.5 сек никто не объявился — сдаёмся.
        stop_evt2 = threading.Event()
        ip2 = self._listen_for_announce(timeout=2.5, stop_evt=stop_evt2,
                                        exclude_ip=self._ip)
        if ip2:
            # SENIOR FIX: аналогично — return False только если connect успешен.
            if self._attempt_full_connect(ip2):
                return False
            print(f"[Recovery] connect к {ip2} упал — сдаёмся (pos>0)")

        print("[Recovery] pos>0 и анонсов нет — сдаёмся (безопаснее чем "
              "split-brain). Пользователь нажмёт 'Переподключиться'.")
        return False

    def _recovery_loop(self) -> None:
        phase     = self._recovery_state
        target_ip = self._recovery_target_ip
        t_start   = time.monotonic()
        if phase == _PHASE_MIGRATING:
            deadline = t_start + MIGRATION_TOTAL_DEADLINE
        else:
            deadline = t_start + (RECONNECT_SAME_HOST_WINDOW_SEC
                                  + DISCOVERY_WINDOW_SEC * 2 + 4.0)

        print(f"[Recovery] START phase={phase} target={target_ip!r} "
              f"deadline={deadline - t_start:.1f}с")

        self._tear_down_before_recovery()
        self.connection_lost.emit()

        try:
            if phase == _PHASE_MIGRATING:
                ok = self._try_connect_migration(target_ip, deadline)
            else:
                ok = self._try_connect_recovery(deadline)

            if ok:
                print("[Recovery] ✅ Подключено")
                self.connection_restored.emit()
                return

            print("[Recovery] ❌ Дедлайн исчерпан — рассматриваем auto-host")
            if self._should_become_host_now():
                self.become_host.emit()
                return

            self.reconnect_failed.emit()

        except Exception:
            import traceback
            print(f"[Recovery] EXCEPTION:\n{traceback.format_exc()}")
            self.reconnect_failed.emit()
        finally:
            self._finish_recovery()

    # ------------------------------------------------------------------
    # Остановка
    # ------------------------------------------------------------------
    def stop(self) -> None:
        print("[Net] stop(): завершаем сетевые потоки...")
        self.running = False
        self._is_connected = False
        # FIX #48: будим все потоки ожидающие на event.wait()
        self._shutdown_event.set()

        self._stop_webrtc()

        for attr in ('tcp_sock', 'udp_sock'):
            sock = getattr(self, attr, None)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        print("[Net] stop(): готово")

    def send_server_transfer(self, target_uid: int) -> None:
        self.send_json({'action': 'server_transfer', 'target_uid': target_uid})

    def send_migrate_ready(self) -> None:
        try:
            self.send_json({'action': CMD_MIGRATE_READY})
        except Exception as e:
            print(f"[Net] send_migrate_ready error: {e}")

    def _process_migrate(self, msg: dict) -> None:
        new_host_uid = msg.get('new_host_uid', 0)
        new_host_ip  = msg.get('new_host_ip', '')
        my_uid       = getattr(self.audio, 'my_uid', 0)

        print(f"[Net] CMD_SERVER_MIGRATE → new_host_uid={new_host_uid}, "
              f"new_host_ip={new_host_ip}, my_uid={my_uid}")

        if new_host_uid == my_uid:
            with self._recovery_lock:
                if self._recovery_state == _PHASE_IDLE:
                    self._recovery_state     = _PHASE_MIGRATING
                    self._recovery_target_ip = new_host_ip
            self.running       = False
            self._is_connected = False
            self.become_host.emit()
        elif new_host_ip:
            self.server_migrating.emit(new_host_ip)
            self.trigger_migration(new_host_ip)

    # ------------------------------------------------------------------
    # UDP
    # ------------------------------------------------------------------
    def udp_receive_loop(self):
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(BUFFER_SIZE)
                if len(data) < UDP_HEADER_SIZE:
                    continue

                uid, ts, seq, flags = UDP_HEADER_STRUCT.unpack(data[:UDP_HEADER_SIZE])

                if flags == 254:
                    self.packets_received += 1
                    delay = (time.time() - ts) * 1000
                    if self.current_ping == 0:
                        self.current_ping = int(delay)
                    else:
                        self.current_ping = int(self.current_ping * 0.7 + delay * 0.3)

                elif flags & FLAG_STREAM_VOICES:
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    speaker_uid, = STREAM_VOICE_HEADER_STRUCT.unpack(
                        data[UDP_HEADER_SIZE: UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE]
                    )
                    if speaker_uid == self.audio.my_uid:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_packet(speaker_uid, seq, opus_payload, flags)

                elif flags & FLAG_WHISPER:
                    if len(data) < UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:
                        continue
                    opus_payload = data[UDP_HEADER_SIZE + STREAM_VOICE_HEADER_SIZE:]
                    self.audio.add_incoming_whisper_packet(uid, seq, opus_payload)

                else:
                    self.audio.add_incoming_packet(uid, seq, data[UDP_HEADER_SIZE:], flags)

            except OSError as e:
                err = getattr(e, 'winerror', None)
                if not self.running or err in (10038, 10022, 10054):
                    break
                import traceback
                print(f"[Net] UDP receive error: {e}\n{traceback.format_exc()}")
                break
            except Exception as e:
                if self.running:
                    import traceback
                    print(f"[Net] UDP receive error: {e}\n{traceback.format_exc()}")

    def udp_sender_loop(self):
        while self.running:
            try:
                packet = self.audio.send_queue.get(timeout=0.1)
                if self.server_addr:
                    self.udp_sock.sendto(packet, self.server_addr)
            except Exception:
                continue

    def udp_keepalive_loop(self):
        """
        FIX #48: используем _shutdown_event.wait() вместо time.sleep(1).
        При stop() поток проснётся мгновенно и не пошлёт на закрытый сокет.
        """
        while self.running and not self._shutdown_event.is_set():
            if self.audio.my_uid != 0:
                flags = (1 if self.audio.is_muted else 0) | (2 if self.audio.is_deafened else 0)
                try:
                    header = UDP_HEADER_STRUCT.pack(
                        self.audio.my_uid, time.time(), 0, flags
                    )
                    self.udp_sock.sendto(header, self.server_addr)
                except OSError as e:
                    err = getattr(e, 'winerror', None)
                    if not self.running or err in (10038, 10022, 10054):
                        break
                    print(f"[Net] Keepalive error: {e}")
                except Exception as e:
                    print(f"[Net] Keepalive error: {e}")
            # FIX #48: wait(1) вместо sleep(1)
            if self._shutdown_event.wait(1):
                break

    def ping_loop(self):
        """
        FIX #48: wait() вместо sleep() для быстрого выхода при stop().
        """
        while self.running and not self._shutdown_event.is_set():
            if self.audio.my_uid != 0:
                try:
                    header = UDP_HEADER_STRUCT.pack(
                        self.audio.my_uid, time.time(), 0, 254
                    )
                    self.udp_sock.sendto(header, self.server_addr)
                    self.packets_sent += 1
                except OSError as e:
                    err = getattr(e, 'winerror', None)
                    if not self.running or err in (10038, 10022, 10054):
                        break
                    print(f"[Net] Ping error: {e}")
                except Exception as e:
                    print(f"[Net] Ping error: {e}")

                if self.current_ping > 0:
                    try:
                        self.send_json({'action': 'report_ping', 'ping_ms': self.current_ping})
                    except Exception:
                        pass
            # FIX #48: wait(3) вместо sleep(3)
            if self._shutdown_event.wait(3):
                break

    # ------------------------------------------------------------------
    # TCP
    # ------------------------------------------------------------------
    def tcp_listen(self):
        """
        FIX #49: UTF-8 incremental decoder обрабатывает multi-byte
        последовательности, разрезанные между recv(). Раньше `decode(errors='ignore')`
        съедал начальные байты кириллицы/эмоджи если они попадали на границу
        chunk, что приводило к JSONDecodeError на следующей итерации.
        """
        raw_data = ""
        _decoder = json.JSONDecoder()
        utf8_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

        while self.running:
            try:
                chunk_bytes = self.tcp_sock.recv(4096)
                if not chunk_bytes:
                    break
                # FIX #49: incremental decode — сохраняет незавершённые
                # последовательности между вызовами decode().
                raw_data += utf8_decoder.decode(chunk_bytes, final=False)
                if len(raw_data) > _TCP_BUFFER_MAX:
                    print(f"[Net] WARN: raw_data overflow ({len(raw_data)} bytes) — clearing")
                    raw_data = ""
                while True:
                    try:
                        msg, idx = _decoder.raw_decode(raw_data)
                        raw_data = raw_data[idx:].lstrip()
                        try:
                            self.process_message(msg)
                        except Exception as pm_err:
                            import traceback
                            print(f"[Net] process_message error: {pm_err}\n"
                                  f"{traceback.format_exc()}")
                    except json.JSONDecodeError:
                        break
            except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
                if self.running:
                    print(f"[Net] TCP connection error: {e}")
                break
            except Exception as e:
                if self.running:
                    print(f"[Net] TCP receive error: {e}")
                break

        print("[Net] TCP listener stopped")
        if self._recovery_state == _PHASE_IDLE and self.running:
            self._on_connection_lost()

    def process_message(self, msg: dict):
        act = msg.get('action')

        if act == 'login_success':
            self.connected.emit(msg)
            self.send_json({'action': CMD_CHAT_HISTORY_REQ})

        elif act == 'sync_users':
            self._host_order      = msg.get('host_order', [])
            self._server_host_uid = msg.get('server_host_uid', 0)

            all_users = msg.get('all_users', {})
            new_ips: dict[int, str] = {}
            for users_in_room in all_users.values():
                for u in users_in_room:
                    u_uid = u.get('uid', 0)
                    u_ip  = u.get('ip',  '')
                    if u_uid and u_ip:
                        new_ips[u_uid] = u_ip
            self._host_order_ips = new_ips

            channel_list = msg.get('channel_list', [])
            if channel_list:
                self.channel_list_updated.emit(channel_list)
            self.global_state_update.emit(all_users)

        elif act == 'channel_created':
            ch_name = msg.get('channel_name', '')
            if ch_name:
                self.channel_created.emit(ch_name)

        elif act == 'create_channel_result':
            if msg.get('ok'):
                ch_name = msg.get('channel_name', '')
                if ch_name:
                    self.channel_created.emit(ch_name)

        elif act == 'channel_deleted':
            ch_name = msg.get('channel_name', '')
            if ch_name:
                self.channel_deleted.emit(ch_name)

        elif act == 'join_room_denied':
            self.join_room_denied.emit(msg.get('room', ''), msg.get('reason', ''))

        elif act == 'channel_auth_result':
            if msg.get('ok'):
                self.channel_auth_ok.emit(msg.get('channel_name', ''))
            else:
                self.join_room_denied.emit(
                    msg.get('channel_name', ''),
                    msg.get('reason', 'wrong_password'),
                )

        elif act == CMD_SERVER_MIGRATE:
            self._process_migrate(msg)

        elif act == CMD_MIGRATE_PREPARE:
            incoming_order = msg.get('host_order')
            if isinstance(incoming_order, list):
                self._host_order = incoming_order
            print("[Net] CMD_MIGRATE_PREPARE → becoming host")
            with self._recovery_lock:
                if self._recovery_state == _PHASE_IDLE:
                    self._recovery_state     = _PHASE_MIGRATING
                    self._recovery_target_ip = ''
            self.running       = False
            self._is_connected = False
            self.become_host.emit()

        elif self._process_webrtc_message(msg, act):
            pass
        elif self._process_chat_message(msg, act):
            pass
        elif self._process_features_message(msg, act):
            pass

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------
    def send_json(self, data):
        """
        FIX #47 (КРИТИЧНО): thread-safe отправка JSON.

        Вызывается из множества потоков: UI (главный), ping_loop,
        chat/webrtc/features mixins, audio, recovery. Без _tcp_send_lock
        две конкурентные send_json могут перемешать байты в TCP stream:
        sendall() гарантирует что все байты уйдут, но НЕ гарантирует
        атомарность относительно другого sendall() из другого потока.

        Результат такого мерж: сервер получает `{"action":"pi{"action":"cha...`
        → JSONDecodeError → tcp_handler уходит в except → клиент получает
        отключение.

        Лок короткий (только на время sendall), блокирующие потоки ждут
        <1ms на нормальном TCP-пути.
        """
        try:
            payload = json.dumps(data).encode('utf-8')
        except Exception as e:
            print(f"[Net] send_json encode error: {e}")
            return

        with self._tcp_send_lock:
            sock = self.tcp_sock
            if sock is None:
                return
            try:
                sock.sendall(payload)
            except Exception as e:
                print(f"[Net] Send JSON error: {e}")

    def update_user_info(self, nick, avatar):
        self.send_json({"action": "update_user", "nick": nick, "avatar": avatar})

    def send_status_update(self, mute, deaf):
        self.send_json({"action": "update_status", "mute": mute, "deaf": deaf})

    def send_presence_update(self, status_icon: str, status_text: str):
        self.send_json({
            "action":      "update_presence",
            "status_icon": status_icon,
            "status_text": status_text,
        })

    def set_video_engine(self, video) -> None:
        self.video = video
        if self._webrtc_loop is not None and not self._webrtc_loop.is_closed():
            video.set_webrtc_loop(self._webrtc_loop)
        print("[Net] VideoEngine registered")
