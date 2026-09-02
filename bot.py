import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
import config
from database import (
    init_db, SessionLocal, get_or_create_user, get_selected_coins,
    set_selected_coins, add_coin, remove_coin, log_action
)
from mexc_client import MexcClient
from rebalancer import Rebalancer

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
(
    WAITING_SEARCH_COIN,
    WAITING_THRESHOLD,
    WAITING_INTERVAL,
    WAITING_MIN_TRADE,
    WAITING_MAX_COINS,
) = range(5)

mexc = None
rebalancer = None


def get_mexc():
    global mexc
    if mexc is None:
        mexc = MexcClient()
    return mexc


def get_rebalancer():
    global rebalancer
    if rebalancer is None:
        rebalancer = Rebalancer(get_mexc())
    return rebalancer


def is_authorized(user_id: int) -> bool:
    if config.ADMIN_TELEGRAM_ID:
        return str(user_id) == str(config.ADMIN_TELEGRAM_ID)
    return True


# ==================== KEYBOARDS ====================

def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 المحفظة الحالية", callback_data="portfolio")],
        [InlineKeyboardButton("🪙 اختيار العملات", callback_data="coins_menu")],
        [InlineKeyboardButton("⚖️ إعادة التوازن", callback_data="rebalance_menu")],
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")],
        [InlineKeyboardButton("📜 السجل", callback_data="logs")],
    ]
    return InlineKeyboardMarkup(keyboard)


