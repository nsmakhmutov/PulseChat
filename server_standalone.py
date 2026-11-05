#!/usr/bin/env python3
# server_standalone.py — автономный запуск InPulse-сервера (headless, для VPS).
#
# ЗАЧЕМ ЭТОТ ФАЙЛ
# ──────────────
# В клиенте сервер поднимается через EmbeddedServerManager.start_embedded()
# (режим «создать сервер из приложения»). Этот режим остаётся как был —
# данный файл его НЕ трогает.
#
# Здесь — ВТОРОЙ вход: тот же самый класс SFUServer запускается отдельным
# процессом на Linux-VPS, без графики, без PyQt, без Windows-DLL. Используется
# метод SFUServer.start() (а не start_embedded()), который биндится на 0.0.0.0
# и гоняет голос/чат/управление чистым Python из стандартной библиотеки.
#
# Видео-SFU (экран/камера) на VPS не нужен: он работает на машине стримера.
# Попытка сервера подцепить локальный Go-SFU обёрнута в try/except ImportError
# внутри start() и мягко деградирует — на VPS это норма.
#
# ЗАПУСК
# ──────
#   python3 server_standalone.py --name "Наш сервер"
#
# Порты берутся из config.py (TCP 5000 / UDP 5001) — те же, что у клиента.
#
# ⚠️  БЕЗОПАСНОСТЬ: на публичном VPS к серверу сможет подключиться любой, кто
#     знает IP:порт. Пока в server.py не вживлена проверка PIN при входе,
#     ОБЯЗАТЕЛЬНО ограничьте доступ файрволом (ufw / security group VPS),
#     пустив только IP ваших друзей. Лаунчер предупредит об этом при старте.

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time

# ── sys.path: корень проекта (где лежат config.py, server/, network_engine/) ──
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _setup_logging(logfile: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if logfile:
        try:
            handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
        except OSError as e:
            print(f"[launcher] Не удалось открыть лог-файл {logfile}: {e}")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _bind_ip_hint() -> str:
    """Best-effort: какой внешний IP увидят клиенты (для печати в баннере)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))      # пакет не уходит, только выбирает маршрут
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Автономный InPulse-сервер для VPS (headless)."
    )
    parser.add_argument(
        "--name", default=os.environ.get("INPULSE_SERVER_NAME", "VPS-сервер"),
        help="Отображаемое имя сервера (или env INPULSE_SERVER_NAME).",
    )
    parser.add_argument(
        "--pin", default=os.environ.get("INPULSE_SERVER_PIN", ""),
        help="PIN-код для входа (или env INPULSE_SERVER_PIN). "
             "Пусто = вход без PIN (как в Radmin).",
    )
    parser.add_argument(
        "--log", default=os.environ.get("INPULSE_SERVER_LOG", ""),
        help="Путь к лог-файлу (по умолчанию только stdout).",
    )
    args = parser.parse_args()

    _setup_logging(args.log or None)
    log = logging.getLogger("launcher")

    # Импортируем ПОСЛЕ настройки sys.path. Тянет только stdlib + config.
    try:
        from config import DEFAULT_PORT_TCP, DEFAULT_PORT_UDP
        from server import SFUServer
    except Exception as e:
        log.error("Не удалось импортировать серверный пакет: %s", e)
        log.error("Запускайте этот файл из корня проекта (рядом с config.py).")
        return 2

    # ── PIN ──────────────────────────────────────────────────────────────────
    # Если PIN не задан — на публичном VPS сервер открыт всем. Предупреждаем.
    pin = (args.pin or "").strip()
    if not pin:
        log.warning("=" * 60)
        log.warning("⚠️  PIN НЕ ЗАДАН. На публичном VPS сервер открыт всем.")
        log.warning("    Задайте --pin КОД (или env INPULSE_SERVER_PIN), либо")
        log.warning("    ограничьте доступ файрволом до доверенных IP.")
        log.warning("=" * 60)
    else:
        log.info("PIN задан — вход только по коду.")

    ip_hint = _bind_ip_hint()
    log.info("InPulse standalone server")
    log.info("  Имя:   %s", args.name)
    log.info("  Bind:  0.0.0.0  (внешний IP похоже %s)", ip_hint)
    log.info("  Порты: TCP %d, UDP %d", DEFAULT_PORT_TCP, DEFAULT_PORT_UDP)
    log.info("  Друзья подключаются: «Ввести вручную» → %s", ip_hint)

    try:
        server = SFUServer(host="0.0.0.0", server_name=args.name, pin=pin)
    except OSError as e:
        log.error("Не удалось занять порты (заняты другим процессом?): %s", e)
        return 1

    # start() — блокирующий accept-цикл. Гоним в фоне, главный поток ждёт сигнал.
    stop_evt = threading.Event()

    def _serve():
        try:
            server.start()
        except Exception as e:
            log.error("Серверный цикл завершился с ошибкой: %s", e)
        finally:
            stop_evt.set()

    threading.Thread(target=_serve, name="inpulse-serve", daemon=True).start()

    def _shutdown(signum, _frame):
        log.info("Получен сигнал %s — останавливаемся…", signum)
        # Разрываем accept-циклы: сбрасываем флаг и закрываем сокеты.
        try:
            server._accepting = False
        except Exception:
            pass
        for sock_attr in ("tcp_sock", "udp_sock"):
            try:
                getattr(server, sock_attr).close()
            except Exception:
                pass
        stop_evt.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Сервер запущен. Ctrl+C или SIGTERM для остановки.")
    # Ждём до сигнала; periodic wakeup чтобы сигналы обрабатывались на всех ОС.
    while not stop_evt.wait(timeout=1.0):
        pass

    time.sleep(0.3)  # дать потокам закрыться
    log.info("Остановлен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
