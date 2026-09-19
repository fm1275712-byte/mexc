"""
    MEXC Portfolio Manager + Signal Engine
    بوت تليجرام لإدارة محافظ متعددة + نظام إشارات تنفيذ ذكي.
"""
import logging
import re
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import config
from database import (
    init_db, SessionLocal, get_or_create_user, get_portfolios, get_portfolio,
    create_portfolio, add_coin_to_portfolio, remove_coin_from_portfolio,
    close_portfolio, delete_portfolio_completely, clear_coin_position,
    delete_orphaned_portfolio_records,
    set_portfolio_running, log_action,
    Portfolio, PortfolioCoin, PortfolioTrade, RebalanceLog,
    get_signal_sources, get_signal_source, create_signal_source,
    update_signal_source, delete_signal_source, log_signal, parse_portfolio_ids,
    SignalLog, update_coin_position, reset_coin_positions, get_open_positions,
    record_trade_event, get_trade_event, get_reentry_candidates,
    get_portfolio_trade_events, mark_reentry_events_used,
)
from mexc_client import MexcClient
from rebalancer import Rebalancer

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

(
    CREATE_NAME, CREATE_AMOUNT, CREATE_COINS, ADD_COIN, INCREASE_AMOUNT,
    SRC_NAME, SRC_MIN_USD, SRC_MAX_TX, SRC_BUY_PFS, SRC_SELL_PFS,
    EDIT_SRC_FIELD, EDIT_SRC_VALUE,
) = range(12)

_mexc: Optional[MexcClient] = None
_reb: Optional[Rebalancer] = None


def get_mexc() -> MexcClient:
    global _mexc
    if _mexc is None:
        _mexc = MexcClient()
    return _mexc


def get_reb() -> Rebalancer:
    global _reb
    if _reb is None:
        _reb = Rebalancer(get_mexc())
    return _reb


def is_admin(user_id: int) -> bool:
    if not config.ADMIN_TELEGRAM_ID:
        return True
    return user_id == config.ADMIN_TELEGRAM_ID


async def ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        msg = update.effective_message
        if msg:
            await msg.reply_text("⛔ *غير مصرح*\nهذا البوت مخصص للأدمن فقط.", parse_mode="Markdown")
        return False
    return True


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 محافظي", callback_data="list_pf"),
            InlineKeyboardButton("➕ محفظة جديدة", callback_data="create_pf"),
        ],
        [
            InlineKeyboardButton("📡 مصادر الإشارات", callback_data="list_sources"),
            InlineKeyboardButton("💰 الرصيد", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("🧹 تنظيف القاعدة", callback_data="cleanup_db"),
            InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings"),
        ],
    ])


def pf_keyboard(pf_id: int, is_running: bool):
    rows = []
    if is_running:
        rows.append([InlineKeyboardButton("⏹ إيقاف المحفظة", callback_data=f"stop_{pf_id}")])
    else:
        rows.append([InlineKeyboardButton("▶️ تشغيل المحفظة", callback_data=f"start_{pf_id}")])
    rows.append([
        InlineKeyboardButton("📈 زيادة رأس المال", callback_data=f"increase_{pf_id}"),
        InlineKeyboardButton("🎯 أهداف TP/SL", callback_data=f"pf_tpsl_{pf_id}"),
    ])
    rows.append([
        InlineKeyboardButton("🔄 تحديث الأهداف", callback_data=f"refresh_tpsl_{pf_id}"),
        InlineKeyboardButton("📊 الإحصائيات", callback_data=f"stats_{pf_id}"),
    ])
    rows.append([
        InlineKeyboardButton("🛑 الاستوبات / إعادة دخول", callback_data=f"stopped_{pf_id}"),
        InlineKeyboardButton("🔎 عملات ناقصة", callback_data=f"missing_{pf_id}"),
    ])
    rows.append([
        InlineKeyboardButton("➕ عملة", callback_data=f"addcoin_{pf_id}"),
        InlineKeyboardButton("➖ عملة", callback_data=f"removecoin_{pf_id}"),
    ])
    rows.append([InlineKeyboardButton("🗑 حذف المحفظة", callback_data=f"close_{pf_id}")])
    rows.append([InlineKeyboardButton("⬅️ رجوع للقائمة", callback_data="list_pf")])
    return InlineKeyboardMarkup(rows)


def _missing_selection_key(pf_id: int) -> str:
    return f"missing_reentry_selection_{pf_id}"


