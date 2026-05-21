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
    "🇲🇦 <b>بوت ديال الصفقات العمومية المغربية</b>\n\n"
    "عطيني أي رابط من <b>marchespublics.gov.ma</b> وغادي نحسب ليك الرابح "
    "على حساب طريقة ثمن المرجع (المادة 13 من RC).\n\n"
    "<b>الصيغة:</b>\n"
    "P = (E + معدل العروض الصالحة) ÷ 2\n"
    "الرابح = العرض اللي أقرب لـ P من تحت ▼\n\n"
    "<b>التصفية اللي كتتطبق بشكل أوتوماتيكي:</b>\n"
    "• العروض الغالية بزاف (&gt;+20% من E) ← مستبعدة\n"
    "• العروض الرخيصة بزاف (&lt;-25% من E) ← مستبعدة\n\n"
    "<b>مثال — حط الرابط وسيفطه:</b>\n"
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
        return "❌ ما لقيناش معطيات. تأكد أن الرابط ديالك فيه نتائج المناقصة كاملة."

    rankings, _, ref_price = calculate_winners(data)
    eligible = [r for r in rankings if r.is_eligible]
    eliminated = [r for r in rankings if not r.is_eligible]
    top10 = eligible[:10]
    winner = top10[0] if top10 else None
    E = data.estimated_price

    lines = []
    lines.append(f"📋 <b>المناقصة رقم {esc(data.reference)}</b>")
    lines.append(f"🔹 {esc(data.object)}")
    lines.append("")

    if winner:
        lines.append(f"🏆 <b>الرابح: {esc(winner.name)}</b>")
        lines.append(f"💰 العرض: <b>{fmt(winner.price)} درهم</b>")
        if ref_price:
            lines.append(f"📏 الفرق مع P: {fmt(ref_price - winner.price)} درهم تحت")
        lines.append("")
    else:
        lines.append("❌ <b>ما كاينش رابح مقبول</b>")
        lines.append("")

    lines.append("📊 <b>تحليل الأثمنة</b>")
    if E:
        lines.append(f"• التقدير (E): {fmt(E)} {esc(data.estimated_price_currency)}")
    if ref_price:
        lines.append(f"• ثمن المرجع (P): <b>{fmt(ref_price)} درهم</b>")
    if E:
        lines.append(f"• الحد الأقصى (+20%): {fmt(E * EXCESSIVE_THRESHOLD)} درهم")
        lines.append(f"• الحد الأدنى (-25%): {fmt(E * LOW_THRESHOLD)} درهم")
    lines.append(f"• المقبولين / المجموع: {len(eligible)} / {len(data.bidders)}")
    if eliminated:
        lines.append(f"• المستبعدين: {len(eliminated)}")
    lines.append("")

    lines.append(f"🏅 <b>أحسن {len(top10)} عروض</b>")
    for i, r in enumerate(top10):
        medal = MEDALS[i] if i < len(MEDALS) else f"{i+1}."
        arrow = "▼" if r.side == "below" else "▲"
        side_label = "تحت P" if r.side == "below" else "فوق P"
        lines.append(
            f"{medal} <b>{esc(r.name)}</b>\n"
            f"   {fmt(r.price)} درهم  ·  Δ {fmt(r.distance_to_ref)}  ·  {arrow} {side_label}"
        )

    lines.append("")
    lines.append("<i>المادة 13 من RC · المرسوم رقم 2-22-431</i>")
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
            send(chat_id, "⚠️ عطيني رابط من <b>marchespublics.gov.ma</b>\n\nكتب /help باش تشوف مثال.")
        return

    typing(chat_id)
    send(chat_id, "⏳ كنجيب البيانات وكنحسب...")

    try:
        send(chat_id, build_result(url))
    except Exception as exc:
        log.exception("Processing error")
        send(chat_id, f"❌ <b>وقع خطأ:</b> {esc(str(exc)[:400])}")


# ── Vercel native handler ─────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self._ok("البوت خدام ✓".encode("utf-8"))

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
