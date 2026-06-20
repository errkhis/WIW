import json
import logging
import os
import sys
from io import BytesIO
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests as http

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from daily_procurements import (
    build_daily_summary_html_document,
    build_daily_summary_message,
    fetch_daily_procurements,
)
from database import (
    DatabaseNotConfigured,
    list_daily_summary_recipients,
)


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
CASABLANCA_TZ = ZoneInfo("Africa/Casablanca")


def _json_response(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(body)


def _cron_secret() -> str:
    return os.environ.get("CRON_SECRET", "").strip()


def _query(path: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(path).query)


def _is_authorized(path: str) -> bool:
    secret = _cron_secret()
    if not secret:
        return False
    provided = (_query(path).get("secret") or [""])[0]
    return provided == secret


def _summary_date(path: str) -> date:
    requested = (_query(path).get("date") or [""])[0].strip()
    if requested:
        return datetime.strptime(requested, "%Y-%m-%d").date()
    return datetime.now(CASABLANCA_TZ).date() - timedelta(days=1)


def _request_base_url(handler) -> str:
    proto = (handler.headers.get("x-forwarded-proto") or "https").strip()
    host = (handler.headers.get("x-forwarded-host") or handler.headers.get("host") or "").strip()
    if not host:
        return os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    return f"{proto}://{host}"


def _send_message(chat_id: int, text: str) -> None:
    response = http.post(
        f"{TG}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    response.raise_for_status()


def _send_document(chat_id: int, filename: str, content: str) -> None:
    payload = {
        "chat_id": str(chat_id),
        "caption": "Fichier HTML du résumé quotidien.",
        "parse_mode": "HTML",
        "disable_content_type_detection": "true",
    }
    files = {
        "document": (
            filename,
            BytesIO(content.encode("utf-8")),
            "text/html; charset=utf-8",
        )
    }
    response = http.post(
        f"{TG}/sendDocument",
        data=payload,
        files=files,
        timeout=30,
    )
    response.raise_for_status()


def run_daily_summary(summary_date: date, browser_api_base_url: Optional[str] = None) -> dict:
    recipients = list_daily_summary_recipients()
    recipient_count = len(recipients)
    if not recipients:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_enabled_premium_recipients",
            "summary_date": summary_date.isoformat(),
            "recipients": 0,
            "sent": 0,
            "errors": 0,
        }

    sent_count = 0
    error_count = 0
    last_error = None
    items = fetch_daily_procurements(summary_date, browser_api_base_url=browser_api_base_url)
    summary_message = build_daily_summary_message(items, summary_date)
    html_document = build_daily_summary_html_document(items, summary_date)
    filename = f"resume-aos-{summary_date.isoformat()}.html"

    for telegram_id in recipients:
        try:
            _send_message(telegram_id, summary_message)
            _send_document(telegram_id, filename, html_document)
            sent_count += 1
        except Exception as exc:
            error_count += 1
            last_error = str(exc)
            log.exception("Daily summary send failed for %s", telegram_id)

    return {
        "ok": error_count == 0,
        "summary_date": summary_date.isoformat(),
        "items": len(items),
        "recipients": recipient_count,
        "sent": sent_count,
        "errors": error_count,
        "last_error": last_error,
    }
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _is_authorized(self.path):
            _json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return

        try:
            summary_date = _summary_date(self.path)
            _json_response(
                self,
                200,
                run_daily_summary(
                    summary_date,
                    browser_api_base_url=_request_base_url(self),
                ),
            )
        except ValueError as exc:
            _json_response(self, 400, {"ok": False, "error": str(exc)})
        except DatabaseNotConfigured:
            _json_response(self, 500, {"ok": False, "error": "database_not_configured"})
        except Exception as exc:
            log.exception("Daily summary endpoint error")
            _json_response(self, 500, {"ok": False, "error": str(exc)[:400]})

    def log_message(self, fmt, *args):
        pass
