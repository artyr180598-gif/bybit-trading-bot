"""
report.py — Превращает результат сканирования в читаемый отчёт для Telegram
═══════════════════════════════════════════════════════════════════════════════

Telegram ограничивает сообщение ~4096 символами, поэтому отчёт разбивается на
несколько частей. Функция build_report(result) возвращает СПИСОК сообщений
(каждое <= 4000 символов), которые бот отправляет по очереди.

Формат — HTML (parse_mode="HTML" в Telegram): <b>жирный</b>, <code>моно</code>.
"""

from __future__ import annotations

from html import escape
from datetime import datetime

from scanner import SEVERITY

MAX_LEN = 3900  # с запасом до лимита Telegram в 4096

GRADE_EMOJI = {"A": "🟢", "B": "🟢", "C": "🟡", "D": "🟠", "F": "🔴"}
GRADE_TEXT = {
    "A": "Отлично — серьёзных проблем не найдено",
    "B": "Хорошо — есть мелкие улучшения",
    "C": "Средне — стоит заняться безопасностью",
    "D": "Слабо — найдены заметные проблемы",
    "F": "Критично — нужны срочные меры",
}


def _chunk(messages: list[str], block: str) -> None:
    """Добавляет блок текста, начиная новое сообщение, если текущее переполнено."""
    if messages and len(messages[-1]) + len(block) < MAX_LEN:
        messages[-1] += block
    else:
        messages.append(block)


def build_report(result: dict) -> list[str]:
    """Главная функция: result (из scanner.scan) → список HTML-сообщений."""
    if not result.get("ok"):
        return [f"❌ <b>Не удалось просканировать</b>\n\n{escape(result.get('error', 'Неизвестная ошибка'))}"]

    host = escape(result["host"])
    grade = result["grade"]
    score = result["score"]
    counts = result["counts"]
    findings = result["findings"]

    messages: list[str] = []

    # ── Шапка отчёта: оценка и сводка ────────────────────────────────────────
    header = (
        f"🛡 <b>Отчёт по безопасности сайта</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 Сайт: <code>{host}</code>\n"
        f"{GRADE_EMOJI.get(grade, '⚪')} Оценка: <b>{grade}</b> "
        f"({score}/100) — {GRADE_TEXT.get(grade, '')}\n\n"
        f"<b>Сводка находок:</b>\n"
    )
    total = sum(counts.values())
    if total == 0:
        header += "✅ Проблем не обнаружено. Отличная работа!\n"
    else:
        for sev in ("critical", "high", "medium", "low", "info"):
            n = counts.get(sev, 0)
            if n:
                meta = SEVERITY[sev]
                header += f"{meta['emoji']} {meta['label']}: <b>{n}</b>\n"
    header += f"\n⏱ Время: {result.get('duration', '?')} c\n"
    messages.append(header)

    # ── Детали по каждой находке ─────────────────────────────────────────────
    if findings:
        _chunk(messages, "\n📋 <b>Детали находок:</b>\n")
    for i, f in enumerate(findings, 1):
        meta = SEVERITY.get(f.severity, SEVERITY["info"])
        block = (
            f"\n{meta['emoji']} <b>{i}. {escape(f.title)}</b>\n"
            f"   <i>Критичность: {meta['label']}</i>\n"
            f"   {escape(f.description)}\n"
        )
        if f.evidence:
            block += f"   🔍 <code>{escape(f.evidence[:200])}</code>\n"
        block += f"   🔧 <b>Решение:</b> {escape(f.remediation)}\n"
        _chunk(messages, block)

    # ── Подвал: дисклеймер ───────────────────────────────────────────────────
    footer = (
        "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ Проверки безопасные и не наносят вреда сайту. Отчёт носит "
        "рекомендательный характер. Сканируйте только сайты, которыми владеете "
        "или на которые есть разрешение.\n"
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    _chunk(messages, footer)

    return messages
