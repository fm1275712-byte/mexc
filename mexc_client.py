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
        balance = self.exchange.fetch_balance()
        free = {}
        for asset, amount in balance.get('free', {}).items():
            if amount and float(amount) > 0:
                free[asset] = float(amount)
        return free

    def get_ticker_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker['last'])

    def get_all_prices(self, symbols: List[str]) -> Dict[str, float]:
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
        return {'total_usdt': total_usdt, 'assets': result_assets}

    def create_market_order(self, symbol: str, side: str, amount: float) -> Optional[dict]:
        try:
            markets = self.exchange.load_markets()
            if symbol in markets:
                amount = float(self.exchange.amount_to_precision(symbol, amount))
            if amount <= 0:
                raise Exception("Amount too small after precision")
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
        if usdt_amount < 1:
            raise Exception(f"Amount too small: {usdt_amount}$")
        price = self.get_ticker_price(symbol)
        if price <= 0:
            raise Exception("Invalid price")
        amount = (usdt_amount * 0.995) / price
        markets = self.exchange.load_markets()
        market = markets.get(symbol)
        if market:
            amount = float(self.exchange.amount_to_precision(symbol, amount))
            min_amount = (market.get('limits') or {}).get('amount', {}).get('min') or 0
            if min_amount and amount < float(min_amount):
                raise Exception(f"Below min amount ({min_amount})")
            min_cost = (market.get('limits') or {}).get('cost', {}).get('min') or 1
            if usdt_amount < float(min_cost):
                raise Exception(f"Below min notional ({min_cost}$)")
        if amount <= 0:
            raise Exception("Amount too small after precision")
        order = self.exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount
        )
        return order

    def sell_all_of(self, asset: str) -> Optional[dict]:
        bal = self.get_balance()
        amount = float(bal.get(asset, 0.0) or 0)
        if amount <= 0:
            return None
        amount = amount * 0.999
        return self.create_market_order(f"{asset}/{self.quote}", "sell", amount)

    def get_markets(self) -> List[str]:
        markets = self.exchange.load_markets()
        bases = []
        for symbol, market in markets.items():
            if market.get('quote') == self.quote and market.get('active', True) and market.get('spot', True):
                bases.append(market['base'])
        return sorted(set(bases))
