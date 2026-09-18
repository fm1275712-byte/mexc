from typing import Dict, List, Optional, Any
from mexc_client import MexcClient
import logging

logger = logging.getLogger(__name__)


class Rebalancer:
    def __init__(self, client: MexcClient):
        self.client = client
        self.quote = client.quote

    def calculate_targets(self, coins: List[str], method: str = "equal") -> Dict[str, float]:
        if not coins:
            return {}
        pct = 100.0 / len(coins)
        return {c: pct for c in coins}

    def place_tp_orders(
        self,
        coins_data: List[Dict[str, Any]],
        take_profit_pct: float,
        stop_loss_pct: float,
    ) -> List[Dict]:
        """
        After buying, place Limit Sell (TP) on MEXC and compute SL.
        coins_data: list of {symbol, amount, entry_price}  (or we fetch amount/price)
        Returns list of results per coin.
        """
        results = []
        for item in coins_data:
            symbol = item["symbol"]
            amount = float(item.get("amount") or 0)
            entry = float(item.get("entry_price") or 0)
            if amount <= 0:
                # try live balance
                amount = self.client.get_free_amount(symbol) * 0.998
            if entry <= 0:
                entry = self.client.get_ticker_price(f"{symbol}/{self.quote}")
            if amount <= 0 or entry <= 0:
                results.append({"symbol": symbol, "error": "no amount or price"})
                continue

            tp_price = entry * (1 + take_profit_pct / 100.0)
            sl_price = entry * (1 - stop_loss_pct / 100.0)

            order_id = None
            try:
                order = self.client.create_limit_sell(symbol, amount, tp_price)
                order_id = order.get("id") if order else None
            except Exception as e:
                logger.exception(f"TP order failed for {symbol}")
                results.append({
                    "symbol": symbol,
                    "entry_price": entry,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "amount": amount,
                    "tp_order_id": None,
                    "error": str(e),
                })
                continue

            results.append({
                "symbol": symbol,
                "entry_price": entry,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "amount": amount,
                "tp_order_id": order_id,
                "error": None,
            })
        return results

    def cancel_tp_orders(self, coins_with_orders: List[Dict]) -> None:
        """Cancel open TP limit orders. coins_with_orders: [{symbol, tp_order_id}, ...]"""
        for item in coins_with_orders:
            oid = item.get("tp_order_id")
            sym = item.get("symbol")
            if oid and sym:
                try:
                    self.client.cancel_order(oid, sym)
                except Exception:
                    pass

    def check_and_manage_positions(
        self,
        positions: List[Any],
    ) -> List[Dict]:
        """
        Cloud monitor:
        - If price <= current_sl → market sell + cancel TP order
        - If TP order filled → raise SL to tp_price (status = tp_hit)
        - If already tp_hit and price <= current_sl → market sell
        Returns list of actions taken.
        """
        actions = []
        for coin in positions:
            symbol = coin.symbol
            status = coin.position_status or "idle"
            if status not in ("open", "tp_hit"):
                continue

            try:
                price = self.client.get_ticker_price(f"{symbol}/{self.quote}")
            except Exception:
                continue
            if price <= 0:
                continue

            # Check if TP limit order still open
            tp_filled = False
            if coin.tp_order_id and status == "open":
                try:
                    order = self.client.fetch_order(coin.tp_order_id, symbol)
                    if order and order.get("status") in ("closed", "filled"):
                        tp_filled = True
                    elif order is None:
                        # order gone → assume filled or cancelled
                        open_orders = self.client.fetch_open_orders(symbol)
                        still_open = any(o.get("id") == coin.tp_order_id for o in open_orders)
                        if not still_open:
                            # check if we still hold the coin
                            free = self.client.get_free_amount(symbol)
                            if free < (coin.amount or 0) * 0.1:
                                tp_filled = True
                except Exception:
                    pass

            if tp_filled and status == "open":
                # Raise SL to TP price
                actions.append({
                    "symbol": symbol,
                    "action": "tp_hit_raise_sl",
                    "old_sl": coin.current_sl_price,
                    "new_sl": coin.tp_price,
                    "price": price,
                })
                # caller will update DB: status=tp_hit, current_sl_price=tp_price, tp_order_id=None
                continue

            # Stop loss hit?
            sl = float(coin.current_sl_price or 0)
            if sl > 0 and price <= sl:
                # Cancel any remaining TP order
                if coin.tp_order_id:
                    try:
                        self.client.cancel_order(coin.tp_order_id, symbol)
                    except Exception:
                        pass
                # Market sell remaining
                amount = self.client.get_free_amount(symbol) * 0.998
                sold = False
                if amount > 0:
                    try:
                        self.client.create_market_order(
                            f"{symbol}/{self.quote}", "sell", amount
                        )
                        sold = True
                    except Exception as e:
                        actions.append({
                            "symbol": symbol,
                            "action": "sl_sell_failed",
                            "error": str(e),
                            "price": price,
                        })
                        continue
                actions.append({
                    "symbol": symbol,
                    "action": "sl_hit_sold",
                    "sl": sl,
                    "price": price,
                    "amount": amount,
                    "sold": sold,
                    "was_raised": status == "tp_hit",
                })
        return actions

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


    def stop_partial(self, coins: list, sell_usdt: float, dry_run: bool = False) -> dict:
        """Sell approximately sell_usdt worth of the portfolio coins (pro-rata)."""
        results = {
            'action': 'stop_partial',
            'sell_usdt_target': sell_usdt,
            'executed': [],
            'errors': [],
            'dry_run': dry_run,
            'total_sold_usdt': 0.0
        }
        current = self.client.get_coins_value(coins)
        total = current['total_usdt']
        if total <= 0:
            results['errors'].append("المحفظة فارغة، لا يوجد ما يُباع")
            return results

        # fraction of holdings to sell
        frac = min(1.0, max(0.0, sell_usdt / total))
        balances = self.client.get_balance()
        prices = self.client.get_all_prices(coins)

        for coin in coins:
            amount = float(balances.get(coin, 0.0)) * frac * 0.999
            if amount <= 0:
                continue
            usdt_value = amount * prices.get(coin, 0.0)
            if usdt_value < 1.0:  # skip dust
                continue
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
