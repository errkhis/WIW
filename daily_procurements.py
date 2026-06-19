import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests as http
from bs4 import BeautifulSoup

from scraper import HEADERS, _parse_price_fr


BASE_URL = "https://www.marchespublics.gov.ma"
LIST_URL = f"{BASE_URL}/index.php?page=entreprise.EntrepriseAdvancedSearch&AllCons"
PAGE_SIZE_SELECT = "ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop"
PAGE_SIZE_SELECT_BOTTOM = "ctl0$CONTENU_PAGE$resultSearch$listePageSizeBottom"
TELEGRAM_MESSAGE_LIMIT = 3900


@dataclass(frozen=True)
class ProcurementSummaryItem:
    reference: str
    title: str
    estimated_price: Optional[float]
    location: str
    due_date: str
    published_date: str
    consultation_url: str


def fetch_daily_procurements(target_date: date) -> list[ProcurementSummaryItem]:
    target = target_date.strftime("%d/%m/%Y")
    with http.Session() as session:
        session.headers.update(HEADERS)
        html = _fetch_500_row_listing(session)
        items = _parse_listing_items(html, target)

    return _with_estimated_prices(items)


def build_daily_summary_messages(
    items: list[ProcurementSummaryItem],
    target_date: date,
) -> list[str]:
    date_label = target_date.strftime("%d/%m/%Y")
    if not items:
        return [
            "📋 <b>Résumé quotidien - Appels d'offres simplifiés</b>\n\n"
            f"Publié le: <b>{date_label}</b>\n"
            "Total: <b>0 consultation</b>\n\n"
            "Aucune consultation publiée hier."
        ]

    entries = [_format_item(i, item) for i, item in enumerate(items, start=1)]
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for entry in entries:
        entry_len = len(entry) + 2
        if current and current_len + entry_len > TELEGRAM_MESSAGE_LIMIT:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(entry)
        current_len += entry_len
    if current:
        chunks.append(current)

    messages = []
    total_parts = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        header = (
            "📋 <b>Résumé quotidien - Appels d'offres simplifiés</b>\n"
            f"Publié le: <b>{date_label}</b>\n"
            f"Total: <b>{len(items)} consultations</b>\n"
        )
        if total_parts > 1:
            header += f"Partie: <b>{index}/{total_parts}</b>\n"
        messages.append(header + "\n" + "\n\n".join(chunk))
    return messages


def _fetch_500_row_listing(session: http.Session) -> str:
    session.headers.update(HEADERS)
    response = session.get(LIST_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    form = soup.find("form")
    if not form:
        raise ValueError("Search result form not found")

    data = _form_data(form)
    data[PAGE_SIZE_SELECT] = "500"
    data[PAGE_SIZE_SELECT_BOTTOM] = "500"
    data["PRADO_POSTBACK_TARGET"] = PAGE_SIZE_SELECT
    data["PRADO_POSTBACK_PARAMETER"] = ""

    action_url = urljoin(LIST_URL, form.get("action") or LIST_URL)
    response = session.post(action_url, data=data, timeout=45)
    response.raise_for_status()
    return response.text


def _form_data(form) -> dict[str, str]:
    data: dict[str, str] = {}
    for tag in form.find_all(["input", "select", "textarea"]):
        name = tag.get("name")
        if not name:
            continue
        if tag.name == "select":
            selected = tag.find("option", selected=True) or tag.find("option")
            data[name] = selected.get("value", "") if selected else ""
        elif tag.name == "textarea":
            data[name] = tag.get_text()
        else:
            input_type = (tag.get("type") or "text").lower()
            if input_type in ("submit", "image", "reset", "button"):
                continue
            if input_type in ("checkbox", "radio") and not tag.has_attr("checked"):
                continue
            data[name] = tag.get("value", "")
    return data


def _parse_listing_items(html: str, published_date: str) -> list[ProcurementSummaryItem]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="table-results")
    if not table:
        return []

    items = []
    for row in table.find_all("tr")[2:]:
        item = _parse_listing_row(row, published_date)
        if item:
            items.append(item)
    return items


