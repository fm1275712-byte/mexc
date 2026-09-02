import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
import config
from database import (
    init_db, SessionLocal, get_or_create_user, get_portfolios, get_portfolio,
    create_portfolio, add_coin_to_portfolio, remove_coin_from_portfolio,
    close_portfolio, log_action, Portfolio, PortfolioCoin
)
from mexc_client import MexcClient
from rebalancer import Rebalancer

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# States
(
    WAIT_PORTFOLIO_NAME,
    WAIT_INVESTMENT,
    WAIT_SEARCH_COIN,
    WAIT_THRESHOLD,
    WAIT_INTERVAL,
    WAIT_MIN_TRADE,
    WAIT_MAX_COINS,
    WAIT_ADD_COIN_TO_PF,
) = range(8)

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


def is_coin_available(symbol: str) -> bool:
    try:
        pair = f"{symbol.upper()}/USDT"
        markets = get_mexc().exchange.load_markets()
        m = markets.get(pair)
        return bool(m and m.get('active', True) and m.get('spot', True))
    except Exception:
        return False


# ==================== KEYBOARDS ====================

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 محافظي", callback_data="my_portfolios")],
        [InlineKeyboardButton("➕ إنشاء محفظة جديدة", callback_data="create_portfolio")],
        [InlineKeyboardButton("📊 رصيد الحساب الكامل", callback_data="full_balance")],
        [InlineKeyboardButton("⚙️ الإعدادات العامة", callback_data="global_settings")],
        [InlineKeyboardButton("📜 السجل", callback_data="logs")],
    ])


def portfolios_list_kb(portfolios):
    buttons = []
    for p in portfolios:
        status = "🟢" if p.status == "active" else "🔴"
        buttons.append([InlineKeyboardButton(
            f"{status} {p.name} ({p.investment_usdt:.0f}$)",
            callback_data=f"pf_{p.id}"
        )])
    buttons.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)


def portfolio_menu_kb(pf_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 تفاصيل المحفظة", callback_data=f"pfdetail_{pf_id}")],
        [InlineKeyboardButton("🪙 العملات", callback_data=f"pfcoins_{pf_id}")],
        [InlineKeyboardButton("💰 زيادة الاستثمار", callback_data=f"pfincrease_{pf_id}")],
        [InlineKeyboardButton("⚖️ إعادة التوازن", callback_data=f"pfrebalance_{pf_id}")],
        [InlineKeyboardButton("⚙️ إعدادات المحفظة", callback_data=f"pfsettings_{pf_id}")],
        [InlineKeyboardButton("🛑 إنهاء المحفظة", callback_data=f"pfclose_{pf_id}")],
        [InlineKeyboardButton("🔙 محافظي", callback_data="my_portfolios")],
    ])


def pf_coins_kb(pf_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 إضافة عملة", callback_data=f"pfaddcoin_{pf_id}")],
        [InlineKeyboardButton("🗑️ حذف عملة", callback_data=f"pfremovecoin_{pf_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"pf_{pf_id}")],
    ])


def rebalance_pf_kb(pf_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 معاينة", callback_data=f"pfdry_{pf_id}")],
        [InlineKeyboardButton("✅ تنفيذ", callback_data=f"pfreal_{pf_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"pf_{pf_id}")],
    ])


def global_settings_kb(user):
    method = "بالتساوي" if user.default_allocation_method == "equal" else "قيمة سوقية"
    mode = "نسبي %" if user.default_rebalance_mode == "threshold" else "بالوقت"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"طريقة التوزيع الافتراضية: {method}", callback_data="gs_method")],
        [InlineKeyboardButton(f"وضع الانحراف الافتراضي: {mode}", callback_data="gs_mode")],
        [InlineKeyboardButton(f"Threshold: {user.default_threshold}%", callback_data="gs_threshold")],
        [InlineKeyboardButton(f"الفترة: {user.default_interval_hours}س", callback_data="gs_interval")],
        [InlineKeyboardButton(f"حد أدنى للصفقة: {user.min_trade_usdt}$", callback_data="gs_mintrade")],
        [InlineKeyboardButton(f"أقصى عملات/محفظة: {user.max_coins_per_portfolio}", callback_data="gs_maxcoins")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ])


