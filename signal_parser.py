"""Parse signal messages for large transfer / withdraw amounts."""
import re
from typing import Optional, Tuple

# Match numbers like: 15M, 15m, 15 million, 15,000,000, 15000000, 15.5M, $20M
_AMOUNT_RE = re.compile(
    r"""
    (?:\$)?\s*
    (\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)
    \s*
    (million|m|مليون|ملين)?
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_amount_usd(text: str) -> Optional[float]:
    """Return largest USD amount found. Supports millions shorthand."""
    if not text:
        return None
    best = 0.0
    for m in _AMOUNT_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("million", "m", "مليون", "ملين") or (unit == "" and val < 1000 and "m" in text.lower()):
            # if unit is million OR bare number next to M context handled by unit group
            if unit in ("million", "m", "مليون", "ملين"):
                val *= 1_000_000
        elif unit == "" and val >= 1_000_000:
            pass  # already full dollars
        elif unit == "" and val < 1000:
            # ambiguous small number without unit — skip
            continue
        if val > best:
            best = val
    # also catch patterns like 15M without space
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[Mmم]", text):
        try:
            val = float(m.group(1)) * 1_000_000
            if val > best:
                best = val
        except ValueError:
            pass
    return best if best > 0 else None


def detect_action(text: str, sell_keywords: str, buy_keywords: str) -> Optional[str]:
    """
    Return 'sell' | 'buy' | None based on keywords.
    Sell = large transfer TO exchange / sent
    Buy  = withdraw FROM exchange / empty wallets outflow
    """
    t = (text or "").lower()
    sells = [k.strip().lower() for k in (sell_keywords or "").split(",") if k.strip()]
    buys = [k.strip().lower() for k in (buy_keywords or "").split(",") if k.strip()]

    sell_hit = any(k in t for k in sells)
    buy_hit = any(k in t for k in buys)

    # prefer more specific if both
    if sell_hit and not buy_hit:
        return "sell"
    if buy_hit and not sell_hit:
        return "buy"
    if sell_hit and buy_hit:
        # heuristic: withdraw/from wins as buy, else sell
        if any(k in t for k in ("withdraw", "withdrew", "from ", "سحب", "من ")):
            return "buy"
        return "sell"
    return None


def evaluate_signal(
    text: str,
    sell_threshold_m: float,
    buy_threshold_m: float,
    sell_keywords: str,
    buy_keywords: str,
) -> Tuple[Optional[str], Optional[float], str]:
    """
    Returns (action, amount_usd, reason).
    action: 'sell' | 'buy' | None
    """
    amount = parse_amount_usd(text)
    action = detect_action(text, sell_keywords, buy_keywords)
    if amount is None or action is None:
        return None, amount, "لم يُستخرج مبلغ أو نوع إشارة واضح"

    threshold = sell_threshold_m if action == "sell" else buy_threshold_m
    threshold_usd = threshold * 1_000_000

    if amount < threshold_usd:
        return None, amount, f"المبلغ {amount:,.0f}$ أقل من الحد {threshold}M"

    return action, amount, f"إشارة {action} | المبلغ ≈ {amount:,.0f}$ ≥ {threshold}M"
