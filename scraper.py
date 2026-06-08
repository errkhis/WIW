import re
import httpx
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Bidder:
    rank: int
    name: str
    admin_status: str
    financial_status: str
    price: Optional[float]
    technical_score: Optional[float] = None
    is_eligible: bool = False


@dataclass
class ConsultationData:
    reference: str
    object: str
    estimated_price: Optional[float]
    estimated_price_currency: str
    procedure: str
    category: str
    bidders: list[Bidder] = field(default_factory=list)
    technical_weight: Optional[float] = None
    financial_weight: Optional[float] = None
    lot_id: Optional[str] = None
    lot_label: Optional[str] = None
    lots: list["ConsultationData"] = field(default_factory=list)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,ar;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _parse_price_fr(text: str) -> Optional[float]:
    """Parse French-formatted numbers like '145 200,00' or '1.450.200,00'."""
    if not text:
        return None
    t = text.strip().replace("\xa0", " ").replace(" ", " ")
    # Remove currency labels
    t = re.sub(r"(MAD|DH|TTC|HT|Dhs?)\s*", "", t, flags=re.IGNORECASE).strip()
    if not t or t in ("-", "—", "N/A", ""):
        return None
    # French format: space/dot as thousands, comma as decimal
    # Remove spaces (thousands sep)
    t = t.replace(" ", "")
    # Replace comma decimal with dot
    if "," in t and "." in t:
        # e.g. "1.450.200,00" → remove dots then replace comma
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def scrape_consultation(url: str) -> ConsultationData:
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        response = client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        data = _build_consultation_data(url, soup)

        lot_options = _extract_lot_options(soup)
        lot_estimates = _fetch_lot_estimates(client, url)
        if len(lot_options) > 1:
            lots = []
            for lot_id, lot_label in lot_options:
                lot_soup = _fetch_lot_soup(client, url, soup, lot_id)
                lot_data = _build_consultation_data(url, lot_soup, lot_id, lot_label)
                if lot_id in lot_estimates:
                    lot_data.estimated_price, lot_data.estimated_price_currency = lot_estimates[lot_id]
                lots.append(lot_data)
            data.lots = lots
            if lots:
                data.bidders = [b for lot in lots for b in lot.bidders]
        elif "1" in lot_estimates:
            data.estimated_price, data.estimated_price_currency = lot_estimates["1"]

        return data


def _build_consultation_data(
    url: str,
    soup: BeautifulSoup,
    lot_id: Optional[str] = None,
    lot_label: Optional[str] = None,
) -> ConsultationData:
    estimated_price, currency = _extract_estimated_price(soup)
    technical_weight, financial_weight = _extract_weights(soup)
    return ConsultationData(
        reference=_meta_from_url(url),
        object=_extract_object(soup),
        estimated_price=estimated_price,
        estimated_price_currency=currency,
        procedure=_extract_labeled_field(soup, r"proc[eé]dure"),
        category=_extract_labeled_field(soup, r"cat[eé]gorie"),
        bidders=_extract_bidders(soup),
        technical_weight=technical_weight,
        financial_weight=financial_weight,
        lot_id=lot_id,
        lot_label=lot_label,
    )


def _extract_lot_options(soup: BeautifulSoup) -> list[tuple[str, str]]:
    select = soup.find("select", id=re.compile(r"lotsDropDownList", re.I))
    if not select:
        return []
    lots = []
    for opt in select.find_all("option"):
        value = (opt.get("value") or "").strip()
        label = opt.get_text(" ", strip=True)
        if value and label:
            lots.append((value, label))
    return lots


