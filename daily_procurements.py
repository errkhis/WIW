import asyncio
import html
import json
import re
import os
import shutil
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests as http
from bs4 import BeautifulSoup

from scraper import HEADERS, _parse_price_fr


BASE_URL = "https://www.marchespublics.gov.ma"
SEARCH_URL = f"{BASE_URL}/index.php?page=entreprise.EntrepriseAdvancedSearch&searchAnnCons"
PROCEDURE_TYPE_SELECT = "ctl0$CONTENU_PAGE$AdvancedSearch$procedureType"
PUBLISHED_DATE_START = "ctl0$CONTENU_PAGE$AdvancedSearch$dateMiseEnLigneCalculeStart"
PUBLISHED_DATE_END = "ctl0$CONTENU_PAGE$AdvancedSearch$dateMiseEnLigneCalculeEnd"
SEARCH_BUTTON = "ctl0$CONTENU_PAGE$AdvancedSearch$lancerRecherche"
PAGE_SIZE_SELECT = "ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop"
SIMPLIFIED_OPEN_TENDER_LABEL = "Appel d'offres ouvert simplifié"
SIMPLIFIED_OPEN_TENDER_VALUE = "50"


@dataclass(frozen=True)
class ProcurementSummaryItem:
    reference: str
    title: str
    estimated_price: Optional[float]
    caution_amount: Optional[float]
    has_documents: bool
    location: str
    due_date: str
    published_date: str
    consultation_url: str


def fetch_daily_procurements(
    target_date: date,
    browser_api_base_url: Optional[str] = None,
) -> list[ProcurementSummaryItem]:
    target = target_date.strftime("%d/%m/%Y")
    end_date = (target_date + timedelta(days=1)).strftime("%d/%m/%Y")
    items = _fetch_listing_items_via_browser_api(target_date, browser_api_base_url)
    if items is None:
        html = _fetch_listing_html(target, end_date)
        items = _parse_listing_items(html, target)

    return _with_detail_data(items)


def _fetch_listing_items_via_browser_api(
    target_date: date,
    browser_api_base_url: Optional[str],
) -> Optional[list[ProcurementSummaryItem]]:
    base_url = _browser_api_base_url(browser_api_base_url)
    if not base_url:
        return None

    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        raise RuntimeError("daily_summary_browser_api_requires_cron_secret")

    response = http.get(
        f"{base_url}/api/daily-summary-browser",
        params={"secret": secret, "date": target_date.isoformat()},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "daily_summary_browser_api_failed")

    return [
        ProcurementSummaryItem(
            reference=str(item.get("reference") or "").strip(),
            title=str(item.get("title") or "").strip(),
            estimated_price=None,
            caution_amount=None,
            has_documents=False,
            location=str(item.get("location") or "—").strip() or "—",
            due_date=str(item.get("due_date") or "—").strip() or "—",
            published_date=str(item.get("published_date") or "").strip(),
            consultation_url=str(item.get("consultation_url") or "").strip(),
        )
        for item in payload.get("items") or []
        if str(item.get("consultation_url") or "").strip()
    ]


def _browser_api_base_url(browser_api_base_url: Optional[str]) -> str:
    return (
        (browser_api_base_url or "").strip().rstrip("/")
        or os.environ.get("DAILY_SUMMARY_BROWSER_API_BASE_URL", "").strip().rstrip("/")
        or os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    )


def _fetch_listing_html(published_date: str, published_end_date: str) -> str:
    remote_base_url = _remote_chrome_base_url()
    if remote_base_url:
        return _fetch_listing_html_via_cdp_http(
            remote_base_url,
            published_date,
            published_end_date,
        )

    chrome_path = _chrome_binary()
    if chrome_path:
        return _fetch_listing_html_in_local_browser(
            chrome_path,
            published_date,
            published_end_date,
        )
    raise RuntimeError(
        "daily_summary_browser_required: no remote Chrome endpoint or local Chrome/Chromium binary found in runtime"
    )


def _chrome_binary() -> str:
    explicit = (
        os.environ.get("GOOGLE_CHROME_BIN", "").strip()
        or os.environ.get("CHROME_BIN", "").strip()
    )
    if explicit:
        return explicit

    for candidate in (
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
    ):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return ""


