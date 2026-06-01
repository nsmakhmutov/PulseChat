# -*- coding: utf-8 -*-
"""
avatar_ring.py
==============

Плавно мигающая (пульсирующая) обводка-индикатор «камера активна» вокруг
аватарки пользователя в дереве комнат.

Отрендерено напрямую под физическое разрешение High-DPI мониторов.
Круг круглый, чёткий и теперь окрашен в насыщенный жёлтый цвет.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen
)

# Мягкий зелёный (свечение «камера активна»). Раньше был насыщенный жёлтый
# (241,196,15) — пользователь попросил заменить на мягко-зелёный.
_RING_RGB = (110, 216, 121)  # #6ed879

# На сколько ступеней квантуем фазу пульсации для кэша
_PHASE_STEPS = 24


def pulse_phase(t_seconds: float, period: float = 1.6) -> float:
    """
    Возвращает фазу пульсации 0..1 по времени.
    """
    x = (t_seconds % period) / period
    return 0.5 - 0.5 * math.cos(2.0 * math.pi * x)


def quantize_phase(phase: float) -> int:
    """Квантуем фазу в целый шаг для ключа кэша."""
    p = max(0.0, min(1.0, phase))
    return int(round(p * (_PHASE_STEPS - 1)))


def make_avatar_with_pulse_ring(base_pix: QPixmap,
                                size: int,
                                phase: float,
                                ring_width: int = 3,
                                gap: int = 0,
                                dpr: float = 1.0) -> QPixmap:
    """
    Накладывает пульсирующее мягко-зелёное РОВНОЕ И ЧЁТКОЕ кольцо на аватарку.
    """
    # 1. Определяем реальный масштаб (DPR) монитора
    current_dpr = dpr
    if base_pix is not None and not base_pix.isNull():
        bdpr = base_pix.devicePixelRatio()
        if bdpr > 0:
            current_dpr = bdpr
    if current_dpr <= 0.0:
        current_dpr = 1.0

    # Вычисляем точный размер в физических пикселях
    physical_size = max(1, int(round(size * current_dpr)))

    # Создаем холст сразу под физические пиксели экрана
    canvas = QImage(physical_size, physical_size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)

    # Привязываем DPR к холсту
    canvas.setDevicePixelRatio(current_dpr)

    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    try:
        # ── 1. Отрисовка аватарки (1 в 1, без проваливания) ─────────────────
        if base_pix is not None and not base_pix.isNull():
            temp_pix = QPixmap(base_pix)
            temp_pix.setDevicePixelRatio(1.0)

            # Скейлим строго под физические пиксели
            scaled = temp_pix.scaled(
                physical_size, physical_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            # Возвращаем системный DPR
            scaled.setDevicePixelRatio(current_dpr)

            # Считаем смещение в логических координатах
            dx = int(round((size - (scaled.width() / current_dpr)) / 2.0))
            dy = int(round((size - (scaled.height() / current_dpr)) / 2.0))

            p.drawPixmap(dx, dy, scaled)

        # ── 2. Отрисовка ЧЁТКОГО геометрического кольца ─────────────────────
        cw = ring_width * (0.85 + 0.35 * phase)  # Динамическая толщина
        alpha = int(150 + 105 * phase)  # Немного поднял базовую яркость для сочности
        r, g_, b = _RING_RGB

        # Вычисляем отступ вовнутрь на половину толщины пера
        half_cw = cw / 2.0
        rect = QRectF(half_cw, half_cw, size - cw, size - cw)

        # Мягкая деликатная подложка (сверхтонкое свечение вокруг основного кольца)
        glow_w = cw * 1.3
        glow_half = glow_w / 2.0
        glow_rect = QRectF(glow_half, glow_half, size - glow_w, size - glow_w)

        p.setPen(QPen(
            QColor(r, g_, b, int(alpha * 0.22)),
            glow_w,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin
        ))
        p.drawEllipse(glow_rect)

        # Основное, резкое и идеально гладкое кольцо
        p.setPen(QPen(
            QColor(r, g_, b, alpha),
            cw,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin
        ))
        p.drawEllipse(rect)

    finally:
        p.end()

    # Конвертируем напрямую в QPixmap
    out = QPixmap.fromImage(canvas)
    return out