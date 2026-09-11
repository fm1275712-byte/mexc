"""
    MEXC Portfolio Manager — Telegram Bot
    بوت تليجرام لإدارة محافظ متعددة على MEXC Spot.
"""
import logging
from typing import Optional

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
)
from mexc_client import MexcClient
from rebalancer import Rebalancer

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
(
    CREATE_NAME,
    CREATE_AMOUNT,
    CREATE_COINS,
    ADD_COIN,
    INCREASE_AMOUNT,
    CONFIRM_ACTION,
) = range(6)

# Global clients (lazy)
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
        return True  # open if not set
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
        [InlineKeyboardButton("💰 الرصيد", callback_data="balance")],
        [InlineKeyboardButton("⚙️ إعدادات", callback_data="settings")],
    ])


def pf_keyboard(pf_id: int, is_running: bool):
    rows = []
    if is_running:
        rows.append([InlineKeyboardButton("⏹ إيقاف", callback_data=f"stop_{pf_id}")])
        rows.append([InlineKeyboardButton("📈 زيادة استثمار", callback_data=f"increase_{pf_id}")])
        rows.append([
            InlineKeyboardButton("🔄 معاينة إعادة توازن", callback_data=f"rebal_dry_{pf_id}"),
            InlineKeyboardButton("✅ تنفيذ إعادة توازن", callback_data=f"rebal_run_{pf_id}"),
        ])
    else:
        rows.append([InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{pf_id}")])

    rows.append([
        InlineKeyboardButton("➕ عملة", callback_data=f"addcoin_{pf_id}"),
        InlineKeyboardButton("➖ عملة", callback_data=f"removecoin_{pf_id}"),
    ])
    rows.append([InlineKeyboardButton("🗑 إنهاء المحفظة", callback_data=f"close_{pf_id}")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="list_pf")])
    return InlineKeyboardMarkup(rows)


def format_pf(p) -> str:
    coins = ", ".join(c.symbol for c in p.coins) or "—"
    status = "🟢 شغالة" if p.is_running else "⚪ متوقفة"
    return (
        f"📁 *{p.name}* (#{p.id})\n"
        f"الحالة: {status}\n"
        f"المخصص: `{p.investment_usdt:.2f}` USDT\n"
        f"العملات: `{coins}`\n"
        f"العتبة: `{p.threshold}%`"
    )


# -------------------- Commands --------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin(update):
        return
    db = SessionLocal()
    try:
        get_or_create_user(db, update.effective_user.id)
    finally:
        db.close()
    await update.message.reply_text(
        "👋 *MEXC Portfolio Manager*\n\nإدارة محافظك على MEXC Spot من التليجرام فقط.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("تم الإلغاء.", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("القائمة الرئيسية:", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# -------------------- Callbacks --------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
                await query.edit_message_text(
                    "لا توجد محافظ نشطة.\nاضغط ➕ لإنشاء محفظة.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            buttons = []
            for p in pfs:
                icon = "🟢" if p.is_running else "⚪"
                buttons.append([InlineKeyboardButton(
                    f"{icon} {p.name} ({p.investment_usdt:.0f}$)",
                    callback_data=f"view_{p.id}"
                )])
            buttons.append([InlineKeyboardButton("⬅️ القائمة", callback_data="menu")])
            await query.edit_message_text("📋 *محافظك النشطة:*", parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup(buttons))
        finally:
            db.close()
        return

    if data.startswith("view_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            # live value
            coins = [c.symbol for c in p.coins]
            live = ""
            if coins:
                try:
                    val = get_mexc().get_coins_value(coins)
                    live = f"\nالقيمة الحالية: `{val['total_usdt']:.2f}` USDT"
                except Exception:
                    pass
            text = format_pf(p) + live
            await query.edit_message_text(text, parse_mode="Markdown",
                                          reply_markup=pf_keyboard(p.id, p.is_running))
        finally:
            db.close()
        return

    if data == "balance":
        try:
            data_bal = get_mexc().get_portfolio_value()
            free = get_mexc().get_free_usdt()
            lines = [f"💰 *الرصيد الكلي:* `{data_bal['total_usdt']:.2f}` USDT",
                     f"USDT حر: `{free:.2f}`\n"]
            for asset, info in sorted(data_bal["assets"].items(), key=lambda x: -x[1]["usdt_value"])[:15]:
                if info["usdt_value"] < 0.5:
                    continue
                lines.append(f"`{asset}`: {info['amount']:.6g} ≈ `{info['usdt_value']:.2f}$`")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup(
                                              [[InlineKeyboardButton("⬅️ رجوع", callback_data="menu")]]))
        except Exception as e:
            await query.edit_message_text(f"خطأ في جلب الرصيد:\n`{e}`", parse_mode="Markdown",
                                          reply_markup=main_menu_keyboard())
        return

    if data == "settings":
        db = SessionLocal()
        try:
            user = get_or_create_user(db, tid)
            text = (
                "⚙️ *الإعدادات*\n\n"
                f"عتبة إعادة التوازن: `{user.default_threshold}%`\n"
                f"أقل صفقة: `{user.min_trade_usdt}` USDT\n"
                f"أقصى عملات لكل محفظة: `{user.max_coins_per_portfolio}`\n"
            )
            await query.edit_message_text(text, parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup(
                                              [[InlineKeyboardButton("⬅️ رجوع", callback_data="menu")]]))
        finally:
            db.close()
        return

    if data == "create_pf":
        context.user_data["create"] = {}
        await query.edit_message_text("أرسل *اسم المحفظة*:", parse_mode="Markdown")
        return CREATE_NAME

    # ---- Portfolio actions ----
    if data.startswith("start_"):
        pf_id = int(data.split("_")[1])
        await _do_start(query, tid, pf_id)
        return
    if data.startswith("stop_"):
        pf_id = int(data.split("_")[1])
        await _do_stop(query, tid, pf_id)
        return
    if data.startswith("increase_"):
        pf_id = int(data.split("_")[1])
        context.user_data["increase_pf"] = pf_id
        await query.edit_message_text("أرسل مبلغ الزيادة بالـ USDT:")
        return INCREASE_AMOUNT
    if data.startswith("rebal_dry_"):
        pf_id = int(data.split("_")[2])
        await _do_rebalance(query, tid, pf_id, dry_run=True)
        return
    if data.startswith("rebal_run_"):
        pf_id = int(data.split("_")[2])
        await _do_rebalance(query, tid, pf_id, dry_run=False)
        return
    if data.startswith("addcoin_"):
        pf_id = int(data.split("_")[1])
        context.user_data["addcoin_pf"] = pf_id
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
            buttons = [[InlineKeyboardButton(f"حذف {c.symbol}", callback_data=f"delcoin_{pf_id}_{c.symbol}")]
                       for c in p.coins]
            buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data=f"view_{pf_id}")])
            await query.edit_message_text("اختر العملة للحذف (سيتم بيعها إن وجدت):",
                                          reply_markup=InlineKeyboardMarkup(buttons))
        finally:
            db.close()
        return
    if data.startswith("delcoin_"):
        parts = data.split("_")
        pf_id, symbol = int(parts[1]), parts[2]
        await _do_remove_coin(query, tid, pf_id, symbol)
        return
    if data.startswith("close_"):
        pf_id = int(data.split("_")[1])
        context.user_data["confirm_close"] = pf_id
        await query.edit_message_text(
            "⚠️ هل أنت متأكد من إنهاء المحفظة؟\n(سيتم بيع كل العملات إن كانت شغالة)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، أنهِ", callback_data=f"confirm_close_{pf_id}")],
                [InlineKeyboardButton("❌ إلغاء", callback_data=f"view_{pf_id}")],
            ])
        )
        return
    if data.startswith("confirm_close_"):
        pf_id = int(data.split("_")[2])
        await _do_close(query, tid, pf_id)
        return


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
            await query.edit_message_text("المحفظة شغالة مسبقاً. استخدم زيادة الاستثمار.",
                                          reply_markup=pf_keyboard(pf_id, True))
            return
        await query.edit_message_text("⏳ جاري الشراء...")
        result = get_reb().start_portfolio(
            coins=coins,
            total_usdt=p.investment_usdt,
            method=p.allocation_method or "equal",
            min_trade_usdt=5.0,
            dry_run=False,
        )
        if result.get("errors") and not result.get("executed"):
            err = "\n".join(str(e) for e in result["errors"])
            await query.edit_message_text(f"❌ فشل التشغيل:\n{err}",
                                          reply_markup=pf_keyboard(pf_id, False))
            return
        set_portfolio_running(db, pf_id, True)
        log_action(db, tid, "start", p.name, True, pf_id)
        msg = f"✅ تم تشغيل *{p.name}*\n"
        for e in result.get("executed", []):
            msg += f"• {e.get('symbol')}: {e.get('usdt', 0):.2f}$\n"
        if result.get("errors"):
            msg += "\nتحذيرات:\n" + "\n".join(str(x) for x in result["errors"])
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=pf_keyboard(pf_id, True))
    except Exception as e:
        await query.edit_message_text(f"خطأ: `{e}`", parse_mode="Markdown",
                                      reply_markup=main_menu_keyboard())
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
        await query.edit_message_text("⏳ جاري البيع...")
        result = get_reb().stop_portfolio(coins, dry_run=False)
        set_portfolio_running(db, pf_id, False)
        log_action(db, tid, "stop", p.name, True, pf_id)
        total = result.get("total_sold_usdt", 0)
        msg = f"⏹ تم إيقاف *{p.name}*\nتم بيع ≈ `{total:.2f}` USDT"
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=pf_keyboard(pf_id, False))
    except Exception as e:
        await query.edit_message_text(f"خطأ: `{e}`", parse_mode="Markdown",
                                      reply_markup=main_menu_keyboard())
    finally:
        db.close()


