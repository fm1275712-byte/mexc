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

    def get_balance(self) -> Dict:
        """Return free balances only (non-zero)"""
        balance = self.exchange.fetch_balance()
        free = {}
        for asset, amount in balance.get('free', {}).items():
            if amount and float(amount) > 0:
                free[asset] = float(amount)
        return free

    def get_free_usdt(self) -> float:
        bal = self.get_balance()
        return float(bal.get(self.quote, 0.0))

    def get_ticker_price(self, symbol: str) -> float:
        """symbol like BTC/USDT"""
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker['last'])

    def get_all_prices(self, symbols: List[str]) -> Dict[str, float]:
        """symbols = ['BTC', 'ETH'] -> prices in USDT"""
        prices = {}
        for s in symbols:
            pair = f"{s}/{self.quote}"
            try:
                prices[s] = self.get_ticker_price(pair)
            except Exception:
                prices[s] = 0.0
        prices[self.quote] = 1.0
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
        """Value of specific coins only (for a virtual portfolio)."""
        balances = self.get_balance()
        prices = self.get_all_prices(symbols)
        total = 0.0
        details = {}
        for s in symbols:
            amount = float(balances.get(s, 0.0))
            price = prices.get(s, 0.0)
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
