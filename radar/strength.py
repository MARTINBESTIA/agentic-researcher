"""Sila dôkazov hypotézy: deterministicky z počtu overených tvrdení a rôznych domén (nie úsudkom modelu)."""

from __future__ import annotations

STRENGTH_ORDER = {"silnejšia": 2, "stredná": 1, "slabá": 0}
# Tvrdenia, ktoré problém nedokladajú: protidôkaz a existujúce riešenie (konkurencia).
NOT_SUPPORTING = {"counter": "protidôkaz", "existing_solution": "existujúce riešenie"}


def is_vendor(e) -> bool:
    """Citát z webu dodávateľa (marketing vlastného produktu) – nie je nezávislý dôkaz problému."""
    return "source_type" in e.keys() and e["source_type"] == "vendor"


def evidence_strength(evidence: list) -> tuple[str, int, int, int]:
    """(štítok, overené, čiastočné, rôzne domény overených tvrdení). Počítajú sa len tvrdenia,
    ktoré problém dokladajú – protidôkazy, existujúce riešenia a citáty dodávateľov silu nezvyšujú."""
    evidence = [e for e in evidence if e["claim_type"] not in NOT_SUPPORTING and not is_vendor(e)]
    verified = [e for e in evidence if e["status"] == "verified"]
    partial = [e for e in evidence if e["status"] == "partial"]
    domains = {e["domain"] for e in verified}
    if len(verified) >= 4 and len(domains) >= 3:
        label = "silnejšia"
    elif len(verified) >= 2 and len(domains) >= 2:
        label = "stredná"
    else:
        label = "slabá"
    return label, len(verified), len(partial), len(domains)


def strength_at_least(label: str, minimum: str) -> bool:
    return STRENGTH_ORDER.get(label, 0) >= STRENGTH_ORDER.get(minimum, 0)
