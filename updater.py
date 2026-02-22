# updater.py
# ──────────────────────────────────────────────────────────────────────────────
# Тихое автообновление через GitHub Releases.
#
# Логика работы:
#   1. check_for_updates_async(callback) — запускает проверку в фоновом потоке.
#   2. Делает GET https://api.github.com/repos/{owner}/{repo}/releases/latest
#   3. Сравнивает tag_name с APP_VERSION.
#   4. Если новее — вызывает callback(new_version: str, download_url: str).
#   5. download_and_install(url) — скачивает .zip/.7z в %TEMP%,
#      распаковывает, находит .exe внутри и запускает его.
#
# Требования к GitHub релизу:
#   - Прикрепи файл VoiceChat.zip или VoiceChat.7z (содержащий VoiceChat.exe)
#   - Тег релиза должен быть в формате "v1.2.3" или "1.2.3"
#
# Зависимости:
#   - py7zr      (для .7z архивов)  pip install py7zr
#   - packaging  (для SemVer)       pip install packaging
# ──────────────────────────────────────────────────────────────────────────────

import threading
import urllib.request
import urllib.error
import json
import os
import sys
import tempfile
import zipfile
import subprocess
import shutil
from packaging.version import Version

from version import APP_VERSION, GITHUB_REPO

# Таймаут HTTP-запроса к API (секунды)
_HTTP_TIMEOUT = 8

# Таймаут скачивания (секунды). Файл 116 МБ на плохом канале может идти долго.
_DOWNLOAD_TIMEOUT = 300

# User-Agent для всех запросов — GitHub CDN блокирует запросы без него.
_USER_AGENT = "VoiceChat-Updater/1.0"

# Поддерживаемые расширения архивов (в порядке приоритета)
_SUPPORTED_EXTENSIONS = (".zip", ".7z")

# Magic bytes для определения типа архива по содержимому (не по расширению)
_ZIP_MAGIC = b"PK\x03\x04"
_7Z_MAGIC  = b"7z\xbc\xaf\x27\x1c"


def _parse_version(tag: str) -> str:
    """Убирает префикс 'v' из тега релиза -> '1.2.3'."""
    return tag.lstrip("vV").strip()


def _is_newer(remote: str, current: str) -> bool:
    """True если remote строго новее current (SemVer)."""
    try:
        return Version(remote) > Version(current)
    except Exception:
        return False


def _find_archive_asset(assets: list):
    """
    Ищет первый поддерживаемый архив (.zip или .7z) в списке релизных активов.
    Возвращает (name, browser_download_url, ext) или None.

    Приоритет: сначала .zip (встроенная библиотека), потом .7z (py7zr).
    Раньше функция называлась _find_zip_asset и искала только .zip —
    это и была причина бага: файл VoiceChatClient.7z не находился,
    код падал в fallback на html_url (HTML-страница GitHub) → не архив → ошибка.
    """
    for ext in _SUPPORTED_EXTENSIONS:
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(ext):
                return asset["name"], asset["browser_download_url"], ext
    return None


def _download_file_with_progress(url: str, dest_path: str, on_progress=None):
    """
    Скачивает файл по URL с поддержкой прогресса.

    ВАЖНО: использует urllib.request.Request с User-Agent.
    urlretrieve() не позволяет передать заголовки — без User-Agent
    GitHub CDN возвращает HTML вместо бинарного файла.

    Raises:
        urllib.error.HTTPError  — сервер вернул HTTP-ошибку (4xx/5xx)
        urllib.error.URLError   — сетевая ошибка
        OSError                 — ошибка записи на диск
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/octet-stream",
        }
    )

    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as response:
        total_size = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 65536  # 64 KB

        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress and total_size > 0:
                    pct = min(99, int(downloaded * 100 / total_size))
                    on_progress(pct)

    if on_progress:
        on_progress(100)


def _detect_archive_type(path: str):
    """
    Определяет тип архива по magic bytes (не по расширению файла).
    Возвращает 'zip', '7z' или None если файл не является поддерживаемым архивом.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(6)
        if header[:4] == _ZIP_MAGIC:
            return "zip"
        if header[:6] == _7Z_MAGIC:
            return "7z"
        return None
    except OSError:
        return None


