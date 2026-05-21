import sys
import os
import re
import json
import logging
import requests
from flask import Flask, request, Response

# Import scraper/calculator from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper import scrape_consultation
from calculator import calculate_winners, EXCESSIVE_THRESHOLD, LOW_THRESHOLD

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg_post(method: str, payload: dict, timeout: int = 10):
    try:
        requests.post(f"{TG_API}/{method}", json=payload, timeout=timeout)
    except Exception:
        pass


def send(chat_id: int, text: str):
    tg_post("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def typing(chat_id: int):
    tg_post("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)


def esc(v) -> str:
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(n) -> str:
    if n is None:
        return "—"
    return f"{n:,.2f}"


# ── Bot messages ──────────────────────────────────────────────────────────────

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


# ── URL extraction ────────────────────────────────────────────────────────────

def extract_url(message: dict) -> str | None:
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []

    # text_link entities (hyperlinked text where URL differs from display text)
    for entity in entities:
        if entity.get("type") == "text_link":
            url = entity.get("url", "")
            if "marchespublics.gov.ma" in url:
                return url

    # Plain URL in text
    for match in re.findall(r"https?://[^\s]+", text):
        if "marchespublics.gov.ma" in match:
            return match.rstrip(".,)")

    return None


# ── Core processing ───────────────────────────────────────────────────────────

MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def build_result_message(url: str) -> str:
    data = scrape_consultation(url)

    if not data.bidders:
        return (
            "❌ No bidder data found.\n"
            "Make sure the URL points to a <b>completed</b> SuiviConsultation results page."
        )

    rankings, _, ref_price = calculate_winners(data)
    eligible = [r for r in rankings if r.is_eligible]
    eliminated = [r for r in rankings if not r.is_eligible]
    top10 = eligible[:10]
    winner = top10[0] if top10 else None

    E = data.estimated_price
    lines = []

    # ── Consultation header
    lines.append(f"📋 <b>Consultation {esc(data.reference)}</b>")
    lines.append(f"🔹 {esc(data.object)}")
    lines.append("")

    # ── Winner
    if winner:
        lines.append(f"🏆 <b>WINNER: {esc(winner.name)}</b>")
        lines.append(f"💰 Offer: <b>{fmt(winner.price)} MAD</b>")
        if ref_price:
            diff = ref_price - winner.price
            lines.append(f"📏 Distance to P: {fmt(diff)} MAD below")
        lines.append("")
    else:
        lines.append("❌ <b>No eligible winner found</b>")
        lines.append("")

    # ── Pricing summary
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
        reasons = {}
        for r in eliminated:
            key = r.note.replace("Eliminated — ", "").split(";")[0].strip()
            reasons[key] = reasons.get(key, 0) + 1
        for reason, count in reasons.items():
            lines.append(f"  ⛔ {esc(reason)}: {count}")
    lines.append("")

    # ── Top 10
    lines.append(f"🏅 <b>Top {len(top10)} Rankings</b>")
    for i, r in enumerate(top10):
        medal = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        arrow = "▼" if r.side == "below" else "▲"
        side_label = "below P" if r.side == "below" else "above P"
        lines.append(
            f"{medal} <b>{esc(r.name)}</b>\n"
            f"   {fmt(r.price)} MAD  ·  Δ {fmt(r.distance_to_ref)}  ·  {arrow} {side_label}"
        )

    # ── Footer
    lines.append("")
    lines.append(f"<i>Calculated per Art. 13 RC · Decree n°2-22-431</i>")

    return "\n".join(lines)


# ── Webhook handler ───────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
@app.route("/api/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return Response("✓ Bot is live", status=200)

    update = request.get_json(silent=True)
    if not update:
        return Response("OK", status=200)

    message = update.get("message") or update.get("edited_message")
    if not message:
        return Response("OK", status=200)

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if not text:
        return Response("OK", status=200)

    # /start or /help
    if text.startswith("/start") or text.startswith("/help"):
        send(chat_id, WELCOME)
        return Response("OK", status=200)

    # URL check
    url = extract_url(message)
    if not url:
        if text.startswith("/"):
            send(chat_id, "Unknown command. Use /help for instructions.")
        else:
            send(
                chat_id,
                "⚠️ Please send a <b>marchespublics.gov.ma</b> URL.\n\nUse /help for an example.",
            )
        return Response("OK", status=200)

    # Process
    typing(chat_id)
    send(chat_id, "⏳ Fetching and analyzing consultation data…")

    try:
        result = build_result_message(url)
        send(chat_id, result)
    except Exception as exc:
        log.exception("Processing error for URL: %s", url)
        send(
            chat_id,
            f"❌ <b>Error processing consultation:</b>\n{esc(str(exc)[:400])}\n\n"
            "Make sure the URL points to a completed results page.",
        )

    return Response("OK", status=200)


# Vercel Python runtime expects this WSGI entry point
handler = app
