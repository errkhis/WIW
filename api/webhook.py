import sys
import os
import re
import json
import logging
import requests as http

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper import scrape_consultation
from calculator import calculate_winners, EXCESSIVE_THRESHOLD, LOW_THRESHOLD

from http.server import BaseHTTPRequestHandler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg(method, payload):
    try:
        http.post(f"{TG}/{method}", json=payload, timeout=10)
    except Exception:
        pass


def send(chat_id, text):
    tg("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def typing(chat_id):
    tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def esc(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(n):
    return "—" if n is None else f"{n:,.2f}"


# ── Bot content ───────────────────────────────────────────────────────────────

WELCOME = (
    "🇲🇦 <b>Moroccan Procurement Winner Bot</b>\n\n"
    "Send me any <b>marchespublics.gov.ma</b> consultation URL and I'll "
    "calculate the winner using the official reference price method (Art. 13 RC).\n\n"
    "<b>Formula:</b>\n"
    "P = (E + average of valid offers) ÷ 2\n"
    "Winner = offer closest to P from below ▼\n\n"
    "<b>Filters applied automatically:</b>\n"
    "• Excessive offers (&gt;+20% of E) → eliminated\n"
    "• Abnormally low offers (&lt;-25% of E) → eliminated\n\n"
    "<b>Example — just paste and send:</b>\n"
    "<code>https://www.marchespublics.gov.ma/?page=entreprise.SuiviConsultation"
    "&amp;refConsultation=997895&amp;orgAcronyme=p1v</code>"
)

MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def extract_url(message):
    text = message.get("text") or message.get("caption") or ""
    for entity in message.get("entities") or []:
        if entity.get("type") == "text_link":
            url = entity.get("url", "")
            if "marchespublics.gov.ma" in url:
                return url
    for match in re.findall(r"https?://[^\s]+", text):
        if "marchespublics.gov.ma" in match:
            return match.rstrip(".,)")
    return None


def build_result(url):
    data = scrape_consultation(url)
    if not data.bidders:
        return "❌ No bidder data found. Make sure the URL points to a <b>completed</b> SuiviConsultation results page."

    rankings, _, ref_price = calculate_winners(data)
    eligible = [r for r in rankings if r.is_eligible]
    eliminated = [r for r in rankings if not r.is_eligible]
    top10 = eligible[:10]
    winner = top10[0] if top10 else None
    E = data.estimated_price

    lines = []
    lines.append(f"📋 <b>Consultation {esc(data.reference)}</b>")
    lines.append(f"🔹 {esc(data.object)}")
    lines.append("")

    if winner:
        lines.append(f"🏆 <b>WINNER: {esc(winner.name)}</b>")
        lines.append(f"💰 Offer: <b>{fmt(winner.price)} MAD</b>")
        if ref_price:
            lines.append(f"📏 Distance to P: {fmt(ref_price - winner.price)} MAD below")
        lines.append("")
    else:
        lines.append("❌ <b>No eligible winner found</b>")
        lines.append("")

    lines.append("📊 <b>Price Analysis</b>")
    if E:
        lines.append(f"• Estimated (E): {fmt(E)} {esc(data.estimated_price_currency)}")
    if ref_price:
        lines.append(f"• Reference price (P): <b>{fmt(ref_price)} MAD</b>")
    if E:
        lines.append(f"• Excessive limit (+20%): {fmt(E * EXCESSIVE_THRESHOLD)} MAD")
        lines.append(f"• Low limit (−25%): {fmt(E * LOW_THRESHOLD)} MAD")
    lines.append(f"• Eligible / Total: {len(eligible)} / {len(data.bidders)}")
    if eliminated:
        lines.append(f"• Eliminated: {len(eliminated)}")
    lines.append("")

    lines.append(f"🏅 <b>Top {len(top10)} Rankings</b>")
    for i, r in enumerate(top10):
        medal = MEDALS[i] if i < len(MEDALS) else f"{i+1}."
        arrow = "▼" if r.side == "below" else "▲"
        lines.append(
            f"{medal} <b>{esc(r.name)}</b>\n"
            f"   {fmt(r.price)} MAD  ·  Δ {fmt(r.distance_to_ref)}  ·  {arrow} {'below' if r.side == 'below' else 'above'} P"
        )

    lines.append("")
    lines.append("<i>Art. 13 RC · Decree n°2-22-431</i>")
    return "\n".join(lines)


def process_update(update):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        send(chat_id, WELCOME)
        return

    url = extract_url(message)
    if not url:
        if not text.startswith("/"):
            send(chat_id, "⚠️ Please send a <b>marchespublics.gov.ma</b> URL.\n\nUse /help for an example.")
        return

    typing(chat_id)
    send(chat_id, "⏳ Fetching and analyzing consultation data…")

    try:
        send(chat_id, build_result(url))
    except Exception as exc:
        log.exception("Processing error")
        send(chat_id, f"❌ <b>Error:</b> {esc(str(exc)[:400])}")


# ── Vercel native handler ─────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self._ok(b"Bot is live")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            update = json.loads(body)
            process_update(update)
        except Exception:
            log.exception("Webhook error")
        self._ok(b"OK")

    def _ok(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence access logs