def _extract_archive(archive_path: str, extract_dir: str, archive_type: str):
    """
    Распаковывает архив в указанную директорию.

    Raises:
        zipfile.BadZipFile  — повреждённый ZIP
        RuntimeError        — py7zr не установлен или неизвестный формат
        Exception           — прочие ошибки py7zr
    """
    if archive_type == "zip":
        with zipfile.ZipFile(archive_path, "r") as z:
            z.extractall(extract_dir)

    elif archive_type == "7z":
        try:
            import py7zr
        except ImportError:
            raise RuntimeError(
                "Для распаковки .7z необходима библиотека py7zr.\n"
                "Установите её командой:  pip install py7zr"
            )
        with py7zr.SevenZipFile(archive_path, mode="r") as z:
            z.extractall(path=extract_dir)

    else:
        raise RuntimeError(f"Неизвестный тип архива: {archive_type!r}")


def check_for_updates(on_update_found=None, on_no_update=None, on_error=None):
    """
    Синхронная проверка обновлений. Вызывается из фонового потока.

    Параметры:
        on_update_found(version: str, download_url: str)  - найдена новая версия
        on_no_update()                                     - версия актуальна
        on_error(message: str)                             - ошибка сети/API
    """
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        if on_error:
            on_error("GITHUB_REPO не настроен в version.py")
        return

    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if on_error:
            on_error(f"GitHub API HTTP {e.code}: {e.reason}")
        return
    except urllib.error.URLError as e:
        if on_error:
            on_error(f"Нет соединения: {e.reason}")
        return
    except Exception as e:
        if on_error:
            on_error(f"Ошибка проверки: {e}")
        return

    tag = data.get("tag_name", "")
    remote_version = _parse_version(tag)

    if not remote_version:
        if on_error:
            on_error("Не удалось прочитать версию из GitHub")
        return

    if _is_newer(remote_version, APP_VERSION):
        assets = data.get("assets", [])
        asset = _find_archive_asset(assets)

        if asset is None:
            # Нет ни .zip ни .7z — сообщаем внятно вместо тихого падения в html_url
            if on_error:
                on_error(
                    f"Найдена новая версия {remote_version}, но к релизу\n"
                    "не прикреплён архив (.zip или .7z).\n"
                    "Скачайте вручную с GitHub."
                )
            return

        _name, download_url, _ext = asset
        if on_update_found:
            on_update_found(remote_version, download_url)
    else:
        if on_no_update:
            on_no_update()


def check_for_updates_async(on_update_found=None, on_no_update=None, on_error=None):
    """
    Запускает проверку обновлений в фоновом потоке-демоне.
    Не блокирует UI. Безопасен для вызова из любого потока.
    """
    t = threading.Thread(
        target=check_for_updates,
        kwargs={
            "on_update_found": on_update_found,
            "on_no_update": on_no_update,
            "on_error": on_error,
        },
        daemon=True,
        name="UpdaterThread",
    )
    t.start()


