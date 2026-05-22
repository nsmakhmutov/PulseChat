
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

_HTTP_TIMEOUT = 8

_DOWNLOAD_TIMEOUT = 300

_USER_AGENT = "VoiceChat-Updater/1.0"

_SUPPORTED_EXTENSIONS = (".zip", ".7z")

_ZIP_MAGIC = b"PK\x03\x04"
_7Z_MAGIC  = b"7z\xbc\xaf\x27\x1c"

_STAMP_FILENAME = ".pulse_just_updated"


def _get_install_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _parse_version(tag: str) -> str:
    return tag.lstrip("vV").strip()


def _is_newer(remote: str, current: str) -> bool:
    try:
        return Version(remote) > Version(current)
    except Exception:
        return False


def _find_archive_asset(assets: list):
    for ext in _SUPPORTED_EXTENSIONS:
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(ext):
                return asset["name"], asset["browser_download_url"], ext
    return None


def _download_file_with_progress(url: str, dest_path: str, on_progress=None):
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
    """Синхронная проверка обновлений. Вызывается из фонового потока."""

    _stamp = os.path.join(_get_install_dir(), _STAMP_FILENAME)
    if os.path.exists(_stamp):
        try:
            os.remove(_stamp)
            print("[Updater] Stamp-файл найден — пропускаем проверку после обновления.")
        except Exception:
            pass
        if on_no_update:
            on_no_update()
        return
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

            if on_done:
                on_done()

            current_pid = os.getpid()

            if getattr(sys, 'frozen', False):
                install_dir = os.path.dirname(sys.executable)
                exe_name    = os.path.basename(sys.executable)
            else:
                install_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
                exe_name    = os.path.basename(exe_to_run)

            new_files_dir = os.path.dirname(exe_to_run)

            target_exe = os.path.join(install_dir, exe_name)

            bat_path = os.path.join(tempfile.gettempdir(), "pulse_update_launcher.bat")
            bat_lines = [
                "@echo off",
                f"taskkill /PID {current_pid} /F >nul 2>&1",
                "ping -n 6 127.0.0.1 >nul",
                f'robocopy /E /PURGE /R:2 /W:1 /NP /NJH /NJS "{new_files_dir}" "{install_dir}" >nul 2>&1',
                "if %ERRORLEVEL% LEQ 7 set ERRORLEVEL=0",
                f'echo. > "{os.path.join(install_dir, _STAMP_FILENAME)}"',
                f'start "" "{target_exe}"',
                "ping -n 2 127.0.0.1 >nul",
                'del "%~f0"',
            ]
            bat_content = "\r\n".join(bat_lines) + "\r\n"
            with open(bat_path, "w", encoding="ascii") as bat_f:
                bat_f.write(bat_content)

            print(f"[Updater] install_dir  : {install_dir}")
            print(f"[Updater] new_files_dir: {new_files_dir}")
            print(f"[Updater] target_exe   : {target_exe}")
            print(f"[Updater] Лончер: {bat_path}")

            subprocess.Popen(
                ["cmd", "/c", bat_path],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            sys.exit(0)

        except Exception as e:
            print(f"[Updater] Неожиданная ошибка: {e}")
            if on_error:
                on_error(f"Ошибка загрузки: {e}")

    threading.Thread(target=_download, daemon=True, name="DownloadThread").start()

download_and_apply = download_and_install