"""
scanner.py — Движок анализа сайтов на уязвимости (безопасный, не ломает сайт)
═══════════════════════════════════════════════════════════════════════════════

ЧТО ЭТО:
    Набор «проверок» (checks). Каждая проверка смотрит на сайт с одной стороны
    (заголовки, SSL, cookies, открытые файлы и т.д.) и возвращает список находок
    (Finding). Главная функция scan(url) запускает все проверки и собирает отчёт.

ПОЧЕМУ ТАК (для новичка):
    • Все проверки ПАССИВНЫЕ или «лёгкие активные» — мы только читаем то, что
      сайт сам отдаёт. Мы НЕ ломаем сайт, НЕ перебираем пароли, НЕ делаем DoS,
      НЕ внедряем вредоносные данные. Это и законно при тестах своих сайтов,
      и безопасно для клиента.
    • Каждая находка имеет severity (критичность) и понятное объяснение +
      рекомендацию «как починить». Это то, что нужно клиенту.

КАК ДОБАВИТЬ СВОЮ ПРОВЕРКУ:
    1. Напиши функцию check_xxx(ctx) -> list[Finding]
    2. Добавь её в список CHECKS внизу файла.
    Всё. Движок сам её вызовет и добавит находки в отчёт.
"""

from __future__ import annotations

import socket
import ssl
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import requests
# Отключаем «шумные» предупреждения requests о самоподписанных сертификатах —
# мы их обрабатываем сами и показываем как находку.
from requests.packages.urllib3.exceptions import InsecureRequestWarning  # type: ignore
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)       # type: ignore


# ─── КРИТИЧНОСТЬ (severity) ───────────────────────────────────────────────────
# rank  — для сортировки (чем выше, тем опаснее)
# score — сколько баллов «снимаем» с оценки сайта за находку этой критичности
SEVERITY = {
    "critical": {"emoji": "🔴", "label": "Критическая", "rank": 5, "score": 35},
    "high":     {"emoji": "🟠", "label": "Высокая",     "rank": 4, "score": 18},
    "medium":   {"emoji": "🟡", "label": "Средняя",      "rank": 3, "score": 8},
    "low":      {"emoji": "🔵", "label": "Низкая",       "rank": 2, "score": 3},
    "info":     {"emoji": "⚪", "label": "Информация",   "rank": 1, "score": 0},
}

USER_AGENT = "SecurityScanBot/1.0 (+authorized vulnerability assessment)"
TIMEOUT = 12  # секунд на один сетевой запрос


@dataclass
class Finding:
    """Одна находка (проблема или наблюдение) на сайте."""
    title: str           # короткое название, напр. «Нет заголовка HSTS»
    severity: str        # ключ из SEVERITY
    description: str      # что это и чем опасно — простыми словами
    remediation: str      # как починить
    evidence: str = ""    # «доказательство»: что именно мы увидели

    @property
    def rank(self) -> int:
        return SEVERITY.get(self.severity, SEVERITY["info"])["rank"]


@dataclass
class ScanContext:
    """
    Общий «контекст» сканирования — то, что нужно всем проверкам.
    Чтобы не качать главную страницу по 10 раз, мы делаем это один раз
    и кладём результат сюда.
    """
    url: str                       # нормализованный URL (с https://)
    host: str                      # домен без схемы
    scheme: str                    # http или https
    session: requests.Session
    response: requests.Response | None = None   # ответ главной страницы
    findings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# ─── ВСПОМОГАТЕЛЬНОЕ ───────────────────────────────────────────────────────────

