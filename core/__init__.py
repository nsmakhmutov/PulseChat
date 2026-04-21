# core/__init__.py — Системные утилиты InPulse
#
# job_object:       Windows Job Object для автокила дочерних процессов
# updater:          Тихое автообновление через GitHub Releases
# rust_video_track: Deprecated заглушка (v3 — не используется)

from .job_object import assign_to_job
from .updater import check_for_updates_async, download_and_apply

__all__ = [
    'assign_to_job',
    'check_for_updates_async', 'download_and_apply',
]
