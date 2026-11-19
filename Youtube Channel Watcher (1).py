import argparse
import gc
import json
import os
import random
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

TARGET_SHORTS_URLS = [
    "https://youtube.com/shorts/gXtuKKEGPRw?si=iGet6RpHN3DWwVFz",
    "https://youtube.com/shorts/I5KOgTqzU6Q?si=Ta8F6KLjRTm6klJw",
    "https://youtube.com/shorts/ix4KpJs-37Q?si=QNY_fmczF8MZy97g",
    "https://youtube.com/shorts/MNnr0l78Aww?si=zzl5RDLnMVqddrIQ",
]

DEVICE_PROFILES = [
    {
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S928B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 3.0,
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
    },
]

HEALTHCHECK_URL = "https://www.youtube.com/generate_204"
PROXIFLY_HTTP_URL = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt"
)

PROXY_ERROR_SIGNATURES = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_TIMED_OUT",
    "Timeout",
)

BOT_WALL_MARKERS = (
    "confirm you’re not a bot",
    "confirm you're not a bot",
    "подтвердите, что вы не робот",
    "unusual traffic from your computer network",
    "необычный трафик",
)


class BotBlockedError(RuntimeError):
    """Исключение при обнаружении капчи или требования входа для проверки на бота."""


def is_proxy_failure(error: Exception) -> bool:
    """Определяет, вызвана ли ошибка отказом прокси, сети или антибот-блокировкой IP."""
    if isinstance(error, BotBlockedError):
        return True
    msg = str(error)
    return any(sig in msg for sig in PROXY_ERROR_SIGNATURES)


def safe_evaluate(page, script: str, arg=None):
    """Выполняет JS на странице, игнорируя временные сбои перестройки DOM/контекста."""
    try:
        if arg is None:
            return page.evaluate(script)
        return page.evaluate(script, arg)
    except Exception:
        return None


class TelegramNotifier:
    """Отправка текстовых отчётов и скриншотов в Telegram без сторонних библиотек."""

    def __init__(self, token: str | None, chat_id: str | None, alert_cooldown: int = 300):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = bool(self.token and self.chat_id)
        self.alert_cooldown = alert_cooldown
        self._last_alert_ts = 0.0

    def send_message(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text[:4000],
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as e:
            print(f"[TG] Не удалось отправить сообщение: {e}", flush=True)

    def send_alert_screenshot(self, page, caption: str) -> None:
        """Отправляет скриншот страницы при ошибке с защитой от спама (не чаще 1 раза в 5 мин)."""
        if not self.enabled or page is None:
            return
        now = time.monotonic()
        if now - self._last_alert_ts < self.alert_cooldown:
            return
        self._last_alert_ts = now

        try:
            png_bytes = page.screenshot(type="png", full_page=False)
        except Exception as e:
            self.send_message(f"⚠️ {caption}\n(Скриншот недоступен: {e})")
            return

        boundary = f"----Boundary{uuid.uuid4().hex}"
        body = bytearray()

        def add_field(name: str, value: str) -> None:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(f"{value}\r\n".encode("utf-8"))

        add_field("chat_id", self.chat_id)
        add_field("caption", caption[:1000])

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            b'Content-Disposition: form-data; name="photo"; filename="alert.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
        )
        body.extend(png_bytes)
        body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

        url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
        req = urllib.request.Request(
            url,
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
        except Exception as e:
            print(f"[TG] Не удалось отправить скриншот: {e}", flush=True)


def normalize_cookie(raw: dict) -> dict | None:
    """Приводит словарь cookie из расширений браузера к строгому формату Playwright."""
    name = raw.get("name")
    value = raw.get("value")
    domain = raw.get("domain")
    if not name or value is None or not domain:
        return None

    same_site_raw = str(raw.get("sameSite") or "Lax").lower()
    if same_site_raw in ("none", "no_restriction"):
        same_site = "None"
    elif same_site_raw == "strict":
        same_site = "Strict"
    else:
        same_site = "Lax"

    secure = bool(raw.get("secure", False))
    if same_site == "None":
        secure = True

    cookie: dict = {
        "name": str(name),
        "value": str(value),
        "domain": str(domain),
        "path": str(raw.get("path") or "/"),
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": secure,
        "sameSite": same_site,
    }

    exp = raw.get("expires") or raw.get("expirationDate")
    if isinstance(exp, (int, float)) and exp > 0:
        cookie["expires"] = float(exp)

    return cookie


def parse_netscape_cookies(text: str) -> list[dict]:
    """Разбирает cookies в классическом текстовом формате Netscape (cookies.txt)."""
    cookies = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure_str, exp_str, name, value = parts[:7]
        try:
            exp = float(exp_str)
        except ValueError:
            exp = -1
        c = normalize_cookie(
            {
                "domain": domain,
                "path": path,
                "secure": secure_str.upper() == "TRUE",
                "expires": exp if exp > 0 else None,
                "name": name,
                "value": value,
            }
        )
        if c:
            cookies.append(c)
    return cookies


def load_cookies(cookies_file: str | None = None) -> list[dict]:
    """Загружает cookies из переменной окружения YOUTUBE_COOKIES или локального файла."""
    raw_text = os.getenv("YOUTUBE_COOKIES", "").strip()

    if not raw_text:
        candidate = cookies_file or ("cookies.json" if os.path.exists("cookies.json") else None)
        if candidate and os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                raw_text = f.read().strip()

    if not raw_text:
        return []

    if raw_text.startswith("[") or raw_text.startswith("{"):
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict) and "cookies" in parsed:
                parsed = parsed["cookies"]
            if isinstance(parsed, list):
                result = []
                for item in parsed:
                    if isinstance(item, dict):
                        c = normalize_cookie(item)
                        if c:
                            result.append(c)
                return result
        except Exception as e:
            print(f"[COOKIES] Ошибка разбора JSON cookies: {e}", flush=True)
            return []

    return parse_netscape_cookies(raw_text)


