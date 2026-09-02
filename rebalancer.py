from typing import Dict, List, Tuple, Optional
from mexc_client import MexcClient
import config


class Rebalancer:
    def __init__(self, client: MexcClient = None):
        self.client = client or MexcClient()
        self.quote = config.QUOTE_ASSET

    def calculate_targets(
        self,
        selected_coins: List[str],
        method: str = "equal",
        include_usdt: bool = False
    ) -> Dict[str, float]:
        """
        Calculate target percentages.
        method: "equal" or "marketcap"
        Returns {'BTC': 25.0, 'ETH': 25.0, ...}
        """
        if not selected_coins:
            return {}

        coins = [c.upper() for c in selected_coins]

        if method == "equal":
            pct = 100.0 / len(coins)
            return {c: pct for c in coins}

        # marketcap weighted (using current market price * circulating is hard,
        # we approximate by current USDT value weight if already held,
        # otherwise by current price ranking / simple equal if no holdings.
        # Better approach: use current price as proxy for relative value,
        # but pure marketcap needs external data.
        # For simplicity and reliability on MEXC only:
        # We fetch current prices and weight by 1/price? No.
        # Real marketcap needs CoinGecko etc.
        # Practical approach used by many bots: weight by current holding value,
        # or fall back to equal if no holdings.
        # Better: use price * a fixed amount, but that's not marketcap.
        # I'll implement a simple "by current market price rank" or better:
        # Fetch ticker and use a pseudo weight based on liquidity/volume if available.
        # Simplest reliable: equal for now + note, OR weight by current portfolio value of those coins.

        portfolio = self.client.get_portfolio_value()
        assets = portfolio.get('assets', {})

        values = {}
        for c in coins:
            values[c] = assets.get(c, {}).get('usdt_value', 0.0)

        total_val = sum(values.values())
        if total_val <= 0:
            # No holdings yet → fall back to equal
            pct = 100.0 / len(coins)
            return {c: pct for c in coins}

        return {c: (v / total_val) * 100 for c, v in values.items()}

    def calculate_rebalance(
        self,
        target_allocations: Dict[str, float],  # {'BTC': 50, 'ETH': 30}
        threshold: float = 2.0,
        min_trade_usdt: float = 5.0
    ) -> Tuple[List[dict], Dict]:
        """
        Returns (orders_to_execute, portfolio_info)
        """
        portfolio = self.client.get_portfolio_value()
        total = portfolio['total_usdt']
        if total <= 0:
            return [], portfolio

        current = portfolio['assets']
        orders = []

        total_target = sum(target_allocations.values())
        if total_target <= 0:
            return [], portfolio
        targets = {k.upper(): (v / total_target) * 100 for k, v in target_allocations.items()}

        all_assets = set(list(current.keys()) + list(targets.keys()))

        for asset in all_assets:
            if asset == self.quote:
                continue

            current_pct = current.get(asset, {}).get('percent', 0.0)
            target_pct = targets.get(asset, 0.0)
            diff = target_pct - current_pct

            if abs(diff) < threshold:
                continue

            target_usdt = (target_pct / 100) * total
            current_usdt = current.get(asset, {}).get('usdt_value', 0.0)
            delta_usdt = target_usdt - current_usdt

            if abs(delta_usdt) < min_trade_usdt:
                continue

            price = current.get(asset, {}).get('price') or self.client.get_ticker_price(f"{asset}/{self.quote}")
            if price <= 0:
                continue

            amount = abs(delta_usdt) / price
            side = 'buy' if delta_usdt > 0 else 'sell'

            if side == 'sell':
                available = current.get(asset, {}).get('amount', 0.0)
                amount = min(amount, available * 0.999)

            if amount <= 0:
                continue

            orders.append({
                'symbol': f"{asset}/{self.quote}",
                'side': side,
                'amount': amount,
                'usdt_value': abs(delta_usdt),
                'asset': asset,
                'current_pct': current_pct,
                'target_pct': target_pct
            })

        orders.sort(key=lambda x: 0 if x['side'] == 'sell' else 1)
        return orders, portfolio

    def execute_rebalance(
        self,
        selected_coins: List[str],
        allocation_method: str = "equal",
        threshold: float = 2.0,
        min_trade_usdt: float = 5.0,
        dry_run: bool = False
    ) -> Dict:
        targets = self.calculate_targets(selected_coins, method=allocation_method)
        orders, portfolio = self.calculate_rebalance(targets, threshold, min_trade_usdt)

        results = {
            'targets': targets,
            'portfolio_before': portfolio,
            'orders_planned': orders,
            'executed': [],
            'errors': [],
            'dry_run': dry_run
        }

        if not orders:
            results['message'] = "لا حاجة لإعادة توازن (داخل نسبة الانحراف)"
            return results

        for order in orders:
            try:
                if dry_run:
                    results['executed'].append({**order, 'status': 'dry_run'})
                else:
                    result = self.client.create_market_order(
                        symbol=order['symbol'],
                        side=order['side'],
                        amount=order['amount']
                    )
                    results['executed'].append({
                        **order,
                        'status': 'filled',
                        'order_id': result.get('id'),
                        'filled': result.get('filled')
                    })
            except Exception as e:
                results['errors'].append({
                    'order': order,
                    'error': str(e)
                })

        results['portfolio_after'] = self.client.get_portfolio_value() if not dry_run else portfolio
        return results