def _missing_reentry_keyboard(pf_id: int, missing_symbols, selected, allow_selection=True):
    rows = []
    if allow_selection:
        for symbol in missing_symbols:
            marker = "✅" if symbol in selected else "⬜"
            rows.append([
                InlineKeyboardButton(
                    f"{marker} {symbol}",
                    callback_data=f"missing_toggle_{pf_id}_{symbol}",
                )
            ])
    if selected and allow_selection:
        rows.append([
            InlineKeyboardButton(
                f"✅ تأكيد إعادة الدخول ({len(selected)})",
                callback_data=f"missing_confirm_{pf_id}",
            )
        ])
    rows.append([InlineKeyboardButton("🔄 إعادة الفحص", callback_data=f"missing_{pf_id}")])
    rows.append([InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")])
    return InlineKeyboardMarkup(rows)


def sources_keyboard(sources):
    buttons = []
    for s in sources:
        icon = "🟢" if s.enabled else "🔴"
        buttons.append([InlineKeyboardButton(f"{icon} {s.name}", callback_data=f"view_src_{s.id}")])
    buttons.append([InlineKeyboardButton("➕ إضافة مصدر جديد", callback_data="create_src")])
    buttons.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def source_detail_keyboard(src_id: int, enabled: bool):
    rows = []
    if enabled:
        rows.append([InlineKeyboardButton("⏹ تعطيل المصدر", callback_data=f"toggle_src_{src_id}")])
    else:
        rows.append([InlineKeyboardButton("▶️ تفعيل المصدر", callback_data=f"toggle_src_{src_id}")])
    rows.append([
        InlineKeyboardButton("✏️ تعديل", callback_data=f"edit_src_{src_id}"),
        InlineKeyboardButton("🗑 حذف", callback_data=f"del_src_{src_id}"),
    ])
    rows.append([InlineKeyboardButton("⬅️ رجوع للمصادر", callback_data="list_sources")])
    return InlineKeyboardMarkup(rows)


def format_pf(p) -> str:
    coins = ", ".join(c.symbol for c in p.coins) or "—"
    status = "🟢 *شغالة*" if p.is_running else "⚪ *متوقفة*"
    lines = [
        f"📁 *{p.name}*  `#{p.id}`",
        "━━━━━━━━━━━━━━━━",
        f"الحالة: {status}",
        f"المخصص: `{p.investment_usdt:.2f}` USDT",
        f"العملات ({len(p.coins)}): `{coins}`",
    ]
    # show portfolio-specific TP/SL if set
    t1 = getattr(p, "tp1_pct", None)
    t2 = getattr(p, "tp2_pct", None)
    t3 = getattr(p, "tp3_pct", None)
    sl = getattr(p, "stop_loss_pct", None)
    if any(v is not None and v > 0 for v in (t1, t2, t3, sl)):
        lines.append(
            f"🎯 أهداف: TP1 `{t1 or '—'}%` · TP2 `{t2 or '—'}%` · "
            f"TP3 `{t3 or '—'}%` · SL `{sl or '—'}%`"
        )
    else:
        lines.append("🎯 الأهداف: *من الإعدادات العامة*")
    if p.is_running:
        lines.append("")
        lines.append("*المراكز المفتوحة:*")
        for c in p.coins:
            if c.position_status in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit") and c.entry_price:
                st_map = {
                    "open": "مفتوح",
                    "tp1_hit": "TP1 ✓",
                    "tp2_hit": "TP2 ✓",
                    "tp3_hit": "TP3 ✓",
                    "tp_hit": "هدف ✓",
                }
                st = st_map.get(c.position_status, c.position_status)
                lines.append(
                    f"• `{c.symbol}` دخول `{c.entry_price:.6g}`\n"
                    f"  TP1 `{getattr(c,'tp1_price',0):.6g}` · TP2 `{getattr(c,'tp2_price',0):.6g}` · "
                    f"TP3 `{getattr(c,'tp3_price',0):.6g}`\n"
                    f"  🛑 استوب `{c.current_sl_price:.6g}` ({st})"
                )
    return "\n".join(lines)


def format_portfolio_stats(p, events, prices=None) -> str:
    """Format realized and current unrealized P&L for one portfolio."""
    prices = prices or {}
    realized = sum(float(e.realized_pnl or 0) for e in events)
    open_cost = 0.0
    open_value = 0.0
    open_lines = []
    for coin in p.coins:
        if coin.position_status not in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"):
            continue
        remaining = float(coin.remaining_amount or coin.amount or 0)
        entry = float(coin.entry_price or 0)
        price = float(prices.get(coin.symbol) or entry or 0)
        if remaining <= 0 or entry <= 0:
            continue
        cost = remaining * entry
        value = remaining * price
        open_cost += cost
        open_value += value
        pnl = value - cost
        emoji = "🟢" if pnl >= 0 else "🔴"
        open_lines.append(f"{emoji} `{coin.symbol}`: `{pnl:+.2f}` USDT")
    unrealized = open_value - open_cost
    total = realized + unrealized
    r_emoji = "🟢" if realized >= 0 else "🔴"
    u_emoji = "🟢" if unrealized >= 0 else "🔴"
    t_emoji = "🟢" if total >= 0 else "🔴"
    result = (
        f"📊 *إحصائيات محفظة {p.name}*\n"
        "━━━━━━━━━━━━━━━━\n"
        f"{r_emoji} المحقق: `{realized:+.2f}` USDT\n"
        f"{u_emoji} غير المحقق: `{unrealized:+.2f}` USDT\n"
        f"{t_emoji} *الإجمالي: `{total:+.2f}` USDT*\n"
        f"عدد العمليات: `{len(events)}`"
    )
    if open_lines:
        result += "\n\n*المراكز المفتوحة:*\n" + "\n".join(open_lines)
    return result


def format_source(s) -> str:
    status = "🟢 *مفعل*" if s.enabled else "🔴 *معطل*"
    return (
        f"📡 *{s.name}*  `#{s.id}`\n"
        "━━━━━━━━━━━━━━━━\n"
        f"الحالة: {status}\n"
        f"الحد الأدنى: `{s.min_usd:,.0f}` $\n"
        f"أقصى تحويلات: `{s.max_tx_count}`\n"
        f"شراء: {'✅ مسموح' if s.allow_buy else '❌ ممنوع'}\n"
        f"بيع: {'✅ مسموح' if s.allow_sell else '❌ ممنوع'}\n"
        f"محافظ الشراء: `{s.buy_portfolio_ids or '—'}`\n"
        f"محافظ البيع: `{s.sell_portfolio_ids or '—'}`\n"
        f"التبريد: `{s.cooldown_minutes}` دقيقة"
    )


def _build_cleanup_plan(db, telegram_id: int, presence: Dict[str, Dict[str, float]]) -> Dict:
    """Find only data that is provably inactive without touching working portfolios."""
    portfolios = get_portfolios(db, telegram_id, status=None)
    closed_portfolios = []
    stale_positions = []

    for portfolio in portfolios:
        portfolio_has_live_asset = any(
            presence.get(coin.symbol, {}).get("present", False)
            for coin in portfolio.coins
        )
        if portfolio.status != "active" and not portfolio_has_live_asset:
            closed_portfolios.append({
                "id": portfolio.id,
                "name": portfolio.name,
                "coins": list(portfolio.coins),
            })
            continue

        # A running portfolio is always protected. A stopped portfolio keeps
        # its configuration, but stale position/TP/SL tracking is removable
        # when MEXC confirms that the asset is no longer held.
        if portfolio.status != "active" or portfolio.is_running:
            continue
        for coin in portfolio.coins:
            if coin.position_status in (None, "", "idle", "waiting_reentry"):
                continue
            if presence.get(coin.symbol, {}).get("present", False):
                continue
            stale_positions.append({
                "id": coin.id,
                "portfolio_id": portfolio.id,
                "portfolio_name": portfolio.name,
                "symbol": coin.symbol,
                "position_status": coin.position_status,
                "coin": coin,
            })

    portfolio_ids = {portfolio.id for portfolio in portfolios}
    trade_query = db.query(PortfolioTrade).filter(
        PortfolioTrade.telegram_id == telegram_id
    )
    log_query = db.query(RebalanceLog).filter(
        RebalanceLog.telegram_id == telegram_id
    )
    if portfolio_ids:
        trade_query = trade_query.filter(~PortfolioTrade.portfolio_id.in_(portfolio_ids))
        log_query = log_query.filter(~RebalanceLog.portfolio_id.in_(portfolio_ids))

    return {
        "closed_portfolios": closed_portfolios,
        "stale_positions": stale_positions,
        "orphan_trades": trade_query.count(),
        "orphan_logs": log_query.count(),
    }


def _cleanup_report(plan: Dict) -> str:
    closed = plan["closed_portfolios"]
    stale = plan["stale_positions"]
    orphan_trades = plan["orphan_trades"]
    orphan_logs = plan["orphan_logs"]
    lines = [
        "🔎 *فحص قاعدة البيانات*",
        "",
        "تم الإبقاء على كل محفظة تعمل وكل عملة لها رصيد أو أمر بيع قائم على MEXC.",
        "",
        f"🗑 محافظ مغلقة بلا أصول: `{len(closed)}`",
        f"🧹 مراكز قديمة بلا رصيد: `{len(stale)}`",
        f"🧾 سجلات عمليات يتيمة: `{orphan_trades}` | سجلات إعادة توازن يتيمة: `{orphan_logs}`",
    ]
    if closed:
        lines.append("\n*المحافظ المرشحة للحذف:*")
        lines.extend(f"• #{item['id']} {item['name']}" for item in closed[:10])
        if len(closed) > 10:
            lines.append(f"• ... و`{len(closed) - 10}` أخرى")
    if stale:
        lines.append("\n*بيانات المراكز المرشحة للمسح:*")
        lines.extend(
            f"• {item['portfolio_name']} — `{item['symbol']}` ({item['position_status']})"
            for item in stale[:15]
        )
        if len(stale) > 15:
            lines.append(f"• ... و`{len(stale) - 15}` أخرى")
    if not closed and not stale and not orphan_trades and not orphan_logs:
        lines.append("\n✅ لا توجد بيانات قديمة آمنة للتنظيف.")
    else:
        lines.extend([
            "",
            "لن يتم بيع أي أصل في عملية التنظيف.",
            "سيتم إلغاء أوامر الأهداف المرتبطة بالبيانات القديمة ثم حذفها فقط بعد إعادة الفحص.",
        ])
    return "\n".join(lines)


async def _show_cleanup_scan(query, context, tid):
    import asyncio

    db = SessionLocal()
    try:
        await query.edit_message_text("⏳ جاري فحص المحافظ والرصيد قبل اقتراح التنظيف...")
        portfolios = get_portfolios(db, tid, status=None)
        symbols = sorted({
            coin.symbol for portfolio in portfolios for coin in portfolio.coins
        })
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None,
                lambda: get_mexc().get_portfolio_presence(symbols) if symbols else {},
            )
        except Exception as exc:
            logger.exception("Database cleanup scan failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص الرصيد من MEXC.\n"
                "لم يتم حذف أو تعديل أي بيانات.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
            return

        plan = _build_cleanup_plan(db, tid, presence)
        context.user_data["cleanup_ready"] = True
        buttons = []
        if plan["closed_portfolios"] or plan["stale_positions"]:
            buttons.append([
                InlineKeyboardButton("✅ تأكيد التنظيف", callback_data="cleanup_confirm"),
            ])
        buttons.extend([
            [InlineKeyboardButton("🔄 إعادة الفحص", callback_data="cleanup_db")],
            [InlineKeyboardButton("⬅️ القائمة", callback_data="menu")],
        ])
        await query.edit_message_text(
            _cleanup_report(plan),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    finally:
        db.close()


async def _do_cleanup(query, context, tid):
    import asyncio

    db = SessionLocal()
    try:
        await query.edit_message_text("⏳ إعادة الفحص ثم تنظيف البيانات غير النشطة...")
        portfolios = get_portfolios(db, tid, status=None)
        symbols = sorted({
            coin.symbol for portfolio in portfolios for coin in portfolio.coins
        })
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None,
                lambda: get_mexc().get_portfolio_presence(symbols) if symbols else {},
            )
        except Exception as exc:
            logger.exception("Database cleanup preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر إعادة فحص الرصيد.\nلم يتم حذف أو تعديل أي بيانات.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
            return

        plan = _build_cleanup_plan(db, tid, presence)
        if (
            not plan["closed_portfolios"]
            and not plan["stale_positions"]
            and not plan["orphan_trades"]
            and not plan["orphan_logs"]
        ):
            context.user_data.pop("cleanup_ready", None)
            await query.edit_message_text(
                "✅ لا توجد بيانات قديمة آمنة للتنظيف بعد إعادة الفحص.",
                reply_markup=main_menu_keyboard(),
            )
            return

        cancelled = 0
        cleared = 0
        deleted_portfolios = 0

        for item in plan["stale_positions"]:
            coin = item["coin"]
            result = get_reb().cancel_tp_orders([{
                "symbol": coin.symbol,
                "tp_order_id": coin.tp_order_id,
                "tp1_order_id": coin.tp1_order_id,
                "tp2_order_id": coin.tp2_order_id,
                "tp3_order_id": coin.tp3_order_id,
            }])
            cancelled += len(result.get("cancelled", []))
            if result.get("errors"):
                continue
            if clear_coin_position(db, coin.id):
                cleared += 1

        for item in plan["closed_portfolios"]:
            portfolio_cancel_failed = False
            for coin in item["coins"]:
                result = get_reb().cancel_tp_orders([{
                    "symbol": coin.symbol,
                    "tp_order_id": coin.tp_order_id,
                    "tp1_order_id": coin.tp1_order_id,
                    "tp2_order_id": coin.tp2_order_id,
                    "tp3_order_id": coin.tp3_order_id,
                }])
                cancelled += len(result.get("cancelled", []))
                portfolio_cancel_failed = portfolio_cancel_failed or bool(result.get("errors"))
            if not portfolio_cancel_failed and delete_portfolio_completely(db, item["id"], tid):
                deleted_portfolios += 1

        deleted_trades, deleted_logs = delete_orphaned_portfolio_records(db, tid)
        context.user_data.pop("cleanup_ready", None)
        await query.edit_message_text(
            "✅ *تم تنظيف قاعدة البيانات بعد إعادة الفحص.*\n\n"
            f"المحافظ المحذوفة: `{deleted_portfolios}`\n"
            f"بيانات المراكز القديمة الممسوحة: `{cleared}`\n"
            f"أوامر الأهداف الملغاة: `{cancelled}`\n\n"
            f"سجلات العمليات اليتيمة المحذوفة: `{deleted_trades}`\n"
            f"سجلات إعادة التوازن اليتيمة المحذوفة: `{deleted_logs}`\n\n"
            "تم ترك المحافظ العاملة والعملات ذات الرصيد كما هي.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        db.close()


def parse_signal_message(text: str) -> Optional[Dict[str, Any]]:
    """
    يدعم:
    1) صيغة Whale Alert
    2) صيغة Arkham (From: ... To: ...)
    3) الصيغة المنظمة #SIGNAL / إشارة
    """
    if not text or len(text) < 8:
        return None

    text = text.strip()
    lower = text.lower()
    result = {
        "source": None,
        "action": None,
        "reason": "",
        "portfolios": [],
        "size": "full",
        "raw": text[:1500],
        "usd_value": 0.0,
        "symbol": None,
    }

    # ========== 1) صيغة Whale Alert ==========
    # مثال:
    # 🚨🚨🚨 1,720 $BTC (131,865,141 USD) transferred from Coinbase Institutional to unknown new wallet
    # 🚨🚨 828 $BTC (63,648,815 USD) transferred from unknown wallet to #Coinbase

    whale_pattern = re.search(
        r"(?:🚨\s*)*"                                    # الإيموجي
        r"([\d,]+(?:\.\d+)?)\s*"                       # الكمية
        r"\$?([A-Za-z0-9]+)\s*"                          # الرمز (BTC / ETH / XRP ...)
        r"\(([\d,]+(?:\.\d+)?)\s*USD\)\s*"           # القيمة بالدولار
        r"transferred from\s+(.+?)\s+to\s+(.+?)(?:\n|Details|$)",
        text,
        re.IGNORECASE | re.DOTALL
    )

    if whale_pattern:
        amount_str = whale_pattern.group(1).replace(",", "")
        symbol = whale_pattern.group(2).upper().lstrip("$")
        usd_str = whale_pattern.group(3).replace(",", "")
        from_entity = whale_pattern.group(4).strip()
        to_entity = whale_pattern.group(5).strip()

        try:
            result["usd_value"] = float(usd_str)
        except Exception:
            result["usd_value"] = 0.0

        result["symbol"] = symbol
        result["source"] = "WhaleAlert"
        result["reason"] = f"{amount_str} {symbol} | {from_entity} → {to_entity}"

        from_l = from_entity.lower()
        to_l = to_entity.lower()

        # قائمة المنصات المعروفة
        exchanges = [
            "coinbase", "kraken", "binance", "uphold", "revolut", "falconx",
            "bitfinex", "okx", "okex", "bybit", "huobi", "htx", "gemini",
            "bitstamp", "zero hash", "zerohash", "bitgo", "cumberland",
            "jump", "wintermute", "b2c2", "galaxy", "robinhood", "crypto.com",
            "kucoin", "mexc", "gate.io", "gateio", "bitget"
        ]

        def is_exchange(s: str) -> bool:
            return any(ex in s for ex in exchanges)

        def is_unknown(s: str) -> bool:
            return "unknown" in s or "new wallet" in s

        # قائمة العملات المستقرة
        stables = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDD", "BUSD", "PYUSD"}
        is_stable = symbol in stables

        # --- قواعد القرار ---
        if is_exchange(from_l) and is_unknown(to_l):
            # سحب من منصة → محفظة غير معروفة
            if is_stable:
                result["action"] = "SELL"      # سحب فلوس = غالباً سلبي
            else:
                result["action"] = "BUY"       # سحب BTC/ETH = تجميع
        elif is_unknown(from_l) and is_exchange(to_l):
            # إيداع في منصة
            if is_stable:
                result["action"] = "BUY"       # إيداع فلوس = استعداد للشراء (إيجابي)
            else:
                result["action"] = "SELL"      # إيداع BTC/ETH = استعداد للبيع
        elif is_exchange(from_l) and is_exchange(to_l):
            # تحويل بين منصات → نتجاهل
            return None
        else:
            # تحويلات أخرى ضعيفة الإشارة
            return None

        if result["action"]:
            return result

    # ========== 2) صيغة Arkham ==========
    from_m = re.search(r"From\s*:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    to_m = re.search(r"To\s*:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if from_m and to_m:
        from_s = from_m.group(1).strip().lower()
        to_s = to_m.group(1).strip().lower()
        is_from_cb = "coinbase" in from_s
        is_to_cb = "coinbase" in to_s
        is_from_br = "blackrock" in from_s or "ibit" in from_s or "etha" in from_s
        is_to_br = "blackrock" in to_s or "ibit" in to_s or "etha" in to_s

        if is_from_cb and is_to_br:
            result["action"] = "BUY"
            result["source"] = "BlackRock"
            result["reason"] = f"Withdrawal from Coinbase → BlackRock | {from_m.group(1).strip()[:80]}"
        elif is_from_br and is_to_cb:
            result["action"] = "SELL"
            result["source"] = "BlackRock"
            result["reason"] = f"Deposit to Coinbase from BlackRock | {to_m.group(1).strip()[:80]}"

        usd_m = re.search(r"\(\$([0-9,]+(?:\.[0-9]+)?)\s*([KMB])?\)", text, re.IGNORECASE)
        if usd_m:
            num = float(usd_m.group(1).replace(",", ""))
            mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get((usd_m.group(2) or "").upper(), 1)
            result["usd_value"] = num * mult
        else:
            usd_m2 = re.search(r"Value\s*:.*?\$([0-9,]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
            if usd_m2:
                result["usd_value"] = float(usd_m2.group(1).replace(",", ""))

        if result["action"]:
            return result

    # ========== 3) الصيغة المنظمة #SIGNAL ==========
    if not any(k in lower for k in ("#signal", "إشارة", "اشارة", "signal")):
        return None

    m = re.search(r"(?:source|المصدر)\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if m:
        result["source"] = m.group(1).strip().split("\n")[0].strip()
    m = re.search(r"(?:action|القرار|قرار)\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if m:
        act = m.group(1).strip().upper()
        if any(x in act for x in ("BUY", "شراء", "LONG")):
            result["action"] = "BUY"
        elif any(x in act for x in ("SELL", "بيع", "SHORT")):
            result["action"] = "SELL"
    m = re.search(r"(?:reason|السبب|سبب)\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if m:
        result["reason"] = m.group(1).strip().split("\n")[0][:300]
    m = re.search(r"(?:portfolios|المحافظ|محافظ)\s*[:：]\s*([0-9,\s]+)", text, re.IGNORECASE)
    if m:
        result["portfolios"] = parse_portfolio_ids(m.group(1))
    m = re.search(r"(?:size|الحجم|حجم)\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if m:
        result["size"] = m.group(1).strip().lower()

    if result["action"] and (result["source"] or result["portfolios"]):
        return result
    return None


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin(update):
        return
    db = SessionLocal()
    try:
        get_or_create_user(db, update.effective_user.id)
    finally:
        db.close()
    await update.message.reply_text(
        "🚀 *MEXC Portfolio Manager*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "إدارة محافظ متعددة + نظام إشارات ذكي\n"
        "تنفيذ تلقائي من المجموعة • أهداف ربح ووقف خسارة\n\n"
        "اختر من القائمة أدناه 👇",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❎ تم الإلغاء.\nرجعت للقائمة الرئيسية 👇",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "🏠 *القائمة الرئيسية*",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def execute_signal(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: Dict):
    tid = config.ADMIN_TELEGRAM_ID or (update.effective_user.id if update.effective_user else 0)
    db = SessionLocal()
    try:
        source_name = parsed.get("source")
        action = parsed["action"]
        reason = parsed.get("reason", "")
        explicit_pfs = parsed.get("portfolios", [])

        sources = get_signal_sources(db, tid)
        matched = None
        enabled_sources = [s for s in sources if s.enabled]

        # 1) مطابقة بالاسم بالظبط
        if source_name:
            for s in enabled_sources:
                if s.name.lower() == source_name.lower():
                    matched = s
                    break

        # 2) لو رسالة WhaleAlert أو Arkham ومفيش مطابقة بالاسم:
        #    فضّل مصدر اسمه فيه whale أو arkham أو blackrock، وإلا أول مصدر مفعل
        raw_lower = (parsed.get("raw") or "").lower()
        is_auto_msg = bool(parsed.get("usd_value")) or (
            ("from:" in raw_lower and "to:" in raw_lower) or
            "transferred from" in raw_lower
        )
        if not matched and is_auto_msg and enabled_sources:
            for s in enabled_sources:
                n = s.name.lower()
                if "whale" in n or "arkham" in n or "blackrock" in n or "ibit" in n:
                    matched = s
                    break
            if not matched:
                matched = enabled_sources[0]

        if not matched and not explicit_pfs:
            await update.message.reply_text(
                f"⚠️ مصدر الإشارة `{source_name or 'Arkham'}` غير موجود أو معطل.\nأضف مصدر مفعل من قائمة مصادر الإشارات.",
                parse_mode="Markdown"
            )
            log_signal(db, tid, action, reason, parsed.get("raw", ""), executed=False, result_msg="مصدر غير موجود")
            return

        if explicit_pfs:
            target_ids = explicit_pfs
        elif matched:
            target_ids = parse_portfolio_ids(
                matched.buy_portfolio_ids if action == "BUY" else matched.sell_portfolio_ids
            )
        else:
            target_ids = []

        if not target_ids:
            await update.message.reply_text("⚠️ مفيش محافظ مرتبطة بهذه الإشارة.")
            log_signal(db, tid, action, reason, parsed.get("raw", ""),
                       source_id=matched.id if matched else None, executed=False, result_msg="لا محافظ")
            return

        # ----- تحقق القيمة -----
        usd_val = float(parsed.get("usd_value") or 0)
        if matched and usd_val > 0 and usd_val < matched.min_usd:
            await update.message.reply_text(
                f"ℹ️ تم تجاهل الإشارة — القيمة `${usd_val:,.0f}` أقل من الحد `${matched.min_usd:,.0f}`",
                parse_mode="Markdown"
            )
            log_signal(db, tid, action, reason, parsed.get("raw", ""),
                       source_id=matched.id, executed=False,
                       result_msg=f"تحت الحد: {usd_val} < {matched.min_usd}")
            return

        # ----- تجميع التحويلات (بدون انتهاء زمني) -----
        needed = matched.max_tx_count if matched and matched.max_tx_count > 0 else 1

        if matched and needed > 1:
            # كل الـ pending المفتوحة لنفس المصدر + القرار
            old_pending = db.query(SignalLog).filter(
                SignalLog.source_id == matched.id,
                SignalLog.action == action,
                SignalLog.executed == False,
                SignalLog.result_msg.like("pending%")
            ).all()

            # مجموع المبالغ السابقة من result_msg: pending 1/2 | usd=123
            prev_usd = 0.0
            for op in old_pending:
                m = re.search(r"usd=([0-9.]+)", op.result_msg or "")
                if m:
                    prev_usd += float(m.group(1))

            current_count = len(old_pending) + 1
            total_usd = prev_usd + (usd_val if usd_val > 0 else 0)

            if current_count < needed:
                log_signal(
                    db, tid, action, reason, parsed.get("raw", ""),
                    source_id=matched.id, executed=False,
                    result_msg=f"pending {current_count}/{needed} | usd={usd_val or 0}"
                )
                remaining = needed - current_count

                def fmt_m(v):
                    if v >= 1_000_000:
                        return f"${v/1_000_000:.2f}M"
                    if v >= 1_000:
                        return f"${v/1_000:.1f}K"
                    return f"${v:,.0f}"

                await update.message.reply_text(
                    f"📥 *تحويل مستلم* ({current_count}/{needed})\n"
                    f"المصدر: `{matched.name}`\n"
                    f"القرار: *{action}*\n"
                    f"السبب: {reason or '—'}\n\n"
                    f"💵 آخر تحويل: *{fmt_m(usd_val)}*\n"
                    f"💰 المجموع حتى الآن: *{fmt_m(total_usd)}*\n"
                    f"🎯 الحد الأدنى للتحويل: *{fmt_m(matched.min_usd)}*\n\n"
                    f"⏳ باقي *{remaining}* تحويل"
                    f"{'ات' if remaining >= 3 else ('ان' if remaining == 2 else '')}"
                    f" لتنفيذ الأمر.",
                    parse_mode="Markdown"
                )
                return
            else:
                # وصلنا للعدد → صفّر الـ pending + اعرض المجموع
                total_usd = prev_usd + (usd_val if usd_val > 0 else 0)
                for op in old_pending:
                    op.result_msg = f"consumed→exec {needed}"
                db.commit()
                # نخزن المجموع عشان يظهر في تقرير التنفيذ
                parsed["usd_value"] = total_usd
                parsed["reason"] = (reason or "") + f" | مجموع {needed} تحويلات ≈ ${total_usd:,.0f}"
                reason = parsed["reason"]

        # ----- cooldown بعد تنفيذ فقط (لو > 0) -----
        if matched and matched.cooldown_minutes and matched.cooldown_minutes > 0:
            cutoff = datetime.utcnow() - timedelta(minutes=matched.cooldown_minutes)
            recent = db.query(SignalLog).filter(
                SignalLog.source_id == matched.id,
                SignalLog.action == action,
                SignalLog.executed == True,
                SignalLog.created_at >= cutoff
            ).first()
            if recent:
                await update.message.reply_text(
                    f"⏳ تم التنفيذ مؤخراً — انتظر {matched.cooldown_minutes} دقيقة قبل تنفيذ جديد."
                )
                return

        if matched:
            if action == "BUY" and not matched.allow_buy:
                await update.message.reply_text("⚠️ المصدر ده مش مسموح له إشارات شراء.")
                return
            if action == "SELL" and not matched.allow_sell:
                await update.message.reply_text("⚠️ المصدر ده مش مسموح له إشارات بيع.")
                return

        executed, errors = [], []
        for pf_id in target_ids:
            p = get_portfolio(db, pf_id, tid)
            if not p or p.status != "active":
                errors.append(f"#{pf_id} غير موجودة")
                continue
            coins = [c.symbol for c in p.coins]
            if not coins:
                errors.append(f"#{pf_id} بدون عملات")
                continue
            try:
                if action == "BUY":
                    if p.is_running:
                        errors.append(f"#{pf_id} شغالة بالفعل")
                        continue
                    result = get_reb().start_portfolio(
                        coins=coins, total_usdt=p.investment_usdt,
                        method=p.allocation_method or "equal", min_trade_usdt=5.0, dry_run=False,
                    )
                    if result.get("errors") and not result.get("executed"):
                        errors.append(f"#{pf_id}: {result['errors'][0]}")
                    else:
                        set_portfolio_running(db, pf_id, True)
                        executed.append(f"#{pf_id} ({p.name})")
                        log_action(db, tid, "signal_buy", f"Signal from {source_name}", True, pf_id)
                else:
                    if not p.is_running:
                        errors.append(f"#{pf_id} متوقفة بالفعل")
                        continue
                    sell_result = get_reb().stop_portfolio(coins, dry_run=False)
                    for sold in sell_result.get("executed", []):
                        symbol = str(sold.get("symbol", "")).split("/")[0]
                        coin = next((c for c in p.coins if c.symbol == symbol), None)
                        amount = float(sold.get("amount") or 0)
                        usdt = float(sold.get("usdt") or 0)
                        exit_price = usdt / amount if amount > 0 else 0.0
                        if coin and amount > 0 and float(coin.entry_price or 0) > 0:
                            record_trade_event(
                                db, tid, p.id, coin.id, symbol, "manual_stop",
                                coin.entry_price, exit_price, amount,
                                (exit_price - coin.entry_price) * amount,
                                details="Signal sell",
                            )
                    set_portfolio_running(db, pf_id, False)
                    executed.append(f"#{pf_id} ({p.name})")
                    log_action(db, tid, "signal_sell", f"Signal from {source_name}", True, pf_id)
            except Exception as e:
                errors.append(f"#{pf_id}: {e}")
                logger.exception(f"Signal exec error pf={pf_id}")

        lines = [
            f"{'✅' if executed else '⚠️'} *نتيجة الإشارة*",
            f"المصدر: `{source_name or '—'}`",
            f"القرار: *{action}*",
            f"السبب: {reason or '—'}",
            "",
        ]
        if executed:
            lines.append("تم التنفيذ على:")
            for e in executed:
                lines.append(f"  • {e}")
        if errors:
            lines.append("\nملاحظات:")
            for e in errors:
                lines.append(f"  • {e}")
        msg = "\n".join(lines)
        await update.message.reply_text(msg, parse_mode="Markdown")
        log_signal(db, tid, action, reason, parsed.get("raw", ""),
                   source_id=matched.id if matched else None,
                   executed=bool(executed), result_msg=msg[:500])
    finally:
        db.close()


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    # رسائل الإشارات (Whale Alert / #SIGNAL / Arkham) تُقبل حتى لو جاية من Forwarder
    is_signal_like = (
        "transferred from" in text.lower()
        or "#signal" in text.lower()
        or "إشارة" in text
        or "اشارة" in text
        or text.strip().lower().startswith("from:")
    )

    if not is_signal_like:
        # رسائل عادية → لازم تكون من الأدمن
        if not await ensure_admin(update):
            return
    else:
        # رسائل إشارات → ننفذها باسم الأدمن
        if not config.ADMIN_TELEGRAM_ID:
            return

    if context.user_data.get("waiting"):
        # Per-portfolio TP/SL edit
        pf_edit = context.user_data.get("edit_pf_tpsl")
        if pf_edit:
            if not await ensure_admin(update):
                return
            try:
                val = float(text.strip().replace("%", "").replace(",", "."))
                if val < 0 or val > 100:
                    await update.message.reply_text("أدخل رقم بين 0 و 100 (0 = استخدم العام).")
                    return
                db = SessionLocal()
                try:
                    pf = get_portfolio(db, pf_edit["pf_id"], update.effective_user.id)
                    if not pf:
                        await update.message.reply_text("المحفظة غير موجودة.")
                        context.user_data.clear()
                        return
                    field = pf_edit["field"]
                    setattr(pf, field, None if val == 0 else val)
                    db.commit()
                    await update.message.reply_text(
                        f"✅ تم ضبط `{field}` للمحفظة *{pf.name}* إلى " + ("العام" if val == 0 else f"`{val}%`"),
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🎯 أهداف المحفظة", callback_data=f"pf_tpsl_{pf.id}")],
                            [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf.id}")],
                        ]),
                    )
                finally:
                    db.close()
                context.user_data.clear()
            except ValueError:
                await update.message.reply_text("أدخل رقم صحيح.")
            return

        # Handle TP/SL percentage edit from global settings
        edit_key = context.user_data.get("edit_setting")
        if edit_key in ("take_profit_pct", "stop_loss_pct", "tp1_pct", "tp2_pct", "tp3_pct", "tp1_sell_pct", "tp2_sell_pct"):
            if not await ensure_admin(update):
                return
            try:
                val = float(text.strip().replace("%", "").replace(",", "."))
                if val <= 0 or val > 100:
                    await update.message.reply_text("أدخل نسبة بين 0.1 و 100.")
                    return
                db = SessionLocal()
                try:
                    user = get_or_create_user(db, update.effective_user.id)
                    setattr(user, edit_key, val)
                    db.commit()
                    labels = {
                        "take_profit_pct": "هدف الربح",
                        "stop_loss_pct": "وقف الخسارة",
                        "tp1_pct": "الهدف 1",
                        "tp2_pct": "الهدف 2",
                        "tp3_pct": "الهدف 3",
                        "tp1_sell_pct": "نسبة البيع عند الهدف 1",
                        "tp2_sell_pct": "نسبة البيع عند الهدف 2",
                    }
                    label = labels.get(edit_key, edit_key)
                    await update.message.reply_text(
                        f"✅ تم ضبط *{label}* إلى `{val}%`",
                        parse_mode="Markdown",
                        reply_markup=main_menu_keyboard(),
                    )
                finally:
                    db.close()
                context.user_data.clear()
            except ValueError:
                await update.message.reply_text("أدخل رقم صحيح (مثال: 5)")
            return
        return

    parsed = parse_signal_message(text)
    if parsed:
        await execute_signal(update, context, parsed)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Answer immediately so Telegram doesn't timeout (query expires ~seconds)
    try:
        await query.answer()
    except Exception:
        pass
    if not await ensure_admin(update):
        return
    data = query.data or ""
    tid = update.effective_user.id

    if data == "menu":
        await query.edit_message_text(
            "🏠 *القائمة الرئيسية*\nاختر ما تريد:",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "list_pf":
        db = SessionLocal()
        try:
            pfs = get_portfolios(db, tid, status="active")
            if not pfs:
                await query.edit_message_text(
                    "📭 لا توجد محافظ نشطة حالياً.\nاضغط ➕ *محفظة جديدة* للبدء.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(),
                )
                return
            buttons = [
                [InlineKeyboardButton(
                    f"{'🟢' if p.is_running else '⚪'} #{p.id} {p.name} · {p.investment_usdt:.0f}$",
                    callback_data=f"view_{p.id}",
                )]
                for p in pfs
            ]
            buttons.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="menu")])
            await query.edit_message_text(
                f"📋 *محافظك النشطة* ({len(pfs)})\n━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        finally:
            db.close()
        return

    if data.startswith("view_") and not data.startswith("view_src_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            coins = [c.symbol for c in p.coins]
            live = ""
            if coins:
                try:
                    val = get_mexc().get_coins_value(coins)
                    live = f"\nالقيمة الحالية: `{val['total_usdt']:.2f}` USDT"
                except Exception:
                    pass
            await query.edit_message_text(format_pf(p) + live, parse_mode="Markdown", reply_markup=pf_keyboard(p.id, p.is_running))
        finally:
            db.close()
        return

    if data.startswith("stats_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            events = get_portfolio_trade_events(db, pf_id, tid)
            prices = {}
            symbols = [
                c.symbol for c in p.coins
                if c.position_status in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit")
            ]
            if symbols:
                try:
                    prices = get_mexc().get_all_prices(symbols)
                except Exception:
                    prices = {}
            await query.edit_message_text(
                format_portfolio_stats(p, events, prices),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 تحديث الإحصائيات", callback_data=f"stats_{pf_id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("missing_toggle_"):
        parts = data.split("_", 3)
        if len(parts) != 4:
            await query.edit_message_text("طلب غير صالح.", reply_markup=main_menu_keyboard())
            return
        await _show_missing_reentry(query, context, tid, int(parts[2]), parts[3])
        return

    if data.startswith("missing_confirm_"):
        await _do_missing_reentry(query, context, tid, int(data.split("_")[2]))
        return

    if data.startswith("missing_"):
        await _show_missing_reentry(query, context, tid, int(data.split("_")[1]))
        return

    if data.startswith("stopped_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            candidates = get_reentry_candidates(db, pf_id, tid)
            if not candidates:
                await query.edit_message_text(
                    f"🛑 لا توجد عملات متاحة لإعادة الدخول في *{p.name}*.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                    ]),
                )
                return
            lines = [f"🛑 *عملات ضربت الاستوب — {p.name}*", "", "اختر العملة لإعادة دخولها يدوياً:"]
            buttons = []
            for event in candidates:
                pnl = float(event.realized_pnl or 0)
                lines.append(
                    f"• `{event.symbol}` — خروج `{event.exit_price:.6g}` — "
                    f"نتيجة `{pnl:+.2f}` USDT"
                )
                buttons.append([
                    InlineKeyboardButton(
                        f"🔄 إعادة دخول {event.symbol}",
                        callback_data=f"reentry_{event.id}",
                    )
                ])
            buttons.append([InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")])
            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        finally:
            db.close()
        return

    if data == "balance":
        try:
            data_bal = get_mexc().get_portfolio_value()
            free = get_mexc().get_free_usdt()
            lines = [f"💰 *الرصيد الكلي:* `{data_bal['total_usdt']:.2f}` USDT", f"USDT حر: `{free:.2f}`\n"]
            for asset, info in sorted(data_bal["assets"].items(), key=lambda x: -x[1]["usdt_value"])[:15]:
                if info["usdt_value"] < 0.5:
                    continue
                lines.append(f"`{asset}`: {info['amount']:.6g} ≈ `{info['usdt_value']:.2f}$`")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="menu")]]))
        except Exception as e:
            await query.edit_message_text(f"خطأ:\n`{e}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if data == "cleanup_db":
        await _show_cleanup_scan(query, context, tid)
        return

    if data == "cleanup_confirm":
        if not context.user_data.get("cleanup_ready"):
            await query.edit_message_text(
                "انتهت صلاحية تقرير التنظيف. شغّل الفحص مرة أخرى.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🧹 فحص قاعدة البيانات", callback_data="cleanup_db")],
                    [InlineKeyboardButton("⬅️ القائمة", callback_data="menu")],
                ]),
            )
            return
        await _do_cleanup(query, context, tid)
        return

    if data == "settings":
        db = SessionLocal()
        try:
            user = get_or_create_user(db, tid)
            tp1 = getattr(user, "tp1_pct", 3.0) or 3.0
            tp2 = getattr(user, "tp2_pct", 5.0) or 5.0
            tp3 = getattr(user, "tp3_pct", 8.0) or 8.0
            s1 = getattr(user, "tp1_sell_pct", 40.0) or 40.0
            s2 = getattr(user, "tp2_sell_pct", 30.0) or 30.0
            sl = getattr(user, "stop_loss_pct", 3.0) or 3.0
            text = (
                f"⚙️ *الإعدادات*\n\n"
                f"أقل صفقة: `{user.min_trade_usdt}` USDT\n"
                f"أقصى عملات: `{user.max_coins_per_portfolio}`\n\n"
                f"🎯 *الهدف 1:* `{tp1}%` (بيع `{s1}%`)\n"
                f"🎯 *الهدف 2:* `{tp2}%` (بيع `{s2}%`)\n"
                f"🎯 *الهدف 3:* `{tp3}%` (الباقي)\n"
                f"🛡 *وقف الخسارة:* `{sl}%`"
            )
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎯 هدف 1 %", callback_data="edit_tp1"),
                     InlineKeyboardButton("بيع عند 1 %", callback_data="edit_tp1_sell")],
                    [InlineKeyboardButton("🎯 هدف 2 %", callback_data="edit_tp2"),
                     InlineKeyboardButton("بيع عند 2 %", callback_data="edit_tp2_sell")],
                    [InlineKeyboardButton("🎯 هدف 3 %", callback_data="edit_tp3")],
                    [InlineKeyboardButton("🛡 وقف الخسارة %", callback_data="edit_sl")],
                    [InlineKeyboardButton("⬅️ رجوع", callback_data="menu")],
                ]),
            )
        finally:
            db.close()
        return

    edit_map = {
        "edit_tp1": ("tp1_pct", "نسبة الهدف 1 (مثال: 3)"),
        "edit_tp2": ("tp2_pct", "نسبة الهدف 2 (مثال: 5)"),
        "edit_tp3": ("tp3_pct", "نسبة الهدف 3 (مثال: 8)"),
        "edit_tp1_sell": ("tp1_sell_pct", "نسبة البيع عند الهدف 1 من الكمية (مثال: 40)"),
        "edit_tp2_sell": ("tp2_sell_pct", "نسبة البيع عند الهدف 2 من الكمية (مثال: 30)"),
        "edit_sl": ("stop_loss_pct", "نسبة وقف الخسارة (مثال: 3)"),
    }
    if data in edit_map:
        field, prompt = edit_map[data]
        context.user_data["waiting"] = True
        context.user_data["edit_setting"] = field
        await query.edit_message_text(
            f"أرسل *{prompt}*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="settings")]]),
        )
        return

    if data == "create_pf":
        context.user_data["create"] = {}
        context.user_data["waiting"] = True
        await query.edit_message_text("أرسل *اسم المحفظة*:", parse_mode="Markdown")
        return CREATE_NAME

    if data.startswith("start_"):
        await _do_start(query, tid, int(data.split("_")[1]))
        return
    if data.startswith("stop_"):
        await _do_stop(query, tid, int(data.split("_")[1]))
        return
    if data.startswith("refresh_tpsl_"):
        await _do_refresh_tpsl(query, tid, int(data.split("_")[2]))
        return
    if data.startswith("reentry_"):
        await _do_manual_reentry(query, tid, int(data.split("_")[1]))
        return

    # Per-portfolio TP/SL settings
    if data.startswith("pf_tpsl_"):
        pf_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            pf = get_portfolio(db, pf_id, tid)
            if not pf:
                await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
                return
            user = get_or_create_user(db, tid)
            def show(v, default):
                return f"`{v}`" if v is not None and float(v) > 0 else f"`{default}` (عام)"
            t1 = getattr(pf, "tp1_pct", None)
            t2 = getattr(pf, "tp2_pct", None)
            t3 = getattr(pf, "tp3_pct", None)
            s1 = getattr(pf, "tp1_sell_pct", None)
            s2 = getattr(pf, "tp2_sell_pct", None)
            sl = getattr(pf, "stop_loss_pct", None)
            ut1 = getattr(user, "tp1_pct", 3.0) or 3.0
            ut2 = getattr(user, "tp2_pct", 5.0) or 5.0
            ut3 = getattr(user, "tp3_pct", 8.0) or 8.0
            us1 = getattr(user, "tp1_sell_pct", 40.0) or 40.0
            us2 = getattr(user, "tp2_sell_pct", 30.0) or 30.0
            usl = getattr(user, "stop_loss_pct", 3.0) or 3.0
            msg = (
                f"🎯 *أهداف المحفظة:* {pf.name}\n\n"
                f"TP1: {show(t1, ut1)}% | بيع: {show(s1, us1)}%\n"
                f"TP2: {show(t2, ut2)}% | بيع: {show(s2, us2)}%\n"
                f"TP3: {show(t3, ut3)}%\n"
                f"استوب: {show(sl, usl)}%\n\n"
                f"_لو فاضية = تستخدم الإعدادات العامة_"
            )
            await query.edit_message_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("TP1 %", callback_data=f"pftpsl_{pf_id}_tp1_pct"),
                     InlineKeyboardButton("بيع 1 %", callback_data=f"pftpsl_{pf_id}_tp1_sell_pct")],
                    [InlineKeyboardButton("TP2 %", callback_data=f"pftpsl_{pf_id}_tp2_pct"),
                     InlineKeyboardButton("بيع 2 %", callback_data=f"pftpsl_{pf_id}_tp2_sell_pct")],
                    [InlineKeyboardButton("TP3 %", callback_data=f"pftpsl_{pf_id}_tp3_pct"),
                     InlineKeyboardButton("استوب %", callback_data=f"pftpsl_{pf_id}_stop_loss_pct")],
                    [InlineKeyboardButton("🗑 امسح تخصيص المحفظة", callback_data=f"pftpsl_clear_{pf_id}")],
                    [InlineKeyboardButton("⬅️ رجوع", callback_data=f"view_{pf_id}")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("pftpsl_clear_"):
        pf_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            pf = get_portfolio(db, pf_id, tid)
            if pf:
                for f in ("tp1_pct", "tp2_pct", "tp3_pct", "tp1_sell_pct", "tp2_sell_pct", "stop_loss_pct"):
                    setattr(pf, f, None)
                db.commit()
            await query.edit_message_text(
                "✅ تم مسح تخصيص المحفظة — هتستخدم الإعدادات العامة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=f"pf_tpsl_{pf_id}")]]),
            )
        finally:
            db.close()
        return

    if data.startswith("pftpsl_") and not data.startswith("pftpsl_clear_"):
        # pftpsl_{id}_{field}
        parts = data.split("_", 2)
        # data = pftpsl_12_tp1_pct  -> need careful parse
        rest = data[len("pftpsl_"):]  # 12_tp1_pct
        pf_id_str, field = rest.split("_", 1)
        pf_id = int(pf_id_str)
        context.user_data["waiting"] = True
        context.user_data["edit_pf_tpsl"] = {"pf_id": pf_id, "field": field}
        labels = {
            "tp1_pct": "هدف 1 %",
            "tp2_pct": "هدف 2 %",
            "tp3_pct": "هدف 3 %",
            "tp1_sell_pct": "نسبة البيع عند الهدف 1",
            "tp2_sell_pct": "نسبة البيع عند الهدف 2",
            "stop_loss_pct": "وقف الخسارة %",
        }
        await query.edit_message_text(
            f"أرسل قيمة *{labels.get(field, field)}* لهذه المحفظة:\n(أو `0` لمسح التخصيص واستخدام العام)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=f"pf_tpsl_{pf_id}")]]),
        )
        return
    if data.startswith("increase_"):
        context.user_data["increase_pf"] = int(data.split("_")[1])
        context.user_data["waiting"] = True
        await query.edit_message_text("أرسل مبلغ الزيادة بالـ USDT:")
        return INCREASE_AMOUNT
    if data.startswith("addcoin_"):
        context.user_data["addcoin_pf"] = int(data.split("_")[1])
        context.user_data["waiting"] = True
        await query.edit_message_text("أرسل رمز العملة (مثال: BTC أو ETH):")
        return ADD_COIN
    if data.startswith("removecoin_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p or not p.coins:
                await query.edit_message_text("لا توجد عملات.", reply_markup=main_menu_keyboard())
                return
            buttons = [[InlineKeyboardButton(f"حذف {c.symbol}", callback_data=f"delcoin_{pf_id}_{c.symbol}")] for c in p.coins]
            buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data=f"view_{pf_id}")])
            await query.edit_message_text("اختر العملة للحذف:", reply_markup=InlineKeyboardMarkup(buttons))
        finally:
            db.close()
        return
    if data.startswith("delcoin_"):
        parts = data.split("_")
        await _do_remove_coin(query, tid, int(parts[1]), parts[2])
        return
    if data.startswith("close_"):
        pf_id = int(data.split("_")[1])
        await query.edit_message_text(
            "⚠️ هل أنت متأكد من حذف المحفظة؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، احذفها", callback_data=f"confirm_close_{pf_id}")],
                [InlineKeyboardButton("❌ إلغاء", callback_data=f"view_{pf_id}")],
            ])
        )
        return
    if data.startswith("confirm_close_"):
        await _do_close(query, tid, int(data.split("_")[2]))
        return

    # Signal sources
    if data == "list_sources":
        db = SessionLocal()
        try:
            sources = get_signal_sources(db, tid)
            text = "📡 *مصادر الإشارات:*\n\n" if sources else "لا توجد مصادر بعد.\nاضغط ➕ لإضافة مصدر."
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=sources_keyboard(sources))
        finally:
            db.close()
        return

    if data == "create_src":
        context.user_data["src"] = {}
        context.user_data["waiting"] = True
        await query.edit_message_text("أرسل *اسم المصدر* (مثال: BlackRock):", parse_mode="Markdown")
        return SRC_NAME

    if data.startswith("view_src_"):
        src_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            s = get_signal_source(db, src_id, tid)
            if not s:
                await query.edit_message_text("المصدر غير موجود.", reply_markup=main_menu_keyboard())
                return
            await query.edit_message_text(format_source(s), parse_mode="Markdown",
                                          reply_markup=source_detail_keyboard(s.id, s.enabled))
        finally:
            db.close()
        return

    if data.startswith("toggle_src_"):
        src_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            s = get_signal_source(db, src_id, tid)
            if s:
                s.enabled = not s.enabled
                db.commit()
                await query.edit_message_text(format_source(s), parse_mode="Markdown",
                                              reply_markup=source_detail_keyboard(s.id, s.enabled))
        finally:
            db.close()
        return

    if data.startswith("del_src_"):
        src_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            delete_signal_source(db, src_id, tid)
            sources = get_signal_sources(db, tid)
            await query.edit_message_text("✅ تم حذف المصدر.", reply_markup=sources_keyboard(sources))
        finally:
            db.close()
        return

    if data.startswith("edit_src_"):
        src_id = int(data.split("_")[2])
        context.user_data["edit_src_id"] = src_id
        await query.edit_message_text(
            "✏️ *اختر الحقل للتعديل:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("الحد الأدنى ($)", callback_data=f"editf_min_usd_{src_id}")],
                [InlineKeyboardButton("أقصى تحويلات", callback_data=f"editf_max_tx_{src_id}")],
                [InlineKeyboardButton("محافظ الشراء", callback_data=f"editf_buy_pfs_{src_id}")],
                [InlineKeyboardButton("محافظ البيع", callback_data=f"editf_sell_pfs_{src_id}")],
                [InlineKeyboardButton("التبريد (دقيقة)", callback_data=f"editf_cooldown_{src_id}")],
                [InlineKeyboardButton("⬅️ رجوع", callback_data=f"view_src_{src_id}")],
            ])
        )
        return

    if data.startswith("editf_"):
        parts = data.split("_")
        src_id = int(parts[-1])
        field = "_".join(parts[1:-1])
        field_map = {
            "min_usd": ("min_usd", "أرسل الحد الأدنى بالدولار (مثال: 1000000):"),
            "max_tx": ("max_tx_count", "أرسل أقصى عدد تحويلات (مثال: 3):"),
            "buy_pfs": ("buy_portfolio_ids", "أرسل أرقام محافظ الشراء مفصولة بفاصلة (مثال: 21,22)\nأو none:"),
            "sell_pfs": ("sell_portfolio_ids", "أرسل أرقام محافظ البيع مفصولة بفاصلة (مثال: 21,22)\nأو none:"),
            "cooldown": ("cooldown_minutes", "أرسل مدة التبريد بالدقائق (مثال: 30):"),
        }
        if field not in field_map:
            await query.edit_message_text("حقل غير معروف.", reply_markup=main_menu_keyboard())
            return
        db_field, prompt = field_map[field]
        context.user_data["edit_src_id"] = src_id
        context.user_data["edit_src_field"] = db_field
        context.user_data["waiting"] = True
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ رجوع", callback_data=f"edit_src_{src_id}")]
            ])
        )
        return EDIT_SRC_VALUE


