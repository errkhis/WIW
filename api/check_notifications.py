import json
import logging
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests as http

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import (
    DatabaseNotConfigured,
    claim_due_bid_watches,
    mark_bid_watch_error,
    mark_bid_watch_notified,
)
from scraper import scrape_consultation


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
MOROCCO_TZ = ZoneInfo("Africa/Casablanca")


def _json_response(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(body)


def _cron_secret() -> str:
    return os.environ.get("CRON_SECRET", "").strip()


def _is_authorized(path: str) -> bool:
    secret = _cron_secret()
    if not secret:
        return False
    query = parse_qs(urlparse(path).query)
    provided = (query.get("secret") or [""])[0]
    return provided == secret


def _inside_check_window(now: datetime) -> bool:
    local_now = now.astimezone(MOROCCO_TZ)
    return 8 <= local_now.hour < 19


def _has_published_results(data) -> bool:
    lots = data.lots or [data]
    return any(lot.bidders for lot in lots)


def _notification_keyboard(reference: str, org: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "🏆 Obtenir le gagnant", "callback_data": f"winner:{reference}:{org}"},
            {"text": "🏙️ Villes des sociétés", "callback_data": f"cities:{reference}:{org}"},
        ]]
    }


def _send_notification(watch) -> None:
    text = (
        "🔔 <b>Les résultats commencent à être publiés</b>\n\n"
        f"Consultation: <b>{_esc(watch.consultation_reference)}</b>"
    )
    payload = {
        "chat_id": watch.telegram_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": _notification_keyboard(
            watch.consultation_reference,
            watch.org_acronyme,
        ),
    }
    response = http.post(f"{TG}/sendMessage", json=payload, timeout=10)
    response.raise_for_status()


def _esc(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_notification_check() -> dict:
    now = datetime.now(MOROCCO_TZ)
    if not _inside_check_window(now):
        return {
            "ok": True,
            "checked": 0,
            "notified": 0,
            "errors": 0,
            "skipped": "outside_check_window",
            "local_time": now.isoformat(),
        }

    limit = int(os.environ.get("NOTIFICATION_CHECK_BATCH_SIZE", "10"))
    watches = claim_due_bid_watches(limit)
    notified = 0
    errors = 0

    for watch in watches:
        try:
            data = scrape_consultation(watch.consultation_url)
            if not _has_published_results(data):
                continue
            _send_notification(watch)
            mark_bid_watch_notified(watch.id)
            notified += 1
        except Exception as exc:
            errors += 1
            mark_bid_watch_error(watch.id, str(exc))
            log.exception("Notification check failed for watch %s", watch.id)

    return {
        "ok": True,
        "checked": len(watches),
        "notified": notified,
        "errors": errors,
        "local_time": now.isoformat(),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _is_authorized(self.path):
            _json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return

        try:
            _json_response(self, 200, run_notification_check())
        except DatabaseNotConfigured:
            _json_response(self, 500, {"ok": False, "error": "database_not_configured"})
        except Exception as exc:
            log.exception("Notification endpoint error")
            _json_response(self, 500, {"ok": False, "error": str(exc)[:400]})

    def log_message(self, fmt, *args):
        pass
