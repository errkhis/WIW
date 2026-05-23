import sys
import os
import re
import json
import logging
import requests as http

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper import scrape_consultation
from calculator import calculate_winners, EXCESSIVE_THRESHOLD, LOW_THRESHOLD
from database import (
    FREE_RESULT_LIMIT,
    DatabaseNotConfigured,
    QuotaExceeded,
    can_create_procurement_result,
    grant_premium,
    record_procurement_result,
    set_free,
    upsert_telegram_user,
)

from http.server import BaseHTTPRequestHandler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
TELEGRAM_ADMIN_USERNAME = os.environ.get("TELEGRAM_ADMIN_USERNAME", "").strip().lstrip("@")


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


def admin_contact():
    return f"@{TELEGRAM_ADMIN_USERNAME}" if TELEGRAM_ADMIN_USERNAME else "أدمن البوت"


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
    "&amp;refConsultation=997895&amp;orgAcronyme=p1v</code>\n\n"
    "━━━━━━━━━━━━━━\n"
    "🧾 <b>الخطة ديالك دابا: Free</b>\n\n"
    f"عندك <b>{FREE_RESULT_LIMIT} نتائج مجانية</b> باش تجرب الخدمة وتحسب الرابح ديال الصفقات.\n"
    f"من بعد ما تسالي {FREE_RESULT_LIMIT} النتائج، البوت غادي يوقف الحسابات الجديدة حتى تفعل "
    "<b>Premium</b>.\n\n"
    "⭐ <b>Premium سنوي</b>\n"
    "• استعمال غير محدود طول العام\n"
    "• تقدر تحلل أي عدد من الصفقات\n"
    f"• بلا حد ديال {FREE_RESULT_LIMIT} نتائج\n\n"
    f"باش تفعل Premium، تاصل مع <b>{esc(admin_contact())}</b> فتيليگرام.\n\n"
    "كتب /me باش تشوف شحال بقا ليك فـ Free."
)

MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def is_admin(message):
    if not TELEGRAM_ADMIN_ID:
        return False
    sender = message.get("from") or {}
    return str(sender.get("id", "")) == TELEGRAM_ADMIN_ID


def fmt_date(dt):
    return dt.strftime("%Y-%m-%d") if dt else "—"


def subscription_limit_message():
    return (
        "🔒 <b>سالاو ليك نتائج الخطة المجانية</b>\n\n"
        f"الخطة المجانية فيها غير <b>{FREE_RESULT_LIMIT}</b> نتائج ديال الصفقات.\n"
        "باش تكمل بلا حدود، طلب الاشتراك السنوي Premium من الأدمن:\n"
        f"<b>{esc(admin_contact())}</b>"
    )


def account_status_message(user):
    if user.is_premium:
        return (
            "👤 <b>الحساب ديالك</b>\n"
            "الخطة: <b>Premium</b>\n"
            f"صالحة حتى: <b>{fmt_date(user.premium_expires_at)}</b>\n"
            "النتائج: <b>غير محدودة</b>"
        )
    return (
        "👤 <b>الحساب ديالك</b>\n"
        "الخطة: <b>Free</b>\n"
        f"استعملتي: <b>{user.free_results_used}/{FREE_RESULT_LIMIT}</b>\n"
        f"الباقي: <b>{user.remaining_free_results}</b>"
    )


def database_error_message():
    return (
        "❌ <b>قاعدة البيانات ما موجدهاش السيرفر.</b>\n"
        "خاص صاحب البوت يضيف DATABASE_URL أو POSTGRES_URL فـ Vercel."
    )


def handle_admin_command(chat_id, text, message):
    if not (text.startswith("/premium") or text.startswith("/free")):
        return False

    if not is_admin(message):
        send(chat_id, "⛔ هاد الأمر خاص بالأدمن فقط.")
        return True

    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        send(chat_id, "الصيغة: <code>/premium TELEGRAM_ID [years]</code> أو <code>/free TELEGRAM_ID</code>")
        return True

    telegram_id = int(parts[1])
    try:
        if text.startswith("/premium"):
            years = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
            user = grant_premium(telegram_id, years)
            send(
                chat_id,
                "✅ تفعل Premium\n"
                f"User ID: <code>{user.telegram_id}</code>\n"
                f"صالحة حتى: <b>{fmt_date(user.premium_expires_at)}</b>",
            )
        else:
            user = set_free(telegram_id)
            send(chat_id, f"✅ رجع Free\nUser ID: <code>{user.telegram_id}</code>")
    except DatabaseNotConfigured:
        send(chat_id, database_error_message())
    except Exception as exc:
        log.exception("Admin command error")
        send(chat_id, f"❌ <b>وقع خطأ:</b> {esc(str(exc)[:400])}")
    return True


def handle_account_command(chat_id, message):
    sender = message.get("from") or {}
    if not sender.get("id"):
        send(chat_id, "❌ ما قدرتش نعرف Telegram user id ديالك.")
        return
    try:
        user = upsert_telegram_user(sender)
        send(chat_id, account_status_message(user))
    except DatabaseNotConfigured:
        send(chat_id, database_error_message())
    except Exception as exc:
        log.exception("Account command error")
        send(chat_id, f"❌ <b>وقع خطأ:</b> {esc(str(exc)[:400])}")


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

    if handle_admin_command(chat_id, text, message):
        return

    if text.startswith("/me") or text.startswith("/subscription"):
        handle_account_command(chat_id, message)
        return

    if text.startswith("/start") or text.startswith("/help"):
        try:
            upsert_telegram_user(message.get("from") or {"id": chat_id})
        except DatabaseNotConfigured:
            log.warning("DATABASE_URL is not configured")
        except Exception:
            log.exception("Failed to register user")
        send(chat_id, WELCOME)
        return

    url = extract_url(message)
    if not url:
        if not text.startswith("/"):
            send(chat_id, "⚠️ عطيني رابط من <b>marchespublics.gov.ma</b>\n\nكتب /help باش تشوف مثال.")
        return

    try:
        user = upsert_telegram_user(message.get("from") or {"id": chat_id})
        if not can_create_procurement_result(user):
            send(chat_id, subscription_limit_message())
            return
    except DatabaseNotConfigured:
        send(chat_id, database_error_message())
        return
    except Exception as exc:
        log.exception("Database error")
        send(chat_id, f"❌ <b>وقع خطأ فقاعدة البيانات:</b> {esc(str(exc)[:400])}")
        return

    typing(chat_id)
    send(chat_id, "⏳ كنجيب البيانات وكنحسب...")

    try:
        result = build_result(url)
        updated_user = record_procurement_result(user.telegram_id, url)
        if not updated_user.is_premium:
            result += (
                "\n\n"
                f"🧾 Free: {updated_user.free_results_used}/{FREE_RESULT_LIMIT} "
                f"· الباقي {updated_user.remaining_free_results}"
            )
        send(chat_id, result)
    except QuotaExceeded:
        send(chat_id, subscription_limit_message())
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