async def _do_refresh_tpsl(query, tid, pf_id):
    """Replace TP orders and the monitored SL without selling the portfolio."""
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if not p.is_running:
            await query.edit_message_text(
                "المحفظة متوقفة؛ شغّلها أولاً حتى يتم تحديث أوامر الأهداف.",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return

        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
        sl_pct = pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        await query.edit_message_text("⏳ جاري تحديث أوامر الأهداف والاستوب بدون بيع...")
        updated = []
        errors = []
        for coin in p.coins:
            if coin.position_status not in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"):
                continue
            amount = float(coin.remaining_amount or coin.amount or 0)
            if amount <= 0 or float(coin.entry_price or 0) <= 0:
                continue
            try:
                get_reb().cancel_tp_orders([{
                    "symbol": coin.symbol,
                    "tp_order_id": getattr(coin, "tp_order_id", None),
                    "tp1_order_id": getattr(coin, "tp1_order_id", None),
                    "tp2_order_id": getattr(coin, "tp2_order_id", None),
                    "tp3_order_id": getattr(coin, "tp3_order_id", None),
                }])
                skipped = []
                if coin.position_status in ("tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"):
                    skipped.append("tp1")
                if coin.position_status in ("tp2_hit", "tp3_hit", "tp_hit"):
                    skipped.append("tp2")
                if coin.position_status in ("tp3_hit", "tp_hit"):
                    skipped.append("tp3")
                result = get_reb().place_tp_orders(
                    [{
                        "symbol": coin.symbol,
                        "amount": amount,
                        "entry_price": coin.entry_price,
                    }],
                    tp1, tp2, tp3, sl_pct, s1, s2, skip_stages=skipped,
                )[0]
                if result.get("error"):
                    errors.append(f"{coin.symbol}: {result['error']}")
                    continue
                if coin.position_status == "open":
                    new_sl = result.get("sl_price", 0)
                elif coin.position_status == "tp1_hit":
                    new_sl = coin.entry_price
                elif coin.position_status == "tp2_hit":
                    new_sl = coin.tp2_price or coin.entry_price
                else:
                    new_sl = coin.tp3_price or coin.tp2_price or coin.entry_price
                update_coin_position(
                    db,
                    coin.id,
                    tp1_price=result.get("tp1_price", coin.tp1_price),
                    tp2_price=result.get("tp2_price", coin.tp2_price),
                    tp3_price=result.get("tp3_price", coin.tp3_price),
                    tp_price=result.get("tp1_price", coin.tp1_price),
                    current_sl_price=new_sl,
                    original_sl_price=coin.original_sl_price or result.get("original_sl_price", 0),
                    tp1_order_id=result.get("tp1_order_id"),
                    tp2_order_id=result.get("tp2_order_id"),
                    tp3_order_id=result.get("tp3_order_id"),
                )
                updated.append(coin.symbol)
            except Exception as exc:
                errors.append(f"{coin.symbol}: {exc}")

        log_action(
            db, tid, "refresh_tpsl",
            f"Updated TP/SL for {p.name}: {', '.join(updated) or 'none'}",
            not errors, pf_id,
        )
        lines = [
            f"✅ تم تحديث الأهداف والاستوب لمحفظة *{p.name}*",
            "لم يتم بيع أي عملة.",
            f"القيم الجديدة: TP1 `{tp1}%` | TP2 `{tp2}%` | TP3 `{tp3}%` | SL `{sl_pct}%`",
        ]
        if updated:
            lines.append("العملات: " + ", ".join(f"`{x}`" for x in updated))
        if errors:
            lines.append("\n⚠️ ملاحظات:\n" + "\n".join(f"• {x}" for x in errors))
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, True),
        )
    finally:
        db.close()