def coins_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔍 بحث وإضافة عملة", callback_data="search_coin")],
        [InlineKeyboardButton("📋 عرض العملات المختارة", callback_data="show_coins")],
        [InlineKeyboardButton("🗑️ حذف عملة", callback_data="remove_coin_menu")],
        [InlineKeyboardButton("🧹 مسح كل العملات", callback_data="clear_coins")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def rebalance_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔍 معاينة (Dry Run)", callback_data="rebalance_dry")],
        [InlineKeyboardButton("✅ تنفيذ فعلي", callback_data="rebalance_real")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def settings_keyboard(user):
    mode_text = "نسبي %" if user.rebalance_mode == "threshold" else "بالوقت"
    method_text = "بالتساوي" if user.allocation_method == "equal" else "حسب القيمة السوقية"
    keyboard = [
        [InlineKeyboardButton(f"وضع إعادة التوازن: {mode_text}", callback_data="toggle_mode")],
        [InlineKeyboardButton(f"طريقة التوزيع: {method_text}", callback_data="toggle_method")],
        [InlineKeyboardButton("📏 نسبة الانحراف (Threshold)", callback_data="set_threshold")],
        [InlineKeyboardButton("⏰ فترة الوقت (ساعات)", callback_data="set_interval")],
        [InlineKeyboardButton("💰 الحد الأدنى للصفقة", callback_data="set_min_trade")],
        [InlineKeyboardButton("🔢 الحد الأقصى للعملات", callback_data="set_max_coins")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def mode_choice_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 نسبي (Threshold %)", callback_data="mode_threshold")],
        [InlineKeyboardButton("⏰ بالوقت (كل X ساعة)", callback_data="mode_time")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def method_choice_keyboard():
    keyboard = [
        [InlineKeyboardButton("⚖️ بالتساوي (Equal)", callback_data="method_equal")],
        [InlineKeyboardButton("📈 حسب القيمة السوقية", callback_data="method_marketcap")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ==================== HELPERS ====================

def format_portfolio(portfolio: dict) -> str:
    if portfolio['total_usdt'] <= 0:
        return "المحفظة فارغة حالياً."

    lines = [f"💰 **إجمالي القيمة:** `{portfolio['total_usdt']:.4f} USDT`\n"]
    lines.append("**الأصول:**")
    sorted_assets = sorted(
        portfolio['assets'].items(),
        key=lambda x: x[1]['usdt_value'],
        reverse=True
    )
    for asset, data in sorted_assets:
        lines.append(
            f"• `{asset}`: {data['amount']:.6f} ≈ `{data['usdt_value']:.4f} USDT` ({data['percent']:.2f}%)"
        )
    return "\n".join(lines)


def format_selected_coins(coins, user) -> str:
    if not coins:
        return "لم يتم اختيار أي عملات بعد."

    symbols = [c.symbol for c in coins]
    method = "بالتساوي" if user.allocation_method == "equal" else "حسب القيمة السوقية"

    # Calculate current targets for display
    targets = get_rebalancer().calculate_targets(symbols, method=user.allocation_method)

    lines = [f"**العملات المختارة ({len(symbols)}/{user.max_coins}):**\n"]
    for s in symbols:
        pct = targets.get(s, 0)
        lines.append(f"• `{s}` → {pct:.1f}%")
    lines.append(f"\nطريقة التوزيع: **{method}**")
    return "\n".join(lines)


def is_coin_available(symbol: str) -> bool:
    """Check live if SYMBOL/USDT exists on MEXC"""
    try:
        symbol = symbol.upper()
        pair = f"{symbol}/USDT"
        markets = get_mexc().exchange.load_markets()
        return pair in markets and markets[pair].get('active', True) and markets[pair].get('spot', True)
    except Exception:
        return False


# ==================== HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    db = SessionLocal()
    try:
        get_or_create_user(db, user.id)
    finally:
        db.close()

    text = (
        f"مرحباً {user.first_name} 👋\n\n"
        "بوت **إعادة توازن محفظة MEXC Spot**\n\n"
        "الخطوات:\n"
        "1️⃣ اختر العملات (بحث لحظي على المنصة)\n"
        "2️⃣ اختر طريقة التوزيع (تساوي أو قيمة سوقية)\n"
        "3️⃣ اختر وضع الانحراف (نسبي % أو بالوقت)\n"
        "4️⃣ نفذ إعادة التوازن\n"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_authorized(user_id):
        await query.edit_message_text("غير مصرح.")
        return

    data = query.data
    db = SessionLocal()

    try:
        user = get_or_create_user(db, user_id)

        # ===== MAIN MENU =====
        if data == "main_menu":
            await query.edit_message_text("القائمة الرئيسية:", reply_markup=main_menu_keyboard())
            return

        # ===== PORTFOLIO =====
        if data == "portfolio":
            try:
                portfolio = get_mexc().get_portfolio_value()
                text = format_portfolio(portfolio)
                await query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"خطأ:\n`{e}`", reply_markup=main_menu_keyboard(), parse_mode="Markdown")
            return

        # ===== COINS MENU =====
        if data == "coins_menu":
            await query.edit_message_text("إدارة العملات:", reply_markup=coins_menu_keyboard())
            return

        if data == "show_coins":
            coins = get_selected_coins(db, user_id)
            text = format_selected_coins(coins, user)
            await query.edit_message_text(text, reply_markup=coins_menu_keyboard(), parse_mode="Markdown")
            return

        if data == "clear_coins":
            set_selected_coins(db, user_id, [])
            await query.edit_message_text("✅ تم مسح كل العملات.", reply_markup=coins_menu_keyboard())
            return

        if data == "search_coin":
            await query.edit_message_text(
                "🔍 أرسل رمز العملة للبحث (مثل: BTC أو ONDO أو HBAR)\n\n"
                "سيتم التحقق لحظياً هل متاحة على MEXC أم لا.\n"
                "أو اكتب /cancel للإلغاء"
            )
            return WAITING_SEARCH_COIN

        if data == "remove_coin_menu":
            coins = get_selected_coins(db, user_id)
            if not coins:
                await query.edit_message_text("لا توجد عملات لحذفها.", reply_markup=coins_menu_keyboard())
                return
            keyboard = []
            for c in coins:
                keyboard.append([InlineKeyboardButton(f"🗑️ {c.symbol}", callback_data=f"del_{c.symbol}")])
            keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="coins_menu")])
            await query.edit_message_text("اختر العملة للحذف:", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        if data.startswith("del_"):
            symbol = data[4:]
            remove_coin(db, user_id, symbol)
            await query.edit_message_text(f"✅ تم حذف `{symbol}`", reply_markup=coins_menu_keyboard(), parse_mode="Markdown")
            return

        # ===== REBALANCE =====
        if data == "rebalance_menu":
            coins = get_selected_coins(db, user_id)
            if not coins:
                await query.edit_message_text(
                    "⚠️ لم تختر أي عملات بعد.\nاذهب إلى «اختيار العملات» أولاً.",
                    reply_markup=main_menu_keyboard()
                )
                return
            await query.edit_message_text("إعادة التوازن:", reply_markup=rebalance_keyboard())
            return

        if data in ("rebalance_dry", "rebalance_real"):
            dry_run = data == "rebalance_dry"
            coins = get_selected_coins(db, user_id)
            if not coins:
                await query.edit_message_text("⚠️ لا توجد عملات مختارة.", reply_markup=rebalance_keyboard())
                return

            symbols = [c.symbol for c in coins]
            result = get_rebalancer().execute_rebalance(
                selected_coins=symbols,
                allocation_method=user.allocation_method,
                threshold=user.threshold,
                min_trade_usdt=user.min_trade_usdt,
                dry_run=dry_run
            )

            lines = []
            if dry_run:
                lines.append("🔍 **معاينة (لم ينفذ شيء)**\n")
            else:
                lines.append("✅ **تم التنفيذ**\n")

            # Show targets
            lines.append("**النسب المستهدفة:**")
            for s, p in result.get('targets', {}).items():
                lines.append(f"• `{s}` → {p:.1f}%")
            lines.append("")

            if result.get('message'):
                lines.append(result['message'])
            else:
                lines.append(f"الأوامر: {len(result['orders_planned'])}")
                for o in result['executed']:
                    lines.append(
                        f"• {o['side'].upper()} `{o['asset']}` "
                        f"{o['amount']:.6f} ≈ {o['usdt_value']:.2f}$ "
                        f"({o['current_pct']:.1f}% → {o['target_pct']:.1f}%)"
                    )
                if result['errors']:
                    lines.append("\n❌ أخطاء:")
                    for e in result['errors']:
                        lines.append(f"• {e['error']}")

            log_action(
                db, user_id,
                "rebalance_dry" if dry_run else "rebalance",
                str(result.get('executed', [])),
                success=len(result.get('errors', [])) == 0
            )
            if not dry_run:
                user.last_rebalance = datetime.utcnow()
                db.commit()

            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=rebalance_keyboard(),
                parse_mode="Markdown"
            )
            return

        # ===== SETTINGS =====
        if data == "settings":
            mode_text = "نسبي %" if user.rebalance_mode == "threshold" else "بالوقت"
            method_text = "بالتساوي" if user.allocation_method == "equal" else "حسب القيمة السوقية"
            text = (
                f"**الإعدادات الحالية:**\n\n"
                f"• وضع إعادة التوازن: `{mode_text}`\n"
                f"• طريقة التوزيع: `{method_text}`\n"
                f"• نسبة الانحراف: `{user.threshold}%`\n"
                f"• الفترة الزمنية: `{user.rebalance_interval_hours}` ساعة\n"
                f"• الحد الأدنى للصفقة: `{user.min_trade_usdt}` USDT\n"
                f"• الحد الأقصى للعملات: `{user.max_coins}`\n"
            )
            await query.edit_message_text(text, reply_markup=settings_keyboard(user), parse_mode="Markdown")
            return

        if data == "toggle_mode":
            await query.edit_message_text(
                "اختر وضع إعادة التوازن:\n\n"
                "• **نسبي %**: يعيد التوازن عندما تنحرف النسبة عن الهدف بأكثر من X%\n"
                "• **بالوقت**: يعيد التوازن كل عدد معين من الساعات",
                reply_markup=mode_choice_keyboard(),
                parse_mode="Markdown"
            )
            return

        if data == "mode_threshold":
            user.rebalance_mode = "threshold"
            db.commit()
            await query.edit_message_text("✅ تم اختيار الوضع النسبي (%)", reply_markup=settings_keyboard(user))
            return

        if data == "mode_time":
            user.rebalance_mode = "time"
            db.commit()
            await query.edit_message_text("✅ تم اختيار الوضع الزمني", reply_markup=settings_keyboard(user))
            return

        if data == "toggle_method":
            await query.edit_message_text(
                "اختر طريقة توزيع النسب:\n\n"
                "• **بالتساوي**: كل عملة تأخذ نفس النسبة\n"
                "• **حسب القيمة السوقية**: التوزيع حسب القيمة الحالية في المحفظة",
                reply_markup=method_choice_keyboard(),
                parse_mode="Markdown"
            )
            return

        if data == "method_equal":
            user.allocation_method = "equal"
            db.commit()
            await query.edit_message_text("✅ التوزيع بالتساوي", reply_markup=settings_keyboard(user))
            return

        if data == "method_marketcap":
            user.allocation_method = "marketcap"
            db.commit()
            await query.edit_message_text("✅ التوزيع حسب القيمة السوقية", reply_markup=settings_keyboard(user))
            return

        if data == "set_threshold":
            await query.edit_message_text(
                "أرسل نسبة الانحراف الجديدة (مثلاً 2 أو 1.5):\nأو /cancel"
            )
            return WAITING_THRESHOLD

        if data == "set_interval":
            await query.edit_message_text(
                "أرسل عدد الساعات بين كل إعادة توازن (مثلاً 6 أو 24):\nأو /cancel"
            )
            return WAITING_INTERVAL

        if data == "set_min_trade":
            await query.edit_message_text(
                "أرسل الحد الأدنى لقيمة الصفقة بالـ USDT (مثلاً 5):\nأو /cancel"
            )
            return WAITING_MIN_TRADE

        if data == "set_max_coins":
            await query.edit_message_text(
                "أرسل الحد الأقصى لعدد العملات (مثلاً 10):\nأو /cancel"
            )
            return WAITING_MAX_COINS

        # ===== LOGS =====
        if data == "logs":
            from database import RebalanceLog
            logs = db.query(RebalanceLog).filter(
                RebalanceLog.telegram_id == user_id
            ).order_by(RebalanceLog.created_at.desc()).limit(10).all()

            if not logs:
                text = "لا يوجد سجل بعد."
            else:
                lines = ["**آخر 10 عمليات:**\n"]
                for log in logs:
                    status = "✅" if log.success else "❌"
                    lines.append(f"{status} `{log.created_at.strftime('%Y-%m-%d %H:%M')}` - {log.action}")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
            return

    finally:
        db.close()


# ==================== CONVERSATION HANDLERS ====================

async def receive_search_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if text == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, user_id)

        # Live check on MEXC
        await update.message.reply_text(f"⏳ جاري التحقق من `{text}` على MEXC...", parse_mode="Markdown")

        available = is_coin_available(text)
        if not available:
            await update.message.reply_text(
                f"❌ `{text}` غير متاحة كتداول Spot مقابل USDT على MEXC.\n"
                "جرب رمز آخر أو /cancel",
                parse_mode="Markdown"
            )
            return WAITING_SEARCH_COIN

        success, msg = add_coin(db, user_id, text, max_coins=user.max_coins)
        coins = get_selected_coins(db, user_id)

        if success:
            extra = format_selected_coins(coins, user)
            await update.message.reply_text(
                f"{msg}\n\n{extra}",
                reply_markup=coins_menu_keyboard(),
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
            return WAITING_SEARCH_COIN
    finally:
        db.close()


async def receive_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    try:
        val = float(text)
        if val <= 0 or val > 50:
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم موجب أقل من 50:")
        return WAITING_THRESHOLD

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, user_id)
        user.threshold = val
        db.commit()
        await update.message.reply_text(
            f"✅ نسبة الانحراف = {val}%",
            reply_markup=settings_keyboard(user)
        )
    finally:
        db.close()
    return ConversationHandler.END


async def receive_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    try:
        val = int(text)
        if val < 1 or val > 168:
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم ساعات بين 1 و 168:")
        return WAITING_INTERVAL

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, user_id)
        user.rebalance_interval_hours = val
        db.commit()
        await update.message.reply_text(
            f"✅ الفترة = كل {val} ساعة",
            reply_markup=settings_keyboard(user)
        )
    finally:
        db.close()
    return ConversationHandler.END


async def receive_min_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    try:
        val = float(text)
        if val < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم ≥ 1:")
        return WAITING_MIN_TRADE

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, user_id)
        user.min_trade_usdt = val
        db.commit()
        await update.message.reply_text(
            f"✅ الحد الأدنى = {val} USDT",
            reply_markup=settings_keyboard(user)
        )
    finally:
        db.close()
    return ConversationHandler.END


async def receive_max_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    try:
        val = int(text)
        if val < 1 or val > 30:
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم بين 1 و 30:")
        return WAITING_MAX_COINS

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, user_id)
        user.max_coins = val
        db.commit()
        await update.message.reply_text(
            f"✅ الحد الأقصى للعملات = {val}",
            reply_markup=settings_keyboard(user)
        )
    finally:
        db.close()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("حدث خطأ داخلي. حاول مرة أخرى.")


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if not config.MEXC_API_KEY or not config.MEXC_API_SECRET:
        raise ValueError("MEXC_API_KEY and MEXC_API_SECRET are required")
    if not config.DATABASE_URL:
        raise ValueError("DATABASE_URL is required")

    init_db()
    logger.info("Database initialized")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Search coin conversation
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^search_coin$")],
        states={
            WAITING_SEARCH_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search_coin)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    thresh_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^set_threshold$")],
        states={
            WAITING_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_threshold)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    interval_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^set_interval$")],
        states={
            WAITING_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_interval)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    min_trade_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^set_min_trade$")],
        states={
            WAITING_MIN_TRADE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_min_trade)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    max_coins_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^set_max_coins$")],
        states={
            WAITING_MAX_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_max_coins)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(search_conv)
    app.add_handler(thresh_conv)
    app.add_handler(interval_conv)
    app.add_handler(min_trade_conv)
    app.add_handler(max_coins_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