async def _do_rebalance(query, tid, pf_id, dry_run: bool):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        if not coins:
            await query.edit_message_text("بدون عملات.", reply_markup=pf_keyboard(pf_id, p.is_running))
            return
        await query.edit_message_text("⏳ جاري الحساب...")
        result = get_reb().rebalance_portfolio(
            coins=coins,
            target_capital=p.investment_usdt,
            method=p.allocation_method or "equal",
            threshold=p.threshold or 2.0,
            min_trade_usdt=5.0,
            dry_run=dry_run,
        )
        prefix = "معاينة" if dry_run else "تنفيذ"
        msg = f"🔄 *{prefix} إعادة التوازن* — {p.name}\n\n"
        for e in result.get("executed", []):
            msg += f"• {e.get('side')} {e.get('symbol')}: {e.get('usdt', 0):.2f}$\n"
        if not result.get("executed"):
            msg += "لا حاجة لتعديل (ضمن العتبة).\n"
        if result.get("errors"):
            msg += "\n" + "\n".join(str(x) for x in result["errors"])
        if not dry_run:
            log_action(db, tid, "rebalance", p.name, True, pf_id)
        await query.edit_message_text(msg, parse_mode="Markdown",
                                      reply_markup=pf_keyboard(pf_id, p.is_running))
    except Exception as e:
        await query.edit_message_text(f"خطأ: `{e}`", parse_mode="Markdown",
                                      reply_markup=main_menu_keyboard())
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
        log_action(db, tid, "remove_coin", symbol, True, pf_id)
        p = get_portfolio(db, pf_id, tid)
        await query.edit_message_text(f"✅ تم حذف وبيع `{symbol}` إن وُجد.",
                                      parse_mode="Markdown",
                                      reply_markup=pf_keyboard(pf_id, p.is_running if p else False))
    finally:
        db.close()


