# updater.py
# ──────────────────────────────────────────────────────────────────────────────
# Тихое автообновление через GitHub Releases.
#
# Логика работы:
#   1. check_for_updates_async(callback) — запускает проверку в фоновом потоке.
#   2. Делает GET https://api.github.com/repos/{owner}/{repo}/releases/latest
#   3. Сравнивает tag_name с APP_VERSION.
#   4. Если новее — вызывает callback(new_version: str, download_url: str).
#   5. download_and_install(url) — скачивает .zip в %TEMP%,
#      распаковывает, находит .exe внутри и запускает его.
#
# Требования к GitHub релизу:
#   - Прикрепи файл VoiceChat.zip (содержащий VoiceChat.exe)
#   - Тег релиза должен быть в формате "v1.2.3" или "1.2.3"
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
from packaging.version import Version

from version import APP_VERSION, GITHUB_REPO

# Таймаут HTTP-запроса (секунды)
_HTTP_TIMEOUT = 8


def _parse_version(tag: str) -> str:
    """Убирает префикс 'v' из тега релиза -> '1.2.3'."""
    return tag.lstrip("vV").strip()


def _is_newer(remote: str, current: str) -> bool:
    """True если remote строго новее current (SemVer)."""
    try:
        return Version(remote) > Version(current)
    except Exception:
        return False


def _find_zip_asset(assets: list):
    """
    Ищет первый .zip ассет в списке релизных активов.
    Возвращает (name, browser_download_url) или None.
    GitHub не принимает .exe — поэтому всегда упаковываем в .zip.
    """
    for asset in assets:
        name = asset.get("name", "").lower()
        if name.endswith(".zip"):
            return asset["name"], asset["browser_download_url"]
    return None


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
        "User-Agent": "VoiceChat-Updater/1.0",
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
        asset = _find_zip_asset(assets)
        download_url = asset[1] if asset else data.get("html_url", "")
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
    Скачивает .zip по URL в %TEMP%, распаковывает, ищет .exe внутри и запускает.
    После запуска установщика - закрывает текущее приложение через sys.exit(0).

    Параметры:
        on_progress(percent: int)   - прогресс загрузки 0..100
        on_done()                   - загрузка завершена, установщик запущен
        on_error(message: str)      - ошибка
    """
    def _download():
        try:
            # 1. Скачиваем zip
            filename = download_url.split("/")[-1] or "update.zip"
            dest_zip = os.path.join(tempfile.gettempdir(), filename)

            def _reporthook(block_num, block_size, total_size):
                if total_size > 0 and on_progress:
                    pct = min(100, int(block_num * block_size * 100 / total_size))
                    on_progress(pct)

            urllib.request.urlretrieve(download_url, dest_zip, reporthook=_reporthook)

            # 2. Распаковываем в temp-папку
            extract_dir = os.path.join(tempfile.gettempdir(), "voicechat_update")
            if os.path.exists(extract_dir):
                import shutil
                shutil.rmtree(extract_dir, ignore_errors=True)
            os.makedirs(extract_dir, exist_ok=True)

            with zipfile.ZipFile(dest_zip, "r") as z:
                z.extractall(extract_dir)

            # 3. Ищем .exe внутри
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

            # 4. Запускаем и закрываем приложение
            if on_done:
                on_done()

            subprocess.Popen([exe_to_run], shell=True)
            sys.exit(0)

        except zipfile.BadZipFile:
            if on_error:
                on_error("Скачанный файл повреждён. Попробуйте ещё раз.")
        except Exception as e:
            if on_error:
                on_error(f"Ошибка загрузки: {e}")

    threading.Thread(target=_download, daemon=True, name="DownloadThread").start()