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
        # Always include USDT price = 1
        prices[self.quote] = 1.0
        return prices

    def get_portfolio_value(self) -> Dict:
        """
        Returns:
        {
            'total_usdt': float,
            'assets': {
                'BTC': {'amount': x, 'usdt_value': y, 'percent': z},
                ...
            }
        }
        """
        balances = self.get_balance()
        if not balances:
            return {'total_usdt': 0.0, 'assets': {}}

        # Get prices for all non-quote assets
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


    def buy_with_usdt(self, symbol: str, usdt_amount: float) -> dict:
        """Market buy using approximate USDT amount. symbol like BTC/USDT"""
        price = self.get_ticker_price(symbol)
        if price <= 0:
            raise Exception("Invalid price")
        amount = (usdt_amount * 0.997) / price  # buffer for fees
        if amount <= 0:
            raise Exception("Amount too small")
        order = self.exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount
        )
        return order

    def sell_all_of(self, asset: str) -> dict:
        """Sell entire free balance of an asset to USDT"""
        bal = self.get_balance()
        amount = float(bal.get(asset, 0.0) or 0)
        if amount <= 0:
            return None
        amount = amount * 0.999
        return self.create_market_order(f"{asset}/{self.quote}", "sell", amount)

    def get_markets(self) -> List[str]:
        """Return list of available base assets that have /USDT pair"""
        markets = self.exchange.load_markets()
        bases = []
        for symbol, market in markets.items():
            if market.get('quote') == self.quote and market.get('active', True) and market.get('spot', True):
                bases.append(market['base'])
        return sorted(set(bases))