async def _do_close(query, tid, pf_id):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        name = p.name
        if p.is_running:
            coins = [c.symbol for c in p.coins]
            try:
                get_reb().stop_portfolio(coins, dry_run=False)
            except Exception:
                pass
        close_portfolio(db, pf_id)
        log_action(db, tid, "close", name, True, pf_id)
        await query.edit_message_text(f"🗑 تم إنهاء المحفظة *{name}*.", parse_mode="Markdown",
                                      reply_markup=main_menu_keyboard())
    finally:
        db.close()


# -------------------- Conversation handlers --------------------

async def create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin(update):
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not name or len(name) > 50:
        await update.message.reply_text("اسم غير صالح. أرسل اسماً أقصر:")
        return CREATE_NAME
    context.user_data.setdefault("create", {})["name"] = name
    await update.message.reply_text("أرسل *مبلغ الاستثمار* بالـ USDT (مثال: 100):", parse_mode="Markdown")
    return CREATE_AMOUNT


async def create_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().replace(",", ".")
    try:
        amount = float(text)
        if amount < 5:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("أدخل رقماً صحيحاً ≥ 5:")
        return CREATE_AMOUNT
    context.user_data["create"]["amount"] = amount
    await update.message.reply_text(
        "أرسل العملات مفصولة بمسافة أو فاصلة\n(مثال: BTC ETH SOL)\nأو أرسل `-` بدون عملات الآن:"
    )
    return CREATE_COINS