def _parse_listing_row(row, published_date: str) -> Optional[ProcurementSummaryItem]:
    cells = row.find_all("td")
    if len(cells) < 6:
        return None

    meta_text = _clean(cells[1].get_text(" ", strip=True))
    procedure = meta_text.split(" ... ", 1)[0].strip()
    dates = re.findall(r"\d{2}/\d{2}/\d{4}", meta_text)
    row_published_date = dates[-1] if dates else ""
    if procedure != "AOO" or row_published_date != published_date:
        return None
    if not _has_simplified_marker(row):
        return None

    consultation_url = _detail_url(row)
    if not consultation_url:
        return None

    detail_text = _clean(cells[2].get_text(" ", strip=True))
    reference = _reference_from_url(consultation_url) or _reference_from_text(detail_text)
    title = _title_from_text(detail_text)
    location = _location_from_text(cells[3].get_text(" ", strip=True))
    due_date = _due_date_from_text(cells[4].get_text(" ", strip=True))

    return ProcurementSummaryItem(
        reference=reference,
        title=title,
        estimated_price=None,
        location=location,
        due_date=due_date,
        published_date=row_published_date,
        consultation_url=consultation_url,
    )


def _has_simplified_marker(row) -> bool:
    for img in row.find_all("img"):
        marker_text = f"{img.get('src', '')} {img.get('alt', '')} {img.get('title', '')}"
        if "logo-mps-small" in marker_text or "Marché Public Simplifié" in marker_text:
            return True
    return False


def _detail_url(row) -> str:
    for link in row.find_all("a", href=True):
        href = link["href"]
        if "EntrepriseDetailConsultation" in href:
            return urljoin(LIST_URL, href)
    return ""


def _reference_from_url(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    return (query.get("refConsultation") or [""])[0]


def _reference_from_text(text: str) -> str:
    return text.split(" - ", 1)[0].strip()


def _title_from_text(text: str) -> str:
    match = re.search(r"Objet\s*:\s*(.*?)\s+Acheteur public\s*:", text, re.I)
    if not match:
        return text
    title = match.group(1).strip()
    if " ... " in title:
        title = title.split(" ... ")[-1].strip()
    return title


def _location_from_text(text: str) -> str:
    value = _clean(text).strip("- ")
    if " ... " in value:
        value = value.split(" ... ")[-1].strip("- ")
    return value or "—"


def _due_date_from_text(text: str) -> str:
    match = re.search(r"\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?", text)
    return match.group(0) if match else "—"


def _with_estimated_prices(
    items: list[ProcurementSummaryItem],
) -> list[ProcurementSummaryItem]:
    if not items:
        return []

    enriched = [item for item in items]
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(_fetch_estimated_price, item.consultation_url): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                estimated_price = future.result()
            except Exception:
                estimated_price = None
            enriched[index] = replace(items[index], estimated_price=estimated_price)
    return enriched


def _fetch_estimated_price(url: str) -> Optional[float]:
    response = http.get(url, headers=HEADERS, timeout=25)
    response.raise_for_status()
    return _extract_estimated_price(response.text)


def _extract_estimated_price(html: str) -> Optional[float]:
    text = _clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    match = re.search(
        r"Estimation\s*\([^)]*\)\s*\*?\s*:\s*([0-9][0-9\s.,]*)",
        text,
        re.I,
    )
    if not match:
        return None
    return _parse_price_fr(match.group(1))


def _format_item(index: int, item: ProcurementSummaryItem) -> str:
    return (
        f"{index}. <b>{_esc(_shorten(item.title, 800))}</b>\n"
        f"Estimation: <b>{_fmt_price(item.estimated_price)}</b>\n"
        f"Lieu: {_esc(_shorten(item.location, 300))}\n"
        f"Date limite: <b>{_esc(item.due_date)}</b>\n"
        f"Lien: <a href=\"{_esc(item.consultation_url)}\">Ouvrir la consultation</a>"
    )


def _fmt_price(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.2f} Dhs TTC"


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _esc(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
