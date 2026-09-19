import ccxt
import config
from typing import Dict, List, Optional


class MexcClient:
    def __init__(self):
        self.exchange = ccxt.mexc({
            'apiKey': config.MEXC_API_KEY,
            'secret': config.MEXC_API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'recvWindow': 10000,
            }
        })
        self.quote = config.QUOTE_ASSET

    @staticmethod
    def normalize_asset_symbol(symbol: str) -> str:
        """Normalize a wallet asset or a configured trading symbol to its base asset."""
        value = str(symbol or "").strip().upper().replace(" ", "")
        value = value.lstrip("$")
        for separator in ("/", ":", "-"):
            if separator in value:
                value = value.split(separator, 1)[0]
                break
        return value

    @staticmethod
    def _as_float(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def get_balance(self) -> Dict:
        """Return free balances only (non-zero)"""
        balance = self.exchange.fetch_balance()
        free = {}
        for asset, amount in balance.get('free', {}).items():
            value = self._as_float(amount)
            if value > 0:
                key = self.normalize_asset_symbol(asset)
                free[key] = max(free.get(key, 0.0), value)
        return free

    def get_total_balance(self) -> Dict[str, float]:
        """Return total balances, including amounts locked in open orders."""
        balance = self.exchange.fetch_balance()
        totals = balance.get("total") or {}
        free = balance.get("free") or {}
        result = {}
        for asset in set(totals) | set(free):
            # Some exchange responses expose a zero/empty total while free
            # already contains the actual available amount. Use the larger
            # value so a present wallet asset is not reported as missing.
            value = max(
                self._as_float(totals.get(asset)),
                self._as_float(free.get(asset)),
            )
            if value > 0:
                key = self.normalize_asset_symbol(asset)
                result[key] = max(result.get(key, 0.0), value)
        return result

    def get_portfolio_presence(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """Return whether each configured coin exists in the wallet.

        Total balance is intentional here: a coin locked in an open sell order
        is still owned and must not be offered for re-entry.

        A small residual amount can remain after a market sell because the
        rebalancer keeps a safety buffer for fees and exchange precision.
        That dust should not make a sold portfolio coin look present.
        """
        requested = [str(symbol).upper().strip() for symbol in symbols if symbol]
        normalized = list(dict.fromkeys(
            self.normalize_asset_symbol(symbol) for symbol in requested
        ))
        balances = self.get_total_balance()
        prices = self.get_all_prices(normalized)
        result = {}
        minimum_value = max(
            0.0,
            float(getattr(config, "BALANCE_PRESENCE_MIN_USDT", 1.0)),
        )
        for requested_symbol in requested:
            asset = self.normalize_asset_symbol(requested_symbol)
            amount = float(balances.get(asset, 0.0))
            price = float(prices.get(asset, 0.0))
            market_value = amount * price if price > 0 else 0.0
            # If the ticker is unavailable, do not classify an asset as
            # missing just because its value cannot be calculated.
            present = amount > 0 and (
                price <= 0 or market_value >= minimum_value
            )
            info = {
                "amount": amount,
                "price": price,
                "market_value": market_value,
                "present": present,
            }
            # Keep the requested key for the bot UI, and the normalized alias
            # for callers that already use base symbols.
            result[requested_symbol] = info
            result[asset] = info
        return result

    def get_free_usdt(self) -> float:
        bal = self.get_balance()
        return float(bal.get(self.quote, 0.0))

    def get_ticker_price(self, symbol: str) -> float:
        """symbol like BTC/USDT"""
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker['last'])

    def get_all_prices(self, symbols: List[str]) -> Dict[str, float]:
        """symbols = ['BTC', 'ETH'] -> prices in USDT.
        Uses batch fetch_tickers when possible for much higher speed.
        """
        prices: Dict[str, float] = {self.quote: 1.0}
        if not symbols:
            return prices

        unique = list(dict.fromkeys(
            self.normalize_asset_symbol(s) for s in symbols if s
        ))
        pairs = [f"{s}/{self.quote}" for s in unique]

        # Batch request — dramatically faster than sequential fetch_ticker
        try:
            tickers = self.exchange.fetch_tickers(pairs)
            for s, pair in zip(unique, pairs):
                t = tickers.get(pair) or {}
                last = t.get("last") or t.get("close")
                prices[s] = float(last) if last is not None else 0.0
        except Exception:
            # Fallback to sequential if batch fails
            for s in unique:
                pair = f"{s}/{self.quote}"
                try:
                    prices[s] = self.get_ticker_price(pair)
                except Exception:
                    prices[s] = 0.0
        return prices

    def get_portfolio_value(self) -> Dict:
        """
        Returns full account value.
        {
            'total_usdt': float,
            'assets': {
                'BTC': {'amount': x, 'usdt_value': y, 'percent': z, 'price': p},
                ...
            }
        }
        """
        balances = self.get_balance()
        if not balances:
            return {'total_usdt': 0.0, 'assets': {}}

        assets = [a for a in balances.keys() if a != self.quote]
        prices = self.get_all_prices(assets)

        total_usdt = 0.0
        result_assets = {}

        for asset, amount in balances.items():
            price = prices.get(asset, 0.0) if asset != self.quote else 1.0
            usdt_value = amount * price
            total_usdt += usdt_value
            result_assets[asset] = {
                'amount': amount,
                'price': price,
                'usdt_value': usdt_value,
                'percent': 0.0
            }

        if total_usdt > 0:
            for asset in result_assets:
                result_assets[asset]['percent'] = (result_assets[asset]['usdt_value'] / total_usdt) * 100

        return {
            'total_usdt': total_usdt,
            'assets': result_assets
        }

    def get_coins_value(self, symbols: List[str]) -> Dict:
        """Value of specific coins only (for a virtual portfolio).

        Uses total balance (free + locked in open orders) so that coins
        sitting in TP limit-sell orders are still counted correctly.
        """
        balances = self.get_total_balance()
        prices = self.get_all_prices(symbols)
        total = 0.0
        details = {}
        for s in symbols:
            key = self.normalize_asset_symbol(s)
            amount = float(balances.get(key, 0.0) or balances.get(s, 0.0))
            price = float(prices.get(key, 0.0) or prices.get(s, 0.0))
            usdt_value = amount * price
            total += usdt_value
            details[s] = {
                'amount': amount,
                'price': price,
                'usdt_value': usdt_value
            }
        return {'total_usdt': total, 'assets': details}

    def create_market_order(self, symbol: str, side: str, amount: float) -> Optional[dict]:
        """
        symbol: BTC/USDT
        side: buy or sell
        amount: base currency amount
        """
        try:
            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=amount
            )
            return order
        except Exception as e:
            raise Exception(f"Order failed: {str(e)}")

    def create_market_buy_usdt(self, symbol: str, usdt_amount: float) -> Optional[dict]:
        """Buy using quote amount (USDT). Tries create_order with cost, falls back to amount calculation."""
        pair = f"{symbol}/{self.quote}"
        try:
            # Prefer cost-based if supported
            order = self.exchange.create_order(
                symbol=pair,
                type='market',
                side='buy',
                amount=None,
                params={'cost': usdt_amount}
            )
            return order
        except Exception:
            # Fallback: calculate amount from price
            price = self.get_ticker_price(pair)
            if price <= 0:
                raise Exception(f"Cannot get price for {pair}")
            amount = (usdt_amount * 0.998) / price   # small buffer for fees
            return self.create_market_order(pair, 'buy', amount)

    def get_markets(self) -> List[str]:
        """Return list of available base assets that have /USDT pair"""
        markets = self.exchange.load_markets()
        bases = []
        for symbol, market in markets.items():
            if market.get('quote') == self.quote and market.get('active', True) and market.get('spot', True):
                bases.append(market['base'])
        return sorted(set(bases))

    def create_limit_sell(self, symbol: str, amount: float, price: float) -> Optional[dict]:
        """Place a limit sell order (used for Take Profit visible on MEXC).

        Returns None (without raising) when the amount is below the exchange
        minimum precision so tiny dust positions do not spam errors.
        """
        pair = f"{symbol}/{self.quote}"
        try:
            # Ensure markets are loaded for precision checks
            if not getattr(self.exchange, "markets", None):
                self.exchange.load_markets()
            market = self.exchange.market(pair)
            min_amount = float((market.get("limits") or {}).get("amount", {}).get("min") or 0)
            precision_amount = market.get("precision", {}).get("amount")
            # Round down to exchange precision
            amount = float(self.exchange.amount_to_precision(pair, amount))
            price = float(self.exchange.price_to_precision(pair, price))
            if amount <= 0 or price <= 0:
                return None
            if min_amount > 0 and amount < min_amount:
                return None
            # Some MEXC pairs treat precision as minimum step
            if precision_amount is not None:
                try:
                    step = float(precision_amount)
                    if 0 < step < 1 and amount < step:
                        return None
                except (TypeError, ValueError):
                    pass
            order = self.exchange.create_order(
                symbol=pair,
                type='limit',
                side='sell',
                amount=amount,
                price=price,
            )
            return order
        except Exception as e:
            msg = str(e).lower()
            # Treat precision / minimum amount errors as skippable
            if any(x in msg for x in ("minimum amount", "min amount", "precision", "too small")):
                return None
            raise Exception(f"Limit sell failed for {pair}: {str(e)}")

    def cancel_order(self, order_id: str, symbol: str, strict: bool = False) -> Optional[dict]:
        """Cancel an open order by id."""
        pair = f"{symbol}/{self.quote}" if "/" not in symbol else symbol
        try:
            return self.exchange.cancel_order(order_id, pair)
        except Exception as e:
            # Order may already be filled/cancelled
            if strict:
                raise
            return None

    def fetch_open_orders(self, symbol: str = None) -> List[dict]:
        """Fetch open orders, optionally filtered by symbol."""
        try:
            if symbol:
                pair = f"{symbol}/{self.quote}" if "/" not in symbol else symbol
                return self.exchange.fetch_open_orders(pair)
            return self.exchange.fetch_open_orders()
        except Exception:
            return []

    def fetch_open_sell_orders(self, symbol: str) -> List[dict]:
        """Fetch only open sell orders for a base asset."""
        return [
            order for order in self.fetch_open_orders(symbol)
            if str(order.get("side") or "").lower() == "sell"
        ]

    def fetch_order(self, order_id: str, symbol: str) -> Optional[dict]:
        """Fetch a single order status."""
        pair = f"{symbol}/{self.quote}" if "/" not in symbol else symbol
        try:
            return self.exchange.fetch_order(order_id, pair)
        except Exception:
            return None

    def get_free_amount(self, symbol: str) -> float:
        """Free balance of a base asset."""
        bal = self.get_balance()
        key = self.normalize_asset_symbol(symbol)
        return float(bal.get(key, 0.0) or bal.get(symbol, 0.0))

    def get_total_amount(self, symbol: str) -> float:
        """Total balance (free + locked in open orders) of a base asset."""
        bal = self.get_total_balance()
        key = self.normalize_asset_symbol(symbol)
        return float(bal.get(key, 0.0) or bal.get(symbol, 0.0))

    def cancel_all_open_sells(self, symbol: str) -> Dict:
        """Cancel every open sell order for a symbol on the exchange."""
        result = {"cancelled": [], "errors": []}
        try:
            orders = self.fetch_open_sell_orders(symbol)
            for order in orders:
                oid = order.get("id")
                if not oid:
                    continue
                try:
                    self.cancel_order(str(oid), symbol, strict=False)
                    result["cancelled"].append(str(oid))
                except Exception as exc:
                    result["errors"].append({"order_id": str(oid), "error": str(exc)})
        except Exception as exc:
            result["errors"].append({"error": str(exc)})
        return result