async def _show_missing_reentry(query, context, tid, pf_id, toggle_symbol=None):
    """Show missing portfolio coins and let the user build a buy selection."""
    import asyncio

    db = SessionLocal()
    try:
        pf = get_portfolio(db, pf_id, tid)
        if not pf:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return

        symbols = [c.symbol for c in pf.coins]
        if not symbols:
            await query.edit_message_text(
                "المحفظة لا تحتوي على عملات.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        loop = asyncio.get_event_loop()
        try:
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(symbols)
            )
        except Exception as exc:
            logger.exception("Portfolio missing-coin scan failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص الرصيد من MEXC.\n"
                "لم يتم اعتبار أي عملة ناقصة ولم يتم تنفيذ أي شراء.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        missing_symbols = [
            symbol for symbol in symbols
            if not presence.get(symbol, {}).get("present", False)
        ]
        key = _missing_selection_key(pf_id)
        selected = set(context.user_data.get(key, []))
        selected.intersection_update(missing_symbols)
        if not pf.is_running:
            selected.clear()
            context.user_data.pop(key, None)

        if toggle_symbol and pf.is_running:
            toggle_symbol = toggle_symbol.upper()
            if toggle_symbol in missing_symbols:
                if toggle_symbol in selected:
                    selected.remove(toggle_symbol)
                else:
                    selected.add(toggle_symbol)
            context.user_data[key] = sorted(selected)

        if not missing_symbols:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                f"✅ فحص *{pf.name}* مكتمل.\n"
                f"كل العملات المسجلة ({len(symbols)}) موجودة بقيمة سوقية فعلية "
                f"(أكبر من `{config.BALANCE_PRESENCE_MIN_USDT:g}` USDT).\n"
                "تم احتساب العملات الموجودة داخل أوامر البيع المفتوحة، "
                "وتجاهل بقايا البيع الصغيرة.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        present_count = len(symbols) - len(missing_symbols)
        lines = [
            f"🔎 *فحص العملات — {pf.name}*",
            "",
            f"المسجلة: `{len(symbols)}` | الموجودة: `{present_count}` | الناقصة: `{len(missing_symbols)}`",
            "",
            f"يتم تجاهل بقايا البيع الأقل من `{config.BALANCE_PRESENCE_MIN_USDT:g}` USDT.",
            (
                "اختر العملات التي تريد إعادة دخولها."
                if pf.is_running
                else "⚠️ المحفظة متوقفة؛ الفحص متاح للعرض فقط، ولن يظهر خيار شراء."
            ),
            "لا يوجد شراء عند الاختيار؛ الشراء لا يبدأ إلا بعد زر التأكيد.",
            "",
            "العملة المحددة: " + (
                ", ".join(f"`{symbol}`" for symbol in sorted(selected))
                if selected else "—"
            ),
        ]
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_missing_reentry_keyboard(
                pf_id, missing_symbols, selected, allow_selection=pf.is_running
            ),
        )
    finally:
        db.close()


