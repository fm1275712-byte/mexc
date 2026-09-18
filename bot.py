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
    close_portfolio, set_portfolio_running, log_action,
    get_signal_sources, get_signal_source, create_signal_source,
    update_signal_source, delete_signal_source, log_signal, parse_portfolio_ids,
    SignalLog, update_coin_position, reset_coin_positions, get_open_positions,
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
            await msg.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
        return False
    return True


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 محافظي", callback_data="list_pf")],
        [InlineKeyboardButton("➕ محفظة جديدة", callback_data="create_pf")],
        [InlineKeyboardButton("📡 مصادر الإشارات", callback_data="list_sources")],
        [InlineKeyboardButton("💰 الرصيد", callback_data="balance")],
        [InlineKeyboardButton("⚙️ إعدادات", callback_data="settings")],
    ])


def pf_keyboard(pf_id: int, is_running: bool):
    rows = []
    if is_running:
        rows.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{pf_id}")])
    else:
        rows.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{pf_id}")])
    rows.append([InlineKeyboardButton("📈 زيادة استثمار", callback_data=f"increase_{pf_id}")])
    rows.append([InlineKeyboardButton("🎯 أهداف هذه المحفظة", callback_data=f"pf_tpsl_{pf_id}")])
    rows.append([
        InlineKeyboardButton("➕ عملة", callback_data=f"addcoin_{pf_id}"),
        InlineKeyboardButton("➖ عملة", callback_data=f"removecoin_{pf_id}"),
    ])
    rows.append([InlineKeyboardButton("🗑 حذف المحفظة", callback_data=f"close_{pf_id}")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="list_pf")])
    return InlineKeyboardMarkup(rows)


