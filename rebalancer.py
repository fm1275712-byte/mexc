from typing import Dict, List
from mexc_client import MexcClient


class Rebalancer:
    def __init__(self, client: MexcClient):
        self.client = client
        self.quote = client.quote

    def calculate_targets(self, coins: List[str], method: str = "equal") -> Dict[str, float]:
        if not coins:
            return {}
        pct = 100.0 / len(coins)
        return {c: pct for c in coins}

    def start_portfolio(
        self,
        coins: List[str],
        total_usdt: float,
        method: str = "equal",
        min_trade_usdt: float = 5.0,
        dry_run: bool = False
    ) -> Dict:
        """Buy coins using up to total_usdt only (respects allocated capital)."""
        results = {
            'action': 'start',
            'total_usdt': total_usdt,
            'executed': [],
            'errors': [],
            'dry_run': dry_run
        }

        free_usdt = self.client.get_free_usdt()
        if free_usdt < total_usdt:
            results['errors'].append(
                f"رصيد USDT الحر غير كافٍ. المتاح: `{free_usdt:.2f}$` | المطلوب: `{total_usdt:.2f}$`"
            )
            return results

        targets = self.calculate_targets(coins, method)
        for coin, pct in targets.items():
            usdt_for_coin = total_usdt * (pct / 100.0)
            if usdt_for_coin < min_trade_usdt:
                results['errors'].append(f"`{coin}`: المبلغ صغير جداً ({usdt_for_coin:.2f}$)")
                continue
            try:
                if dry_run:
                    results['executed'].append({
                        'symbol': f"{coin}/{self.quote}",
                        'side': 'buy',
                        'usdt': usdt_for_coin,
                        'status': 'dry_run'
                    })
                else:
                    order = self.client.create_market_buy_usdt(coin, usdt_for_coin)
                    results['executed'].append({
                        'symbol': f"{coin}/{self.quote}",
                        'side': 'buy',
                        'usdt': usdt_for_coin,
                        'status': 'filled',
                        'order_id': order.get('id') if order else None
                    })
            except Exception as e:
                results['errors'].append({coin: str(e)})

        return results

    def stop_portfolio(self, coins: List[str], dry_run: bool = False) -> Dict:
        """Sell all holdings of the given coins. Does not delete portfolio."""
        results = {
            'action': 'stop',
            'executed': [],
            'errors': [],
            'dry_run': dry_run,
            'total_sold_usdt': 0.0
        }

        balances = self.client.get_balance()
        prices = self.client.get_all_prices(coins)

        for coin in coins:
            amount = float(balances.get(coin, 0.0))
            if amount <= 0:
                continue
            amount = amount * 0.999
            usdt_value = amount * prices.get(coin, 0.0)
            try:
                if dry_run:
                    results['executed'].append({
                        'symbol': f"{coin}/{self.quote}",
                        'side': 'sell',
                        'amount': amount,
                        'usdt': usdt_value,
                        'status': 'dry_run'
                    })
                    results['total_sold_usdt'] += usdt_value
                else:
                    order = self.client.create_market_order(
                        symbol=f"{coin}/{self.quote}",
                        side='sell',
                        amount=amount
                    )
                    results['executed'].append({
                        'symbol': f"{coin}/{self.quote}",
                        'side': 'sell',
                        'amount': amount,
                        'usdt': usdt_value,
                        'status': 'filled',
                        'order_id': order.get('id') if order else None
                    })
                    results['total_sold_usdt'] += usdt_value
            except Exception as e:
                results['errors'].append({coin: str(e)})

        return results

    def rebalance_portfolio(
        self,
        coins: List[str],
        target_capital: float,
        method: str = "equal",
        threshold: float = 2.0,
        min_trade_usdt: float = 5.0,
        dry_run: bool = False
    ) -> Dict:
        """Rebalance only within the portfolio coins / allocated capital."""
        targets = self.calculate_targets(coins, method)
        current = self.client.get_coins_value(coins)
        current_total = current['total_usdt']
        base = max(target_capital, current_total)

        results = {
            'action': 'rebalance',
            'targets': targets,
            'current_total': current_total,
            'target_capital': target_capital,
            'executed': [],
            'errors': [],
            'dry_run': dry_run
        }

        if current_total <= 0:
            results['message'] = "المحفظة فارغة. استخدم **تشغيل الاستراتيجية** أولاً."
            return results

        orders = []
        for coin, target_pct in targets.items():
            target_usdt = base * (target_pct / 100.0)
            current_usdt = current['assets'].get(coin, {}).get('usdt_value', 0.0)
            delta = target_usdt - current_usdt

            if abs(delta) < min_trade_usdt:
                continue
            if current_total > 0:
                current_pct = (current_usdt / current_total) * 100
                if abs(current_pct - target_pct) < threshold:
                    continue

            price = current['assets'].get(coin, {}).get('price', 0.0)
            if price <= 0:
                continue

            amount = abs(delta) / price
            side = 'buy' if delta > 0 else 'sell'

            if side == 'sell':
                available = current['assets'].get(coin, {}).get('amount', 0.0)
                amount = min(amount, available * 0.999)

            if amount <= 0:
                continue

            orders.append({
                'symbol': f"{coin}/{self.quote}",
                'side': side,
                'amount': amount,
                'usdt': abs(delta),
                'coin': coin
            })

        orders.sort(key=lambda x: 0 if x['side'] == 'sell' else 1)

        for order in orders:
            try:
                if dry_run:
                    results['executed'].append({**order, 'status': 'dry_run'})
                else:
                    if order['side'] == 'buy':
                        o = self.client.create_market_buy_usdt(order['coin'], order['usdt'])
                    else:
                        o = self.client.create_market_order(
                            symbol=order['symbol'],
                            side='sell',
                            amount=order['amount']
                        )
                    results['executed'].append({
                        **order,
                        'status': 'filled',
                        'order_id': o.get('id') if o else None
                    })
            except Exception as e:
                results['errors'].append({order['coin']: str(e)})

        if not orders:
            results['message'] = "لا حاجة لإعادة توازن (داخل نسبة الانحراف)"

        return results