def _fetch_lot_soup(
    client: httpx.Client,
    url: str,
    base_soup: BeautifulSoup,
    lot_id: str,
) -> BeautifulSoup:
    form = base_soup.find("form")
    select = base_soup.find("select", id=re.compile(r"lotsDropDownList", re.I))
    if not form or not select or not select.get("name"):
        return base_soup

    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        input_type = (inp.get("type") or "").lower()
        if name and input_type not in ("image", "submit", "button"):
            data[name] = inp.get("value", "")

    select_name = select["name"]
    data[select_name] = lot_id
    data["PRADO_CALLBACK_TARGET"] = select_name
    data["PRADO_CALLBACK_PARAMETER"] = lot_id
    data["PRADO_POSTBACK_TARGET"] = ""
    data["PRADO_POSTBACK_PARAMETER"] = ""

    action = form.get("action") or url
    action_url = str(httpx.URL(url).join(action))
    response = client.post(
        action_url,
        data=data,
        headers={
            **HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "X-Prototype-Version": "1.7",
            "Accept": "text/javascript, text/html, application/xml, text/xml, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "lxml")


def _fetch_lot_estimates(
    client: httpx.Client,
    url: str,
) -> dict[str, tuple[float, str]]:
    reference = _meta_from_url(url)
    org = _meta_from_url_param(url, "orgAcronyme") or _meta_from_url_param(url, "orgAccronyme")
    if not reference or not org:
        return {}

    popup_url = (
        f"https://www.marchespublics.gov.ma/index.php"
        f"?page=commun.PopUpDetailLots&orgAccronyme={org}"
        f"&refConsultation={reference}&lang=fr"
    )
    try:
        response = client.get(popup_url)
        response.raise_for_status()
    except httpx.HTTPError:
        return {}

    soup = BeautifulSoup(response.text, "lxml")
    estimates: dict[str, tuple[float, str]] = {}
    for tag in soup.find_all(id=re.compile(r"repeaterLots_ctl(\d+).*panelReferentielZoneText", re.I)):
        text = tag.get_text(" ", strip=True)
        if not re.search(r"estimation", text, re.I):
            continue
        value = _parse_first_price(text)
        if value is None:
            continue
        idx_match = re.search(r"repeaterLots_ctl(\d+)", tag.get("id", ""), re.I)
        if not idx_match:
            continue
        lot_id = str(int(idx_match.group(1)) + 1)
        currency = "MAD TTC" if re.search(r"TTC", text, re.I) else "MAD"
        estimates[lot_id] = (value, currency)
    return estimates


def _parse_first_price(text: str) -> Optional[float]:
    for match in re.findall(r"\d[\d\s.\xa0 ]*,\d{2}", text):
        value = _parse_price_fr(match)
        if value is not None:
            return value
    return _parse_price_fr(text)


def _meta_from_url(url: str) -> str:
    m = re.search(r"refConsultation=(\w+)", url)
    return m.group(1) if m else "Unknown"


def _meta_from_url_param(url: str, name: str) -> Optional[str]:
    m = re.search(rf"[?&]{re.escape(name)}=([^&]+)", url)
    return m.group(1) if m else None


def _extract_object(soup: BeautifulSoup) -> str:
    # Try specific ID pattern first
    for tag in soup.find_all("span", id=re.compile(r"labelReferentielZoneText", re.I)):
        container = tag.find_parent()
        if container:
            ctx = container.get_text(separator=" ", strip=True)
            if re.search(r"objet", ctx, re.IGNORECASE):
                return tag.get_text(strip=True)

    # Fallback: look for label "objet" in page text
    for tag in soup.find_all(string=re.compile(r"\bobjet\b", re.IGNORECASE)):
        parent = tag.find_parent()
        if parent:
            nxt = parent.find_next_sibling()
            if nxt:
                return nxt.get_text(strip=True)
    return "N/A"


def _extract_labeled_field(soup: BeautifulSoup, pattern: str) -> str:
    for tag in soup.find_all(string=re.compile(pattern, re.IGNORECASE)):
        parent = tag.find_parent()
        if parent:
            # Try to find the value in a nearby span/td
            container = parent.find_parent()
            if container:
                value_span = container.find("span", id=re.compile(r"labelReferentielZoneText", re.I))
                if value_span:
                    return value_span.get_text(strip=True)
            nxt = parent.find_next_sibling()
            if nxt:
                v = nxt.get_text(strip=True)
                if v and v not in (":", ""):
                    return v
    return "N/A"


def _extract_estimated_price(soup: BeautifulSoup) -> tuple[Optional[float], str]:
    # Primary: span with ID "labelReferentielZoneText" whose container mentions estimation/prix
    for tag in soup.find_all("span", id=re.compile(r"labelReferentielZoneText", re.I)):
        container = tag.find_parent()
        if not container:
            continue
        ctx = container.get_text(separator=" ", strip=True)
        if re.search(r"estimat|prix\s*estimatif|budget|montant", ctx, re.IGNORECASE):
            val = _parse_price_fr(tag.get_text(strip=True))
            if val and val > 100:
                currency = "MAD TTC" if "TTC" in ctx else ("MAD HT" if "HT" in ctx else "MAD")
                return val, currency

    # Fallback: any text near "Estimation" keyword
    for tag in soup.find_all(string=re.compile(r"estimation|prix estimatif", re.IGNORECASE)):
        row = tag.find_parent("tr") or tag.find_parent("div") or tag.find_parent("li")
        if row:
            numbers = re.findall(r"[\d\s]+,\d{2}", row.get_text())
            for n in numbers:
                val = _parse_price_fr(n)
                if val and val > 100:
                    return val, "MAD"

    return None, "MAD"


def _extract_weights(soup: BeautifulSoup) -> tuple[Optional[float], Optional[float]]:
    tech_w = fin_w = None
    for tag in soup.find_all(string=re.compile(r"poids|pond[eé]ration|weight", re.IGNORECASE)):
        row = tag.find_parent("tr")
        if row:
            cells = row.find_all(["td", "th"])
            for i, cell in enumerate(cells):
                ct = cell.get_text(strip=True).lower()
                if "tech" in ct and i + 1 < len(cells):
                    v = _parse_price_fr(cells[i + 1].get_text(strip=True))
                    if v is not None:
                        tech_w = v
                elif "fin" in ct and i + 1 < len(cells):
                    v = _parse_price_fr(cells[i + 1].get_text(strip=True))
                    if v is not None:
                        fin_w = v
    return tech_w, fin_w


def _extract_bidders(soup: BeautifulSoup) -> list[Bidder]:
    # The page has exactly one table with bidder data
    # Structure: row[0]=empty, row[1]=headers, row[2]=subheaders, row[3+]=data
    # Columns: [Entreprise, Enveloppes admin, Enveloppes financières, Prix avant correction, Prix après correction]
    # Some consultations may also have a technical score column

    target_table = _find_bidder_table(soup)
    if not target_table:
        return []

    rows = target_table.find_all("tr")
    if len(rows) < 3:
        return []

    # Find header row (the one with 'Entreprise')
    header_idx = 0
    for i, row in enumerate(rows):
        txt = row.get_text().lower()
        if "entreprise" in txt or "soumissionnaire" in txt:
            header_idx = i
            break

    # Sub-header row is right after — skip it if it has no company data
    data_start = header_idx + 2  # skip header + subheader

    # Determine column layout from subheader row
    subheader_row = rows[header_idx + 1] if header_idx + 1 < len(rows) else None
    col = _infer_columns(rows[header_idx], subheader_row)

    bidders: list[Bidder] = []
    for idx, row in enumerate(rows[data_start:], start=1):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        texts = [c.get_text(strip=True) for c in cells]

        # Skip rows that look like sub-totals or separators
        if len(texts) < 3:
            continue

        name = texts[col["name"]] if col["name"] < len(texts) else ""
        if not name or re.match(r"^\d+$", name) or name.lower() in ("total", ""):
            continue

        admin_s = texts[col["admin"]] if col["admin"] < len(texts) else ""
        fin_s = texts[col["fin"]] if col["fin"] < len(texts) else ""
        price_before_t = texts[col["price_before"]] if col["price_before"] is not None and col["price_before"] < len(texts) else ""
        price_after_t = texts[col["price_after"]] if col["price_after"] is not None and col["price_after"] < len(texts) else ""
        price_t = texts[col["price"]] if col["price"] is not None and col["price"] < len(texts) else ""
        score_t = texts[col["score"]] if col["score"] is not None and col["score"] < len(texts) else ""

        price_after = _parse_price_fr(price_after_t)
        price_before = _parse_price_fr(price_before_t)
        price = price_after if price_after is not None else price_before
        if price is None:
            price = _parse_price_fr(price_t)
        score = _parse_price_fr(score_t) if score_t else None

        bidders.append(
            Bidder(
                rank=idx,
                name=name,
                admin_status=admin_s,
                financial_status=fin_s,
                price=price,
                technical_score=score if score and score <= 100 else None,
                is_eligible=price is not None,
            )
        )

    return bidders


def _find_bidder_table(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    for table in soup.find_all("table"):
        txt = table.get_text().lower()
        if (
            ("admissible" in txt or "écartée" in txt or "ecartée" in txt)
            and "entreprise" in txt
        ):
            return table
    # Fallback: largest table
    tables = soup.find_all("table")
    if tables:
        return max(tables, key=lambda t: len(t.find_all("tr")))
    return None


def _infer_columns(header_row, subheader_row) -> dict:
    """Map column names to indices based on the header rows."""
    # Default layout for marchespublics.gov.ma:
    # 0: Entreprise, 1: Admin, 2: Financial status, 3: Prix avant correction, 4: Prix après correction
    col = {
        "name": 0,
        "admin": 1,
        "fin": 2,
        "price": 3,
        "price_before": 3,
        "price_after": 4,
        "score": None,
    }

    if header_row is None:
        return col

    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
    sub = []
    if subheader_row:
        sub = [th.get_text(strip=True).lower() for th in subheader_row.find_all(["th", "td"])]

    all_h = headers + sub
    for i, h in enumerate(headers):
        hn = _norm(h)
        if any(k in hn for k in ["entreprise", "soumissionnaire", "societe", "raison sociale"]):
            col["name"] = i
        elif "admin" in hn:
            col["admin"] = i
        elif "financ" in hn:
            col["fin"] = i
        elif any(k in hn for k in ["note", "score", "technique"]):
            col["score"] = i

    generic_price_idx = None
    for i, h in enumerate(sub):
        hn = _norm(h)
        if "apres" in hn:
            col["price_after"] = i
        elif "avant" in hn:
            col["price_before"] = i
        elif "prix" in hn or "montant" in hn or "offre" in hn:
            generic_price_idx = i

    if generic_price_idx is not None:
        col["price"] = generic_price_idx

    # If price still unset, use col 3 (typical position)
    if col["price"] is None:
        col["price"] = 3

    return col


def _norm(s: str) -> str:
    return (
        s.strip()
        .lower()
        .replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("â", "a")
        .replace("î", "i")
        .replace("ô", "o")
        .replace("û", "u")
        .replace("ç", "c")
    )