def download_and_install(download_url: str, on_progress=None, on_done=None, on_error=None):
    """
    Скачивает .zip/.7z по URL в %TEMP%, распаковывает, ищет .exe и запускает.
    После запуска установщика закрывает текущее приложение через sys.exit(0).

    Параметры:
        on_progress(percent: int)   - прогресс загрузки 0..100
        on_done()                   - загрузка завершена, установщик запущен
        on_error(message: str)      - ошибка
    """
    def _download():
        try:
            # 1. Сохраняем с оригинальным именем из URL (включая расширение .7z/.zip)
            filename = download_url.split("/")[-1].split("?")[0] or "update.bin"
            dest_path = os.path.join(tempfile.gettempdir(), filename)

            print(f"[Updater] Скачиваю: {download_url}")
            print(f"[Updater] Сохраняю в: {dest_path}")

            try:
                _download_file_with_progress(download_url, dest_path, on_progress)
            except urllib.error.HTTPError as e:
                if on_error:
                    on_error(f"Ошибка сервера при скачивании: HTTP {e.code} {e.reason}")
                return
            except urllib.error.URLError as e:
                if on_error:
                    on_error(f"Нет соединения при скачивании: {e.reason}")
                return

            file_size = os.path.getsize(dest_path)
            print(f"[Updater] Скачано байт: {file_size}")

            # 2. Определяем тип архива по magic bytes (не доверяем расширению)
            archive_type = _detect_archive_type(dest_path)
            print(f"[Updater] Тип архива по magic bytes: {archive_type}")

            if archive_type is None:
                try:
                    with open(dest_path, "rb") as f:
                        preview = f.read(256).decode("utf-8", errors="replace")
                except Exception:
                    preview = "<не читается>"
                print(f"[Updater] Не архив. Начало файла: {preview[:120]!r}")
                if on_error:
                    on_error(
                        "Скачанный файл не является архивом (.zip или .7z).\n"
                        "Возможно, GitHub вернул страницу ошибки.\n"
                        "Попробуйте ещё раз или скачайте вручную."
                    )
                return

            # 3. Распаковываем
            extract_dir = os.path.join(tempfile.gettempdir(), "voicechat_update")
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir, ignore_errors=True)
            os.makedirs(extract_dir, exist_ok=True)

            print(f"[Updater] Распаковываю ({archive_type}) в: {extract_dir}")
            try:
                _extract_archive(dest_path, extract_dir, archive_type)
            except RuntimeError as e:
                if on_error:
                    on_error(str(e))
                return
            except zipfile.BadZipFile:
                if on_error:
                    on_error("ZIP-архив повреждён. Попробуйте скачать ещё раз.")
                return
            except Exception as e:
                if on_error:
                    on_error(f"Ошибка распаковки: {e}")
                return

            # 4. Ищем .exe внутри
            exe_to_run = None
            for root, _dirs, files in os.walk(extract_dir):
                for f in files:
                    if f.lower().endswith(".exe"):
                        exe_to_run = os.path.join(root, f)
                        break
                if exe_to_run:
                    break

            if exe_to_run is None:
                if on_error:
                    on_error(
                        "В архиве не найден .exe файл.\n"
                        f"Распакован в: {extract_dir}\n"
                        "Установите вручную."
                    )
                subprocess.Popen(["explorer", extract_dir])
                return

            # 5. Сообщаем UI что сейчас закроемся
            if on_done:
                on_done()

            # 6. Запускаем через bat-лончер: он дожидается смерти
            #    текущего процесса (по PID) и только потом открывает
            #    новый exe. Без этого оба окна живут одновременно,
            #    потому что Qt-cleanup занимает время после sys.exit(0).
            current_pid = os.getpid()
            bat_path = os.path.join(tempfile.gettempdir(), "pulse_update_launcher.bat")
            # taskkill /PID — убивает старый процесс принудительно,
            # ping -n 3 — пауза ~2 сек (стандартный трюк вместо sleep в cmd),
            # start "" — открывает новый exe в отдельном процессе,
            # del — самоудаляется после запуска.
            bat_lines = [
                "@echo off",
                f"taskkill /PID {current_pid} /F >nul 2>&1",
                "ping -n 3 127.0.0.1 >nul",
                f'start "" "{exe_to_run}"',
                'del "%~f0"',
            ]
            bat_content = "\n".join(bat_lines) + "\n"
            with open(bat_path, "w", encoding="ascii") as bat_f:
                bat_f.write(bat_content)

            print(f"[Updater] Лончер: {bat_path}")
            subprocess.Popen(
                ["cmd", "/c", bat_path],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            # sys.exit вызывает Qt-cleanup и закрывает окно.
            # Батник убьёт нас через taskkill если Qt завис.
            sys.exit(0)

        except Exception as e:
            print(f"[Updater] Неожиданная ошибка: {e}")
            if on_error:
                on_error(f"Ошибка загрузки: {e}")

    threading.Thread(target=_download, daemon=True, name="DownloadThread").start()