def parse_proxy(raw: str) -> dict | None:
    """Преобразует строку прокси любого популярного формата в словарь для Playwright."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None

    if "://" not in raw and "@" not in raw and raw.count(":") == 3:
        host, port, username, password = raw.split(":", 3)
        return {
            "server": f"http://{host}:{port}",
            "username": username,
            "password": password,
        }

    scheme = "http"
    rest = raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)

    username = None
    password = None
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        if ":" in creds:
            username, password = creds.split(":", 1)
        else:
            username = creds
    else:
        hostport = rest

    if not hostport:
        return None

    proxy_cfg = {"server": f"{scheme}://{hostport}"}
    if username is not None:
        proxy_cfg["username"] = username
    if password is not None:
        proxy_cfg["password"] = password
    return proxy_cfg


def load_proxies(proxy_file: str | None = None) -> tuple[list[dict], str]:
    """Загружает прокси из PROXY_LIST или локального файла. Возвращает (список, режим)."""
    raw_items: list[str] = []

    env_proxies = os.getenv("PROXY_LIST", "").strip()
    env_lower = env_proxies.lower()
    if env_lower in ("direct", "none"):
        return [], "direct"
    if env_lower == "auto":
        return [], "auto"
    if env_proxies:
        raw_items.extend(re.split(r"[\n,;]+", env_proxies))

    if proxy_file and os.path.exists(proxy_file):
        with open(proxy_file, "r", encoding="utf-8") as f:
            raw_items.extend(f.readlines())

    proxies = []
    for item in raw_items:
        parsed = parse_proxy(item)
        if parsed:
            proxies.append(parsed)
    return proxies, ("manual" if proxies else "empty")


def fetch_proxifly_candidates(source_url: str = PROXIFLY_HTTP_URL, sample_size: int = 120) -> list[dict]:
    """Скачивает свежий список прокси из Proxifly и возвращает случайную выборку кандидатов."""
    print(f"Загружаю свежий список прокси из {source_url}...", flush=True)
    req = urllib.request.Request(
        source_url,
        headers={"User-Agent": DEVICE_PROFILES[0]["user_agent"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[PROXY] Не удалось скачать список Proxifly: {e}", flush=True)
        return []

    candidates = []
    for line in text.splitlines():
        parsed = parse_proxy(line)
        if parsed:
            candidates.append(parsed)

    if not candidates:
        print("[PROXY] Список Proxifly пуст.", flush=True)
        return []

    random.shuffle(candidates)
    selected = candidates[:sample_size]
    print(f"Получено {len(candidates)} адресов, отобрано на проверку: {len(selected)}.", flush=True)
    return selected


def check_single_proxy(proxy_cfg: dict, timeout: float = 6.0) -> tuple[dict, bool, int, str]:
    """Проверяет доступность YouTube через один прокси."""
    parsed = urllib.parse.urlparse(proxy_cfg["server"])
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    port = parsed.port
    username = proxy_cfg.get("username")
    password = proxy_cfg.get("password")

    if not host or not port:
        return proxy_cfg, False, 0, "неверный формат адреса"

    if scheme.startswith("socks") and username is not None:
        return proxy_cfg, False, 0, "Chromium не поддерживает SOCKS с паролем"

    start = time.perf_counter()

    if scheme in ("http", "https"):
        if username is not None:
            user_enc = urllib.parse.quote(username, safe="")
            pass_enc = urllib.parse.quote(password or "", safe="")
            auth_proxy = f"{scheme}://{user_enc}:{pass_enc}@{host}:{port}"
        else:
            auth_proxy = f"{scheme}://{host}:{port}"

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": auth_proxy, "https": auth_proxy})
        )
        req = urllib.request.Request(
            HEALTHCHECK_URL,
            headers={"User-Agent": DEVICE_PROFILES[0]["user_agent"]},
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                latency_ms = int((time.perf_counter() - start) * 1000)
                if resp.status in (200, 204):
                    return proxy_cfg, True, latency_ms, f"HTTP {resp.status}"
                return proxy_cfg, False, latency_ms, f"HTTP {resp.status}"
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return proxy_cfg, False, latency_ms, type(e).__name__

    curl_bin = shutil.which("curl")
    if curl_bin:
        cmd = [
            curl_bin,
            "-s",
            "-o",
            os.devnull,
            "-w",
            "%{http_code}",
            "--max-time",
            str(max(1, int(timeout))),
            "-x",
            proxy_cfg["server"],
            HEALTHCHECK_URL,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
            latency_ms = int((time.perf_counter() - start) * 1000)
            code = res.stdout.strip()
            if code in ("200", "204"):
                return proxy_cfg, True, latency_ms, f"HTTP {code}"
            return proxy_cfg, False, latency_ms, f"HTTP {code or 'ошибка SOCKS'}"
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return proxy_cfg, False, latency_ms, type(e).__name__

    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency_ms = int((time.perf_counter() - start) * 1000)
            return proxy_cfg, True, latency_ms, "TCP OK"
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return proxy_cfg, False, latency_ms, type(e).__name__


def filter_working_proxies(
    proxies: list[dict],
    timeout: float = 6.0,
    max_workers: int = 25,
    max_results: int | None = None,
    quiet_dead: bool = False,
) -> list[dict]:
    """Параллельно проверяет список прокси и возвращает только рабочие, отсортированные по задержке."""
    if not proxies:
        return []
    print(f"Проверяю {len(proxies)} прокси на доступ к YouTube (таймаут {timeout:.0f} сек)...", flush=True)
    alive_with_latency: list[tuple[int, dict]] = []

    workers = min(max_workers, len(proxies))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(check_single_proxy, p, timeout) for p in proxies]
        for future in as_completed(futures):
            proxy_cfg, is_alive, latency_ms, status = future.result()
            label = proxy_cfg["server"]
            if is_alive:
                print(f"  [OK]   {label} ({latency_ms} мс, {status})", flush=True)
                alive_with_latency.append((latency_ms, proxy_cfg))
            elif not quiet_dead:
                print(f"  [DEAD] {label} ({status})", flush=True)

    alive_with_latency.sort(key=lambda item: item[0])
    alive = [item[1] for item in alive_with_latency]
    if max_results is not None and max_results > 0:
        alive = alive[:max_results]
    print(f"Итог проверки: рабочих {len(alive)} из {len(proxies)}.", flush=True)
    return alive


def obtain_auto_proxies(
    source_url: str,
    limit: int,
    timeout: float,
    sample_size: int = 120,
) -> list[dict]:
    """Скачивает кандидатов из Proxifly и отбирает живые прокси."""
    candidates = fetch_proxifly_candidates(source_url=source_url, sample_size=sample_size)
    if not candidates:
        return []
    return filter_working_proxies(
        candidates,
        timeout=timeout,
        max_workers=30,
        max_results=limit,
        quiet_dead=True,
    )


def apply_stealth_scripts(context) -> None:
    """Скрывает признаки автоматизации Playwright в браузере."""
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        window.chrome = window.chrome || { runtime: {} };
        """
    )