def _remote_chrome_base_url() -> str:
    return (
        os.environ.get("REMOTE_CHROME_HTTP_URL", "").strip()
        or os.environ.get("CHROME_CDP_HTTP_URL", "").strip()
    ).rstrip("/")


def _remote_chrome_token() -> str:
    return (
        os.environ.get("REMOTE_CHROME_TOKEN", "").strip()
        or os.environ.get("CHROME_CDP_TOKEN", "").strip()
    )


def _fetch_listing_html_in_local_browser(
    chrome_path: str,
    published_date: str,
    published_end_date: str,
) -> str:
    port = _free_tcp_port()
    with tempfile.TemporaryDirectory(
        prefix="winner-chrome-",
        ignore_cleanup_errors=True,
    ) as profile_dir:
        process = subprocess.Popen(
            [
                chrome_path,
                "--headless=new",
                "--disable-gpu",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            _wait_for_devtools(base_url, process)
            return _fetch_listing_html_via_cdp_http(
                base_url,
                published_date,
                published_end_date,
            )
        finally:
            _stop_process(process)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_devtools(base_url: str, process: Optional[subprocess.Popen], timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    endpoint = _cdp_json_url(base_url, "/json/version")
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("headless_chrome_exited_early")
        try:
            response = http.get(endpoint, timeout=1)
            if response.ok:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("headless_chrome_debug_endpoint_not_ready")


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _fetch_listing_html_via_cdp_http(
    base_url: str,
    published_date: str,
    published_end_date: str,
) -> str:
    _wait_for_devtools(base_url, None)
    return asyncio.run(
        _browser_search_listing_html(
            base_url,
            published_date,
            published_end_date,
        )
    )


def _cdp_json_url(base_url: str, path: str, raw_first_query: str = "") -> str:
    token = _remote_chrome_token()
    query_parts = []
    if raw_first_query:
        query_parts.append(raw_first_query)
    if token:
        query_parts.append(f"token={quote(token, safe='')}")
    query = ""
    if query_parts:
        query = "?" + "&".join(query_parts)
    return f"{base_url.rstrip('/')}{path}{query}"


async def _browser_search_listing_html(
    base_url: str,
    published_date: str,
    published_end_date: str,
) -> str:
    import websockets

    target = http.put(_cdp_json_url(base_url, "/json/new", "about:blank"), timeout=10)
    target.raise_for_status()
    websocket_url = target.json()["webSocketDebuggerUrl"]

    async with websockets.connect(websocket_url, max_size=50_000_000) as websocket:
        client = _CdpClient(websocket)
        await client.command("Page.enable")
        await client.command("Runtime.enable")
        await client.command("Network.enable")
        await client.command("Page.navigate", {"url": SEARCH_URL})
        await client.wait_for_event("Page.loadEventFired", timeout=30)

        await client.command(
            "Runtime.evaluate",
            {
                "expression": _search_script(published_date, published_end_date),
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        await client.wait_for_event("Page.loadEventFired", timeout=30)

        resized = await client.command(
            "Runtime.evaluate",
            {
                "expression": _page_size_script(),
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if resized.get("result", {}).get("result", {}).get("value"):
            await client.wait_for_event("Page.loadEventFired", timeout=30)

        html = await client.command(
            "Runtime.evaluate",
            {
                "expression": "document.documentElement.outerHTML",
                "returnByValue": True,
            },
        )
        return html["result"]["result"]["value"]


class _CdpClient:
    def __init__(self, websocket):
        self.websocket = websocket
        self.next_id = 0
        self.buffer: list[dict] = []

    async def command(self, method: str, params: Optional[dict] = None) -> dict:
        self.next_id += 1
        request_id = self.next_id
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self.websocket.send(json.dumps(message))

        while True:
            payload = await self._recv_json()
            if payload.get("id") == request_id:
                return payload
            self.buffer.append(payload)

    async def wait_for_event(self, method: str, timeout: float) -> dict:
        deadline = time.time() + timeout
        while True:
            for index, payload in enumerate(self.buffer):
                if payload.get("method") == method:
                    return self.buffer.pop(index)

            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"cdp_event_timeout:{method}")
            payload = await self._recv_json(timeout=remaining)
            if payload.get("method") == method:
                return payload
            self.buffer.append(payload)

    async def _recv_json(self, timeout: Optional[float] = None) -> dict:
        if timeout is None:
            raw = await self.websocket.recv()
        else:
            raw = await asyncio.wait_for(self.websocket.recv(), timeout=timeout)
        return json.loads(raw)


def _search_script(published_date: str, published_end_date: str) -> str:
    return f"""
(() => {{
  const setValue = (name, value) => {{
    const element = document.querySelector(`[name="${{name}}"]`);
    if (!element) {{
      throw new Error(`missing_field:${{name}}`);
    }}
    element.value = value;
    element.dispatchEvent(new Event('change', {{ bubbles: true }}));
  }};

  setValue('{PROCEDURE_TYPE_SELECT}', '{SIMPLIFIED_OPEN_TENDER_VALUE}');
  setValue('{PUBLISHED_DATE_START}', '{published_date}');
  setValue('{PUBLISHED_DATE_END}', '{published_end_date}');

  const button = document.querySelector('[name="{SEARCH_BUTTON}"]');
  if (!button) {{
    throw new Error('missing_search_button');
  }}
  button.click();
  return true;
}})();
"""


def _page_size_script() -> str:
    return f"""
(() => {{
  const select = document.querySelector('[name="{PAGE_SIZE_SELECT}"]');
  if (!select) {{
    return false;
  }}
  if (select.value === '500') {{
    return false;
  }}
  select.value = '500';
  select.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return true;
}})();
"""


def build_daily_summary_message(
    items: list[ProcurementSummaryItem],
    target_date: date,
) -> str:
    date_label = target_date.strftime("%d/%m/%Y")
    if not items:
        return (
            "📋 <b>Résumé quotidien - Appels d'offres ouverts simplifiés</b>\n\n"
            f"Publié le: <b>{date_label}</b>\n"
            "Total: <b>0 consultation</b>\n\n"
            "Aucune consultation publiée hier."
        )

    locations = len({item.location for item in items if item.location and item.location != "—"})
    total_estimation = sum(item.estimated_price or 0 for item in items)
    latest_due_date = min((item.due_date for item in items if item.due_date != "—"), default="—")
    return (
        "📋 <b>Résumé quotidien - Appels d'offres ouverts simplifiés</b>\n\n"
        f"Publié le: <b>{date_label}</b>\n"
        f"Total: <b>{len(items)} consultations</b>\n"
        f"Lieux couverts: <b>{locations}</b>\n"
        f"Estimation cumulée: <b>{_fmt_price(total_estimation)}</b>\n"
        f"Prochaine date limite: <b>{_esc(latest_due_date)}</b>\n\n"
        "Le détail complet est joint en fichier HTML."
    )


def build_daily_summary_html_document(
    items: list[ProcurementSummaryItem],
    target_date: date,
) -> str:
    date_label = target_date.strftime("%d/%m/%Y")
    rows = []
    for index, item in enumerate(items, start=1):
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{_html(item.reference)}</td>"
            f"<td>{_html(item.title)}</td>"
            f"<td>{_html(_fmt_price(item.estimated_price))}</td>"
            f"<td>{_html(_fmt_price(item.caution_amount))}</td>"
            f"<td>{_html(_yes_no(item.has_documents))}</td>"
            f"<td>{_html(item.location)}</td>"
            f"<td>{_html(item.due_date)}</td>"
            f"<td><a href=\"{_html(item.consultation_url)}\">Consultation</a></td>"
            "</tr>"
        )

    table_rows = "\n".join(rows) or (
        "<tr><td colspan=\"9\">Aucune consultation publiee pour cette date.</td></tr>"
    )
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Resume AOS {date_label}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f1e8;
      --paper: #fffdf8;
      --ink: #1d2a33;
      --muted: #69757d;
      --line: #d7d0c2;
      --accent: #0d5c63;
      --accent-soft: #e1f0ef;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 32px;
      background: linear-gradient(180deg, var(--bg), #ebe4d7);
      color: var(--ink);
      font: 14px/1.5 Arial, sans-serif;
    }}
    .sheet {{
      max-width: 1400px;
      margin: 0 auto;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
      box-shadow: 0 18px 50px rgba(0, 0, 0, 0.08);
    }}
    header {{
      padding: 28px 32px 20px;
      background: var(--accent-soft);
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
    }}
    p {{
      margin: 4px 0;
      color: var(--muted);
    }}
    .table-wrap {{
      overflow: auto;
      padding: 0 0 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      text-align: left;
    }}
    th {{
      position: sticky;
      top: 0;
      background: #f8f5ee;
      z-index: 1;
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    td:nth-child(1), td:nth-child(4), td:nth-child(6) {{
      white-space: nowrap;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    @media (max-width: 720px) {{
      body {{ padding: 12px; }}
      header {{ padding: 20px; }}
      th, td {{ padding: 10px; }}
    }}
  </style>
</head>
<body>
  <div class="sheet">
    <header>
      <h1>Resume quotidien - Appels d'offres ouverts simplifies</h1>
      <p>Date de publication filtree: {html.escape(date_label)}</p>
      <p>Nombre total de consultations: {len(items)}</p>
    </header>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Reference</th>
            <th>Objet</th>
            <th>Estimation</th>
            <th>Caution</th>
            <th>Documents</th>
            <th>Lieu</th>
            <th>Date limite</th>
            <th>Lien</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


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
    if row_published_date != published_date:
        return None
    if procedure and procedure != "AOS":
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
        caution_amount=None,
        has_documents=False,
        location=location,
        due_date=due_date,
        published_date=row_published_date,
        consultation_url=consultation_url,
    )

def _detail_url(row) -> str:
    for link in row.find_all("a", href=True):
        href = link["href"]
        if "EntrepriseDetailConsultation" in href:
            return urljoin(BASE_URL, href)
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


def _with_detail_data(
    items: list[ProcurementSummaryItem],
) -> list[ProcurementSummaryItem]:
    if not items:
        return []

    enriched: list[Optional[ProcurementSummaryItem]] = [None for _ in items]
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(_fetch_detail_data, item): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                enriched[index] = future.result()
            except Exception:
                enriched[index] = None
    return [item for item in enriched if item is not None]


def _fetch_detail_data(item: ProcurementSummaryItem) -> Optional[ProcurementSummaryItem]:
    response = http.get(item.consultation_url, headers=HEADERS, timeout=25)
    response.raise_for_status()
    if not _is_simplified_open_tender_detail(response.text):
        return None
    return replace(
        item,
        estimated_price=_extract_estimated_price(response.text),
        caution_amount=_extract_caution_amount(response.text),
        has_documents=_extract_has_documents(response.text),
    )


def _is_simplified_open_tender_detail(html: str) -> bool:
    text = _clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True)).lower()
    return "procédure : appel d'offres ouvert simplifié" in text or (
        "procedure : appel d'offres ouvert simplifie" in _norm(text)
    )


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


def _extract_caution_amount(html: str) -> Optional[float]:
    text = _clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    match = re.search(
        r"Caution\s+provisoire\s*:\s*([0-9][0-9\s.,]*)",
        text,
        re.I,
    )
    if not match:
        return None
    return _parse_price_fr(match.group(1))


def _extract_has_documents(html: str) -> bool:
    text = _clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    match = re.search(
        r"Prospectus,\s*notices\s+ou\s+autres\s+documents\s*:\s*(.+?)\s+(?:Réunion|Visites des lieux|Variante)\s*:",
        text,
        re.I,
    )
    if not match:
        return False
    value = match.group(1).strip()
    return value not in {"-", "—", ""}


def _fmt_price(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.2f} Dhs TTC"


def _yes_no(value: bool) -> str:
    return "Oui" if value else "Non"


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _norm(value: str) -> str:
    return (
        value.replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("à", "a")
        .replace("â", "a")
    )


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _esc(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _html(value) -> str:
    return html.escape(str(value), quote=True)