def sources_keyboard(sources):
    buttons = []
    for s in sources:
        icon = "🟢" if s.enabled else "🔴"
        buttons.append([InlineKeyboardButton(f"{icon} {s.name}", callback_data=f"view_src_{s.id}")])
    buttons.append([InlineKeyboardButton("➕ مصدر جديد", callback_data="create_src")])
    buttons.append([InlineKeyboardButton("⬅️ القائمة", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def source_detail_keyboard(src_id: int, enabled: bool):
    rows = []
    if enabled:
        rows.append([InlineKeyboardButton("⏹ تعطيل", callback_data=f"toggle_src_{src_id}")])
    else:
        rows.append([InlineKeyboardButton("▶️ تفعيل", callback_data=f"toggle_src_{src_id}")])
    rows.append([InlineKeyboardButton("✏️ تعديل", callback_data=f"edit_src_{src_id}")])
    rows.append([InlineKeyboardButton("🗑 حذف المصدر", callback_data=f"del_src_{src_id}")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="list_sources")])
    return InlineKeyboardMarkup(rows)


def format_pf(p) -> str:
    coins = ", ".join(c.symbol for c in p.coins) or "—"
    status = "🟢 شغالة" if p.is_running else "⚪ متوقفة"
    lines = [
        f"📁 *{p.name}* (#{p.id})",
        f"الحالة: {status}",
        f"المخصص: `{p.investment_usdt:.2f}` USDT",
        f"العملات: `{coins}`",
    ]
    # show portfolio-specific TP/SL if set
    t1 = getattr(p, "tp1_pct", None)
    t2 = getattr(p, "tp2_pct", None)
    t3 = getattr(p, "tp3_pct", None)
    sl = getattr(p, "stop_loss_pct", None)
    if any(v is not None and v > 0 for v in (t1, t2, t3, sl)):
        lines.append(
            f"🎯 أهداف المحفظة: TP1 `{t1 or '—'}`% | TP2 `{t2 or '—'}`% | "
            f"TP3 `{t3 or '—'}`% | SL `{sl or '—'}%`"
        )
    else:
        lines.append("🎯 الأهداف: *من الإعدادات العامة*")
    if p.is_running:
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
                    f"  `{c.symbol}` دخول `{c.entry_price:.6g}`\n"
                    f"    TP1 `{getattr(c,'tp1_price',0):.6g}` | TP2 `{getattr(c,'tp2_price',0):.6g}` | "
                    f"TP3 `{getattr(c,'tp3_price',0):.6g}`\n"
                    f"    استوب `{c.current_sl_price:.6g}` ({st})"
                )
    return "\n".join(lines)


def format_source(s) -> str:
    return (
        f"📡 *{s.name}* (#{s.id})\n"
        f"الحالة: {'🟢 مفعل' if s.enabled else '🔴 معطل'}\n"
        f"الحد الأدنى: `{s.min_usd:,.0f}$`\n"
        f"أقصى تحويلات: `{s.max_tx_count}`\n"
        f"شراء مسموح: {'نعم' if s.allow_buy else 'لا'}\n"
        f"بيع مسموح: {'نعم' if s.allow_sell else 'لا'}\n"
        f"محافظ الشراء: `{s.buy_portfolio_ids or '—'}`\n"
        f"محافظ البيع: `{s.sell_portfolio_ids or '—'}`\n"
        f"التبريد: `{s.cooldown_minutes}` دقيقة"
    )


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
        "👋 *MEXC Portfolio Manager + Signal Engine*\n\nإدارة محافظك + تنفيذ إشارات ذكية من المجموعة.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("تم الإلغاء.", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("القائمة الرئيسية:", reply_markup=main_menu_keyboard())
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
                    get_reb().stop_portfolio(coins, dry_run=False)
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
        await query.edit_message_text("القائمة الرئيسية:", reply_markup=main_menu_keyboard())
        return

    if data == "list_pf":
        db = SessionLocal()
        try:
            pfs = get_portfolios(db, tid, status="active")
            if not pfs:
                await query.edit_message_text("لا توجد محافظ نشطة.\nاضغط ➕ لإنشاء محفظة.", reply_markup=main_menu_keyboard())
                return
            buttons = [[InlineKeyboardButton(f"{'🟢' if p.is_running else '⚪'} #{p.id} {p.name} ({p.investment_usdt:.0f}$)", callback_data=f"view_{p.id}")] for p in pfs]
            buttons.append([InlineKeyboardButton("⬅️ القائمة", callback_data="menu")])
            await query.edit_message_text("📋 *محافظك النشطة:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
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


async def _do_start(query, tid, pf_id):
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

        await query.edit_message_text("⏳ جاري الشراء...")
        result = get_reb().start_portfolio(
            coins=coins, total_usdt=p.investment_usdt,
            method=p.allocation_method or "equal", min_trade_usdt=5.0, dry_run=False,
        )
        if result.get("errors") and not result.get("executed"):
            err = "\n".join(str(e) for e in result["errors"])
            await query.edit_message_text(f"❌ فشل:\n`{err}`", parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, False))
            return

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
            f"✅ تم تشغيل *{p.name}*",
            f"🎯 TP1 `{tp1}%`({s1}%) | TP2 `{tp2}%`({s2}%) | TP3 `{tp3}%`",
            f"🛡 استوب `{sl_pct}%`",
            "",
        ]
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

        if coins:
            get_reb().stop_portfolio(coins, dry_run=False)

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
        try:
            get_reb().stop_portfolio([symbol], dry_run=False)
        except Exception:
            pass
        remove_coin_from_portfolio(db, pf_id, symbol)
        await query.edit_message_text(f"✅ تم حذف `{symbol}`", parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, p.is_running))
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
            coin = next((c for c in positions if c.symbol == symbol), None)
            if not coin:
                continue
            pf = coin.portfolio
            tid = pf.telegram_id if pf else config.ADMIN_TELEGRAM_ID

            if act["action"] == "tp1_hit":
                update_coin_position(
                    db, coin.id,
                    position_status="tp1_hit",
                    current_sl_price=act["new_sl"],
                    tp1_order_id=None,
                )
                msg = (
                    f"🎯 *تحقق الهدف 1* — `{symbol}`\n"
                    f"السعر: `{act['price']:.6g}`\n"
                    f"تم رفع الاستوب إلى `{act['new_sl']:.6g}`\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "tp2_hit":
                update_coin_position(
                    db, coin.id,
                    position_status="tp2_hit",
                    current_sl_price=act["new_sl"],
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
                update_coin_position(
                    db, coin.id,
                    position_status="tp3_hit",
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
                update_coin_position(
                    db, coin.id,
                    position_status="closed",
                    tp_order_id=None,
                    amount=0.0,
                )
                raised = " (بعد رفع الاستوب)" if act.get("was_raised") else ""
                msg = (
                    f"🛡 *ضرب الاستوب{raised}* — `{symbol}`\n"
                    f"تم البيع فوراً بسعر السوق ≈ `{act['price']:.6g}`\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
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