# ==================== HELPERS ====================

def format_full_balance(portfolio: dict) -> str:
    if portfolio['total_usdt'] <= 0:
        return "الحساب فارغ حالياً."
    lines = [f"💰 **إجمالي الحساب:** `{portfolio['total_usdt']:.4f} USDT`\n"]
    sorted_assets = sorted(portfolio['assets'].items(), key=lambda x: x[1]['usdt_value'], reverse=True)
    for asset, data in sorted_assets:
        lines.append(f"• `{asset}`: {data['amount']:.6f} ≈ `{data['usdt_value']:.4f}$` ({data['percent']:.2f}%)")
    return "\n".join(lines)


def format_portfolio_detail(p: Portfolio, targets: dict = None) -> str:
    coins = [c.symbol for c in p.coins]
    method = "بالتساوي" if p.allocation_method == "equal" else "قيمة سوقية"
    mode = "نسبي %" if p.rebalance_mode == "threshold" else "بالوقت"
    lines = [
        f"📁 **{p.name}**",
        f"الحالة: {'🟢 نشطة' if p.status == 'active' else '🔴 منتهية'}",
        f"الاستثمار المستهدف: `{p.investment_usdt:.2f} USDT`",
        f"عدد العملات: {len(coins)}",
        f"طريقة التوزيع: {method}",
        f"وضع الانحراف: {mode}",
        f"Threshold: {p.threshold}%",
        f"الفترة: {p.rebalance_interval_hours} ساعة",
        "",
        "**العملات:**"
    ]
    if targets:
        for s in coins:
            lines.append(f"• `{s}` → {targets.get(s, 0):.1f}%")
    else:
        for s in coins:
            lines.append(f"• `{s}`")
    return "\n".join(lines)


