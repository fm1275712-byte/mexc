"""Parse signal messages — especially Arkham AlertBot and whale transfer alerts."""
import re
from typing import Optional, Tuple

# Match amounts: $5,097,428.00 | ($33,514,569.61) | 15M | 15 million | 15000000
_AMOUNT_RE = re.compile(
    r"""
    (?:\$|\(\$)?\s*
    (\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (million|m|مليون|ملين)?
    \)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Exchange names commonly appearing in Arkham "From:" / "To:" lines
_EXCHANGES = (
    "binance", "coinbase", "okx", "okex", "bybit", "bitget", "kucoin",
    "mexc", "gate", "gate.io", "huobi", "htx", "kraken", "bitfinex",
    "crypto.com", "gemini", "bitstamp", "upbit", "bithumb", "bitmex",
    "deribit", "bitflyer", "poloniex", "exchange", "cex",
)


def parse_amount_usd(text: str) -> Optional[float]:
    """Return largest USD amount found. Supports millions shorthand and $X,XXX format."""
    if not text:
        return None
    best = 0.0

    # Explicit ($X) or $X patterns — preferred for Arkham "Value: ... ($N)"
    for m in re.finditer(
        r"\(\s*\$\s*([\d,]+(?:\.\d+)?)\s*\)|\$\s*([\d,]+(?:\.\d+)?)",
        text,
    ):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        try:
            val = float(raw)
            if val > best:
                best = val
        except ValueError:
            pass

    for m in _AMOUNT_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("million", "m", "مليون", "ملين"):
            val *= 1_000_000
        elif unit == "" and val < 1000:
            # ambiguous small number without unit — skip unless already captured via $
            continue
        if val > best:
            best = val

    # 15M / 20m without space
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[Mmم]", text):
        try:
            val = float(m.group(1)) * 1_000_000
            if val > best:
                best = val
        except ValueError:
            pass

    return best if best > 0 else None


def _line_has_exchange(line: str) -> bool:
    t = line.lower()
    return any(ex in t for ex in _EXCHANGES)


def detect_action_arkham(text: str) -> Optional[str]:
    """
    Arkham format:
      From: Entity / Wallet
      To:   Entity / Wallet
      Value: ... ($N)

    Rules (spot portfolio logic):
      - To exchange   → sell pressure  → action 'sell'
      - From exchange → outflow / buy pressure → action 'buy'
      - Both or neither → fall through to keyword detection
    """
    from_line = ""
    to_line = ""
    for line in (text or "").splitlines():
        low = line.strip().lower()
        if low.startswith("from:"):
            from_line = low
        elif low.startswith("to:"):
            to_line = low

    if not from_line and not to_line:
        # sometimes "From X To Y" on one line
        m = re.search(r"from\s*[:\-]?\s*(.+?)\s+to\s*[:\-]?\s*(.+)", text or "", re.I | re.S)
        if m:
            from_line = "from: " + m.group(1).lower()
            to_line = "to: " + m.group(2).lower()

    to_ex = _line_has_exchange(to_line) if to_line else False
    from_ex = _line_has_exchange(from_line) if from_line else False

    if to_ex and not from_ex:
        return "sell"
    if from_ex and not to_ex:
        return "buy"
    if to_ex and from_ex:
        # exchange → exchange: treat as sell (inflow side) if To has deposit keyword
        if any(k in to_line for k in ("deposit", "hot wallet", "cold wallet", "hotwallet")):
            return "sell"
        if any(k in from_line for k in ("deposit", "hot wallet", "cold wallet", "hotwallet")):
            return "buy"
        return "sell"
    return None


def detect_action(text: str, sell_keywords: str, buy_keywords: str) -> Optional[str]:
    """
    Return 'sell' | 'buy' | None.
    1) Try Arkham From/To exchange logic first.
    2) Fall back to configured keyword lists.
    """
    arkham = detect_action_arkham(text)
    if arkham:
        return arkham

    t = (text or "").lower()
    sells = [k.strip().lower() for k in (sell_keywords or "").split(",") if k.strip()]
    buys = [k.strip().lower() for k in (buy_keywords or "").split(",") if k.strip()]

    # Extra Arkham-friendly defaults always checked
    sells += [
        "to binance", "to coinbase", "to okx", "to bybit", "to bitget",
        "to exchange", "to mexc", "to kucoin", "to gate", "to upbit",
        "moving to", "sent to", "transfer to", "deposit to",
    ]
    buys += [
        "from binance", "from coinbase", "from okx", "from bybit", "from bitget",
        "from exchange", "from mexc", "from kucoin", "from gate", "from upbit",
        "withdrew", "withdraw", "outflow",
    ]

    sell_hit = any(k in t for k in sells)
    buy_hit = any(k in t for k in buys)

    if sell_hit and not buy_hit:
        return "sell"
    if buy_hit and not sell_hit:
        return "buy"
    if sell_hit and buy_hit:
        if any(k in t for k in ("withdraw", "withdrew", "from ", "سحب", "من ", "outflow")):
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

    if amount is None and action is None:
        return None, None, "لم يُستخرج مبلغ أو نوع إشارة واضح"
    if action is None:
        return None, amount, f"مبلغ ≈ {amount:,.0f}$ لكن نوع الإشارة غير واضح (From/To أو كلمات البيع/الشراء)"
    if amount is None:
        return None, None, f"نوع الإشارة = {action} لكن لم يُستخرج مبلغ بالدولار"

    threshold = sell_threshold_m if action == "sell" else buy_threshold_m
    threshold_usd = threshold * 1_000_000

    if amount < threshold_usd:
        return None, amount, f"المبلغ {amount:,.0f}$ أقل من الحد {threshold}M"

    direction = "إلى بورصة (ضغط بيع)" if action == "sell" else "من بورصة (ضغط شراء)"
    return action, amount, f"إشارة {action} | {direction} | المبلغ ≈ {amount:,.0f}$ ≥ {threshold}M"


# Quick self-check examples (not run on import)
if __name__ == "__main__":
    samples = [
        """⚡USDC/USDT >3MM ETH PUMP 📈💰 ALERT⚡
From: Circle: Hot Wallet (0x55F)
To: Wintermute: Coinbase Deposit (0x459)
Value: 5,097,428.000000 USD Coin ($5,097,428.00)
Network: Ethereum
Time: 2025-05-27 12:00:23 UTC""",
        """🚨 WINTERMUTE MOVING BTC TO BINANCE
From: Wintermute : Binance Deposit (1KbDE)
To: Binance : Hot Wallet (bc1qm)
Value: 420.899827 BTC ($33,514,569.61)
Network: Bitcoin""",
        """From: Binance: Hot Wallet
To: Unknown Wallet
Value: ($22,000,000.00)""",
        "BlackRock sent 20M BTC to Coinbase",
        "withdrew 18 million from exchange",
    ]
    for s in samples:
        print("---")
        print(evaluate_signal(s, 5.0, 5.0, "sent,transfer", "withdraw,from exchange"))
