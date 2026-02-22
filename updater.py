# updater.py
# ──────────────────────────────────────────────────────────────────────────────
# Тихое автообновление через GitHub Releases.
#
# Логика работы:
#   1. check_for_updates_async(callback) — запускает проверку в фоновом потоке.
#   2. Делает GET https://api.github.com/repos/{owner}/{repo}/releases/latest
#   3. Сравнивает tag_name с APP_VERSION.
#   4. Если новее — вызывает callback(new_version: str, download_url: str).
#   5. download_and_install(url) — скачивает .exe в %TEMP% и запускает его.
#
# Требования к GitHub:
#   - В релизе должен быть приложен файл .exe (или .zip)
#   - Тег релиза должен быть в формате "v1.2.3" или "1.2.3"
# ──────────────────────────────────────────────────────────────────────────────

import threading
import urllib.request
import urllib.error
import json
import os
import sys
import tempfile
import subprocess
from packaging.version import Version

from version import APP_VERSION, GITHUB_REPO

# Таймаут HTTP-запроса (секунды)
_HTTP_TIMEOUT = 8


def _parse_version(tag: str) -> str:
    """Убирает префикс 'v' из тега релиза → '1.2.3'."""
    return tag.lstrip("vV").strip()


def _is_newer(remote: str, current: str) -> bool:
    """True если remote строго новее current (SemVer)."""
    try:
        return Version(remote) > Version(current)
    except Exception:
        return False


def _find_exe_asset(assets: list) -> tuple[str, str] | None:
    """
    Ищет первый .exe или .zip ассет в списке релизных активов.
    Возвращает (name, browser_download_url) или None.
    """
    for asset in assets:
        name = asset.get("name", "").lower()
        if name.endswith(".exe") or name.endswith(".zip"):
            return asset["name"], asset["browser_download_url"]
    return None


def check_for_updates(on_update_found=None, on_no_update=None, on_error=None):
    """
    Синхронная проверка обновлений. Вызывается из фонового потока.

    Параметры:
        on_update_found(version: str, download_url: str)  — найдена новая версия
        on_no_update()                                     — версия актуальна
        on_error(message: str)                             — ошибка сети/API
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
        asset = _find_exe_asset(assets)
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
    Скачивает файл по URL в %TEMP% и запускает его (для .exe — silent install).
    Запускается в фоновом потоке. После запуска установщика завершает приложение.

    Параметры:
        on_progress(percent: int)   — прогресс загрузки 0..100
        on_done()                   — загрузка завершена, установщик запущен
        on_error(message: str)      — ошибка
    """
    def _download():
        try:
            # Определяем имя файла из URL
            filename = download_url.split("/")[-1] or "update.exe"
            dest = os.path.join(tempfile.gettempdir(), filename)

            # Скачиваем с отображением прогресса
            def _reporthook(block_num, block_size, total_size):
                if total_size > 0 and on_progress:
                    pct = min(100, int(block_num * block_size * 100 / total_size))
                    on_progress(pct)

            urllib.request.urlretrieve(download_url, dest, reporthook=_reporthook)

            if on_done:
                on_done()

            # Запускаем установщик / архив
            if dest.lower().endswith(".exe"):
                subprocess.Popen([dest], shell=True)
            else:
                # .zip — открываем папку для ручной установки
                subprocess.Popen(["explorer", os.path.dirname(dest)])

            # Закрываем приложение чтобы установщик мог заменить файлы
            sys.exit(0)

        except Exception as e:
            if on_error:
                on_error(f"Ошибка загрузки: {e}")

    threading.Thread(target=_download, daemon=True, name="DownloadThread").start()