# ==================== HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("غير مصرح.")
        return
    db = SessionLocal()
    try:
        get_or_create_user(db, user.id)
    finally:
        db.close()

    text = (
        f"مرحباً {user.first_name} 👋\n\n"
        "بوت **محافظ MEXC المتعددة**\n\n"
        "• أنشئ محافظ منفصلة\n"
        "• كل محفظة لها عملاتها وإعداداتها واستثمارها\n"
        "• تحكم كامل في كل محفظة (إضافة/حذف عملات، زيادة استثمار، إعادة توازن، إنهاء)\n"
        "• الحد الأدنى 5$ لكل عملة\n"
    )
    await update.message.reply_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


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

        if data == "main_menu":
            await query.edit_message_text("القائمة الرئيسية:", reply_markup=main_menu_kb())
            return

        # ===== FULL BALANCE =====
        if data == "full_balance":
            try:
                bal = get_mexc().get_portfolio_value()
                text = format_full_balance(bal)
                await query.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"خطأ: `{e}`", reply_markup=main_menu_kb(), parse_mode="Markdown")
            return

        # ===== MY PORTFOLIOS =====
        if data == "my_portfolios":
            pfs = get_portfolios(db, user_id, status="active")
            if not pfs:
                await query.edit_message_text(
                    "لا توجد محافظ نشطة.\nأنشئ محفظة جديدة.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("➕ إنشاء محفظة", callback_data="create_portfolio")],
                        [InlineKeyboardButton("🔙 الرئيسية", callback_data="main_menu")],
                    ])
                )
                return
            await query.edit_message_text("محافظك النشطة:", reply_markup=portfolios_list_kb(pfs))
            return

        # ===== CREATE PORTFOLIO START =====
        if data == "create_portfolio":
            context.user_data.clear()
            context.user_data['creating'] = True
            context.user_data['coins'] = []
            await query.edit_message_text(
                "📁 **إنشاء محفظة جديدة**\n\nأرسل اسم المحفظة (مثلاً: محفظة الذكاء الاصطناعي):\nأو /cancel",
                parse_mode="Markdown"
            )
            return WAIT_PORTFOLIO_NAME

        # ===== OPEN PORTFOLIO =====
        if data.startswith("pf_") and not data.startswith("pfdetail_") and not data.startswith("pfcoins_") \
                and not data.startswith("pfincrease_") and not data.startswith("pfrebalance_") \
                and not data.startswith("pfsettings_") and not data.startswith("pfclose_") \
                and not data.startswith("pfaddcoin_") and not data.startswith("pfremovecoin_") \
                and not data.startswith("pfdry_") and not data.startswith("pfreal_") \
                and not data.startswith("pfdel_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_kb())
                return
            await query.edit_message_text(
                f"المحفظة: **{p.name}**",
                reply_markup=portfolio_menu_kb(pf_id),
                parse_mode="Markdown"
            )
            return

        # ===== PORTFOLIO DETAIL =====
        if data.startswith("pfdetail_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=main_menu_kb())
                return
            symbols = [c.symbol for c in p.coins]
            targets = get_rebalancer().calculate_targets(symbols, p.allocation_method)
            text = format_portfolio_detail(p, targets)
            await query.edit_message_text(text, reply_markup=portfolio_menu_kb(pf_id), parse_mode="Markdown")
            return

        # ===== PORTFOLIO COINS =====
        if data.startswith("pfcoins_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                return
            symbols = [c.symbol for c in p.coins]
            text = f"**عملات محفظة {p.name}:**\n\n" + ("\n".join(f"• `{s}`" for s in symbols) if symbols else "لا توجد عملات")
            await query.edit_message_text(text, reply_markup=pf_coins_kb(pf_id), parse_mode="Markdown")
            return

        if data.startswith("pfaddcoin_"):
            pf_id = int(data.split("_")[1])
            context.user_data['add_to_pf'] = pf_id
            await query.edit_message_text(
                "أرسل رمز العملة لإضافتها (مثل ONDO):\nأو /cancel"
            )
            return WAIT_ADD_COIN_TO_PF

        if data.startswith("pfremovecoin_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p or not p.coins:
                await query.edit_message_text("لا توجد عملات.", reply_markup=pf_coins_kb(pf_id))
                return
            buttons = [[InlineKeyboardButton(f"🗑️ {c.symbol}", callback_data=f"pfdel_{pf_id}_{c.symbol}")] for c in p.coins]
            buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"pfcoins_{pf_id}")])
            await query.edit_message_text("اختر العملة للحذف:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("pfdel_"):
            parts = data.split("_")
            pf_id = int(parts[1])
            symbol = parts[2]
            remove_coin_from_portfolio(db, pf_id, symbol)
            await query.edit_message_text(f"✅ تم حذف `{symbol}`", reply_markup=pf_coins_kb(pf_id), parse_mode="Markdown")
            return

        # ===== INCREASE INVESTMENT =====
        if data.startswith("pfincrease_"):
            pf_id = int(data.split("_")[1])
            context.user_data['increase_pf'] = pf_id
            await query.edit_message_text(
                "أرسل المبلغ الإضافي بالـ USDT لزيادته على استثمار المحفظة:\nأو /cancel"
            )
            return WAIT_INVESTMENT  # reuse, we'll check context

        # ===== REBALANCE =====
        if data.startswith("pfrebalance_"):
            pf_id = int(data.split("_")[1])
            await query.edit_message_text("إعادة توازن المحفظة:", reply_markup=rebalance_pf_kb(pf_id))
            return

        if data.startswith("pfdry_") or data.startswith("pfreal_"):
            dry = data.startswith("pfdry_")
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p or not p.coins:
                await query.edit_message_text("المحفظة فارغة من العملات.", reply_markup=rebalance_pf_kb(pf_id))
                return

            symbols = [c.symbol for c in p.coins]
            result = get_rebalancer().execute_rebalance(
                selected_coins=symbols,
                allocation_method=p.allocation_method,
                threshold=p.threshold,
                min_trade_usdt=user.min_trade_usdt,
                dry_run=dry
            )

            lines = ["🔍 **معاينة**\n" if dry else "✅ **تم التنفيذ**\n"]
            lines.append("**النسب المستهدفة:**")
            for s, pct in result.get('targets', {}).items():
                lines.append(f"• `{s}` → {pct:.1f}%")
            lines.append("")

            if result.get('message'):
                lines.append(result['message'])
            else:
                for o in result['executed']:
                    lines.append(
                        f"• {o['side'].upper()} `{o['asset']}` "
                        f"{o['amount']:.6f} ≈ {o['usdt_value']:.2f}$ "
                        f"({o['current_pct']:.1f}%→{o['target_pct']:.1f}%)"
                    )
                if result['errors']:
                    lines.append("\n❌ أخطاء:")
                    for e in result['errors']:
                        lines.append(f"• {e['error']}")

            log_action(db, user_id, "rebalance_dry" if dry else "rebalance",
                       str(result.get('executed', [])), success=not result['errors'], portfolio_id=pf_id)
            if not dry:
                p.last_rebalance = datetime.utcnow()
                db.commit()

            await query.edit_message_text("\n".join(lines), reply_markup=rebalance_pf_kb(pf_id), parse_mode="Markdown")
            return

        # ===== CLOSE PORTFOLIO =====
        if data.startswith("pfclose_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if p:
                close_portfolio(db, pf_id)
                await query.edit_message_text(
                    f"✅ تم إنهاء محفظة **{p.name}**",
                    reply_markup=main_menu_kb(),
                    parse_mode="Markdown"
                )
            return

        # ===== GLOBAL SETTINGS =====
        if data == "global_settings":
            await query.edit_message_text(
                "**الإعدادات العامة** (تُطبق على المحافظ الجديدة):",
                reply_markup=global_settings_kb(user),
                parse_mode="Markdown"
            )
            return

        if data == "gs_method":
            user.default_allocation_method = "marketcap" if user.default_allocation_method == "equal" else "equal"
            db.commit()
            await query.edit_message_text("تم التحديث.", reply_markup=global_settings_kb(user))
            return

        if data == "gs_mode":
            user.default_rebalance_mode = "time" if user.default_rebalance_mode == "threshold" else "threshold"
            db.commit()
            await query.edit_message_text("تم التحديث.", reply_markup=global_settings_kb(user))
            return

        if data == "gs_threshold":
            await query.edit_message_text("أرسل نسبة Threshold الجديدة (مثلاً 2):\nأو /cancel")
            return WAIT_THRESHOLD

        if data == "gs_interval":
            await query.edit_message_text("أرسل عدد الساعات (مثلاً 24):\nأو /cancel")
            return WAIT_INTERVAL

        if data == "gs_mintrade":
            await query.edit_message_text("أرسل الحد الأدنى للصفقة بالـ USDT:\nأو /cancel")
            return WAIT_MIN_TRADE

        if data == "gs_maxcoins":
            await query.edit_message_text("أرسل أقصى عدد عملات لكل محفظة:\nأو /cancel")
            return WAIT_MAX_COINS

        # ===== LOGS =====
        if data == "logs":
            from database import RebalanceLog
            logs = db.query(RebalanceLog).filter(RebalanceLog.telegram_id == user_id)\
                .order_by(RebalanceLog.created_at.desc()).limit(15).all()
            if not logs:
                text = "لا يوجد سجل."
            else:
                lines = ["**آخر العمليات:**\n"]
                for log in logs:
                    st = "✅" if log.success else "❌"
                    lines.append(f"{st} `{log.created_at.strftime('%m-%d %H:%M')}` {log.action}")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")
            return

    finally:
        db.close()


# ==================== CONVERSATIONS ====================

async def receive_portfolio_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    context.user_data['pf_name'] = text[:80]
    await update.message.reply_text(
        f"اسم المحفظة: **{text}**\n\n"
        "الآن أرسل مبلغ الاستثمار بالـ USDT (مثلاً 50 أو 100):\n"
        "ملاحظة: الحد الأدنى = 5$ × عدد العملات\nأو /cancel",
        parse_mode="Markdown"
    )
    return WAIT_INVESTMENT


async def receive_investment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    # Check if this is increase
    if context.user_data.get('increase_pf'):
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("أدخل رقم موجب:")
            return WAIT_INVESTMENT

        pf_id = context.user_data['increase_pf']
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, update.effective_user.id)
            if p:
                p.investment_usdt += amount
                db.commit()
                await update.message.reply_text(
                    f"✅ تم زيادة استثمار **{p.name}** بمبلغ {amount}$\n"
                    f"الإجمالي الجديد: `{p.investment_usdt:.2f}$`",
                    reply_markup=portfolio_menu_kb(pf_id),
                    parse_mode="Markdown"
                )
        finally:
            db.close()
        context.user_data.pop('increase_pf', None)
        return ConversationHandler.END

    # Creating new portfolio
    try:
        amount = float(text)
        if amount < 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل مبلغ ≥ 5 USDT:")
        return WAIT_INVESTMENT

    context.user_data['pf_investment'] = amount
    context.user_data['coins'] = []
    await update.message.reply_text(
        f"الاستثمار: `{amount}$`\n\n"
        "الآن أضف العملات.\nأرسل رمز العملة (مثل BTC أو ONDO):\n"
        "بعد كل عملة سأتحقق لحظياً هل متاحة على MEXC.\n"
        "عندما تنتهي اكتب **تم**\nأو /cancel",
        parse_mode="Markdown"
    )
    return WAIT_SEARCH_COIN


async def receive_search_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if text == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if text in ("تم", "DONE", "انتهيت"):
        coins = context.user_data.get('coins', [])
        if not coins:
            await update.message.reply_text("لازم تضيف عملة واحدة على الأقل. أرسل رمز عملة:")
            return WAIT_SEARCH_COIN

        name = context.user_data['pf_name']
        investment = context.user_data['pf_investment']
        user_id = update.effective_user.id

        db = SessionLocal()
        try:
            user = get_or_create_user(db, user_id)
            min_needed = user.min_usdt_per_coin * len(coins)
            if investment < min_needed:
                await update.message.reply_text(
                    f"⚠️ المبلغ قليل. الحد الأدنى لـ {len(coins)} عملات = {min_needed}$\n"
                    "أرسل مبلغ أكبر أو احذف عملات."
                )
                return WAIT_SEARCH_COIN

            p = create_portfolio(
                db, user_id, name, investment, coins,
                allocation_method=user.default_allocation_method,
                rebalance_mode=user.default_rebalance_mode,
                threshold=user.default_threshold,
                interval=user.default_interval_hours
            )
            await update.message.reply_text(
                f"✅ تم إنشاء محفظة **{p.name}**\n"
                f"الاستثمار: `{p.investment_usdt}$`\n"
                f"العملات: {', '.join(coins)}",
                reply_markup=portfolio_menu_kb(p.id),
                parse_mode="Markdown"
            )
        finally:
            db.close()
        context.user_data.clear()
        return ConversationHandler.END

    # Add coin
    await update.message.reply_text(f"⏳ جاري التحقق من `{text}`...", parse_mode="Markdown")
    if not is_coin_available(text):
        await update.message.reply_text(f"❌ `{text}` غير متاحة على MEXC Spot/USDT.\nجرب غيرها أو اكتب تم", parse_mode="Markdown")
        return WAIT_SEARCH_COIN

    coins = context.user_data.get('coins', [])
    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        max_c = user.max_coins_per_portfolio
    finally:
        db.close()

    if text in coins:
        await update.message.reply_text(f"`{text}` موجودة بالفعل.", parse_mode="Markdown")
        return WAIT_SEARCH_COIN
    if len(coins) >= max_c:
        await update.message.reply_text(f"وصلت للحد الأقصى ({max_c}). اكتب **تم**")
        return WAIT_SEARCH_COIN

    coins.append(text)
    context.user_data['coins'] = coins
    await update.message.reply_text(
        f"✅ تم إضافة `{text}`\n"
        f"العملات الحالية ({len(coins)}): {', '.join(coins)}\n\n"
        "أرسل عملة أخرى أو اكتب **تم**",
        parse_mode="Markdown"
    )
    return WAIT_SEARCH_COIN


async def receive_add_coin_to_pf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if text == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    pf_id = context.user_data.get('add_to_pf')
    if not pf_id:
        await update.message.reply_text("خطأ.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ جاري التحقق من `{text}`...", parse_mode="Markdown")
    if not is_coin_available(text):
        await update.message.reply_text(f"❌ `{text}` غير متاحة على MEXC.", parse_mode="Markdown")
        return WAIT_ADD_COIN_TO_PF

    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        ok, msg = add_coin_to_portfolio(db, pf_id, text, max_coins=user.max_coins_per_portfolio)
        await update.message.reply_text(msg, reply_markup=pf_coins_kb(pf_id), parse_mode="Markdown")
    finally:
        db.close()
    context.user_data.pop('add_to_pf', None)
    return ConversationHandler.END


async def receive_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    try:
        val = float(text)
        if not (0 < val <= 50):
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم بين 0 و 50:")
        return WAIT_THRESHOLD

    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        user.default_threshold = val
        db.commit()
        await update.message.reply_text(f"✅ Threshold = {val}%", reply_markup=global_settings_kb(user))
    finally:
        db.close()
    return ConversationHandler.END


async def receive_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    try:
        val = int(text)
        if not (1 <= val <= 168):
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم بين 1 و 168:")
        return WAIT_INTERVAL

    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        user.default_interval_hours = val
        db.commit()
        await update.message.reply_text(f"✅ الفترة = {val} ساعة", reply_markup=global_settings_kb(user))
    finally:
        db.close()
    return ConversationHandler.END


async def receive_min_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    try:
        val = float(text)
        if val < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم ≥ 1:")
        return WAIT_MIN_TRADE

    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        user.min_trade_usdt = val
        db.commit()
        await update.message.reply_text(f"✅ الحد الأدنى = {val}$", reply_markup=global_settings_kb(user))
    finally:
        db.close()
    return ConversationHandler.END


async def receive_max_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.upper() == "/CANCEL":
        await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
        return ConversationHandler.END
    try:
        val = int(text)
        if not (1 <= val <= 30):
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم بين 1 و 30:")
        return WAIT_MAX_COINS

    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        user.max_coins_per_portfolio = val
        db.commit()
        await update.message.reply_text(f"✅ أقصى عملات = {val}", reply_markup=global_settings_kb(user))
    finally:
        db.close()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb())
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("حدث خطأ. حاول مرة أخرى.")


def main():
    if not all([config.TELEGRAM_BOT_TOKEN, config.MEXC_API_KEY, config.MEXC_API_SECRET, config.DATABASE_URL]):
        raise ValueError("Missing required environment variables")

    init_db()
    logger.info("DB ready")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    create_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^create_portfolio$")],
        states={
            WAIT_PORTFOLIO_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_portfolio_name)],
            WAIT_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_investment)],
            WAIT_SEARCH_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search_coin)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    add_coin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^pfaddcoin_")],
        states={
            WAIT_ADD_COIN_TO_PF: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_add_coin_to_pf)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    increase_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^pfincrease_")],
        states={
            WAIT_INVESTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_investment)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    thresh_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^gs_threshold$")],
        states={WAIT_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_threshold)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # Fix: WAIT_THRESHOLD etc already defined
    thresh_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^gs_threshold$")],
        states={WAIT_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_threshold)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    interval_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^gs_interval$")],
        states={WAIT_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_interval)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    mintrade_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^gs_mintrade$")],
        states={WAIT_MIN_TRADE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_min_trade)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    maxcoins_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^gs_maxcoins$")],
        states={WAIT_MAX_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_max_coins)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(create_conv)
    app.add_handler(add_coin_conv)
    app.add_handler(increase_conv)
    app.add_handler(thresh_conv)
    app.add_handler(interval_conv)
    app.add_handler(mintrade_conv)
    app.add_handler(maxcoins_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_error_handler(error_handler)

    logger.info("Bot starting (multi-portfolio)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
