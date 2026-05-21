"""
Moroccan Public Procurement — Simplified Open Tender (AOO Simplifié)
Winner calculation per Article 13 of the RC and Decree n°2-22-431 (08 mars 2023).

Steps:
1. Discard admin-rejected bidders.
2. Discard excessive offers  (price > E × 1.20).
3. Discard abnormally low offers (price < E × 0.75).
4. Compute reference price: P = (E + mean(valid_prices)) / 2
5. Rank:
   - Priority 1: offers ≤ P  → sorted descending by price (closest to P from below)
   - Priority 2: offers >  P → sorted ascending by price  (closest to P from above)
   Only if there are zero offers ≤ P do we fall back to Priority 2.
"""

from dataclasses import dataclass
from typing import Optional
from scraper import Bidder, ConsultationData

EXCESSIVE_THRESHOLD = 1.20   # > 20 % above estimate
LOW_THRESHOLD = 0.75         # > 25 % below estimate  (100 % - 25 % = 75 %)


@dataclass
class RankedBidder:
    position: int
    name: str
    price: Optional[float]
    distance_to_ref: Optional[float]   # |price - P|
    side: str                           # "below" | "above" | "N/A"
    admin_status: str
    financial_status: str
    is_eligible: bool
    note: str


def calculate_winners(
    data: ConsultationData,
) -> tuple[list[RankedBidder], str, Optional[float]]:
    """
    Returns (ranked_bidders, method_description, reference_price).
    ranked_bidders is sorted: eligible winners first, then eliminated last.
    """
    estimated = data.estimated_price

    # ── 1. Admin-rejected bidders ─────────────────────────────────────────────
    admin_rejected = [b for b in data.bidders if not b.is_eligible]
    admin_ok = [b for b in data.bidders if b.is_eligible]

    # ── 2 & 3. Price-range filters ────────────────────────────────────────────
    valid: list[Bidder] = []
    price_rejected: list[tuple[Bidder, str]] = []

    for b in admin_ok:
        if b.price is None:
            price_rejected.append((b, "no price"))
            continue
        if estimated:
            if b.price > estimated * EXCESSIVE_THRESHOLD:
                pct = (b.price / estimated - 1) * 100
                price_rejected.append((b, f"Excessive (+{pct:.1f}% > 20%)"))
                continue
            if b.price < estimated * LOW_THRESHOLD:
                pct = (1 - b.price / estimated) * 100
                price_rejected.append((b, f"Abnormally low (-{pct:.1f}% > 25%)"))
                continue
        valid.append(b)

    # ── 4. Reference price ────────────────────────────────────────────────────
    reference_price: Optional[float] = None
    if valid and estimated:
        avg = sum(b.price for b in valid) / len(valid)
        reference_price = (estimated + avg) / 2

    method = "reference_price"

    # ── 5. Rank valid bidders ─────────────────────────────────────────────────
    below = [b for b in valid if reference_price is not None and b.price <= reference_price]
    above = [b for b in valid if reference_price is not None and b.price > reference_price]
    no_ref = [b for b in valid if reference_price is None]

    # Sort: below → descending price (highest = closest to P); above → ascending
    below.sort(key=lambda b: b.price, reverse=True)
    above.sort(key=lambda b: b.price)

    if below:
        ordered = below + above
    elif above:
        ordered = above            # fallback: no offers below P
    else:
        ordered = no_ref           # no estimated price available

    ranked: list[RankedBidder] = []
    for pos, b in enumerate(ordered, start=1):
        dist = abs(b.price - reference_price) if reference_price else None
        side = (
            "below" if reference_price and b.price <= reference_price
            else "above" if reference_price else "N/A"
        )
        note = ""
        if pos == 1:
            note = "Winner"
        ranked.append(
            RankedBidder(
                position=pos,
                name=b.name,
                price=b.price,
                distance_to_ref=round(dist, 2) if dist is not None else None,
                side=side,
                admin_status=b.admin_status,
                financial_status=b.financial_status,
                is_eligible=True,
                note=note,
            )
        )

    # ── Append eliminated bidders (price-range) ───────────────────────────────
    elim_start = len(ranked) + 1
    for i, (b, reason) in enumerate(price_rejected):
        ranked.append(
            RankedBidder(
                position=elim_start + i,
                name=b.name,
                price=b.price,
                distance_to_ref=None,
                side="N/A",
                admin_status=b.admin_status,
                financial_status=b.financial_status,
                is_eligible=False,
                note=f"Eliminated — {reason}",
            )
        )

    # ── Append admin-rejected bidders ─────────────────────────────────────────
    elim2_start = len(ranked) + 1
    for i, b in enumerate(admin_rejected):
        reason_parts = []
        if _norm(b.admin_status) not in ("admissible", ""):
            reason_parts.append(f"admin: {b.admin_status}")
        if _norm(b.financial_status) not in ("admissible", "ouverte", ""):
            reason_parts.append(f"financial: {b.financial_status}")
        ranked.append(
            RankedBidder(
                position=elim2_start + i,
                name=b.name,
                price=b.price,
                distance_to_ref=None,
                side="N/A",
                admin_status=b.admin_status,
                financial_status=b.financial_status,
                is_eligible=False,
                note="Eliminated — " + "; ".join(reason_parts) if reason_parts else "Eliminated",
            )
        )

    return ranked, method, reference_price


def _norm(s: str) -> str:
    return (
        s.strip().lower()
        .replace("é", "e").replace("è", "e")
        .replace("ê", "e").replace("â", "a")
    )