def normalize_url(raw: str) -> str | None:
    """
    Приводим введённый пользователем адрес к нормальному виду.
    'example.com'         → 'https://example.com'
    'http://example.com/' → 'http://example.com/'
    Возвращаем None, если адрес совсем некорректный.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        return None
    # Запрещаем сканировать локальные/внутренние адреса (защита от SSRF и от
    # случайного скана чужой внутренней сети).
    host = parsed.hostname or ""
    if _is_private_host(host):
        return None
    return raw


def _is_private_host(host: str) -> bool:
    """True, если адрес локальный/приватный (localhost, 127.x, 10.x, и т.д.)."""
    host = host.lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    # Приватные диапазоны IPv4
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)", host):
        return True
    return False


def _doh_txt(name: str) -> list[str]:
    """
    Получить TXT-записи домена через DNS-over-HTTPS (Google).
    Используем HTTPS вместо обычного DNS — работает на любом сервере без
    дополнительных библиотек. Возвращаем список строк TXT.
    """
    try:
        r = requests.get(
            "https://dns.google/resolve",
            params={"name": name, "type": "TXT"},
            timeout=TIMEOUT,
            headers={"accept": "application/dns-json"},
        )
        data = r.json()
        out = []
        for ans in data.get("Answer", []):
            txt = ans.get("data", "").strip('"')
            out.append(txt)
        return out
    except Exception:
        return []


# ─── ПРОВЕРКИ ──────────────────────────────────────────────────────────────────

def check_https(ctx: ScanContext) -> list[Finding]:
    """Сайт вообще работает по HTTPS? И редиректит ли HTTP → HTTPS?"""
    out = []
    # 1. Доступен ли HTTPS
    https_ok = False
    try:
        r = ctx.session.get(f"https://{ctx.host}", timeout=TIMEOUT,
                            allow_redirects=True, verify=True)
        https_ok = r.ok or r.status_code < 500
    except requests.exceptions.SSLError:
        https_ok = True  # HTTPS есть, просто проблема с сертификатом (проверим отдельно)
    except Exception:
        https_ok = False

    if not https_ok:
        out.append(Finding(
            title="Сайт недоступен по HTTPS",
            severity="high",
            description="Сайт не отвечает по защищённому протоколу HTTPS. Весь "
                        "трафик (включая пароли) может передаваться в открытом "
                        "виде и быть перехвачен.",
            remediation="Установите бесплатный SSL-сертификат (например, Let's "
                        "Encrypt) и включите HTTPS на сервере/хостинге.",
        ))
        return out

    # 2. Редиректит ли HTTP на HTTPS
    try:
        r = ctx.session.get(f"http://{ctx.host}", timeout=TIMEOUT,
                            allow_redirects=False, verify=False)
        loc = r.headers.get("Location", "")
        if r.status_code in (301, 302, 307, 308) and loc.lower().startswith("https"):
            pass  # хорошо — редиректит на HTTPS
        elif 200 <= r.status_code < 400:
            out.append(Finding(
                title="HTTP не перенаправляется на HTTPS",
                severity="medium",
                description="Сайт открывается по незащищённому http:// без "
                            "автоматического перенаправления на https://. "
                            "Пользователь может случайно работать по открытому "
                            "каналу.",
                remediation="Настройте постоянный редирект 301 со всех http:// "
                            "адресов на https://.",
                evidence=f"http://{ctx.host} → код {r.status_code}",
            ))
    except Exception:
        pass  # HTTP закрыт совсем — это даже хорошо
    return out


def check_tls_certificate(ctx: ScanContext) -> list[Finding]:
    """Проверяем SSL-сертификат: валиден ли, не истёк ли, скоро ли истекает."""
    out = []
    host = ctx.host.split(":")[0]
    port = 443
    if ":" in ctx.host:
        try:
            port = int(ctx.host.split(":")[1])
        except ValueError:
            port = 443

    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
    except ssl.SSLCertVerificationError as e:
        out.append(Finding(
            title="Невалидный SSL-сертификат",
            severity="high",
            description="Сертификат сайта не прошёл проверку (просрочен, "
                        "самоподписан или не соответствует домену). Браузеры "
                        "будут показывать страшное предупреждение, клиенты "
                        "уходят.",
            remediation="Выпустите корректный сертификат для вашего домена "
                        "(Let's Encrypt бесплатно) и убедитесь, что он покрывает "
                        "и www, и основной домен.",
            evidence=str(e),
        ))
        return out
    except Exception as e:
        ctx.errors.append(f"TLS: {e}")
        return out

    # Сертификат получен — проверяем срок действия
    not_after = cert.get("notAfter")
    if not_after:
        try:
            expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc)
            days_left = (expires - datetime.now(timezone.utc)).days
            if days_left < 0:
                out.append(Finding(
                    title="SSL-сертификат истёк",
                    severity="critical",
                    description="Срок действия сертификата закончился. Сайт "
                                "показывает критическую ошибку безопасности всем "
                                "посетителям.",
                    remediation="Срочно перевыпустите сертификат и настройте "
                                "автообновление.",
                    evidence=f"Истёк {not_after}",
                ))
            elif days_left < 14:
                out.append(Finding(
                    title=f"SSL-сертификат истекает через {days_left} дн.",
                    severity="medium",
                    description="Сертификат скоро закончится. Если не обновить "
                                "вовремя — сайт перестанет открываться.",
                    remediation="Включите автоматическое обновление сертификата "
                                "(certbot / панель хостинга).",
                    evidence=f"Действует до {not_after}",
                ))
        except Exception:
            pass
    return out


# Заголовки безопасности: имя → (severity, объяснение, как починить)
SECURITY_HEADERS = {
    "strict-transport-security": (
        "medium",
        "Нет HSTS — браузер не запоминает, что сайт должен открываться только "
        "по HTTPS. Возможна атака с понижением до HTTP.",
        "Добавьте заголовок: Strict-Transport-Security: max-age=31536000; "
        "includeSubDomains",
    ),
    "content-security-policy": (
        "medium",
        "Нет Content-Security-Policy (CSP) — главный механизм защиты от XSS "
        "(внедрения чужих скриптов). Без него вредоносный скрипт легче выполнить "
        "в браузере жертвы.",
        "Настройте политику CSP, начав с отчётного режима "
        "Content-Security-Policy-Report-Only.",
    ),
    "x-frame-options": (
        "medium",
        "Нет X-Frame-Options — сайт можно встроить в <iframe> на чужой странице "
        "и обмануть пользователя (clickjacking).",
        "Добавьте: X-Frame-Options: SAMEORIGIN (или директиву frame-ancestors в "
        "CSP).",
    ),
    "x-content-type-options": (
        "low",
        "Нет X-Content-Type-Options — браузер может «угадывать» тип файла и "
        "выполнить его не так, как задумано (MIME-sniffing).",
        "Добавьте: X-Content-Type-Options: nosniff",
    ),
    "referrer-policy": (
        "low",
        "Нет Referrer-Policy — при переходах с сайта во внешние ссылки может "
        "утекать полный адрес страницы (иногда с токенами).",
        "Добавьте: Referrer-Policy: strict-origin-when-cross-origin",
    ),
    "permissions-policy": (
        "low",
        "Нет Permissions-Policy — не ограничен доступ страниц к камере, "
        "микрофону, геолокации и т.п.",
        "Добавьте Permissions-Policy, отключив неиспользуемые возможности, "
        "напр.: Permissions-Policy: camera=(), microphone=(), geolocation=()",
    ),
}


def check_security_headers(ctx: ScanContext) -> list[Finding]:
    """Проверяем наличие рекомендованных заголовков безопасности."""
    out = []
    if ctx.response is None:
        return out
    headers = {k.lower(): v for k, v in ctx.response.headers.items()}
    for name, (sev, desc, fix) in SECURITY_HEADERS.items():
        if name not in headers:
            out.append(Finding(
                title=f"Отсутствует заголовок {name}",
                severity=sev,
                description=desc,
                remediation=fix,
            ))
    return out


def check_info_disclosure(ctx: ScanContext) -> list[Finding]:
    """Сайт раскрывает версии ПО? Это помогает атакующему искать эксплойты."""
    out = []
    if ctx.response is None:
        return out
    headers = {k.lower(): v for k, v in ctx.response.headers.items()}

    # Версия в Server / X-Powered-By / X-AspNet-Version и т.п.
    leak_headers = ["server", "x-powered-by", "x-aspnet-version",
                    "x-aspnetmvc-version", "x-generator"]
    for h in leak_headers:
        val = headers.get(h, "")
        # Считаем утечкой, только если есть цифры версии (Apache/2.4.41, PHP/7.4)
        if val and re.search(r"\d+\.\d+", val):
            out.append(Finding(
                title=f"Раскрыта версия ПО в заголовке {h}",
                severity="low",
                description="Сервер сообщает точную версию ПО. Зная версию, "
                            "атакующий быстро найдёт известные уязвимости именно "
                            "под неё.",
                remediation=f"Скройте версию: уберите/обезличьте заголовок {h} "
                            "в настройках веб-сервера (ServerTokens Prod в Apache, "
                            "server_tokens off в nginx).",
                evidence=f"{h}: {val}",
            ))
    return out


def check_cookies(ctx: ScanContext) -> list[Finding]:
    """Проверяем флаги безопасности у cookies (Secure, HttpOnly, SameSite)."""
    out = []
    if ctx.response is None:
        return out
    # requests схлопывает несколько Set-Cookie, поэтому смотрим «сырой» заголовок
    raw = ctx.response.raw.headers.getlist("Set-Cookie") \
        if hasattr(ctx.response.raw.headers, "getlist") else []
    if not raw:
        sc = ctx.response.headers.get("Set-Cookie")
        raw = [sc] if sc else []

    for cookie in raw:
        low = cookie.lower()
        name = cookie.split("=", 1)[0].strip()
        missing = []
        if "secure" not in low:
            missing.append("Secure")
        if "httponly" not in low:
            missing.append("HttpOnly")
        if "samesite" not in low:
            missing.append("SameSite")
        if missing:
            out.append(Finding(
                title=f"Cookie «{name}» без флагов {', '.join(missing)}",
                severity="medium" if "HttpOnly" in missing else "low",
                description="У cookie не выставлены защитные флаги. Без HttpOnly "
                            "cookie с сессией можно украсть через XSS; без Secure "
                            "она уйдёт по открытому HTTP; без SameSite возможна "
                            "CSRF-атака.",
                remediation="Выставьте флаги при установке cookie: Secure; "
                            "HttpOnly; SameSite=Lax (или Strict).",
                evidence=cookie[:120],
            ))
    return out


def check_cors(ctx: ScanContext) -> list[Finding]:
    """Слишком открытая политика CORS (Access-Control-Allow-Origin: *)?"""
    out = []
    if ctx.response is None:
        return out
    acao = ctx.response.headers.get("Access-Control-Allow-Origin", "")
    acac = ctx.response.headers.get("Access-Control-Allow-Credentials", "")
    if acao == "*" and acac.lower() == "true":
        out.append(Finding(
            title="Опасная настройка CORS",
            severity="high",
            description="Сайт разрешает читать ответы любому стороннему домену "
                        "(Allow-Origin: *) вместе с передачей учётных данных. "
                        "Чужой сайт может действовать от имени пользователя.",
            remediation="Уберите '*' и перечислите только доверенные домены, либо "
                        "отключите Allow-Credentials.",
            evidence="Access-Control-Allow-Origin: * + Allow-Credentials: true",
        ))
    elif acao == "*":
        out.append(Finding(
            title="CORS разрешён для всех доменов",
            severity="low",
            description="Заголовок Access-Control-Allow-Origin: * разрешает "
                        "читать ответы любому сайту. Для публичных данных это "
                        "ок, но для приватных API — риск утечки.",
            remediation="Если за этим адресом есть приватные данные — ограничьте "
                        "список доменов в CORS.",
            evidence="Access-Control-Allow-Origin: *",
        ))
    return out


# Чувствительные файлы/пути, которые НЕ должны быть доступны публично.
# signature — строка, которая должна встретиться в ответе, чтобы это был
# действительно тот файл, а не страница-заглушка 404 с кодом 200.
SENSITIVE_PATHS = [
    ("/.git/HEAD",       "ref:",          "high",     "Открыт каталог .git"),
    ("/.git/config",     "[core]",        "high",     "Открыт .git/config"),
    ("/.env",            "=",             "critical", "Открыт файл .env с настройками"),
    ("/.env.local",      "=",             "critical", "Открыт файл .env.local"),
    ("/wp-config.php.bak","DB_PASSWORD",  "critical", "Бэкап wp-config с паролями БД"),
    ("/config.php.bak",  "<?php",         "critical", "Бэкап config.php"),
    ("/.htaccess",       "",              "medium",   "Доступен .htaccess"),
    ("/.svn/entries",    "",              "high",     "Открыт каталог .svn"),
    ("/.DS_Store",       "",              "low",      "Доступен .DS_Store (macOS)"),
    ("/backup.zip",      "",              "high",     "Доступен архив backup.zip"),
    ("/backup.sql",      "",              "critical", "Доступен дамп базы backup.sql"),
    ("/dump.sql",        "",              "critical", "Доступен дамп базы dump.sql"),
    ("/phpinfo.php",     "phpinfo()",     "high",     "Доступна страница phpinfo()"),
    ("/server-status",   "Apache Server", "medium",   "Открыт Apache server-status"),
    ("/.well-known/security.txt", "",     "info",     "Есть security.txt (это хорошо)"),
]


def check_sensitive_files(ctx: ScanContext) -> list[Finding]:
    """
    Аккуратно проверяем доступность чувствительных файлов.
    Сначала получаем «эталон 404»: запрашиваем заведомо несуществующий путь.
    Если сайт на него отвечает 200 (так делают SPA), мы будем сравнивать с этим
    эталоном, чтобы не выдавать ложные срабатывания.
    """
    out = []
    base = f"{ctx.scheme}://{ctx.host}"

    # Эталон несуществующей страницы
    baseline_text = ""
    baseline_200 = False
    try:
        rb = ctx.session.get(urljoin(base, "/this_path_should_not_exist_4242x"),
                            timeout=TIMEOUT, verify=False, allow_redirects=False)
        baseline_200 = (rb.status_code == 200)
        baseline_text = rb.text[:2000]
    except Exception:
        pass

    for path, signature, sev, title in SENSITIVE_PATHS:
        try:
            r = ctx.session.get(urljoin(base, path), timeout=TIMEOUT,
                               verify=False, allow_redirects=False)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        body = r.text[:2000]
        # Если сайт отвечает 200 на всё (SPA) и контент совпал с эталоном —
        # это не настоящий файл, пропускаем.
        if baseline_200 and body.strip() == baseline_text.strip():
            continue
        # Если задана сигнатура — она должна встретиться в теле
        if signature and signature.lower() not in body.lower():
            continue
        sev_final = "info" if "security.txt" in path else sev
        out.append(Finding(
            title=title,
            severity=sev_final,
            description="По публичному адресу доступен файл/каталог, который "
                        "обычно содержит чувствительные данные (исходники, "
                        "пароли, настройки). Это одна из самых частых причин "
                        "взлома." if sev_final != "info" else
                        "Найден файл security.txt — это хорошая практика: он "
                        "сообщает исследователям, куда писать о проблемах.",
            remediation="Закройте доступ к этому пути на веб-сервере и удалите "
                        "файл из публичной директории, если он не нужен."
                        if sev_final != "info" else "Ничего делать не нужно.",
            evidence=f"{base}{path} → 200 OK",
        ))
    return out


def check_directory_listing(ctx: ScanContext) -> list[Finding]:
    """Включён ли листинг директорий (видно содержимое папок)?"""
    out = []
    base = f"{ctx.scheme}://{ctx.host}"
    dirs = ["/images/", "/uploads/", "/files/", "/backup/", "/static/",
            "/assets/", "/css/", "/js/"]
    for d in dirs:
        try:
            r = ctx.session.get(urljoin(base, d), timeout=TIMEOUT, verify=False)
        except Exception:
            continue
        if r.status_code == 200 and re.search(r"<title>\s*Index of /|Directory listing for",
                                              r.text, re.I):
            out.append(Finding(
                title=f"Открыт листинг директории {d}",
                severity="medium",
                description="Веб-сервер показывает список файлов в папке. "
                            "Атакующий видит структуру сайта и может найти "
                            "забытые/служебные файлы.",
                remediation="Отключите автолистинг: 'Options -Indexes' в Apache "
                            "или 'autoindex off;' в nginx.",
                evidence=f"{base}{d}",
            ))
            break  # одного примера достаточно
    return out


def check_email_security(ctx: ScanContext) -> list[Finding]:
    """
    Проверяем DNS-записи защиты почты: SPF и DMARC.
    Без них от имени домена легко рассылать фишинг/спам.
    """
    out = []
    # Берём «корневой» домен (example.com из www.example.com) — упрощённо
    parts = ctx.host.split(":")[0].split(".")
    domain = ".".join(parts[-2:]) if len(parts) >= 2 else ctx.host

    txt = _doh_txt(domain)
    has_spf = any(t.lower().startswith("v=spf1") for t in txt)
    if not has_spf:
        out.append(Finding(
            title="Нет SPF-записи для домена",
            severity="low",
            description="В DNS нет записи SPF. Без неё злоумышленники могут "
                        "отправлять письма, подделывая ваш домен в поле «От кого» "
                        "(фишинг от вашего имени).",
            remediation="Добавьте TXT-запись SPF, напр.: "
                        "v=spf1 include:_spf.вашпочтовыйпровайдер.com ~all",
            evidence=f"Проверен домен {domain}",
        ))

    dmarc = _doh_txt(f"_dmarc.{domain}")
    has_dmarc = any(t.lower().startswith("v=dmarc1") for t in dmarc)
    if not has_dmarc:
        out.append(Finding(
            title="Нет DMARC-записи для домена",
            severity="low",
            description="Нет политики DMARC. Она говорит почтовым серверам, что "
                        "делать с поддельными письмами от вашего домена, и даёт "
                        "отчёты о попытках подделки.",
            remediation="Добавьте TXT-запись _dmarc с политикой, напр.: "
                        "v=DMARC1; p=quarantine; rua=mailto:you@домен",
            evidence=f"Проверен _dmarc.{domain}",
        ))
    return out


def check_robots(ctx: ScanContext) -> list[Finding]:
    """Информационно: смотрим robots.txt — иногда там «прячут» админки."""
    out = []
    base = f"{ctx.scheme}://{ctx.host}"
    try:
        r = ctx.session.get(urljoin(base, "/robots.txt"), timeout=TIMEOUT,
                           verify=False)
    except Exception:
        return out
    if r.status_code == 200 and "disallow" in r.text.lower():
        # Ищем «вкусные» для атакующего пути
        interesting = re.findall(r"Disallow:\s*(\S*(?:admin|login|backup|"
                                 r"private|config|secret|api)\S*)",
                                 r.text, re.I)
        if interesting:
            out.append(Finding(
                title="robots.txt раскрывает служебные пути",
                severity="info",
                description="В robots.txt перечислены «секретные» пути (админки, "
                            "бэкапы). robots.txt не защищает их — наоборот, "
                            "подсказывает атакующему, куда смотреть.",
                remediation="Не полагайтесь на robots.txt для защиты. Закройте "
                            "такие пути авторизацией, а из robots.txt уберите.",
                evidence=", ".join(interesting[:5]),
            ))
    return out


# Список всех проверок. Порядок = порядок выполнения.
CHECKS = [
    check_https,
    check_tls_certificate,
    check_security_headers,
    check_info_disclosure,
    check_cookies,
    check_cors,
    check_sensitive_files,
    check_directory_listing,
    check_email_security,
    check_robots,
]


# ─── ГЛАВНАЯ ФУНКЦИЯ ───────────────────────────────────────────────────────────

def scan(raw_url: str, progress=None) -> dict:
    """
    Просканировать сайт и вернуть отчёт (dict).

    progress — необязательная функция progress(текст), которую мы зовём по ходу,
    чтобы бот мог показывать «Проверяю заголовки...» и т.п.

    Возвращает dict:
      {
        "ok": bool, "error": str|None,
        "url": str, "host": str,
        "findings": [Finding, ...],   # отсортированы по критичности
        "counts": {"critical": N, ...},
        "score": int (0-100), "grade": "A".."F",
        "started": iso, "finished": iso, "duration": сек,
      }
    """
    started = datetime.now(timezone.utc)

    url = normalize_url(raw_url)
    if not url:
        return {"ok": False, "error": "Некорректный или запрещённый адрес. "
                "Укажите публичный сайт, напр. example.com",
                "findings": [], "url": raw_url}

    parsed = urlparse(url)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml,*/*"})
    session.max_redirects = 5

    ctx = ScanContext(
        url=url,
        host=parsed.netloc,
        scheme=parsed.scheme,
        session=session,
    )

    # Качаем главную страницу один раз (её используют многие проверки)
    if progress:
        progress("🌐 Подключаюсь к сайту…")
    try:
        ctx.response = session.get(url, timeout=TIMEOUT, allow_redirects=True,
                                   verify=False)
    except Exception as e:
        return {"ok": False,
                "error": f"Не удалось подключиться к сайту: {e}",
                "findings": [], "url": url, "host": ctx.host}

    # Запускаем все проверки по очереди
    for check in CHECKS:
        name = check.__name__.replace("check_", "").replace("_", " ")
        if progress:
            progress(f"🔎 Проверяю: {name}…")
        try:
            ctx.findings.extend(check(ctx))
        except Exception as e:
            ctx.errors.append(f"{check.__name__}: {e}")

    # Сортируем находки по критичности (сначала самые опасные)
    ctx.findings.sort(key=lambda f: f.rank, reverse=True)

    # Считаем итоги
    counts = {k: 0 for k in SEVERITY}
    deduction = 0
    for f in ctx.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
        deduction += SEVERITY.get(f.severity, SEVERITY["info"])["score"]

    score = max(0, 100 - deduction)
    grade = _grade(score, counts)

    finished = datetime.now(timezone.utc)
    return {
        "ok": True,
        "error": None,
        "url": url,
        "host": ctx.host,
        "findings": ctx.findings,
        "counts": counts,
        "score": score,
        "grade": grade,
        "errors": ctx.errors,
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "duration": round((finished - started).total_seconds(), 1),
    }


def _grade(score: int, counts: dict) -> str:
    """Буквенная оценка. Критические находки автоматически роняют оценку."""
    if counts.get("critical", 0) > 0:
        return "F"
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "F"
