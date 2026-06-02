"""
Moroccan Public Procurement — Simplified Open Tender (AOO Simplifié)
Winner calculation per Article 13 of the RC and Decree n°2-22-431 (08 mars 2023).

Steps:
1. Keep only bidders with a submitted price.
2. Compute reference price: P = (E + mean(priced_offers)) / 2
3. Rank:
   - Priority 1: offers ≤ P  → sorted descending by price (closest to P from below)
   - Priority 2: offers >  P → sorted ascending by price  (closest to P from above)
   Only if there are zero offers ≤ P do we fall back to Priority 2.
"""

from dataclasses import dataclass
from typing import Optional
from scraper import Bidder, ConsultationData

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

    # ── 1. Only priced offers enter the calculation ───────────────────────────
    priced = [b for b in data.bidders if b.price is not None]
    no_price = [b for b in data.bidders if b.price is None]

    # ── 2. Reference price ────────────────────────────────────────────────────
    reference_price: Optional[float] = None
    if priced and estimated:
        avg = sum(b.price for b in priced) / len(priced)
        reference_price = (estimated + avg) / 2

    method = "reference_price"

    # ── 3. Rank priced bidders ────────────────────────────────────────────────
    below = [b for b in priced if reference_price is not None and b.price <= reference_price]
    above = [b for b in priced if reference_price is not None and b.price > reference_price]
    no_ref = sorted([b for b in priced if reference_price is None], key=lambda b: b.price)

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
    winning_price = ordered[0].price if ordered and reference_price is not None else None
    for pos, b in enumerate(ordered, start=1):
        dist = abs(b.price - reference_price) if reference_price else None
        side = (
            "below" if reference_price and b.price <= reference_price
            else "above" if reference_price else "N/A"
        )
        note = ""
        if winning_price is not None and b.price == winning_price:
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
                is_eligible=reference_price is not None,
                note=note,
            )
        )

    # ── Append bidders without a submitted price ──────────────────────────────
    elim2_start = len(ranked) + 1
    for i, b in enumerate(no_price):
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
                note="Eliminated — no price",
            )
        )

    return ranked, method, reference_price


def _norm(s: str) -> str:
    return (
        s.strip().lower()
        .replace("é", "e").replace("è", "e")
        .replace("ê", "e").replace("â", "a")
    )