async def _do_missing_reentry(query, context, tid, pf_id):
    """Re-check and buy only the coins explicitly confirmed by the user."""
    import asyncio

    key = _missing_selection_key(pf_id)
    if context.user_data.get(f"{key}_in_progress"):
        await query.edit_message_text("⏳ إعادة الدخول قيد التنفيذ بالفعل.")
        return

    selected = set(context.user_data.get(key, []))
    if not selected:
        await query.edit_message_text(
            "اختر عملة واحدة على الأقل أولاً.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 فحص العملات الناقصة", callback_data=f"missing_{pf_id}")],
                [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
            ]),
        )
        return

    db = SessionLocal()
    try:
        pf = get_portfolio(db, pf_id, tid)
        if not pf:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if not pf.is_running:
            await query.edit_message_text(
                "⚠️ المحفظة أصبحت متوقفة. لم يتم تنفيذ أي شراء.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        coin_map = {coin.symbol.upper(): coin for coin in pf.coins}
        selected = {symbol.upper() for symbol in selected if symbol.upper() in coin_map}
        if not selected:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                "لم تعد هناك عملات صالحة لإعادة الدخول.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        # Re-check immediately before placing any order. A balance appearing
        # after the first scan must remove that coin from the buy list.
        loop = asyncio.get_event_loop()
        try:
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(list(coin_map))
            )
        except Exception as exc:
            logger.exception("Final portfolio missing-coin scan failed")
            await query.edit_message_text(
                "⚠️ تعذر إعادة فحص الرصيد قبل التنفيذ.\n"
                "تم إلغاء العملية بالكامل ولم يتم شراء أي عملة.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 إعادة الفحص", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        no_longer_missing = {
            symbol for symbol in selected
            if presence.get(symbol, {}).get("present", False)
        }
        selected -= no_longer_missing
        if not selected:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                "ℹ️ العملات المحددة أصبحت موجودة بالفعل في الرصيد.\n"
                "تم إلغاء الشراء.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        per_coin_usdt = max(
            5.0,
            float(pf.investment_usdt or 0) / max(1, len(pf.coins)),
        )
        required_usdt = per_coin_usdt * len(selected)
        try:
            free_usdt = await loop.run_in_executor(None, get_mexc().get_free_usdt)
        except Exception as exc:
            logger.exception("Free USDT preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر التحقق من رصيد USDT الحر.\n"
                "تم إلغاء العملية ولم يتم شراء أي عملة.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 إعادة الفحص", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return
        if free_usdt < required_usdt:
            await query.edit_message_text(
                f"⚠️ رصيد USDT الحر غير كافٍ.\n"
                f"المتاح: `{free_usdt:.2f}` USDT\n"
                f"المطلوب تقريباً: `{required_usdt:.2f}` USDT\n\n"
                "تم إلغاء العملية ولم يتم شراء أي عملة.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        context.user_data[f"{key}_in_progress"] = True
        selected_symbols = sorted(selected)
        await query.edit_message_text(
            "⏳ تم التأكيد.\n"
            f"سيتم تنفيذ إعادة دخول لـ `{len(selected_symbols)}` عملة فقط:\n"
            + ", ".join(f"`{symbol}`" for symbol in selected_symbols),
            parse_mode="Markdown",
        )

        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
        sl_pct = pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        succeeded = []
        errors = []
        for symbol in selected_symbols:
            coin = coin_map[symbol]
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda symbol=symbol: get_reb().reentry_buy_and_place_tp(
                        symbol, per_coin_usdt, tp1, tp2, tp3, sl_pct, s1, s2
                    ),
                )
            except Exception as exc:
                logger.exception("Missing-coin re-entry failed for %s", symbol)
                errors.append(f"`{symbol}`: {exc}")
                continue

            if result.get("error"):
                errors.append(f"`{symbol}`: {result['error']}")
                continue

            update_coin_position(
                db, coin.id,
                entry_price=result.get("entry_price", 0),
                tp1_price=result.get("tp1_price", 0),
                tp2_price=result.get("tp2_price", 0),
                tp3_price=result.get("tp3_price", 0),
                tp_price=result.get("tp1_price", 0),
                current_sl_price=result.get("sl_price", 0),
                original_sl_price=result.get("original_sl_price") or result.get("sl_price", 0),
                amount=result.get("amount", 0),
                remaining_amount=result.get("amount", 0),
                tp1_order_id=result.get("tp1_order_id"),
                tp2_order_id=result.get("tp2_order_id"),
                tp3_order_id=result.get("tp3_order_id"),
                position_status="open",
                reentry_used=True,
                reentry_touched=False,
                reentry_price=0.0,
            )
            mark_reentry_events_used(db, pf.id, symbol)
            log_action(db, tid, "missing_coin_reentry", f"Re-entry for missing {symbol}", True, pf.id)
            success_line = f"`{symbol}` عند `{float(result.get('entry_price') or 0):.6g}`"
            if result.get("tp_warning"):
                success_line += f" (تحذير TP: {result['tp_warning']})"
            succeeded.append(success_line)

        context.user_data.pop(key, None)
        lines = ["🔄 *نتيجة إعادة الدخول*"]
        if succeeded:
            lines.append("\n✅ تم الشراء:")
            lines.extend(f"• {item}" for item in succeeded)
        if errors:
            lines.append("\n⚠️ لم يتم الشراء:")
            lines.extend(f"• {item}" for item in errors)
        lines.append("\nلم يتم تنفيذ أي عملة لم تكن محددة في شاشة التأكيد.")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf.id, pf.is_running),
        )
    finally:
        context.user_data.pop(f"{key}_in_progress", None)
        db.close()


