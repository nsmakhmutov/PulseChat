#!/usr/bin/env python3
"""
make_requirements.py
──────────────────────────────────────────────────────────────────────────────
Генерирует чистый requirements.txt — только то, что реально импортируется
в коде проекта, без мусора накопленного pip freeze в виртуальном окружении.

Использует pipreqs. Установка (один раз):
    pip install pipreqs

Запуск из корня проекта:
    python make_requirements.py

Результат: requirements.txt в корне проекта.
──────────────────────────────────────────────────────────────────────────────
"""

import subprocess
import sys
import os


def _get_pipreqs_exe() -> str:
    """
    Возвращает путь к исполняемому файлу pipreqs.
    Ищет в папке Scripts/bin рядом с текущим python.exe —
    это надёжно работает и с venv, и с глобальным окружением.

    pipreqs 0.5.x не имеет __main__.py, поэтому 'python -m pipreqs'
    не работает — нужно запускать exe напрямую.
    """
    scripts_dir = os.path.dirname(sys.executable)   # .venv/Scripts  (Windows)
                                                     # .venv/bin      (Linux/Mac)
    # Windows
    candidate_win = os.path.join(scripts_dir, "pipreqs.exe")
    if os.path.exists(candidate_win):
        return candidate_win

    # Linux / Mac
    candidate_unix = os.path.join(scripts_dir, "pipreqs")
    if os.path.exists(candidate_unix):
        return candidate_unix

    return ""   # не найден — установим ниже


def main():
    pipreqs_exe = _get_pipreqs_exe()

    # Если pipreqs не установлен — ставим и ищем ещё раз
    if not pipreqs_exe:
        print("pipreqs не найден. Установка...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pipreqs"],
            check=True
        )
        pipreqs_exe = _get_pipreqs_exe()

    if not pipreqs_exe:
        print("ОШИБКА: pipreqs.exe не найден даже после установки.")
        print(f"Папка Scripts: {os.path.dirname(sys.executable)}")
        print("Попробуйте вручную: pip install pipreqs")
        sys.exit(1)

    print(f"[make_requirements] Используется: {pipreqs_exe}")

    project_root = os.path.dirname(os.path.abspath(__file__))
    output_file  = os.path.join(project_root, "requirements.txt")

    print(f"[make_requirements] Сканирую импорты в: {project_root}")
    print(f"[make_requirements] Результат: {output_file}")

    result = subprocess.run(
        [
            pipreqs_exe,            # E:\.venv\Scripts\pipreqs.exe
            project_root,
            "--force",              # перезаписать если файл уже есть
            "--encoding", "utf-8",
            "--savepath", output_file,
            "--ignore", "dist,build,__pycache__,.git,venv,.venv,env",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("ОШИБКА pipreqs:")
        print(result.stderr or result.stdout)
        sys.exit(1)

    # pipreqs не умеет определять некоторые пакеты по имени модуля.
    # Добавляем их вручную если они не попали в вывод.
    MANUAL_ADDITIONS = {
        "PyQt6":                    "PyQt6>=6.4",
        "packaging":                "packaging>=23.0",   # нужен updater.py
        "opuslib":                  "opuslib",
        "sounddevice":              "sounddevice",
        "numpy":                    "numpy",
        "pygame":                   "pygame",
        "py7zr":                    "py7zr>=0.20",       # нужен updater.py (распаковка .7z)
        "opencv-python-headless":   "opencv-python-headless",  # нужен dxcam внутри себя
        # pipreqs не видит "import py7zr" внутри try/except — добавляем вручную
    }

    with open(output_file, "r", encoding="utf-8") as f:
        content = f.read()

    lines = [l.strip() for l in content.splitlines() if l.strip()]
    existing_pkgs = {l.split(">=")[0].split("==")[0].lower() for l in lines}

    additions = []
    for pkg_key, pkg_line in MANUAL_ADDITIONS.items():
        if pkg_key.lower() not in existing_pkgs:
            print(f"[make_requirements] Добавляю вручную: {pkg_line}")
            additions.append(pkg_line)

    if additions:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write("\n# Добавлено make_requirements.py (ручные исключения)\n")
            for line in additions:
                f.write(line + "\n")

    print("[make_requirements] ✅ requirements.txt готов:")
    with open(output_file, "r", encoding="utf-8") as f:
        print(f.read())


if __name__ == "__main__":
    main()