def dismiss_consent(page) -> None:
    """Закрывает окно принятия cookies или всплывающие баннеры на YouTube."""
    selectors = [
        "button[aria-label*='Accept']",
        "button[aria-label*='Принять']",
        "button:has-text('Accept all')",
        "button:has-text('Принять все')",
        "button:has-text('Отклонить все')",
        "button:has-text('Reject all')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1200):
                btn.click()
                time.sleep(random.uniform(0.6, 1.2))
                return
        except Exception:
            pass


def check_bot_wall(page) -> None:
    """Проверяет, не показал ли YouTube капчу или заглушку антибота."""
    current_url = (page.url or "").lower()
    if "google.com/sorry" in current_url:
        raise BotBlockedError("YouTube/Google перенаправил на страницу капчи (sorry)")

    try:
        body_text = (page.locator("body").inner_text(timeout=2000) or "").lower()
        for marker in BOT_WALL_MARKERS:
            if marker in body_text:
                raise BotBlockedError(f"Обнаружена антибот-заглушка YouTube: '{marker}'")
    except BotBlockedError:
        raise
    except Exception:
        pass


def wait_out_ads_if_present(page, max_ad_wait: float = 20.0, session_deadline: float | None = None) -> None:
    """Если перед роликом включилась реклама, пропускает её или дожидается окончания."""
    start = time.monotonic()
    while time.monotonic() - start < max_ad_wait:
        if session_deadline and time.monotonic() >= session_deadline:
            return

        ad_active = safe_evaluate(
            page,
            """() => {
                const player = document.querySelector('#movie_player, .html5-video-player');
                return Boolean(player && player.classList.contains('ad-showing'));
            }""",
        )
        if not ad_active:
            return

        for skip_sel in (".ytp-ad-skip-button", ".ytp-skip-ad-button", ".ytp-ad-skip-button-modern"):
            try:
                btn = page.locator(skip_sel).first
                if btn.is_visible(timeout=500):
                    btn.click()
                    time.sleep(0.5)
                    break
            except Exception:
                pass
        time.sleep(1.0)


def simulate_human_interaction(page, viewport: dict) -> None:
    """Имитирует лёгкое движение пальца по экрану без переключения ролика."""
    try:
        x = random.randint(int(viewport["width"] * 0.25), int(viewport["width"] * 0.75))
        y = random.randint(int(viewport["height"] * 0.35), int(viewport["height"] * 0.65))
        page.mouse.move(x, y, steps=random.randint(4, 10))
    except Exception:
        pass


def watch_short_to_completion(
    page,
    mute: bool,
    viewport: dict,
    fallback_min: int,
    fallback_max: int,
    session_deadline: float | None,
) -> float:
    """Считывает реальную длительность Shorts, проверяет живой поток кадров и досматривает до конца."""
    check_bot_wall(page)
    wait_out_ads_if_present(page, session_deadline=session_deadline)

    duration = None
    meta_deadline = time.monotonic() + 12.0

    # 1. Ждём готовности видеопотока (readyState >= 2 и реальная ширина кадра > 0)
    while time.monotonic() < meta_deadline:
        if session_deadline and time.monotonic() >= session_deadline:
            return 0.0

        info = safe_evaluate(
            page,
            """(muteAudio) => {
                const videos = Array.from(document.querySelectorAll('video'));
                const v = videos.find(el => el.readyState >= 2 && el.videoWidth > 0 && Number.isFinite(el.duration) && el.duration > 0) || videos[0];
                if (!v) return null;
                v.muted = muteAudio;
                v.play().catch(() => {});
                if (v.readyState >= 2 && v.videoWidth > 0 && Number.isFinite(v.duration) && v.duration > 0) {
                    return v.duration;
                }
                return null;
            }""",
            mute,
        )
        if info and info > 0:
            duration = float(info)
            break
        time.sleep(0.4)

    if duration is None:
        check_bot_wall(page)
        fallback_sec = random.uniform(fallback_min, fallback_max)
        if session_deadline:
            remaining = max(0.0, session_deadline - time.monotonic())
            fallback_sec = min(fallback_sec, remaining)
        print(f"Не удалось считать длительность кадра — запасной просмотр {fallback_sec:.1f} сек...", flush=True)
        time.sleep(fallback_sec)
        return fallback_sec

    # 2. Перематываем ролик строго на 0:00 и запускаем воспроизведение
    safe_evaluate(
        page,
        """(muteAudio) => {
            const videos = Array.from(document.querySelectorAll('video'));
            const v = videos.find(el => el.videoWidth > 0 && Number.isFinite(el.duration) && el.duration > 0) || videos[0];
            if (v) {
                v.muted = muteAudio;
                v.currentTime = 0;
                v.play().catch(() => {});
            }
        }""",
        mute,
    )
    time.sleep(0.3)

    print(f"Длительность Shorts: {duration:.1f} сек. Досматриваю от начала до конца...", flush=True)

    watch_start = time.monotonic()
    last_progress_ts = watch_start
    last_current_time = 0.0
    max_seen_time = 0.0
    real_playing_ticks = 0
    human_touched = False
    max_allowed_wait = max(duration * 2.5 + 15.0, 30.0)

    # 3. Контроль реального прогресса воспроизведения
    while time.monotonic() - watch_start < max_allowed_wait:
        if session_deadline and time.monotonic() >= session_deadline:
            break

        state = safe_evaluate(
            page,
            """(muteAudio) => {
                const videos = Array.from(document.querySelectorAll('video'));
                const v = videos.find(el => el.videoWidth > 0 && Number.isFinite(el.duration) && el.duration > 0) || videos[0];
                if (!v) return null;
                v.muted = muteAudio;
                if (v.paused && !v.ended) {
                    v.play().catch(() => {});
                }
                return {
                    currentTime: v.currentTime || 0,
                    duration: Number.isFinite(v.duration) ? v.duration : 0,
                    ended: Boolean(v.ended),
                    paused: Boolean(v.paused),
                    readyState: v.readyState || 0,
                    videoWidth: v.videoWidth || 0
                };
            }""",
            mute,
        )

        if state is None:
            if time.monotonic() - last_progress_ts > 12.0:
                raise TimeoutError("Плеер пропал со страницы или не отвечает более 12 сек")
            time.sleep(0.5)
            continue

        cur = float(state["currentTime"])
        dur = float(state["duration"]) or duration
        ended = bool(state["ended"])
        max_seen_time = max(max_seen_time, cur)

        # Имитация человеческого микродвижения в середине ролика
        if not human_touched and cur >= dur * 0.45 and random.random() < 0.45:
            simulate_human_interaction(page, viewport)
            human_touched = True

        # Проверка прогресса времени воспроизведения
        if abs(cur - last_current_time) >= 0.15 and state["videoWidth"] > 0:
            real_playing_ticks += 1
            last_current_time = cur
            last_progress_ts = time.monotonic()
        elif time.monotonic() - last_progress_ts > 12.0:
            raise TimeoutError("Видеопоток остановился (currentTime не продвигается более 12 сек)")

        reached_end = ended or (dur > 0 and cur >= dur - 0.35)
        looped_after_finish = max_seen_time > max(1.5, dur * 0.5) and cur < max_seen_time - 1.0

        if (reached_end or looped_after_finish) and real_playing_ticks >= 2:
            tail_pause = random.uniform(0.4, 2.2)
            time.sleep(tail_pause)
            elapsed = time.monotonic() - watch_start
            print(f"Ролик досмотрен полностью (реальное время в плеере: {elapsed:.1f} сек).", flush=True)
            return elapsed

        time.sleep(0.5)

    elapsed = time.monotonic() - watch_start
    print(f"Лимит ожидания плеера достигнут ({elapsed:.1f} сек) — перехожу дальше.", flush=True)
    return elapsed


def create_browser_session(p, headless: bool, mute: bool, proxy_cfg: dict | None, cookies: list[dict]):
    """Создаёт изолированный экземпляр браузера с маскировкой, лимитами кэша и cookies."""
    profile = random.choice(DEVICE_PROFILES)
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disk-cache-size=1",
        "--media-cache-size=1",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if mute:
        launch_args.append("--mute-audio")

    browser = p.chromium.launch(
        headless=headless,
        proxy=proxy_cfg,
        args=launch_args,
    )
    context = browser.new_context(
        viewport=profile["viewport"],
        user_agent=profile["user_agent"],
        device_scale_factor=profile["device_scale_factor"],
        is_mobile=True,
        has_touch=True,
        locale="ru-RU",
        timezone_id="Europe/Moscow",
    )
    apply_stealth_scripts(context)

    if cookies:
        try:
            context.add_cookies(cookies)
        except Exception as e:
            print(f"[COOKIES] Предупреждение при загрузке cookies: {e}", flush=True)

    page = context.new_page()
    return browser, context, page, profile["viewport"]


def close_browser_session(browser, context, page) -> None:
    """Корректно закрывает страницу, контекст и процесс Chromium для освобождения ОЗУ."""
    for obj in (page, context, browser):
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass
    gc.collect()


def format_duration(seconds: float) -> str:
    mins, secs = divmod(int(max(0, seconds)), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}ч {mins:02d}м {secs:02d}с"
    return f"{mins}м {secs:02d}с"


def run_watcher(
    headless: bool,
    mute: bool,
    shuffle: bool,
    min_watch: int,
    max_watch: int,
    proxy_file: str | None,
    cookies_file: str | None,
    skip_proxy_check: bool,
    proxy_timeout: float,
    fallback_direct: bool,
    max_proxy_errors: int,
    restart_every: int,
    max_session_minutes: int,
    report_interval_minutes: int,
    tg_token: str | None,
    tg_chat_id: str | None,
    auto_proxies: bool,
    proxy_source_url: str,
    auto_proxy_limit: int,
):
    notifier = TelegramNotifier(token=tg_token, chat_id=tg_chat_id)
    cookies = load_cookies(cookies_file)
    if cookies:
        print(f"Загружено cookies: {len(cookies)} шт.", flush=True)
    else:
        print("Cookies не заданы — запуск в гостевом режиме.", flush=True)

    proxies, proxy_mode = load_proxies(proxy_file)
    use_auto_proxies = (proxy_mode == "auto") or (proxy_mode == "empty" and auto_proxies)
    initial_proxy_count = len(proxies)

    if proxy_mode == "direct":
        print("Задан режим direct — запуск напрямую без прокси.", flush=True)
    elif use_auto_proxies:
        proxies = obtain_auto_proxies(
            source_url=proxy_source_url,
            limit=auto_proxy_limit,
            timeout=proxy_timeout,
        )
        initial_proxy_count = len(proxies)
        if not proxies:
            if fallback_direct:
                print("Живые прокси из Proxifly не найдены — перехожу на прямой IP.", flush=True)
            else:
                msg = "Не удалось получить рабочие прокси из Proxifly. Сессия остановлена."
                print(msg, flush=True)
                notifier.send_message(f"🛑 {msg}")
                return
    elif proxies:
        if not skip_proxy_check:
            proxies = filter_working_proxies(proxies, timeout=proxy_timeout)
            if not proxies:
                if fallback_direct:
                    print("Рабочих прокси не найдено — перехожу на прямой IP.", flush=True)
                else:
                    msg = "Ни один прокси из списка не прошёл проверку. Сессия остановлена."
                    print(msg, flush=True)
                    notifier.send_message(f"🛑 {msg}")
                    return

    if proxies:
        if shuffle:
            random.shuffle(proxies)
        print(f"В работу взято прокси: {len(proxies)} шт. Ротация IP включена.", flush=True)
    elif not use_auto_proxies and proxy_mode != "direct":
        print("Список PROXY_LIST пуст — запуск напрямую без прокси.", flush=True)

    session_start = time.monotonic()
    session_deadline = (
        session_start + max_session_minutes * 60 if max_session_minutes > 0 else None
    )
    last_report_ts = session_start

    stats = {
        "cycles": 0,
        "views_ok": 0,
        "errors": 0,
        "watched_seconds": 0.0,
        "banned_proxies": 0,
    }

    notifier.send_message(
        f"▶️ Бот запущен\n"
        f"• Роликов в списке: {len(TARGET_SHORTS_URLS)}\n"
        f"• Прокси: {len(proxies)} из {initial_proxy_count}"
        f"{' (авто Proxifly)' if use_auto_proxies else ''}\n"
        f"• Cookies: {'загружены (' + str(len(cookies)) + ')' if cookies else 'нет'}\n"
        f"• Лимит сессии: {max_session_minutes} мин"
    )

    with sync_playwright() as p:
        cycle = 1
        proxy_index = 0
        browser = None
        context = None
        page = None
        viewport = DEVICE_PROFILES[0]["viewport"]
        cycles_with_current_browser = 0
        current_proxy_cfg = None

        try:
            while True:
                if session_deadline and time.monotonic() >= session_deadline:
                    print(
                        f"\nДостигнут лимит времени сессии ({max_session_minutes} мин). Плановое завершение.",
                        flush=True,
                    )
                    break

                # Периодический отчёт в Telegram
                if (
                    report_interval_minutes > 0
                    and time.monotonic() - last_report_ts >= report_interval_minutes * 60
                ):
                    last_report_ts = time.monotonic()
                    uptime = format_duration(time.monotonic() - session_start)
                    notifier.send_message(
                        f"📊 Отчёт за {uptime}\n"
                        f"• Циклов: {stats['cycles']}\n"
                        f"• Успешных просмотров: {stats['views_ok']}\n"
                        f"• Время просмотра: {format_duration(stats['watched_seconds'])}\n"
                        f"• Ошибок: {stats['errors']}\n"
                        f"• Активных прокси: {len(proxies)} (забанено: {stats['banned_proxies']})"
                    )

                if proxies:
                    proxy_index %= len(proxies)
                    desired_proxy_cfg = proxies[proxy_index]
                    proxy_label = f"{desired_proxy_cfg['server']} [{proxy_index + 1}/{len(proxies)}]"
                else:
                    desired_proxy_cfg = None
                    proxy_label = "прямой IP"

                # Перезапускаем браузер при смене прокси ИЛИ каждые restart_every циклов от утечек памяти
                need_browser_restart = (
                    browser is None
                    or desired_proxy_cfg != current_proxy_cfg
                    or cycles_with_current_browser >= max(1, restart_every)
                )

                if need_browser_restart:
                    if browser is not None:
                        print("Перезапуск браузера для смены IP и очистки оперативной памяти...", flush=True)
                        close_browser_session(browser, context, page)
                        browser = context = page = None

                    try:
                        browser, context, page, viewport = create_browser_session(
                            p=p,
                            headless=headless,
                            mute=mute,
                            proxy_cfg=desired_proxy_cfg,
                            cookies=cookies,
                        )
                        current_proxy_cfg = desired_proxy_cfg
                        cycles_with_current_browser = 0
                    except Exception as e:
                        print(f"Сбой запуска браузера ({proxy_label}): {e}", flush=True)
                        stats["errors"] += 1
                        if desired_proxy_cfg and desired_proxy_cfg in proxies:
                            proxies.remove(desired_proxy_cfg)
                            stats["banned_proxies"] += 1
                            print(f"[BAN] Прокси {desired_proxy_cfg['server']} удалён.", flush=True)
                            if not proxies and use_auto_proxies:
                                print("Обновляю пул прокси из Proxifly...", flush=True)
                                proxies = obtain_auto_proxies(
                                    source_url=proxy_source_url,
                                    limit=auto_proxy_limit,
                                    timeout=proxy_timeout,
                                )
                            if not proxies and not fallback_direct:
                                notifier.send_message("🛑 Все прокси отсеяны при старте браузера. Остановка.")
                                break
                        time.sleep(2)
                        continue

                urls = TARGET_SHORTS_URLS.copy()
                if shuffle:
                    random.shuffle(urls)

                print(
                    f"\n=== Цикл #{cycle} | Прокси: {proxy_label} | Роликов: {len(urls)} ===",
                    flush=True,
                )

                proxy_failed = False
                consecutive_errors = 0

                for idx, url in enumerate(urls, start=1):
                    if session_deadline and time.monotonic() >= session_deadline:
                        break

                    # Человеческая пауза между роликами
                    if idx > 1:
                        pause = random.uniform(1.5, 4.0)
                        time.sleep(pause)

                    try:
                        print(f"[{idx}/{len(urls)}] Открываю: {url}", flush=True)
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        dismiss_consent(page)

                        try:
                            watched_sec = watch_short_to_completion(
                                page=page,
                                mute=mute,
                                viewport=viewport,
                                fallback_min=min_watch,
                                fallback_max=max_watch,
                                session_deadline=session_deadline,
                            )
                        except TimeoutError as stall_err:
                            # Одна автоматическая перезагрузка страницы при зависании буферизации
                            print(f"Зависание плеера ({stall_err}) — перезагружаю страницу...", flush=True)
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                            dismiss_consent(page)
                            watched_sec = watch_short_to_completion(
                                page=page,
                                mute=mute,
                                viewport=viewport,
                                fallback_min=min_watch,
                                fallback_max=max_watch,
                                session_deadline=session_deadline,
                            )

                        stats["views_ok"] += 1
                        stats["watched_seconds"] += watched_sec
                        consecutive_errors = 0

                    except Exception as e:
                        consecutive_errors += 1
                        stats["errors"] += 1
                        print(f"Ошибка на {url}: {e}", flush=True)

                        notifier.send_alert_screenshot(
                            page,
                            f"Ошибка на {proxy_label}\nURL: {url}\nПричина: {str(e)[:250]}",
                        )

                        if current_proxy_cfg and (
                            is_proxy_failure(e) or consecutive_errors >= max_proxy_errors
                        ):
                            proxy_failed = True
                            break

                        # При повторяющейся ошибке без прокси принудительно пересоздаём браузер
                        if consecutive_errors >= 2:
                            close_browser_session(browser, context, page)
                            browser = context = page = None
                            break

                        time.sleep(2)

                stats["cycles"] += 1
                cycles_with_current_browser += 1

                if proxy_failed and current_proxy_cfg is not None:
                    close_browser_session(browser, context, page)
                    browser = context = page = None
                    if current_proxy_cfg in proxies:
                        proxies.remove(current_proxy_cfg)
                        stats["banned_proxies"] += 1
                    print(
                        f"[BAN] Прокси {current_proxy_cfg['server']} удалён из пула. "
                        f"Осталось: {len(proxies)} шт.",
                        flush=True,
                    )
                    if not proxies and use_auto_proxies:
                        print("Пул исчерпан — добираю свежие прокси из Proxifly...", flush=True)
                        proxies = obtain_auto_proxies(
                            source_url=proxy_source_url,
                            limit=auto_proxy_limit,
                            timeout=proxy_timeout,
                        )
                        proxy_index = 0
                    if not proxies:
                        if fallback_direct:
                            print("Все прокси отсеяны — переключаюсь на прямой IP.", flush=True)
                        else:
                            msg = "Все прокси отсеяны во время работы. Сессия остановлена."
                            print(msg, flush=True)
                            notifier.send_message(f"🛑 {msg}")
                            break
                elif proxies:
                    proxy_index += 1

                cycle += 1
        finally:
            close_browser_session(browser, context, page)
            total_uptime = format_duration(time.monotonic() - session_start)
            summary = (
                f"🏁 Сессия завершена ({total_uptime})\n"
                f"• Циклов пройдено: {stats['cycles']}\n"
                f"• Успешных просмотров: {stats['views_ok']}\n"
                f"• Суммарно в плеере: {format_duration(stats['watched_seconds'])}\n"
                f"• Ошибок: {stats['errors']}\n"
                f"• Забанено прокси: {stats['banned_proxies']}"
            )
            print(f"\n{summary}", flush=True)
            notifier.send_message(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Режим без окна браузера")
    parser.add_argument("--mute", action="store_true", help="Отключить звук")
    parser.add_argument("--shuffle", action="store_true", help="Перемешивать ссылки и прокси")
    parser.add_argument("--min-watch", type=int, default=25, help="Запасное мин. время, если длительность не считалась")
    parser.add_argument("--max-watch", type=int, default=45, help="Запасное макс. время, если длительность не считалась")
    parser.add_argument("--proxy-file", type=str, default=None, help="Путь к локальному файлу прокси")
    parser.add_argument("--cookies-file", type=str, default=None, help="Путь к файлу cookies (JSON или Netscape)")
    parser.add_argument("--skip-proxy-check", action="store_true", help="Пропустить предварительную проверку прокси")
    parser.add_argument("--proxy-timeout", type=float, default=6.0, help="Таймаут проверки одного прокси (сек)")
    parser.add_argument("--fallback-direct", action="store_true", help="Работать напрямую, если все прокси мертвы")
    parser.add_argument("--max-proxy-errors", type=int, default=2, help="Лимит ошибок подряд до удаления прокси")
    parser.add_argument("--restart-every", type=int, default=8, help="Перезапуск браузера каждые N циклов от утечек ОЗУ")
    parser.add_argument(
        "--no-auto-proxies",
        action="store_true",
        help="Не скачивать прокси из Proxifly автоматически, если PROXY_LIST пуст",
    )
    parser.add_argument(
        "--proxy-source-url",
        type=str,
        default=os.getenv("PROXY_SOURCE_URL", PROXIFLY_HTTP_URL),
        help="URL списка прокси (по умолчанию HTTP-список Proxifly)",
    )
    parser.add_argument(
        "--auto-proxy-limit",
        type=int,
        default=int(os.getenv("AUTO_PROXY_LIMIT", "15")),
        help="Сколько самых быстрых рабочих прокси отбирать из Proxifly",
    )
    parser.add_argument(
        "--max-session-minutes",
        type=int,
        default=int(os.getenv("MAX_SESSION_MINUTES", "345")),
        help="Лимит длительности сессии в минутах (0 = без лимита)",
    )
    parser.add_argument(
        "--report-interval",
        type=int,
        default=int(os.getenv("REPORT_INTERVAL_MINUTES", "60")),
        help="Интервал отправки сводных отчётов в Telegram (мин)",
    )
    parser.add_argument("--tg-token", type=str, default=os.getenv("TG_BOT_TOKEN"), help="Токен Telegram-бота")
    parser.add_argument("--tg-chat-id", type=str, default=os.getenv("TG_CHAT_ID"), help="ID чата в Telegram")
    args = parser.parse_args()

    run_watcher(
        headless=args.headless,
        mute=args.mute,
        shuffle=args.shuffle,
        min_watch=args.min_watch,
        max_watch=args.max_watch,
        proxy_file=args.proxy_file,
        cookies_file=args.cookies_file,
        skip_proxy_check=args.skip_proxy_check,
        proxy_timeout=args.proxy_timeout,
        fallback_direct=args.fallback_direct,
        max_proxy_errors=args.max_proxy_errors,
        restart_every=args.restart_every,
        max_session_minutes=args.max_session_minutes,
        report_interval_minutes=args.report_interval,
        tg_token=args.tg_token,
        tg_chat_id=args.tg_chat_id,
        auto_proxies=not args.no_auto_proxies,
        proxy_source_url=args.proxy_source_url,
        auto_proxy_limit=args.auto_proxy_limit,
    )