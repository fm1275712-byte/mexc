from typing import Dict, List, Optional, Any
from mexc_client import MexcClient
import logging

logger = logging.getLogger(__name__)


class Rebalancer:
    """Buy / sell / multi-TP placement & cloud SL monitor. No rebalancing."""

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
        dry_run: bool = False,
    ) -> Dict:
        """Buy coins using up to total_usdt only."""
        results = {
            "action": "start",
            "total_usdt": total_usdt,
            "executed": [],
            "errors": [],
            "dry_run": dry_run,
        }
        free_usdt = self.client.get_free_usdt()
        if free_usdt < total_usdt:
            results["errors"].append(
                f"رصيد USDT الحر غير كافٍ. المتاح: `{free_usdt:.2f}$` | المطلوب: `{total_usdt:.2f}$`"
            )
            return results

        targets = self.calculate_targets(coins, method)
        for coin, pct in targets.items():
            usdt_for_coin = total_usdt * (pct / 100.0)
            if usdt_for_coin < min_trade_usdt:
                results["errors"].append(f"`{coin}`: المبلغ صغير جداً ({usdt_for_coin:.2f}$)")
                continue
            try:
                if dry_run:
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "buy",
                        "usdt": usdt_for_coin,
                        "status": "dry_run",
                    })
                else:
                    order = self.client.create_market_buy_usdt(coin, usdt_for_coin)
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "buy",
                        "usdt": usdt_for_coin,
                        "status": "filled",
                        "order_id": order.get("id") if order else None,
                    })
            except Exception as e:
                results["errors"].append({coin: str(e)})
        return results

    def stop_portfolio(self, coins: List[str], dry_run: bool = False) -> Dict:
        """Sell all holdings of the given coins."""
        results = {
            "action": "stop",
            "executed": [],
            "errors": [],
            "dry_run": dry_run,
            "total_sold_usdt": 0.0,
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
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "sell",
                        "amount": amount,
                        "usdt": usdt_value,
                        "status": "dry_run",
                    })
                    results["total_sold_usdt"] += usdt_value
                else:
                    order = self.client.create_market_order(
                        symbol=f"{coin}/{self.quote}", side="sell", amount=amount
                    )
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "sell",
                        "amount": amount,
                        "usdt": usdt_value,
                        "status": "filled",
                        "order_id": order.get("id") if order else None,
                    })
                    results["total_sold_usdt"] += usdt_value
            except Exception as e:
                results["errors"].append({coin: str(e)})
        return results

    def place_tp_orders(
        self,
        coins_data: List[Dict[str, Any]],
        tp1_pct: float,
        tp2_pct: float,
        tp3_pct: float,
        stop_loss_pct: float,
        tp1_sell_pct: float = 40.0,
        tp2_sell_pct: float = 30.0,
    ) -> List[Dict]:
        """Place 3 Limit Sell orders (partial) on MEXC + initial SL."""
        results = []
        for item in coins_data:
            symbol = item["symbol"]
            amount = float(item.get("amount") or 0)
            entry = float(item.get("entry_price") or 0)
            if amount <= 0:
                amount = self.client.get_free_amount(symbol) * 0.998
            if entry <= 0:
                entry = self.client.get_ticker_price(f"{symbol}/{self.quote}")
            if amount <= 0 or entry <= 0:
                results.append({"symbol": symbol, "error": "no amount or price"})
                continue

            tp1 = entry * (1 + tp1_pct / 100.0)
            tp2 = entry * (1 + tp2_pct / 100.0)
            tp3 = entry * (1 + tp3_pct / 100.0)
            sl = entry * (1 - stop_loss_pct / 100.0)

            a1 = amount * (tp1_sell_pct / 100.0)
            a2 = amount * (tp2_sell_pct / 100.0)
            a3 = max(0.0, amount - a1 - a2)

            orders = {"tp1_order_id": None, "tp2_order_id": None, "tp3_order_id": None}
            errors = []
            for key, qty, price in [
                ("tp1_order_id", a1, tp1),
                ("tp2_order_id", a2, tp2),
                ("tp3_order_id", a3, tp3),
            ]:
                if qty <= 0:
                    continue
                try:
                    order = self.client.create_limit_sell(symbol, qty, price)
                    orders[key] = order.get("id") if order else None
                except Exception as e:
                    logger.exception(f"{key} failed for {symbol}")
                    errors.append(f"{key}: {e}")

            results.append({
                "symbol": symbol,
                "entry_price": entry,
                "tp1_price": tp1,
                "tp2_price": tp2,
                "tp3_price": tp3,
                "sl_price": sl,
                "amount": amount,
                "remaining_amount": amount,
                **orders,
                "error": "; ".join(errors) if errors else None,
            })
        return results

    def cancel_tp_orders(self, coins_with_orders: List[Dict]) -> None:
        for item in coins_with_orders:
            sym = item.get("symbol")
            if not sym:
                continue
            for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                oid = item.get(key)
                if oid:
                    try:
                        self.client.cancel_order(oid, sym)
                    except Exception:
                        pass

    def _order_filled(self, order_id: Optional[str], symbol: str) -> bool:
        if not order_id:
            return False
        try:
            order = self.client.fetch_order(order_id, symbol)
            if order and order.get("status") in ("closed", "filled"):
                return True
            open_orders = self.client.fetch_open_orders(symbol)
            return not any(o.get("id") == order_id for o in open_orders)
        except Exception:
            return False

    def check_and_manage_positions(self, positions: List[Any]) -> List[Dict]:
        """
        Multi-TP monitor:
        TP1 → raise SL to TP1
        TP2 → raise SL to TP2
        TP3 → mark done
        SL hit → cancel limits + market sell remaining
        """
        actions = []
        for coin in positions:
            symbol = coin.symbol
            status = coin.position_status or "idle"
            if status not in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"):
                continue
            try:
                price = self.client.get_ticker_price(f"{symbol}/{self.quote}")
            except Exception:
                continue
            if price <= 0:
                continue

            if status == "open" and self._order_filled(getattr(coin, "tp1_order_id", None), symbol):
                actions.append({
                    "symbol": symbol,
                    "action": "tp1_hit",
                    "new_sl": coin.tp1_price,
                    "price": price,
                })
                continue
            if status in ("open", "tp1_hit") and self._order_filled(getattr(coin, "tp2_order_id", None), symbol):
                actions.append({
                    "symbol": symbol,
                    "action": "tp2_hit",
                    "new_sl": coin.tp2_price,
                    "price": price,
                })
                continue
            if status in ("open", "tp1_hit", "tp2_hit") and self._order_filled(getattr(coin, "tp3_order_id", None), symbol):
                actions.append({"symbol": symbol, "action": "tp3_hit", "price": price})
                continue

            sl = float(coin.current_sl_price or 0)
            if sl > 0 and price <= sl:
                for oid in (
                    getattr(coin, "tp1_order_id", None),
                    getattr(coin, "tp2_order_id", None),
                    getattr(coin, "tp3_order_id", None),
                    getattr(coin, "tp_order_id", None),
                ):
                    if oid:
                        try:
                            self.client.cancel_order(oid, symbol)
                        except Exception:
                            pass
                amount = self.client.get_free_amount(symbol) * 0.998
                sold = False
                if amount > 0:
                    try:
                        self.client.create_market_order(f"{symbol}/{self.quote}", "sell", amount)
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
                    "was_raised": status in ("tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"),
                })
        return actions
