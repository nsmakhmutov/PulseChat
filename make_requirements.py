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


def main():
    # Проверяем наличие pipreqs
    try:
        subprocess.run(
            [sys.executable, "-m", "pipreqs", "--version"],
            check=True, capture_output=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("pipreqs не найден. Установка...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pipreqs"], check=True)

    project_root = os.path.dirname(os.path.abspath(__file__))
    output_file  = os.path.join(project_root, "requirements.txt")

    print(f"[make_requirements] Сканирую импорты в: {project_root}")
    print(f"[make_requirements] Результат: {output_file}")

    result = subprocess.run(
        [
            "pipreqs",
            project_root,
            "--force",
            "--encoding", "utf-8",
            "--savepath", output_file,
            "--ignore", "dist,build,__pycache__,.git,venv,.venv,env",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("ОШИБКА pipreqs:")
        print(result.stderr)
        sys.exit(1)

    # pipreqs не знает о некоторых пакетах по имени модуля (напр. PyQt6 → PyQt6).
    # Добавляем известные исключения вручную если они есть в коде но не попали.
    MANUAL_ADDITIONS = {
        "PyQt6":       "PyQt6>=6.4",
        "packaging":   "packaging>=23.0",   # нужен updater.py
        "opuslib":     "opuslib",
        "sounddevice": "sounddevice",
        "numpy":       "numpy",
        "pygame":      "pygame",
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