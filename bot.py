"""MangaDex -> Telegram notifier.

Checks the chapter feed of manga you follow on MangaDex and sends you a
Telegram message for every new chapter in the configured language (English).
"""
import json
import logging
import time
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "seen_chapters.json"

AUTH_URL = "https://auth.mangadex.org/realms/mangadex/protocol/openid-connect/token"
API = "https://api.mangadex.org"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("mdbot")

cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


class MangaDexAuth:
    def __init__(self):
        self.access = None
        self.refresh = None
        self.expires_at = 0

    def _store(self, data):
        self.access = data["access_token"]
        self.refresh = data.get("refresh_token")
        self.expires_at = time.time() + data.get("expires_in", 900) - 60

    def _password_login(self):
        r = requests.post(
            AUTH_URL,
            data={
                "grant_type": "password",
                "username": cfg["mangadex_username"],
                "password": cfg["mangadex_password"],
                "client_id": cfg["mangadex_client_id"],
                "client_secret": cfg["mangadex_client_secret"],
            },
            timeout=30,
        )
        r.raise_for_status()
        self._store(r.json())

    def token(self):
        if self.access and time.time() < self.expires_at:
            return self.access
        if self.refresh:
            try:
                r = requests.post(
                    AUTH_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh,
                        "client_id": cfg["mangadex_client_id"],
                        "client_secret": cfg["mangadex_client_secret"],
                    },
                    timeout=30,
                )
                r.raise_for_status()
                self._store(r.json())
                return self.access
            except requests.RequestException:
                log.warning("Refresh failed, logging in again")
        self._password_login()
        return self.access


auth = MangaDexAuth()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(ids):
    # keep the file from growing forever
    STATE_FILE.write_text(json.dumps(sorted(ids)[-5000:]), encoding="utf-8")


def fetch_feed():
    """Latest chapters of followed manga in the configured language."""
    params = {
        "translatedLanguage[]": cfg.get("language", "en"),
        "order[publishAt]": "desc",
        "limit": 100,
        "includes[]": "manga",
        "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
    }
    r = requests.get(
        f"{API}/user/follows/manga/feed",
        params=params,
        headers={"Authorization": f"Bearer {auth.token()}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def manga_title(chapter):
    for rel in chapter.get("relationships", []):
        if rel["type"] == "manga":
            titles = rel.get("attributes", {}).get("title", {})
            if titles:
                return titles.get("en") or next(iter(titles.values()))
    return "Unknown manga"


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage",
        json={
            "chat_id": cfg["telegram_user_id"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(ch):
    a = ch["attributes"]
    num = a.get("chapter") or "?"
    ch_title = a.get("title")
    line = f"📖 <b>{esc(manga_title(ch))}</b>\nChapter {esc(num)}"
    if ch_title:
        line += f": {esc(ch_title)}"
    url = a.get("externalUrl") or f"https://mangadex.org/chapter/{ch['id']}"
    return f'{line}\n<a href="{url}">Read</a>'


def check():
    chapters = fetch_feed()
    seen = load_state()
    current_ids = {c["id"] for c in chapters}

    if seen is None:
        # First run: remember what already exists, don't spam.
        save_state(current_ids)
        log.info("First run: stored %d existing chapters", len(current_ids))
        try:
            send_telegram("✅ MangaDex bot started. You'll get notified about new chapters.")
        except requests.RequestException as e:
            log.error("Telegram test message failed: %s", e)
        return

    seen = set(seen)
    new = [c for c in chapters if c["id"] not in seen]
    # oldest first so notifications arrive in order
    for ch in reversed(new):
        send_telegram(format_message(ch))
        seen.add(ch["id"])
        time.sleep(0.5)
    if new:
        log.info("Sent %d notifications", len(new))
        save_state(seen)


def main():
    interval = max(1, int(cfg.get("check_interval_minutes", 10))) * 60
    while True:
        try:
            check()
        except Exception as e:  # keep running on network hiccups
            log.error("Check failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