async def _do_manual_reentry(query, tid, event_id):
    """Buy a stopped coin again only after the user presses its button."""
    import asyncio

    db = SessionLocal()
    try:
        event = get_trade_event(db, event_id, tid)
        if not event or event.event_type != "stop_loss" or not event.reentry_available or event.reentry_used:
            await query.edit_message_text("عملية إعادة الدخول غير متاحة أو تم تنفيذها مسبقاً.", reply_markup=main_menu_keyboard())
            return
        pf = get_portfolio(db, event.portfolio_id, tid)
        coin = next((c for c in (pf.coins if pf else []) if c.id == event.portfolio_coin_id), None)
        if not pf or not coin:
            await query.edit_message_text("المحفظة أو العملة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
        sl_pct = pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
        usdt = max(5.0, float(pf.investment_usdt or 0) / max(1, len(pf.coins)))

        await query.edit_message_text(f"⏳ جاري إعادة دخول `{coin.symbol}`...", parse_mode="Markdown")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: get_reb().reentry_buy_and_place_tp(
                coin.symbol, usdt, tp1, tp2, tp3, sl_pct, s1, s2,
            ),
        )
        if result.get("error"):
            await query.edit_message_text(
                f"⚠️ فشل إعادة دخول `{coin.symbol}`:\n`{result['error']}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛑 الاستوبات / إعادة الدخول", callback_data=f"stopped_{pf.id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf.id}")],
                ]),
            )
            return

        update_coin_position(
            db, coin.id,
            entry_price=result.get("entry_price", 0),
            tp1_price=result.get("tp1_price", 0),
            tp2_price=result.get("tp2_price", 0),
            tp3_price=result.get("tp3_price", 0),
            tp_price=result.get("tp1_price", 0),
            current_sl_price=result.get("sl_price", 0),
            original_sl_price=result.get("original_sl_price") or result.get("sl_price", 0),
            amount=result.get("amount", 0),
            remaining_amount=result.get("amount", 0),
            tp1_order_id=result.get("tp1_order_id"),
            tp2_order_id=result.get("tp2_order_id"),
            tp3_order_id=result.get("tp3_order_id"),
            position_status="open",
            reentry_used=True,
            reentry_touched=False,
            reentry_price=0.0,
        )
        event.reentry_available = False
        event.reentry_used = True
        db.commit()
        log_action(db, tid, "manual_reentry", f"Manual re-entry for {coin.symbol}", True, pf.id)
        await query.edit_message_text(
            f"✅ تمت إعادة دخول `{coin.symbol}` في محفظة *{pf.name}*\n"
            f"الدخول `{result.get('entry_price', 0):.6g}` | "
            f"TP1 `{result.get('tp1_price', 0):.6g}` | "
            f"TP2 `{result.get('tp2_price', 0):.6g}` | "
            f"TP3 `{result.get('tp3_price', 0):.6g}`\n"
            f"SL `{result.get('sl_price', 0):.6g}`",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf.id, pf.is_running),
        )
    finally:
        db.close()


async def _do_start(query, tid, pf_id):
    import asyncio

    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        if not coins:
            await query.edit_message_text("المحفظة بدون عملات.", reply_markup=pf_keyboard(pf_id, False))
            return
        if p.is_running:
            await query.edit_message_text("المحفظة شغالة مسبقاً.", reply_markup=pf_keyboard(pf_id, True))
            return

        # Check the wallet before buying so restarting a stopped portfolio
        # cannot purchase coins that are already held.
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(coins)
            )
        except Exception as exc:
            logger.exception("Portfolio start balance preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص أرصدة المحفظة من MEXC.\n"
                "لم يتم تشغيل المحفظة ولم يتم تنفيذ أي شراء.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return

        present_coins = [
            coin for coin in coins
            if presence.get(str(coin).upper().strip(), {}).get("present", False)
        ]
        missing_coins = [coin for coin in coins if coin not in present_coins]
        balance_lines = []
        for coin in present_coins:
            info = presence.get(str(coin).upper().strip(), {})
            amount = float(info.get("amount") or 0)
            value = float(info.get("market_value") or 0)
            balance_lines.append(f"`{coin}`: `{amount:.6g}` ≈ `{value:.2f}` USDT")

        user = get_or_create_user(db, tid)
        # Portfolio-specific overrides, else user defaults
        def _pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default
        tp1 = _pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
        sl_pct = _pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        purchase_result = {"executed": [], "errors": []}
        if missing_coins:
            # Keep the original per-coin allocation when only part of the
            # stopped portfolio is missing; never buy already-held coins.
            per_coin_usdt = float(p.investment_usdt or 0) / max(1, len(coins))
            purchase_total = per_coin_usdt * len(missing_coins)
            await query.edit_message_text(
                "⏳ جاري فحص المحفظة ثم شراء العملات الناقصة فقط...\n"
                f"الموجود: `{len(present_coins)}` | الناقص: `{len(missing_coins)}`"
            )
            purchase_result = await loop.run_in_executor(
                None,
                lambda: get_reb().start_portfolio(
                    coins=missing_coins,
                    total_usdt=purchase_total,
                    method=p.allocation_method or "equal",
                    min_trade_usdt=5.0,
                    dry_run=False,
                ),
            )
            if purchase_result.get("errors") and not purchase_result.get("executed"):
                err = "\n".join(str(e) for e in purchase_result["errors"])
                await query.edit_message_text(
                    f"❌ فشل شراء العملات الناقصة:\n`{err}`",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, False),
                )
                return
        else:
            await query.edit_message_text("✅ الرصيد موجود. جاري تشغيل المحفظة بدون شراء جديد...")

        import time
        time.sleep(1.5)
        coins_data = []
        for c in p.coins:
            amount = get_mexc().get_free_amount(c.symbol)
            price = get_mexc().get_ticker_price(f"{c.symbol}/USDT")
            coins_data.append({"symbol": c.symbol, "amount": amount, "entry_price": price})

        tp_results = get_reb().place_tp_orders(
            coins_data, tp1, tp2, tp3, sl_pct, s1, s2
        )

        lines = [
            (
                f"✅ الرصيد موجود وتم تشغيل *{p.name}* بدون شراء جديد."
                if not missing_coins
                else f"✅ تم تشغيل *{p.name}*"
            ),
            f"🎯 TP1 `{tp1}%`({s1}%) | TP2 `{tp2}%`({s2}%) | TP3 `{tp3}%`",
            f"🛡 استوب `{sl_pct}%`",
            "",
        ]
        if balance_lines:
            lines.extend(["💰 *الرصيد الموجود:*", *balance_lines, ""])
        if missing_coins:
            lines.append("🛒 تم شراء العملات الناقصة فقط: " + ", ".join(f"`{x}`" for x in missing_coins))
            lines.append("")
        if purchase_result.get("errors"):
            lines.append("⚠️ ملاحظات الشراء:\n" + "\n".join(f"• {x}" for x in purchase_result["errors"]))
        for r in tp_results:
            coin_obj = next((c for c in p.coins if c.symbol == r["symbol"]), None)
            if not coin_obj:
                continue
            update_coin_position(
                db, coin_obj.id,
                entry_price=r.get("entry_price", 0),
                tp1_price=r.get("tp1_price", 0),
                tp2_price=r.get("tp2_price", 0),
                tp3_price=r.get("tp3_price", 0),
                tp_price=r.get("tp1_price", 0),
                current_sl_price=r.get("sl_price", 0),
                original_sl_price=r.get("original_sl_price") or r.get("sl_price", 0),
                amount=r.get("amount", 0),
                remaining_amount=r.get("amount", 0),
                tp1_order_id=r.get("tp1_order_id"),
                tp2_order_id=r.get("tp2_order_id"),
                tp3_order_id=r.get("tp3_order_id"),
                position_status="open",
                reentry_used=False,
                reentry_touched=False,
                reentry_price=0.0,
            )
            if r.get("error"):
                lines.append(f"⚠️ `{r['symbol']}`: {r['error']}")
            lines.append(
                f"`{r['symbol']}` دخول `{r.get('entry_price',0):.6g}`\n"
                f"  TP1 `{r.get('tp1_price',0):.6g}` | TP2 `{r.get('tp2_price',0):.6g}` | "
                f"TP3 `{r.get('tp3_price',0):.6g}` | SL `{r.get('sl_price',0):.6g}`"
            )

        set_portfolio_running(db, pf_id, True)
        log_action(db, tid, "start", f"Started {p.name} multi-TP", True, pf_id)
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, True))
    finally:
        db.close()