async def create_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    coins = []
    if raw != "-":
        for part in raw.replace(",", " ").split():
            s = part.strip().upper()
            if s and s not in coins:
                coins.append(s)
    data = context.user_data.get("create", {})
    name = data.get("name", "محفظة")
    amount = data.get("amount", 0)
    tid = update.effective_user.id
    db = SessionLocal()
    try:
        # validate markets lightly
        for c in coins:
            pair = f"{c}/USDT"
            try:
                markets = get_mexc().exchange.load_markets()
                m = markets.get(pair)
                if not (m and m.get("active", True) and m.get("spot", True)):
                    await update.message.reply_text(f"❌ `{c}` غير متاحة على MEXC Spot. أعد إدخال العملات:")
                    return CREATE_COINS
            except Exception:
                pass
        p = create_portfolio(db, tid, name, amount, coins)
        log_action(db, tid, "create", name, True, p.id)
        await update.message.reply_text(
            f"✅ تم إنشاء المحفظة *{name}*\nالمخصص: `{amount:.2f}` USDT\nالعملات: `{', '.join(coins) or '—'}`",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(p.id, False),
        )
    except Exception as e:
        await update.message.reply_text(f"خطأ: {e}", reply_markup=main_menu_keyboard())
    finally:
        db.close()
        context.user_data.pop("create", None)
    return ConversationHandler.END


async def add_coin_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin(update):
        return ConversationHandler.END
    symbol = (update.message.text or "").strip().upper()
    pf_id = context.user_data.get("addcoin_pf")
    if not pf_id or not symbol:
        await update.message.reply_text("ألغيت.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    tid = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, tid)
        ok, msg = add_coin_to_portfolio(db, pf_id, symbol, max_coins=user.max_coins_per_portfolio)
        if not ok:
            await update.message.reply_text(msg, parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard())
            return ConversationHandler.END
        p = get_portfolio(db, pf_id, tid)
        buy_info = None
        if p and p.is_running and p.investment_usdt > 0:
            n = len(p.coins) or 1
            usdt_for_new = p.investment_usdt / n
            try:
                buy_info = get_reb().start_portfolio(
                    coins=[symbol], total_usdt=usdt_for_new,
                    method="equal", min_trade_usdt=5.0, dry_run=False,
                )
            except Exception as e:
                buy_info = {"errors": [str(e)]}
        log_action(db, tid, "add_coin", symbol, True, pf_id)
        extra = ""
        if buy_info and buy_info.get("executed"):
            extra = f"\nتم شراء جزء من `{symbol}`."
        await update.message.reply_text(msg + extra, parse_mode="Markdown",
                                        reply_markup=pf_keyboard(pf_id, p.is_running if p else False))
    finally:
        db.close()
        context.user_data.pop("addcoin_pf", None)
    return ConversationHandler.END


async def increase_amount_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip().replace(",", ".")
    try:
        amount = float(text)
        if amount < 5:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("أدخل رقماً ≥ 5:")
        return INCREASE_AMOUNT
    pf_id = context.user_data.get("increase_pf")
    if not pf_id:
        await update.message.reply_text("ألغيت.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    tid = update.effective_user.id
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await update.message.reply_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return ConversationHandler.END
        coins = [c.symbol for c in p.coins]
        p.investment_usdt = (p.investment_usdt or 0) + amount
        db.commit()
        result = None
        if p.is_running and coins:
            result = get_reb().start_portfolio(
                coins=coins, total_usdt=amount,
                method=p.allocation_method or "equal",
                min_trade_usdt=5.0, dry_run=False,
            )
        log_action(db, tid, "increase", f"+{amount}", True, pf_id)
        msg = f"✅ تم زيادة المخصص بمبلغ `{amount:.2f}$`\nالجديد: `{p.investment_usdt:.2f}$`"
        if result and result.get("executed"):
            msg += "\nتم شراء الزيادة."
        await update.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=pf_keyboard(pf_id, p.is_running))
    except Exception as e:
        await update.message.reply_text(f"خطأ: {e}", reply_markup=main_menu_keyboard())
    finally:
        db.close()
        context.user_data.pop("increase_pf", None)
    return ConversationHandler.END


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN مطلوب في .env")
    if not config.MEXC_API_KEY or not config.MEXC_API_SECRET:
        raise SystemExit("MEXC_API_KEY و MEXC_API_SECRET مطلوبان")
    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL مطلوب")

    init_db()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    async def entry_create(update, context):
        await on_callback(update, context)
        return CREATE_NAME

    async def entry_addcoin(update, context):
        await on_callback(update, context)
        return ADD_COIN

    async def entry_increase(update, context):
        await on_callback(update, context)
        return INCREASE_AMOUNT

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_create, pattern="^create_pf$"),
            CallbackQueryHandler(entry_addcoin, pattern="^addcoin_"),
            CallbackQueryHandler(entry_increase, pattern="^increase_"),
        ],
        states={
            CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_name)],
            CREATE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_amount)],
            CREATE_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_coins)],
            ADD_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_coin_msg)],
            INCREASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, increase_amount_msg)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_callback))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
