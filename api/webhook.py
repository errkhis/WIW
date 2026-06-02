import sys
import os
import re
import json
import logging
import requests as http

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper import scrape_consultation
from calculator import calculate_winners
from company_city import lookup_company_cities
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


def send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg("sendMessage", payload)


def answer_callback(callback_id, text=""):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    tg("answerCallbackQuery", payload)


def typing(chat_id):
    tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def esc(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(n):
    return "—" if n is None else f"{n:,.2f}"


def fmt_pct(n):
    if n is None:
        return "—"
    return f"{'+' if n >= 0 else ''}{n:.2f}%"


def admin_contact():
    return f"@{TELEGRAM_ADMIN_USERNAME}" if TELEGRAM_ADMIN_USERNAME else "l'administrateur"


# ── Bot content ───────────────────────────────────────────────────────────────

WELCOME = (
    "🇲🇦 <b>Analyse des appels d'offres publics marocains</b>\n\n"
    "Envoyez un lien <b>marchespublics.gov.ma</b> et le bot calcule le classement "
    "par lot avec la méthode du prix de référence.\n\n"
    "<b>Règles utilisées :</b>\n"
    "• seules les offres avec prix sont utilisées\n"
    "• les sociétés sans prix ne sont pas incluses dans les calculs\n"
    "• aucune exclusion automatique par seuil +20% / -25%\n\n"
    "<b>Exemple :</b>\n"
    "<code>https://www.marchespublics.gov.ma/?page=entreprise.SuiviConsultation"
    "&amp;refConsultation=997895&amp;orgAcronyme=p1v</code>\n\n"
    "━━━━━━━━━━━━━━\n"
    "🧾 <b>Plan actuel : Free</b>\n\n"
    f"Vous disposez de <b>{FREE_RESULT_LIMIT} résultats gratuits</b>.\n"
    "Pour un accès illimité, contactez "
    f"<b>{esc(admin_contact())}</b>.\n\n"
    "Utilisez /me pour consulter votre statut."
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
        "🔒 <b>Limite du plan gratuit atteinte</b>\n\n"
        f"Le plan gratuit contient <b>{FREE_RESULT_LIMIT}</b> résultats.\n"
        "Pour continuer sans limite, demandez l'abonnement Premium :\n"
        f"<b>{esc(admin_contact())}</b>"
    )


def account_status_message(user):
    if user.is_premium:
        return (
            "👤 <b>Votre compte</b>\n"
            "Plan : <b>Premium</b>\n"
            f"Valide jusqu'au : <b>{fmt_date(user.premium_expires_at)}</b>\n"
            "Résultats : <b>illimités</b>"
        )
    return (
        "👤 <b>Votre compte</b>\n"
        "Plan : <b>Free</b>\n"
        f"Utilisés : <b>{user.free_results_used}/{FREE_RESULT_LIMIT}</b>\n"
        f"Restants : <b>{user.remaining_free_results}</b>"
    )


def database_error_message():
    return (
        "❌ <b>Base de données non configurée.</b>\n"
        "Ajoutez DATABASE_URL ou POSTGRES_URL dans Vercel."
    )


def handle_admin_command(chat_id, text, message):
    if not (text.startswith("/premium") or text.startswith("/free")):
        return False

    if not is_admin(message):
        send(chat_id, "⛔ Cette commande est réservée à l'administrateur.")
        return True

    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        send(chat_id, "Format : <code>/premium TELEGRAM_ID [years]</code> ou <code>/free TELEGRAM_ID</code>")
        return True

    telegram_id = int(parts[1])
    try:
        if text.startswith("/premium"):
            years = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
            user = grant_premium(telegram_id, years)
            send(
                chat_id,
                "✅ Premium activé\n"
                f"User ID: <code>{user.telegram_id}</code>\n"
                f"Valide jusqu'au : <b>{fmt_date(user.premium_expires_at)}</b>",
            )
        else:
            user = set_free(telegram_id)
            send(chat_id, f"✅ Plan Free rétabli\nUser ID: <code>{user.telegram_id}</code>")
    except DatabaseNotConfigured:
        send(chat_id, database_error_message())
    except Exception as exc:
        log.exception("Admin command error")
        send(chat_id, f"❌ <b>Erreur :</b> {esc(str(exc)[:400])}")
    return True


def handle_account_command(chat_id, message):
    sender = message.get("from") or {}
    if not sender.get("id"):
        send(chat_id, "❌ Impossible d'identifier votre Telegram user id.")
        return
    try:
        user = upsert_telegram_user(sender)
        send(chat_id, account_status_message(user))
    except DatabaseNotConfigured:
        send(chat_id, database_error_message())
    except Exception as exc:
        log.exception("Account command error")
        send(chat_id, f"❌ <b>Erreur :</b> {esc(str(exc)[:400])}")


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


def consultation_meta_from_url(url):
    ref_match = re.search(r"refConsultation=([^&]+)", url)
    org_match = re.search(r"orgAcronyme=([^&]+)", url)
    if not ref_match:
        return None, None
    return ref_match.group(1), org_match.group(1) if org_match else ""


def build_consultation_url(reference, org):
    url = (
        "https://www.marchespublics.gov.ma/index.php"
        f"?page=entreprise.SuiviConsultation&refConsultation={reference}"
    )
    if org:
        url += f"&orgAcronyme={org}"
    return url


def send_action_choice(chat_id, url):
    reference, org = consultation_meta_from_url(url)
    if not reference:
        send(chat_id, "❌ Lien invalide : refConsultation est introuvable.")
        return
    keyboard = {
        "inline_keyboard": [[
            {"text": "🏆 Obtenir le gagnant", "callback_data": f"winner:{reference}:{org}"},
            {"text": "🏙️ Villes des sociétés", "callback_data": f"cities:{reference}:{org}"},
        ]]
    }
    send(
        chat_id,
        "Que voulez-vous faire avec cette consultation ?",
        reply_markup=keyboard,
    )


def build_result(url):
    data = scrape_consultation(url)
    lots = data.lots or [data]
    if not any(lot.bidders for lot in lots):
        return "❌ Aucune donnée trouvée. Vérifiez que le lien contient les résultats de la consultation."

    lines = []
    lines.append(f"Consultation: <b>{esc(data.reference)}</b>")
    lines.append("")
    if len(lots) > 1:
        lines.append(f"Cette consultation contient <b>{len(lots)} lots</b>.")
        lines.append("")

    for lot_index, lot in enumerate(lots, start=1):
        lines.extend(_build_lot_result_lines(lot, lot_index))
        lines.append("")

    return "\n".join(lines).strip()


def build_company_cities_result(url):
    data = scrape_consultation(url)
    lots = data.lots or [data]
    if not any(lot.bidders for lot in lots):
        return "❌ Aucune société trouvée dans cette consultation."

    names = []
    for lot in lots:
        names.extend(b.name for b in lot.bidders if b.name)

    cities = lookup_company_cities(names)
    lines = [f"Consultation: <b>{esc(data.reference)}</b>", ""]
    lines.append("<b>Villes des sociétés:</b>")
    for item in cities:
        city = esc(item.city or "Ville introuvable")
        lines.append(f"- {esc(item.name)}: <b>{city}</b>")
    return "\n".join(lines)


def _build_lot_result_lines(data, lot_index):
    rankings, _, ref_price = calculate_winners(data)
    priced_rankings = [
        r for r in rankings
        if r.price is not None and not r.note.startswith("Eliminated")
    ]
    eligible = [r for r in rankings if r.is_eligible]
    ordered = eligible or priced_rankings
    winner = next((r for r in eligible if r.position == 1), None)
    winners = [r for r in eligible if winner and r.price == winner.price]
    E = data.estimated_price
    avg_price = (
        sum(r.price for r in priced_rankings) / len(priced_rankings)
        if priced_rankings else None
    )
    avg_diff_pct = (
        (avg_price - E) / E * 100
        if avg_price is not None and E else None
    )

    lines = []
    lines.append(f"<b>Lot {data.lot_id or lot_index}:</b>")
    lines.append("")
    lines.append(f"- Sociétés trouvées: <b>{len(data.bidders)}</b>")
    lines.append(f"- Offres avec prix utilisées: <b>{len(priced_rankings)}</b>")
    lines.append(f"- E: <b>{fmt(E)}</b>")
    lines.append(f"- Moyenne: <b>{fmt(avg_price)}</b>")
    lines.append(f"- Écart: <b>{fmt_pct(avg_diff_pct)}</b>")
    if len(winners) > 1:
        lines.append(f"- Prix gagnant ex aequo: <b>{fmt(winner.price)}</b>")
        lines.append("- Gagnants: <b>" + esc(", ".join(r.name for r in winners)) + "</b>")
    elif winner:
        lines.append(f"- Gagnant: <b>{esc(winner.name)}</b>")
    else:
        lines.append("- Gagnant: <b>—</b>")
    lines.append("")

    lines.append("<b>Top 5 des sociétés:</b>")
    for i, r in enumerate(ordered[:5], start=1):
        icon = MEDALS[i - 1] if i <= len(MEDALS) else f"{i}."
        lines.append(f"{icon} {esc(r.name)} - {fmt(r.price)}")
    return lines


def process_update(update):
    callback = update.get("callback_query")
    if callback:
        process_callback(callback)
        return

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
            send(chat_id, "⚠️ Envoyez un lien <b>marchespublics.gov.ma</b>.\n\nUtilisez /help pour voir un exemple.")
        return

    try:
        upsert_telegram_user(message.get("from") or {"id": chat_id})
    except DatabaseNotConfigured:
        send(chat_id, database_error_message())
        return
    except Exception as exc:
        log.exception("Database error")
        send(chat_id, f"❌ <b>Erreur base de données :</b> {esc(str(exc)[:400])}")
        return

    send_action_choice(chat_id, url)


def process_callback(callback):
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    sender = callback.get("from") or {}
    data = callback.get("data") or ""

    if callback_id:
        answer_callback(callback_id, "Traitement en cours...")
    if not chat_id:
        return

    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] not in ("winner", "cities"):
        send(chat_id, "❌ Action inconnue.")
        return

    action, reference, org = parts
    url = build_consultation_url(reference, org)

    try:
        user = upsert_telegram_user(sender or {"id": chat_id})
        if not can_create_procurement_result(user):
            send(chat_id, subscription_limit_message())
            return
    except DatabaseNotConfigured:
        send(chat_id, database_error_message())
        return
    except Exception as exc:
        log.exception("Database error")
        send(chat_id, f"❌ <b>Erreur base de données :</b> {esc(str(exc)[:400])}")
        return

    typing(chat_id)
    if action == "winner":
        send(chat_id, "⏳ Calcul du gagnant en cours...")
    else:
        send(chat_id, "⏳ Recherche des villes des sociétés en cours...")

    try:
        result = build_result(url) if action == "winner" else build_company_cities_result(url)
        updated_user = record_procurement_result(user.telegram_id, url)
        if not updated_user.is_premium:
            result += (
                "\n\n"
                f"🧾 Free: {updated_user.free_results_used}/{FREE_RESULT_LIMIT} "
                f"· restants {updated_user.remaining_free_results}"
            )
        send(chat_id, result)
    except QuotaExceeded:
        send(chat_id, subscription_limit_message())
    except Exception as exc:
        log.exception("Processing error")
        send(chat_id, f"❌ <b>Erreur :</b> {esc(str(exc)[:400])}")


# ── Vercel native handler ─────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self._ok("Bot actif ✓".encode("utf-8"))

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