async def _do_stop(query, tid, pf_id):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        await query.edit_message_text("⏳ جاري الإيقاف (إلغاء أوامر الهدف + بيع)...")

        # Cancel any open TP limit orders on MEXC
        tp_orders = []
        for c in p.coins:
            tp_orders.append({
                "symbol": c.symbol,
                "tp_order_id": getattr(c, "tp_order_id", None),
                "tp1_order_id": getattr(c, "tp1_order_id", None),
                "tp2_order_id": getattr(c, "tp2_order_id", None),
                "tp3_order_id": getattr(c, "tp3_order_id", None),
            })
        get_reb().cancel_tp_orders(tp_orders)

        stop_result = get_reb().stop_portfolio(coins, dry_run=False) if coins else {"executed": []}
        for sold in stop_result.get("executed", []):
            symbol = str(sold.get("symbol", "")).split("/")[0]
            coin = next((c for c in p.coins if c.symbol == symbol), None)
            amount = float(sold.get("amount") or 0)
            usdt = float(sold.get("usdt") or 0)
            exit_price = usdt / amount if amount > 0 else 0.0
            if coin and amount > 0 and float(coin.entry_price or 0) > 0:
                record_trade_event(
                    db, tid, p.id, coin.id, symbol, "manual_stop",
                    coin.entry_price, exit_price, amount,
                    (exit_price - coin.entry_price) * amount,
                    details="Manual portfolio stop",
                )

        from database import reset_coin_positions
        reset_coin_positions(db, pf_id)
        set_portfolio_running(db, pf_id, False)
        log_action(db, tid, "stop", f"Stopped {p.name}", True, pf_id)
        await query.edit_message_text(
            f"⏹ تم إيقاف *{p.name}*\nتم إلغاء أوامر الهدف وبيع العملات.\nالمحفظة محفوظة.",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, False),
        )
    finally:
        db.close()



async def _do_remove_coin(query, tid, pf_id, symbol):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coin = next((c for c in p.coins if c.symbol.upper() == symbol.upper()), None)
        if not coin:
            await query.edit_message_text(
                f"العملة `{symbol}` غير موجودة في المحفظة.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        await query.edit_message_text(
            f"⏳ جاري إلغاء أهداف `{coin.symbol}` وبيع الرصيد المتاح بسعر السوق..."
        )
        cancel_result = get_reb().cancel_tp_orders([{
            "symbol": coin.symbol,
            "tp_order_id": getattr(coin, "tp_order_id", None),
            "tp1_order_id": getattr(coin, "tp1_order_id", None),
            "tp2_order_id": getattr(coin, "tp2_order_id", None),
            "tp3_order_id": getattr(coin, "tp3_order_id", None),
        }])
        if cancel_result.get("errors"):
            error_text = "\n".join(
                str(error.get("error") or error)
                for error in cancel_result["errors"]
            )
            await query.edit_message_text(
                f"❌ تعذر إلغاء كل أهداف `{coin.symbol}`.\n"
                "لم يتم البيع أو حذف العملة من قاعدة البيانات حفاظًا على المركز.\n\n"
                f"`{error_text}`",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return
        other_running = db.query(PortfolioCoin).join(Portfolio).filter(
            PortfolioCoin.symbol == coin.symbol,
            PortfolioCoin.portfolio_id != p.id,
            Portfolio.telegram_id == tid,
            Portfolio.status == "active",
            Portfolio.is_running == True,
        ).first()
        tracked_amount = float(
            getattr(coin, "remaining_amount", 0)
            or getattr(coin, "amount", 0)
            or 0
        )
        if other_running and tracked_amount <= 0:
            # This portfolio has no tracked position to sell. Do not sell the
            # shared wallet balance that belongs to another running portfolio.
            stop_result = {"executed": [], "errors": []}
        else:
            stop_result = get_reb().stop_portfolio(
                [coin.symbol],
                dry_run=False,
                amount_overrides={coin.symbol: tracked_amount} if other_running else None,
            )
        errors = stop_result.get("errors") or []
        if errors:
            error_text = "\n".join(str(error) for error in errors)
            await query.edit_message_text(
                f"❌ لم يتم حذف `{coin.symbol}` من قاعدة البيانات لأن البيع لم يكتمل.\n"
                "تمت محاولة إلغاء أهدافها، لكن يجب معالجة خطأ البيع أولًا.\n\n"
                f"`{error_text}`",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        if not remove_coin_from_portfolio(db, pf_id, coin.symbol):
            await query.edit_message_text(
                "تعذر حذف سجل العملة من قاعدة البيانات بعد نجاح البيع.",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return
        cancelled = len(cancel_result.get("cancelled", []))
        sold = sum(float(item.get("usdt") or 0) for item in stop_result.get("executed", []))
        await query.edit_message_text(
            f"✅ تم حذف `{coin.symbol}` من المحفظة.\n"
            f"أوامر الأهداف الملغاة: `{cancelled}`\n"
            f"البيع بسعر السوق: `{sold:.2f}` USDT\n"
            "تم حذف وقف الخسارة السحابي مع بيانات العملة.",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, p.is_running),
        )
    finally:
        db.close()


async def _do_close(query, tid, pf_id):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        if p.is_running and coins:
            tp_orders = [{
                "symbol": c.symbol,
                "tp_order_id": getattr(c, "tp_order_id", None),
                "tp1_order_id": getattr(c, "tp1_order_id", None),
                "tp2_order_id": getattr(c, "tp2_order_id", None),
                "tp3_order_id": getattr(c, "tp3_order_id", None),
            } for c in p.coins]
            get_reb().cancel_tp_orders(tp_orders)
            get_reb().stop_portfolio(coins, dry_run=False)
        close_portfolio(db, pf_id)
        await query.edit_message_text("✅ تم حذف المحفظة.", reply_markup=main_menu_keyboard())
    finally:
        db.close()


async def create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["create"]["name"] = update.message.text.strip()
    await update.message.reply_text("أرسل *مبلغ الاستثمار* بالـ USDT:", parse_mode="Markdown")
    return CREATE_AMOUNT


async def create_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ""))
        if amount < 5:
            await update.message.reply_text("المبلغ لازم يكون ≥ 5 USDT")
            return CREATE_AMOUNT
        context.user_data["create"]["amount"] = amount
    except ValueError:
        await update.message.reply_text("أدخل رقم صحيح.")
        return CREATE_AMOUNT
    await update.message.reply_text("أرسل رموز العملات مفصولة بمسافة\nمثال: `BTC ETH SOL`", parse_mode="Markdown")
    return CREATE_COINS


async def create_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper().replace(",", " ")
    coins = [c.strip() for c in raw.split() if c.strip()]
    if not coins:
        await update.message.reply_text("أدخل عملة واحدة على الأقل.")
        return CREATE_COINS
    if len(coins) > 30:
        await update.message.reply_text("الحد الأقصى 30 عملة.")
        return CREATE_COINS
    data = context.user_data["create"]
    db = SessionLocal()
    try:
        p = create_portfolio(db, update.effective_user.id, data["name"], data["amount"], coins)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ تم إنشاء *{p.name}* (#{p.id})\nالمبلغ: `{p.investment_usdt}`\nالعملات: `{', '.join(coins)}`",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def add_coin_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.strip().upper()
    pf_id = context.user_data.get("addcoin_pf")
    if not pf_id:
        context.user_data.clear()
        await update.message.reply_text("حدث خطأ.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        ok, msg = add_coin_to_portfolio(db, pf_id, symbol, max_coins=user.max_coins_per_portfolio or 30)
        context.user_data.clear()
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def increase_amount_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ""))
        if amount <= 0:
            await update.message.reply_text("المبلغ لازم يكون موجب.")
            return INCREASE_AMOUNT
    except ValueError:
        await update.message.reply_text("أدخل رقم صحيح.")
        return INCREASE_AMOUNT
    pf_id = context.user_data.get("increase_pf")
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, update.effective_user.id)
        if not p:
            await update.message.reply_text("غير موجودة.", reply_markup=main_menu_keyboard())
            context.user_data.clear()
            return ConversationHandler.END
        p.investment_usdt += amount
        db.commit()
        if p.is_running:
            coins = [c.symbol for c in p.coins]
            if coins:
                get_reb().start_portfolio(coins=coins, total_usdt=amount,
                                          method=p.allocation_method or "equal", min_trade_usdt=5.0, dry_run=False)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ تم زيادة `{amount}` USDT\nالمخصص الجديد: `{p.investment_usdt:.2f}`",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def src_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("الاسم قصير جداً.")
        return SRC_NAME
    context.user_data["src"]["name"] = name
    await update.message.reply_text("أرسل *الحد الأدنى للقيمة بالدولار* (مثال: 15000000):", parse_mode="Markdown")
    return SRC_MIN_USD


async def src_min_usd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.strip().replace(",", "").replace("_", ""))
        context.user_data["src"]["min_usd"] = val
    except ValueError:
        await update.message.reply_text("أدخل رقم صحيح.")
        return SRC_MIN_USD
    await update.message.reply_text("أرسل *أقصى عدد تحويلات* (مثال: 3):")
    return SRC_MAX_TX


async def src_max_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text.strip())
        context.user_data["src"]["max_tx_count"] = val
    except ValueError:
        await update.message.reply_text("أدخل رقم صحيح.")
        return SRC_MAX_TX
    await update.message.reply_text("أرسل *أرقام محافظ الشراء* مفصولة بفاصلة (مثال: 1,3,5)\nأو `none`:", parse_mode="Markdown")
    return SRC_BUY_PFS


async def src_buy_pfs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    context.user_data["src"]["buy_portfolio_ids"] = "" if text == "none" else text.replace(" ", "")
    await update.message.reply_text("أرسل *أرقام محافظ البيع* مفصولة بفاصلة (مثال: 2,4)\nأو `none`:", parse_mode="Markdown")
    return SRC_SELL_PFS


async def src_sell_pfs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    data = context.user_data["src"]
    data["sell_portfolio_ids"] = "" if text == "none" else text.replace(" ", "")
    db = SessionLocal()
    try:
        src = create_signal_source(
            db, update.effective_user.id,
            name=data["name"],
            min_usd=data.get("min_usd", 15_000_000),
            max_tx_count=data.get("max_tx_count", 3),
            buy_portfolio_ids=data.get("buy_portfolio_ids", ""),
            sell_portfolio_ids=data.get("sell_portfolio_ids", ""),
        )
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ تم إنشاء مصدر *{src.name}*\n\n{format_source(src)}",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def edit_src_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    src_id = context.user_data.get("edit_src_id")
    field = context.user_data.get("edit_src_field")
    text = update.message.text.strip()
    if not src_id or not field:
        context.user_data.clear()
        await update.message.reply_text("حدث خطأ.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    kwargs = {}
    try:
        if field == "min_usd":
            kwargs["min_usd"] = float(text.replace(",", "").replace("_", ""))
        elif field == "max_tx_count":
            kwargs["max_tx_count"] = int(text)
        elif field == "cooldown_minutes":
            kwargs["cooldown_minutes"] = int(text)
        elif field in ("buy_portfolio_ids", "sell_portfolio_ids"):
            kwargs[field] = "" if text.lower() == "none" else text.replace(" ", "")
        else:
            await update.message.reply_text("حقل غير مدعوم.")
            return ConversationHandler.END
    except ValueError:
        await update.message.reply_text(
            "قيمة غير صحيحة، حاول مرة أخرى.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ رجوع", callback_data=f"edit_src_{src_id}")]
            ])
        )
        return EDIT_SRC_VALUE

    db = SessionLocal()
    try:
        src = update_signal_source(db, src_id, **kwargs)
        context.user_data.clear()
        if src:
            await update.message.reply_text(
                f"✅ تم التعديل\n\n{format_source(src)}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ تعديل تاني", callback_data=f"edit_src_{src_id}")],
                    [InlineKeyboardButton("📡 المصادر", callback_data="list_sources")],
                    [InlineKeyboardButton("⬅️ القائمة", callback_data="menu")],
                ])
            )
        else:
            await update.message.reply_text("المصدر غير موجود.", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE):
    """Background job: check open positions for TP fill / SL hit / re-entry."""
    import asyncio
    db = SessionLocal()
    try:
        positions = get_open_positions(db)
        if not positions:
            return
        # Run sync CCXT work off the event loop so Telegram stays responsive
        loop = asyncio.get_event_loop()
        actions = await loop.run_in_executor(
            None, lambda: get_reb().check_and_manage_positions(positions)
        )
        for act in actions:
            symbol = act["symbol"]
            coin_id = act.get("coin_id")
            if coin_id is None:
                # Compatibility with action payloads produced by older code.
                coin = next((c for c in positions if c.symbol == symbol), None)
            else:
                # A symbol can exist in multiple portfolios. Always reload the
                # exact row that produced the action, and skip it if a user
                # removed it while this monitor cycle was running.
                coin = db.query(PortfolioCoin).filter(
                    PortfolioCoin.id == coin_id
                ).first()
            if not coin:
                continue
            pf = coin.portfolio
            tid = pf.telegram_id if pf else config.ADMIN_TELEGRAM_ID

            if act["action"] == "tp1_hit":
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or 0)
                if filled_amount <= 0:
                    filled_amount = remaining_before * 0.40
                filled_amount = min(filled_amount, remaining_before)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp1_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "tp1",
                    coin.entry_price, fill_price, filled_amount, pnl,
                    details="TP1 filled",
                )
                update_coin_position(
                    db, coin.id,
                    position_status="tp1_hit",
                    # Break-even is the original entry, not TP1.
                    current_sl_price=coin.entry_price,
                    remaining_amount=max(0.0, remaining_before - filled_amount),
                    tp1_order_id=None,
                )
                msg = (
                    f"🎯 *تحقق الهدف 1* — `{symbol}`\n"
                    f"السعر: `{act['price']:.6g}`\n"
                    f"تم نقل الاستوب إلى سعر الدخول `{coin.entry_price:.6g}`\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "tp2_hit":
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or 0)
                if filled_amount <= 0:
                    filled_amount = remaining_before * 0.50
                filled_amount = min(filled_amount, remaining_before)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp2_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "tp2",
                    coin.entry_price, fill_price, filled_amount, pnl,
                    details="TP2 filled",
                )
                update_coin_position(
                    db, coin.id,
                    position_status="tp2_hit",
                    current_sl_price=act["new_sl"],
                    remaining_amount=max(0.0, remaining_before - filled_amount),
                    tp2_order_id=None,
                )
                msg = (
                    f"🎯 *تحقق الهدف 2* — `{symbol}`\n"
                    f"السعر: `{act['price']:.6g}`\n"
                    f"تم رفع الاستوب إلى `{act['new_sl']:.6g}`\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "tp3_hit":
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or remaining_before)
                filled_amount = min(filled_amount, remaining_before)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp3_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "tp3",
                    coin.entry_price, fill_price, filled_amount, pnl,
                    details="TP3 filled",
                )
                update_coin_position(
                    db, coin.id,
                    position_status="closed",
                    current_sl_price=0.0,
                    remaining_amount=max(0.0, remaining_before - filled_amount),
                    amount=max(0.0, remaining_before - filled_amount),
                    tp3_order_id=None,
                )
                msg = (
                    f"🎯 *تحقق الهدف 3 (الأخير)* — `{symbol}`\n"
                    f"السعر: `{act['price']:.6g}`\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "sl_hit_wait_reentry":
                update_coin_position(
                    db, coin.id,
                    position_status="waiting_reentry",
                    current_sl_price=0.0,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    amount=0.0,
                    reentry_price=act["reentry_price"],
                    reentry_touched=False,
                    reentry_used=False,
                )
                msg = (
                    f"🛡 *ضرب الاستوب{' المرفوع' if act.get('was_raised') else ''}* — `{symbol}`\n"
                    f"تم البيع ≈ `{act['price']:.6g}`\n"
                    f"⏳ انتظار إعادة دخول عند الاستوب الأصلي `{act['reentry_price']:.6g}`\n"
                    f"(لمس + ارتداد 1%)\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "reentry_touched":
                update_coin_position(db, coin.id, reentry_touched=True)
                msg = (
                    f"📍 *لمس منطقة إعادة الدخول* — `{symbol}`\n"
                    f"السعر `{act['price']:.6g}` ≤ `{act['reentry_price']:.6g}`\n"
                    f"في انتظار ارتداد +1% للشراء..."
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "reentry_buy":
                # buy again with equal share of portfolio investment
                user = get_or_create_user(db, tid)
                def _pct(pf_val, user_val, default):
                    if pf_val is not None and float(pf_val) > 0:
                        return float(pf_val)
                    if user_val is not None and float(user_val) > 0:
                        return float(user_val)
                    return default
                tp1 = _pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
                tp2 = _pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
                tp3 = _pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
                s1 = _pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
                s2 = _pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
                sl_pct = _pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
                n_coins = max(1, len(pf.coins) if pf else 1)
                usdt = (pf.investment_usdt if pf else 20) / n_coins
                loop = asyncio.get_event_loop()
                buy_res = await loop.run_in_executor(
                    None,
                    lambda: get_reb().reentry_buy_and_place_tp(
                        symbol, usdt, tp1, tp2, tp3, sl_pct, s1, s2
                    ),
                )
                if buy_res.get("error"):
                    try:
                        await context.bot.send_message(
                            tid, f"⚠️ فشل إعادة دخول `{symbol}`: `{buy_res['error']}`", parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                else:
                    update_coin_position(
                        db, coin.id,
                        entry_price=buy_res.get("entry_price", act["price"]),
                        tp1_price=buy_res.get("tp1_price", 0),
                        tp2_price=buy_res.get("tp2_price", 0),
                        tp3_price=buy_res.get("tp3_price", 0),
                        current_sl_price=buy_res.get("sl_price", 0),
                        original_sl_price=buy_res.get("original_sl_price") or buy_res.get("sl_price", 0),
                        amount=buy_res.get("amount", 0),
                        remaining_amount=buy_res.get("amount", 0),
                        tp1_order_id=buy_res.get("tp1_order_id"),
                        tp2_order_id=buy_res.get("tp2_order_id"),
                        tp3_order_id=buy_res.get("tp3_order_id"),
                        position_status="open",
                        reentry_used=True,
                        reentry_touched=False,
                        reentry_price=0.0,
                    )
                    msg = (
                        f"🔄 *إعادة دخول* — `{symbol}`\n"
                        f"شراء عند ≈ `{buy_res.get('entry_price', act['price']):.6g}`\n"
                        f"TP1 `{buy_res.get('tp1_price', 0):.6g}` | "
                        f"TP2 `{buy_res.get('tp2_price', 0):.6g}` | "
                        f"TP3 `{buy_res.get('tp3_price', 0):.6g}`\n"
                        f"SL `{buy_res.get('sl_price', 0):.6g}`\n"
                        f"(مرة واحدة فقط لهذه الدورة)\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                    try:
                        await context.bot.send_message(tid, msg, parse_mode="Markdown")
                    except Exception:
                        pass

            elif act["action"] == "sl_hit_sold":
                exit_price = float(act.get("price") or 0)
                stopped_amount = float(act.get("amount") or 0)
                entry_price = float(coin.entry_price or 0)
                pnl = (exit_price - entry_price) * stopped_amount
                reentry_available = bool(act.get("reentry_available"))
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "stop_loss",
                    entry_price, exit_price, stopped_amount, pnl,
                    reentry_available=reentry_available,
                    details="Stop loss filled",
                )
                update_coin_position(
                    db, coin.id,
                    position_status="stopped",
                    current_sl_price=0.0,
                    tp_order_id=None,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    amount=0.0,
                    remaining_amount=0.0,
                    reentry_price=act.get("reentry_price", 0.0),
                    reentry_touched=False,
                    reentry_used=not reentry_available,
                )
                raised = " (بعد رفع الاستوب)" if act.get("was_raised") else ""
                msg = (
                    f"🛡 *ضرب الاستوب{raised}* — `{symbol}`\n"
                    f"تم البيع فوراً بسعر السوق ≈ `{act['price']:.6g}`\n"
                    f"النتيجة: `{pnl:+.2f}` USDT\n"
                    f"{'يمكنك اختيار إعادة الدخول من زر الاستوبات.' if reentry_available else 'لا توجد إعادة دخول متاحة لهذه الدورة.'}\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(
                        tid,
                        msg,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                "🔄 عرض العملات وإعادة الدخول",
                                callback_data=f"stopped_{pf.id}",
                            )]
                        ]) if reentry_available else None,
                    )
                except Exception:
                    pass

            elif act["action"] == "sl_sell_failed":
                try:
                    await context.bot.send_message(
                        tid,
                        f"⚠️ فشل بيع `{symbol}` بعد ضرب الاستوب:\n`{act.get('error')}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
    except Exception:
        logger.exception("monitor_positions_job error")
    finally:
        db.close()


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN مطلوب")
    if not config.MEXC_API_KEY or not config.MEXC_API_SECRET:
        raise SystemExit("MEXC_API_KEY و MEXC_API_SECRET مطلوبان")
    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL مطلوب")

    init_db()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Cloud monitor for TP/SL every 25 seconds
    if app.job_queue:
        app.job_queue.run_repeating(
            monitor_positions_job,
            interval=45,
            first=15,
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
        logger.info("Position monitor job scheduled (every 45s)")
    else:
        logger.warning("JobQueue not available — install python-telegram-bot[job-queue]")

    async def entry_create(update, context):
        await on_callback(update, context)
        return CREATE_NAME

    async def entry_addcoin(update, context):
        await on_callback(update, context)
        return ADD_COIN

    async def entry_increase(update, context):
        await on_callback(update, context)
        return INCREASE_AMOUNT

    async def entry_create_src(update, context):
        await on_callback(update, context)
        return SRC_NAME

    async def entry_edit_src(update, context):
        await on_callback(update, context)
        return EDIT_SRC_VALUE

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_create, pattern="^create_pf$"),
            CallbackQueryHandler(entry_addcoin, pattern="^addcoin_"),
            CallbackQueryHandler(entry_increase, pattern="^increase_"),
            CallbackQueryHandler(entry_create_src, pattern="^create_src$"),
            CallbackQueryHandler(entry_edit_src, pattern="^editf_"),
        ],
        states={
            CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_name)],
            CREATE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_amount)],
            CREATE_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_coins)],
            ADD_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_coin_msg)],
            INCREASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, increase_amount_msg)],
            SRC_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, src_name)],
            SRC_MIN_USD: [MessageHandler(filters.TEXT & ~filters.COMMAND, src_min_usd)],
            SRC_MAX_TX: [MessageHandler(filters.TEXT & ~filters.COMMAND, src_max_tx)],
            SRC_BUY_PFS: [MessageHandler(filters.TEXT & ~filters.COMMAND, src_buy_pfs)],
            SRC_SELL_PFS: [MessageHandler(filters.TEXT & ~filters.COMMAND, src_sell_pfs)],
            EDIT_SRC_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_src_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

    logger.info("Bot starting (Portfolio Manager + Signal Engine)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
