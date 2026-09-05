"""
MEXC Multi-Portfolio Rebalancer - Telegram Bot
تحكم كامل من تليجرام فقط
"""
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
    close_portfolio, set_portfolio_running, log_action,
    get_signal_settings, list_signal_bots, add_signal_bot, remove_signal_bot,
    get_enabled_signal_bots,
)
from signal_parser import evaluate_signal
import signal_listener
from mexc_client import MexcClient
from rebalancer import Rebalancer

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
(
    WAIT_PF_NAME,
    WAIT_PF_INVESTMENT,
    WAIT_PF_COINS,
    WAIT_ADD_COIN,
    WAIT_INCREASE,
    WAIT_THRESHOLD,
    WAIT_EDIT_ALLOC,
    WAIT_SIGNAL_BOT,
    WAIT_SIGNAL_THRESH,
    WAIT_SIGNAL_TEST,
) = range(10)

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
        return bool(m and m.get("active", True) and m.get("spot", True))
    except Exception:
        return False


# ==================== KEYBOARDS ====================

def kb_main():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📁  محافظي", callback_data="my_pfs"),
            InlineKeyboardButton("✨  إنشاء محفظة", callback_data="create_pf"),
        ],
        [
            InlineKeyboardButton("💎  رصيد الحساب", callback_data="balance"),
            InlineKeyboardButton("📡  إشارات", callback_data="signals"),
        ],
        [
            InlineKeyboardButton("⚙️  الإعدادات", callback_data="settings"),
        ],
    ])


def kb_portfolios(portfolios):
    rows = []
    for p in portfolios[:15]:
        icon = "🟢" if p.is_running else "⚪"
        rows.append([InlineKeyboardButton(
            f"{icon}  {p.name}  ·  {p.investment_usdt:.0f} USDT",
            callback_data=f"pf_{p.id}"
        )])
    rows.append([InlineKeyboardButton("◀️  رجوع للقائمة الرئيسية", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def kb_portfolio(pf_id: int, is_running: bool = False):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️  تشغيل", callback_data=f"start_{pf_id}"),
            InlineKeyboardButton("⏹️  إيقاف", callback_data=f"stop_{pf_id}"),
        ],
        [
            InlineKeyboardButton("💰  زيادة استثمار", callback_data=f"inc_{pf_id}"),
            InlineKeyboardButton("✏️  تعديل المخصص", callback_data=f"editalloc_{pf_id}"),
        ],
        [
            InlineKeyboardButton("⚖️  إعادة توازن", callback_data=f"reb_{pf_id}"),
            InlineKeyboardButton("🔄  تحديث", callback_data=f"pf_{pf_id}"),
        ],
        [
            InlineKeyboardButton("➕  إضافة عملة", callback_data=f"addcoin_{pf_id}"),
            InlineKeyboardButton("🗑️  حذف عملة", callback_data=f"removecoin_{pf_id}"),
        ],
        [InlineKeyboardButton("🗑️  إنهاء المحفظة", callback_data=f"close_{pf_id}")],
        [InlineKeyboardButton("◀️  رجوع — محافظي", callback_data="my_pfs")],
        [InlineKeyboardButton("🏠  القائمة الرئيسية", callback_data="main")],
    ])


def kb_rebalance(pf_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍  معاينة فقط", callback_data=f"dry_{pf_id}"),
            InlineKeyboardButton("✅  تنفيذ فعلي", callback_data=f"real_{pf_id}"),
        ],
        [InlineKeyboardButton("◀️  رجوع للمحفظة", callback_data=f"pf_{pf_id}")],
    ])


def kb_confirm_stop(pf_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅  نعم — أوقف وبيع", callback_data=f"dostop_{pf_id}")],
        [InlineKeyboardButton("◀️  رجوع — إلغاء", callback_data=f"pf_{pf_id}")],
    ])


def kb_confirm_close(pf_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅  نعم — إنهاء نهائي", callback_data=f"doclose_{pf_id}")],
        [InlineKeyboardButton("◀️  رجوع — إلغاء", callback_data=f"pf_{pf_id}")],
    ])


def kb_confirm_increase(pf_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅  نعم — زيادة الاستثمار", callback_data=f"doinc_{pf_id}")],
        [InlineKeyboardButton("◀️  رجوع — إلغاء", callback_data=f"pf_{pf_id}")],
    ])


def kb_settings():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄  تبديل طريقة التوزيع", callback_data="tog_method")],
        [InlineKeyboardButton("🔄  تبديل وضع الانحراف", callback_data="tog_mode")],
        [InlineKeyboardButton("📊  تعديل Threshold", callback_data="set_threshold")],
        [InlineKeyboardButton("◀️  رجوع للقائمة الرئيسية", callback_data="main")],
    ])


def kb_back_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️  رجوع للقائمة الرئيسية", callback_data="main")],
    ])


def kb_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️  إلغاء والرجوع", callback_data="main")],
    ])



def kb_signals(enabled: bool):
    status = "🟢 مفعّل" if enabled else "🔴 متوقف"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"الحالة: {status}", callback_data="sig_toggle")],
        [InlineKeyboardButton("🤖  إدارة بوتات الإشارات", callback_data="sig_bots")],
        [InlineKeyboardButton("📊  تعديل شروط الإشارة", callback_data="sig_rules")],
        [InlineKeyboardButton("🧪  رسالة تجريبية (تست)", callback_data="sig_test")],
        [InlineKeyboardButton("◀️  رجوع للقائمة الرئيسية", callback_data="main")],
    ])


def kb_sig_bots():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕  إضافة بوت إشارة", callback_data="sig_bot_add")],
        [InlineKeyboardButton("🗑️  حذف بوت", callback_data="sig_bot_del_menu")],
        [InlineKeyboardButton("◀️  رجوع — إشارات", callback_data="signals")],
    ])


def kb_sig_rules():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️  تعديل حد البيع/الشراء (M)", callback_data="sig_set_thresh")],
        [InlineKeyboardButton("◀️  رجوع — إشارات", callback_data="signals")],
    ])


# ==================== TEXT HELPERS ====================

def txt_main(name: str) -> str:
    return (
        f"*MEXC Portfolio Manager*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"مرحباً *{name}* 👋\n\n"
        f"إدارة محافظ متعددة على منصة MEXC\n"
        f"بتحكم كامل ومبلغ مخصص لكل محفظة.\n\n"
        f"اختر من القائمة بالأسفل:"
    )


def txt_portfolio(p, targets=None, current_value=None) -> str:
    status = "🟢 *تعمل*" if p.is_running else "⚪ *متوقفة*"
    method = "بالتساوي" if p.allocation_method == "equal" else "قيمة سوقية"
    coins = [c.symbol for c in p.coins]
    lines = [
        f"*{p.name}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"الحالة: {status}",
        f"المخصص: `{p.investment_usdt:.2f} USDT`",
    ]
    if current_value is not None:
        lines.append(f"القيمة الحالية: `{current_value:.2f} USDT`")
    lines += [
        f"التوزيع: {method}",
        f"Threshold: `{p.threshold}%`",
        "",
        f"*العملات ({len(coins)}):*",
    ]
    if not coins:
        lines.append("— لا توجد عملات —")
    elif targets:
        for s in coins:
            lines.append(f"• `{s}` → *{targets.get(s, 0):.1f}%*")
    else:
        for s in coins:
            lines.append(f"• `{s}`")
    return "\n".join(lines)


def txt_balance(data: dict) -> str:
    if data["total_usdt"] <= 0:
        return "*رصيد الحساب*\n━━━━━━━━━━━━━━━━━━━━\n\nالحساب فارغ حالياً."
    lines = [
        "*رصيد الحساب*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"*الإجمالي:* `{data['total_usdt']:.4f} USDT`",
        "",
        "*الأصول:*",
    ]
    sorted_assets = sorted(data["assets"].items(), key=lambda x: x[1]["usdt_value"], reverse=True)
    for asset, d in sorted_assets[:15]:
        lines.append(
            f"• `{asset}`  {d['amount']:.6f}  ·  `{d['usdt_value']:.2f}$`  ({d['percent']:.1f}%)"
        )
    return "\n".join(lines)



# ==================== SIGNAL EXECUTION ====================

async def execute_signal_action(action: str, amount: float, reason: str, notify_chat_id: int, context) -> str:
    """Sell all running portfolios OR start all stopped portfolios."""
    db = SessionLocal()
    lines = [f"📡 *تنفيذ إشارة*", f"السبب: {reason}", ""]
    try:
        pfs = get_portfolios(db, int(config.ADMIN_TELEGRAM_ID or notify_chat_id), status="active")
        if not pfs:
            return "لا توجد محافظ نشطة لتنفيذ الإشارة."

        if action == "sell":
            lines.append("🔴 *بيع فوري — كل المحافظ الشغالة*")
            for p in pfs:
                if not p.is_running:
                    lines.append(f"• {p.name}: متوقفة (تخطي)")
                    continue
                coins = [c.symbol for c in p.coins]
                result = get_rebalancer().stop_portfolio(coins, dry_run=False)
                set_portfolio_running(db, p.id, False)
                sold = result.get("total_sold_usdt", 0)
                lines.append(f"• {p.name}: بيع ≈ `{sold:.2f}$`")
                log_action(db, p.telegram_id, "signal_sell", reason, True, p.id)

        elif action == "buy":
            lines.append("🟢 *شراء فوري — تشغيل كل المحافظ*")
            for p in pfs:
                coins = [c.symbol for c in p.coins]
                if not coins:
                    lines.append(f"• {p.name}: بدون عملات (تخطي)")
                    continue
                if p.is_running:
                    lines.append(f"• {p.name}: تعمل بالفعل (تخطي)")
                    continue
                result = get_rebalancer().start_portfolio(
                    coins=coins, total_usdt=p.investment_usdt,
                    method=p.allocation_method, min_trade_usdt=5.0, dry_run=False
                )
                if result.get("executed"):
                    set_portfolio_running(db, p.id, True)
                    lines.append(f"• {p.name}: تشغيل بمخصص `{p.investment_usdt:.0f}$`")
                else:
                    err = result.get("errors") or ["فشل"]
                    lines.append(f"• {p.name}: خطأ {err[0]}")
                log_action(db, p.telegram_id, "signal_buy", reason, bool(result.get("executed")), p.id)
        else:
            return "إجراء غير معروف"

        # save last signal
        admin_id = int(config.ADMIN_TELEGRAM_ID or notify_chat_id)
        s = get_signal_settings(db, admin_id)
        from datetime import datetime as dt
        s.last_signal_at = dt.utcnow()
        s.last_signal_action = action
        s.last_signal_text = reason[:500]
        db.commit()
    finally:
        db.close()

    msg = "\n".join(lines)
    try:
        await context.bot.send_message(chat_id=notify_chat_id, text=msg, parse_mode="Markdown")
    except Exception:
        pass
    return msg


async def process_signal_text(text: str, source: str, context, notify_chat_id: int, force_test: bool = False) -> str:
    db = SessionLocal()
    try:
        admin_id = int(config.ADMIN_TELEGRAM_ID or notify_chat_id)
        settings = get_signal_settings(db, admin_id)
        if not settings.enabled and not force_test:
            return "نظام الإشارات متوقف. فعّله من قائمة 📡 إشارات."

        action, amount, reason = evaluate_signal(
            text,
            settings.sell_threshold_m,
            settings.buy_threshold_m,
            settings.sell_keywords,
            settings.buy_keywords,
        )
        report = f"المصدر: `{source}`\n{reason}"
        if action is None:
            return f"🧪 تحليل الإشارة\n{report}\n\n❌ لم يُنفَّذ شيء."

        report = f"المصدر: `{source}`\n{reason}\nالمبلغ: `{amount:,.0f}$`"
        result = await execute_signal_action(action, amount or 0, report, notify_chat_id, context)
        return f"✅ تم التنفيذ\n{report}\n\n{result}"
    finally:
        db.close()


# ==================== HANDLERS ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return
    db = SessionLocal()
    try:
        get_or_create_user(db, user.id)
    finally:
        db.close()
    await update.message.reply_text(
        txt_main(user.first_name or user.username or "User"),
        reply_markup=kb_main(),
        parse_mode="Markdown"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("تم الإلغاء.", reply_markup=kb_main())
    return ConversationHandler.END


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
        # ---- Main ----
        if data == "main":
            await query.edit_message_text(
                txt_main(query.from_user.first_name or "User"),
                reply_markup=kb_main(),
                parse_mode="Markdown"
            )
            return

        # ---- Balance ----
        if data == "balance":
            try:
                bal = get_mexc().get_portfolio_value()
                await query.edit_message_text(txt_balance(bal), reply_markup=kb_back_main(), parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"❌ خطأ: `{e}`", reply_markup=kb_back_main(), parse_mode="Markdown")
            return

        # ---- Settings ----
        if data == "settings":
            user = get_or_create_user(db, user_id)
            method = "بالتساوي" if user.default_allocation_method == "equal" else "قيمة سوقية"
            mode = "نسبي %" if user.default_rebalance_mode == "threshold" else "بالوقت"
            text = (
                f"*الإعدادات العامة*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                f"• التوزيع: `{method}`\n"
                f"• الانحراف: `{mode}`\n"
                f"• Threshold: `{user.default_threshold}%`\n"
                f"• الفترة: `{user.default_interval_hours}` ساعة\n"
                f"• حد أدنى للصفقة: `{user.min_trade_usdt}$`\n"
                f"• أقصى عملات: `{user.max_coins_per_portfolio}`"
            )
            await query.edit_message_text(text, reply_markup=kb_settings(), parse_mode="Markdown")
            return

        if data == "tog_method":
            user = get_or_create_user(db, user_id)
            user.default_allocation_method = "marketcap" if user.default_allocation_method == "equal" else "equal"
            db.commit()
            method = "بالتساوي" if user.default_allocation_method == "equal" else "قيمة سوقية"
            await query.edit_message_text(f"✅ طريقة التوزيع: *{method}*", reply_markup=kb_settings(), parse_mode="Markdown")
            return

        if data == "tog_mode":
            user = get_or_create_user(db, user_id)
            user.default_rebalance_mode = "time" if user.default_rebalance_mode == "threshold" else "threshold"
            db.commit()
            mode = "نسبي %" if user.default_rebalance_mode == "threshold" else "بالوقت"
            await query.edit_message_text(f"✅ وضع الانحراف: *{mode}*", reply_markup=kb_settings(), parse_mode="Markdown")
            return

        if data == "set_threshold":
            context.user_data["waiting"] = "threshold"
            await query.edit_message_text(
                "أرسل نسبة Threshold الجديدة (مثلاً: `2`):\nأو /cancel",
                parse_mode="Markdown"
            )
            return WAIT_THRESHOLD

        # ---- My Portfolios ----
        if data == "my_pfs":
            pfs = get_portfolios(db, user_id, status="active")
            if not pfs:
                await query.edit_message_text(
                    "*محافظي*\n━━━━━━━━━━━━━━━━━━━━\n\nلا توجد محافظ نشطة بعد.\nأنشئ محفظة جديدة للبدء.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✨  إنشاء محفظة", callback_data="create_pf")],
                        [InlineKeyboardButton("◀️  رجوع للقائمة الرئيسية", callback_data="main")],
                    ]),
                    parse_mode="Markdown"
                )
                return
            lines = ["*محافظك النشطة*", "━━━━━━━━━━━━━━━━━━━━", ""]
            for p in pfs:
                status = "🟢" if p.is_running else "⚪"
                lines.append(f"{status}  *{p.name}*  ·  `{p.investment_usdt:.0f}$`")
            await query.edit_message_text("\n".join(lines), reply_markup=kb_portfolios(pfs), parse_mode="Markdown")
            return

        # ---- Create Portfolio (start conversation) ----
        if data == "create_pf":
            context.user_data.clear()
            context.user_data["creating"] = True
            await query.edit_message_text(
                "*إنشاء محفظة جديدة*\n━━━━━━━━━━━━━━━━━━━━\n\nالخطوة 1/3\nأرسل *اسم المحفظة*:\n\nللإلغاء: /cancel",
                parse_mode="Markdown"
            )
            return WAIT_PF_NAME

        # ---- Open Portfolio ----
        if data.startswith("pf_") and data.count("_") == 1:
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=kb_back_main())
                return
            coins = [c.symbol for c in p.coins]
            targets = get_rebalancer().calculate_targets(coins, p.allocation_method)
            current = get_mexc().get_coins_value(coins)
            await query.edit_message_text(
                txt_portfolio(p, targets, current["total_usdt"]),
                reply_markup=kb_portfolio(p.id, p.is_running),
                parse_mode="Markdown"
            )
            return

        # ---- Start Strategy ----
        if data.startswith("start_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=kb_back_main())
                return

            if p.is_running:
                await query.edit_message_text(
                    f"ℹ️ المحفظة *{p.name}* تعمل بالفعل.\n\nالمخصص الحالي: `{p.investment_usdt:.2f} USDT`\n\nهل تريد *زيادة* مبلغ الاستثمار؟",
                    reply_markup=kb_confirm_increase(pf_id),
                    parse_mode="Markdown"
                )
                return

            coins = [c.symbol for c in p.coins]
            if not coins:
                await query.edit_message_text("أضف عملات أولاً.", reply_markup=kb_portfolio(pf_id, False), parse_mode="Markdown")
                return

            await query.edit_message_text("⏳ جاري التشغيل...", parse_mode="Markdown")
            result = get_rebalancer().start_portfolio(
                coins=coins, total_usdt=p.investment_usdt,
                method=p.allocation_method, min_trade_usdt=5.0, dry_run=False
            )

            if result["errors"] and not result["executed"]:
                err = "\n".join(str(e) for e in result["errors"])
                await query.edit_message_text(f"❌ فشل التشغيل:\n{err}", reply_markup=kb_portfolio(pf_id, False), parse_mode="Markdown")
                return

            set_portfolio_running(db, p.id, True)
            log_action(db, user_id, "start", str(result.get("executed")), True, p.id)

            lines = [f"✅ تم تشغيل *{p.name}*", f"المبلغ: `{p.investment_usdt:.2f} USDT`\n"]
            for o in result["executed"]:
                lines.append(f"• شراء `{o['symbol']}` ≈ `{o['usdt']:.2f}$`")
            if result["errors"]:
                lines.append("\n⚠️ أخطاء جزئية:")
                for e in result["errors"]:
                    lines.append(f"• {e}")

            p = get_portfolio(db, pf_id, user_id)
            coins = [c.symbol for c in p.coins]
            targets = get_rebalancer().calculate_targets(coins, p.allocation_method)
            current = get_mexc().get_coins_value(coins)
            await query.edit_message_text(
                "\n".join(lines) + "\n\n" + txt_portfolio(p, targets, current["total_usdt"]),
                reply_markup=kb_portfolio(pf_id, True),
                parse_mode="Markdown"
            )
            return

        # ---- Confirm Increase (from start when already running) ----
        if data.startswith("doinc_"):
            pf_id = int(data.split("_")[1])
            context.user_data["increase_pf"] = pf_id
            context.user_data["also_buy"] = True
            await query.edit_message_text(
                "◈  *زيادة استثمار*\n\nأرسل المبلغ *الإضافي* بالـ USDT (مثلاً: `20`):\nسيتم إضافته على المخصص الحالي.\nأو /cancel",
                parse_mode="Markdown"
            )
            return WAIT_INCREASE

        # ---- Stop ----
        if data.startswith("stop_") and not data.startswith("dostop_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=kb_back_main())
                return
            if not p.is_running:
                await query.edit_message_text("المحفظة متوقفة أصلاً.", reply_markup=kb_portfolio(pf_id, False), parse_mode="Markdown")
                return
            await query.edit_message_text(
                f"⚠️ *تأكيد الإيقاف*\n\nالمحفظة: *{p.name}*\n\nسيتم بيع عملات المحفظة وتحويلها إلى USDT.\nالمحفظة *لن تُحذف* ويمكن تشغيلها لاحقاً.",
                reply_markup=kb_confirm_stop(pf_id),
                parse_mode="Markdown"
            )
            return

        if data.startswith("dostop_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=kb_back_main())
                return
            await query.edit_message_text("⏳ جاري الإيقاف والبيع...", parse_mode="Markdown")
            coins = [c.symbol for c in p.coins]
            result = get_rebalancer().stop_portfolio(coins, dry_run=False)
            set_portfolio_running(db, p.id, False)
            log_action(db, user_id, "stop", str(result.get("executed")), True, p.id)

            lines = [f"⏹️ تم إيقاف *{p.name}*", f"تم بيع ≈ `{result.get('total_sold_usdt', 0):.2f} USDT`\n"]
            for o in result.get("executed", []):
                lines.append(f"• بيع `{o['symbol']}` ≈ `{o.get('usdt', 0):.2f}$`")
            if result.get("errors"):
                lines.append("\n⚠️ أخطاء:")
                for e in result["errors"]:
                    lines.append(f"• {e}")

            p = get_portfolio(db, pf_id, user_id)
            coins = [c.symbol for c in p.coins]
            targets = get_rebalancer().calculate_targets(coins, p.allocation_method)
            current = get_mexc().get_coins_value(coins)
            await query.edit_message_text(
                "\n".join(lines) + "\n\n" + txt_portfolio(p, targets, current["total_usdt"]),
                reply_markup=kb_portfolio(pf_id, False),
                parse_mode="Markdown"
            )
            return

        # ---- Edit allocated amount ----
        if data.startswith("editalloc_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=kb_back_main())
                return
            context.user_data["edit_alloc_pf"] = pf_id
            await query.edit_message_text(
                f"◈  *تعديل المبلغ المخصص*\n\n"
                f"المحفظة: *{p.name}*\n"
                f"المخصص الحالي: `{p.investment_usdt:.2f} USDT`\n\n"
                f"أرسل *المبلغ الجديد* بالـ USDT (مثلاً: `100`):\n"
                f"⚠️ يغيّر الرقم فقط بدون شراء أو بيع.\nأو /cancel",
                parse_mode="Markdown"
            )
            return WAIT_EDIT_ALLOC

        # ---- Increase Investment ----
        if data.startswith("inc_"):
            pf_id = int(data.split("_")[1])
            context.user_data["increase_pf"] = pf_id
            context.user_data["also_buy"] = False
            await query.edit_message_text(
                "◈  *زيادة استثمار*\n\nأرسل المبلغ *الإضافي* بالـ USDT (مثلاً: `20`):\nسيتم إضافته على المخصص الحالي.\nأو /cancel",
                parse_mode="Markdown"
            )
            return WAIT_INCREASE

        # ---- Rebalance menu ----
        if data.startswith("reb_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=kb_back_main())
                return
            if not p.is_running:
                await query.edit_message_text(
                    "المحفظة متوقفة. شغّلها أولاً قبل إعادة التوازن.",
                    reply_markup=kb_portfolio(pf_id, False),
                    parse_mode="Markdown"
                )
                return
            await query.edit_message_text(
                f"*إعادة التوازن*\n━━━━━━━━━━━━━━━━━━━━\n\nالمحفظة: *{p.name}*\n\n🔍 معاينة — بدون تنفيذ\n✅ تنفيذ — صفقات حقيقية",
                reply_markup=kb_rebalance(pf_id),
                parse_mode="Markdown"
            )
            return

        if data.startswith("dry_") or data.startswith("real_"):
            dry_run = data.startswith("dry_")
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=kb_back_main())
                return
            await query.edit_message_text("⏳ جاري الحساب...", parse_mode="Markdown")
            coins = [c.symbol for c in p.coins]
            result = get_rebalancer().rebalance_portfolio(
                coins=coins, target_capital=p.investment_usdt,
                method=p.allocation_method, threshold=p.threshold,
                min_trade_usdt=5.0, dry_run=dry_run
            )
            title = "🔍 معاينة" if dry_run else "✅ تم التنفيذ"
            lines = [f"*{title}* — {p.name}\n"]
            if result.get("message"):
                lines.append(result["message"])
            for o in result.get("executed", []):
                side = "شراء" if o["side"] == "buy" else "بيع"
                lines.append(f"• {side} `{o['symbol']}` ≈ `{o.get('usdt', 0):.2f}$`")
            if result.get("errors"):
                lines.append("\n⚠️ أخطاء:")
                for e in result["errors"]:
                    lines.append(f"• {e}")
            if len(lines) == 1:
                lines.append("لا توجد عمليات مطلوبة.")

            log_action(db, user_id, "rebalance_dry" if dry_run else "rebalance",
                       str(result.get("executed", [])), success=not result["errors"], portfolio_id=pf_id)
            if not dry_run and result.get("executed"):
                p.last_rebalance = datetime.utcnow()
                db.commit()

            await query.edit_message_text("\n".join(lines), reply_markup=kb_rebalance(pf_id), parse_mode="Markdown")
            return

        # ---- Coins manage ----
        if data.startswith("addcoin_"):
            pf_id = int(data.split("_")[1])
            context.user_data["add_coin_pf"] = pf_id
            await query.edit_message_text(
                "أرسل رمز العملة (مثل `BTC` أو `ONDO`):\nأو /cancel",
                parse_mode="Markdown"
            )
            return WAIT_ADD_COIN

        if data.startswith("removecoin_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p or not p.coins:
                await query.edit_message_text("لا توجد عملات للحذف.", reply_markup=kb_portfolio(pf_id), parse_mode="Markdown")
                return
            buttons = [[InlineKeyboardButton(f"🗑️ {c.symbol}", callback_data=f"del_{pf_id}_{c.symbol}")] for c in p.coins]
            buttons.append([InlineKeyboardButton("◀️  رجوع للمحفظة", callback_data=f"pf_{pf_id}")])
            await query.edit_message_text("اختر العملة للحذف:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            return

        if data.startswith("del_"):
            parts = data.split("_")
            pf_id = int(parts[1])
            symbol = parts[2]
            remove_coin_from_portfolio(db, pf_id, symbol)
            p = get_portfolio(db, pf_id, user_id)
            coins = [c.symbol for c in p.coins] if p else []
            targets = get_rebalancer().calculate_targets(coins, p.allocation_method) if p else {}
            current = get_mexc().get_coins_value(coins) if p else {"total_usdt": 0}
            await query.edit_message_text(
                f"✅ تم حذف `{symbol}`\n\n" + (txt_portfolio(p, targets, current["total_usdt"]) if p else ""),
                reply_markup=kb_portfolio(pf_id, p.is_running if p else False),
                parse_mode="Markdown"
            )
            return

        # ---- Close portfolio ----
        if data.startswith("close_") and not data.startswith("doclose_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=kb_back_main())
                return
            await query.edit_message_text(
                f"⚠️ *تأكيد الإنهاء النهائي*\n\nالمحفظة: *{p.name}*\n\n• إن كانت تعمل → يتم البيع أولاً\n• ثم حذف المحفظة نهائياً\n\nلا يمكن التراجع.",
                reply_markup=kb_confirm_close(pf_id),
                parse_mode="Markdown"
            )
            return

        if data.startswith("doclose_"):
            pf_id = int(data.split("_")[1])
            p = get_portfolio(db, pf_id, user_id)
            if not p:
                await query.edit_message_text("غير موجودة.", reply_markup=kb_back_main())
                return
            name = p.name
            if p.is_running:
                coins = [c.symbol for c in p.coins]
                get_rebalancer().stop_portfolio(coins, dry_run=False)
            close_portfolio(db, p.id)
            log_action(db, user_id, "close", name, True, p.id)
            await query.edit_message_text(
                f"✅ تم إنهاء محفظة *{name}* نهائياً.",
                reply_markup=kb_back_main(),
                parse_mode="Markdown"
            )
            return

        # ---- Signals menu ----
        if data == "signals":
            s = get_signal_settings(db, user_id)
            bots = list_signal_bots(db, user_id)
            last = ""
            if s.last_signal_at:
                last = f"\nآخر إشارة: `{s.last_signal_action}` — {s.last_signal_at.strftime('%Y-%m-%d %H:%M')}"
            msg = (
                f"*إدارة الإشارات*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"الحالة: {'🟢 مفعّل' if s.enabled else '🔴 متوقف'}\n"
                f"حد البيع: `{s.sell_threshold_m}M USDT`\n"
                f"حد الشراء: `{s.buy_threshold_m}M USDT`\n"
                f"بوتات مسجّلة: `{len(bots)}`"
                f"{last}\n\n"
                f"• إرسال ≥ الحد → *بيع كل المحافظ الشغالة*\n"
                f"• سحب ≥ الحد → *تشغيل/شراء كل المحافظ*"
            )
            await query.edit_message_text(msg, reply_markup=kb_signals(s.enabled), parse_mode="Markdown")
            return

        if data == "sig_toggle":
            s = get_signal_settings(db, user_id)
            s.enabled = not s.enabled
            db.commit()
            await query.edit_message_text(
                f"{'🟢 تم تفعيل' if s.enabled else '🔴 تم إيقاف'} نظام الإشارات.",
                reply_markup=kb_signals(s.enabled),
                parse_mode="Markdown"
            )
            return

        if data == "sig_bots":
            bots = list_signal_bots(db, user_id)
            lines = ["*بوتات الإشارات*\n━━━━━━━━━━━━━━━━━━━━\n"]
            if not bots:
                lines.append("لا يوجد بوتات بعد. أضف بوت إشارة.")
            else:
                for b in bots:
                    st = "🟢" if b.enabled else "🔴"
                    un = f"@{b.bot_username}" if b.bot_username else ""
                    bid = f"id:`{b.bot_id}`" if b.bot_id else ""
                    lines.append(f"{st} {b.label or ''} {un} {bid}".strip())
            await query.edit_message_text("\n".join(lines), reply_markup=kb_sig_bots(), parse_mode="Markdown")
            return

        if data == "sig_bot_add":
            context.user_data["waiting_signal"] = "add_bot"
            await query.edit_message_text(
                "*إضافة بوت إشارة*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                "أرسل واحداً من:\n"
                "• يوزر البوت مثل `ArkhamAlerterBot`\n"
                "• أو الايدي الرقمي مثل `123456789`\n\n"
                "للإلغاء: /cancel",
                parse_mode="Markdown"
            )
            return WAIT_SIGNAL_BOT

        if data == "sig_bot_del_menu":
            bots = list_signal_bots(db, user_id)
            if not bots:
                await query.edit_message_text("لا يوجد بوتات.", reply_markup=kb_sig_bots(), parse_mode="Markdown")
                return
            rows = [[InlineKeyboardButton(f"🗑️ {b.label or b.bot_username or b.bot_id}", callback_data=f"sig_del_{b.id}")] for b in bots]
            rows.append([InlineKeyboardButton("◀️ رجوع", callback_data="sig_bots")])
            await query.edit_message_text("اختر بوت للحذف:", reply_markup=InlineKeyboardMarkup(rows))
            return

        if data.startswith("sig_del_"):
            rid = int(data.split("_")[2])
            remove_signal_bot(db, user_id, rid)
            await query.edit_message_text("✅ تم الحذف.", reply_markup=kb_sig_bots(), parse_mode="Markdown")
            return

        if data == "sig_rules":
            s = get_signal_settings(db, user_id)
            msg = (
                f"*شروط الإشارة*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                f"حد البيع: `{s.sell_threshold_m}M`\n"
                f"حد الشراء: `{s.buy_threshold_m}M`\n\n"
                f"كلمات البيع:\n`{s.sell_keywords}`\n\n"
                f"كلمات الشراء:\n`{s.buy_keywords}`"
            )
            await query.edit_message_text(msg, reply_markup=kb_sig_rules(), parse_mode="Markdown")
            return

        if data == "sig_set_thresh":
            context.user_data["waiting_signal"] = "thresh"
            await query.edit_message_text(
                "*تعديل الحدود*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                "أرسل رقم الحد بالمليون (ينطبق على البيع والشراء).\n"
                "مثال: `15`  يعني 15,000,000 USDT\n\n"
                "للإلغاء: /cancel",
                parse_mode="Markdown"
            )
            return WAIT_SIGNAL_THRESH

        if data == "sig_test":
            context.user_data["waiting_signal"] = "test"
            await query.edit_message_text(
                "*رسالة تجريبية*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                "اكتب رسالة كأنها من بوت الإشارة.\n\n"
                "أمثلة:\n"
                "`BlackRock sent 20M BTC to Coinbase`\n"
                "`withdrew 18 million from exchange`\n\n"
                "البوت سيحلّلها وينفّذ إن تحقّق الشرط.\n"
                "للإلغاء: /cancel",
                parse_mode="Markdown"
            )
            return WAIT_SIGNAL_TEST

    finally:
        db.close()


# ==================== CONVERSATION HANDLERS ====================

async def wait_pf_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name or len(name) > 80:
        await update.message.reply_text("اسم غير صالح. أرسل اسم المحفظة أو /cancel")
        return WAIT_PF_NAME
    context.user_data["pf_name"] = name
    await update.message.reply_text(
        f"الاسم: *{name}*\n\nأرسل *مبلغ الاستثمار* بالـ USDT (مثلاً: `50`):\nأو /cancel",
        parse_mode="Markdown"
    )
    return WAIT_PF_INVESTMENT


async def wait_pf_investment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        inv = float((update.message.text or "").strip())
        if inv < 5:
            await update.message.reply_text("الحد الأدنى 5 USDT. أعد الإرسال أو /cancel")
            return WAIT_PF_INVESTMENT
    except ValueError:
        await update.message.reply_text("رقم غير صحيح. أعد الإرسال أو /cancel")
        return WAIT_PF_INVESTMENT
    context.user_data["pf_investment"] = inv
    await update.message.reply_text(
        f"المبلغ: `{inv}` USDT\n\nأرسل *العملات* مفصولة بمسافة (مثل: `BTC ETH XRP ADA`):\nأو /cancel",
        parse_mode="Markdown"
    )
    return WAIT_PF_COINS


async def wait_pf_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").replace(",", " ").replace("،", " ")
    symbols = [s.strip().upper() for s in raw.split() if s.strip()]
    if not symbols:
        await update.message.reply_text("أضف عملة واحدة على الأقل أو /cancel")
        return WAIT_PF_COINS

    valid, invalid = [], []
    for s in symbols:
        (valid if is_coin_available(s) else invalid).append(s)

    if not valid:
        await update.message.reply_text(f"كل العملات غير متاحة: {', '.join(invalid)}\nأعد الإرسال أو /cancel")
        return WAIT_PF_COINS

    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        if len(valid) > user.max_coins_per_portfolio:
            await update.message.reply_text(f"أقصى عدد = {user.max_coins_per_portfolio}. أعد الإرسال أو /cancel")
            return WAIT_PF_COINS
        min_needed = user.min_usdt_per_coin * len(valid)
        inv = context.user_data["pf_investment"]
        if inv < min_needed:
            await update.message.reply_text(f"الحد الأدنى لـ {len(valid)} عملات = {min_needed}$. أعد الإرسال أو /cancel")
            return WAIT_PF_COINS

        p = create_portfolio(
            db, update.effective_user.id,
            context.user_data["pf_name"], inv, valid,
            allocation_method=user.default_allocation_method,
            rebalance_mode=user.default_rebalance_mode,
            threshold=user.default_threshold,
            interval=user.default_interval_hours
        )
        targets = get_rebalancer().calculate_targets(valid, p.allocation_method)
        msg = f"✅ تم إنشاء *{p.name}*\nالمخصص: `{p.investment_usdt}$`\nالعملات: {', '.join(valid)}"
        if invalid:
            msg += f"\n\n⚠️ تم تجاهل: {', '.join(invalid)}"
        msg += "\n\n⚪ المحفظة متوقفة — اضغط *تشغيل* للبدء."
        await update.message.reply_text(
            msg + "\n\n" + txt_portfolio(p, targets, 0.0),
            reply_markup=kb_portfolio(p.id, False),
            parse_mode="Markdown"
        )
    finally:
        db.close()
        context.user_data.clear()
    return ConversationHandler.END


async def wait_add_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sym = (update.message.text or "").strip().upper()
    pf_id = context.user_data.get("add_coin_pf")
    if not pf_id:
        await update.message.reply_text("انتهت الجلسة.", reply_markup=kb_main())
        return ConversationHandler.END
    if not is_coin_available(sym):
        await update.message.reply_text(f"❌ `{sym}` غير متاحة على MEXC. أعد الإرسال أو /cancel", parse_mode="Markdown")
        return WAIT_ADD_COIN
    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        ok, msg = add_coin_to_portfolio(db, pf_id, sym, max_coins=user.max_coins_per_portfolio)
        p = get_portfolio(db, pf_id, update.effective_user.id)
        coins = [c.symbol for c in p.coins] if p else []
        targets = get_rebalancer().calculate_targets(coins, p.allocation_method) if p else {}
        current = get_mexc().get_coins_value(coins) if p else {"total_usdt": 0}
        await update.message.reply_text(
            msg + "\n\n" + (txt_portfolio(p, targets, current["total_usdt"]) if p else ""),
            reply_markup=kb_portfolio(pf_id, p.is_running if p else False),
            parse_mode="Markdown"
        )
    finally:
        db.close()
        context.user_data.pop("add_coin_pf", None)
    return ConversationHandler.END


async def wait_increase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float((update.message.text or "").strip())
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("رقم غير صحيح. أعد الإرسال أو /cancel")
        return WAIT_INCREASE

    pf_id = context.user_data.get("increase_pf")
    also_buy = context.user_data.get("also_buy", False)
    if not pf_id:
        await update.message.reply_text("انتهت الجلسة.", reply_markup=kb_main())
        return ConversationHandler.END

    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, update.effective_user.id)
        if not p:
            await update.message.reply_text("المحفظة غير موجودة.", reply_markup=kb_main())
            return ConversationHandler.END

        p.investment_usdt += val
        db.commit()
        msg = f"✅ تم زيادة *{p.name}* بمبلغ `{val}$`\nالإجمالي: `{p.investment_usdt:.2f}$`"

        if also_buy and p.is_running:
            coins = [c.symbol for c in p.coins]
            result = get_rebalancer().start_portfolio(
                coins=coins, total_usdt=val, method=p.allocation_method, min_trade_usdt=5.0, dry_run=False
            )
            if result["executed"]:
                msg += "\n\nتم شراء الزيادة:"
                for o in result["executed"]:
                    msg += f"\n• `{o['symbol']}` ≈ `{o['usdt']:.2f}$`"

        coins = [c.symbol for c in p.coins]
        targets = get_rebalancer().calculate_targets(coins, p.allocation_method)
        current = get_mexc().get_coins_value(coins)
        await update.message.reply_text(
            msg + "\n\n" + txt_portfolio(p, targets, current["total_usdt"]),
            reply_markup=kb_portfolio(pf_id, p.is_running),
            parse_mode="Markdown"
        )
    finally:
        db.close()
        context.user_data.clear()
    return ConversationHandler.END


async def wait_edit_alloc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float((update.message.text or "").strip())
        if val < 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل مبلغ صحيح (حد أدنى 5) أو /cancel")
        return WAIT_EDIT_ALLOC

    pf_id = context.user_data.get("edit_alloc_pf")
    if not pf_id:
        await update.message.reply_text("انتهت الجلسة.", reply_markup=kb_main())
        return ConversationHandler.END

    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, update.effective_user.id)
        if not p:
            await update.message.reply_text("المحفظة غير موجودة.", reply_markup=kb_main())
            return ConversationHandler.END

        old = p.investment_usdt
        p.investment_usdt = val
        db.commit()

        coins = [c.symbol for c in p.coins]
        targets = get_rebalancer().calculate_targets(coins, p.allocation_method)
        current = get_mexc().get_coins_value(coins)
        msg = f"✅ تم تعديل المخصص\nمن `{old:.2f}$` → `{val:.2f}$`\n\n⚠️ لم يتم شراء أو بيع — الرقم فقط."
        await update.message.reply_text(
            msg + "\n\n" + txt_portfolio(p, targets, current["total_usdt"]),
            reply_markup=kb_portfolio(pf_id, p.is_running),
            parse_mode="Markdown"
        )
    finally:
        db.close()
        context.user_data.clear()
    return ConversationHandler.END



async def wait_signal_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip().lstrip("@")
    if not raw:
        await update.message.reply_text("أرسل يوزر أو آيدي صحيح أو /cancel")
        return WAIT_SIGNAL_BOT
    db = SessionLocal()
    try:
        bot_id = None
        username = None
        if raw.isdigit():
            bot_id = int(raw)
        else:
            username = raw
        row = add_signal_bot(db, update.effective_user.id, bot_username=username, bot_id=bot_id, label=raw)
        await update.message.reply_text(
            f"✅ تمت إضافة بوت الإشارة: `{raw}`",
            reply_markup=kb_sig_bots(),
            parse_mode="Markdown"
        )
    finally:
        db.close()
        context.user_data.clear()
    return ConversationHandler.END


async def wait_signal_thresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float((update.message.text or "").strip())
        if val <= 0 or val > 10000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم موجب (مثلاً 15) أو /cancel")
        return WAIT_SIGNAL_THRESH
    db = SessionLocal()
    try:
        s = get_signal_settings(db, update.effective_user.id)
        s.sell_threshold_m = val
        s.buy_threshold_m = val
        db.commit()
        await update.message.reply_text(
            f"✅ حد البيع والشراء = `{val}M USDT`",
            reply_markup=kb_sig_rules(),
            parse_mode="Markdown"
        )
    finally:
        db.close()
        context.user_data.clear()
    return ConversationHandler.END


async def wait_signal_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    body = (update.message.text or "").strip()
    if not body:
        await update.message.reply_text("أرسل نص الرسالة التجريبية أو /cancel")
        return WAIT_SIGNAL_TEST
    result = await process_signal_text(
        body, source="TEST", context=context,
        notify_chat_id=update.effective_user.id, force_test=True
    )
    await update.message.reply_text(result, reply_markup=kb_signals(True), parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def wait_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float((update.message.text or "").strip())
        if not (0 < val <= 50):
            raise ValueError
    except ValueError:
        await update.message.reply_text("أدخل رقم بين 0 و 50 أو /cancel")
        return WAIT_THRESHOLD
    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        user.default_threshold = val
        db.commit()
        await update.message.reply_text(f"✅ Threshold = `{val}%`", reply_markup=kb_settings(), parse_mode="Markdown")
    finally:
        db.close()
        context.user_data.clear()
    return ConversationHandler.END


# ==================== MAIN ====================


async def on_any_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listen in groups/channels for signal messages from registered sources."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup", "channel"):
        return

    db = SessionLocal()
    try:
        admin = int(config.ADMIN_TELEGRAM_ID) if config.ADMIN_TELEGRAM_ID else None
        if not admin:
            logger.warning("signal skipped: ADMIN_TELEGRAM_ID not set")
            return
        bots = get_enabled_signal_bots(db, admin)
        if not bots:
            return

        # Collect possible source ids/usernames from the post
        candidates_ids = set()
        candidates_names = set()
        source_label = "unknown"

        user = msg.from_user
        if user:
            candidates_ids.add(int(user.id))
            if user.username:
                candidates_names.add(user.username.lower())
            source_label = user.username or str(user.id)

        # Channel posts often have sender_chat (the channel) and no from_user
        sender_chat = getattr(msg, "sender_chat", None)
        if sender_chat:
            candidates_ids.add(int(sender_chat.id))
            if getattr(sender_chat, "username", None):
                candidates_names.add(sender_chat.username.lower())
            source_label = getattr(sender_chat, "username", None) or str(sender_chat.id)

        # The chat itself (channel/group id) can be registered as source
        candidates_ids.add(int(chat.id))
        if getattr(chat, "username", None):
            candidates_names.add(chat.username.lower())

        matched = False
        for b in bots:
            if b.bot_id and int(b.bot_id) in candidates_ids:
                matched = True
                source_label = b.label or str(b.bot_id)
                break
            if b.bot_username and b.bot_username.lower().lstrip("@") in candidates_names:
                matched = True
                source_label = b.bot_username
                break
        if not matched:
            logger.info(
                "signal ignored (no source match) chat=%s ids=%s names=%s",
                chat.id, candidates_ids, candidates_names
            )
            return

        result = await process_signal_text(
            msg.text, source=str(source_label), context=context,
            notify_chat_id=admin, force_test=False
        )
        try:
            await context.bot.send_message(chat_id=admin, text=result, parse_mode="Markdown")
        except Exception as e:
            logger.warning("notify admin failed: %s", e)
    finally:
        db.close()


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

    # Callbacks work inside every conversation state + as entry points
    cb = CallbackQueryHandler(button_handler)
    text_filters = filters.TEXT & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[cb],
        states={
            WAIT_PF_NAME: [
                MessageHandler(text_filters, wait_pf_name),
                cb,
            ],
            WAIT_PF_INVESTMENT: [
                MessageHandler(text_filters, wait_pf_investment),
                cb,
            ],
            WAIT_PF_COINS: [
                MessageHandler(text_filters, wait_pf_coins),
                cb,
            ],
            WAIT_ADD_COIN: [
                MessageHandler(text_filters, wait_add_coin),
                cb,
            ],
            WAIT_INCREASE: [
                MessageHandler(text_filters, wait_increase),
                cb,
            ],
            WAIT_THRESHOLD: [
                MessageHandler(text_filters, wait_threshold),
                cb,
            ],
            WAIT_EDIT_ALLOC: [
                MessageHandler(text_filters, wait_edit_alloc),
                cb,
            ],
            WAIT_SIGNAL_BOT: [
                MessageHandler(text_filters, wait_signal_bot),
                cb,
            ],
            WAIT_SIGNAL_THRESH: [
                MessageHandler(text_filters, wait_signal_thresh),
                cb,
            ],
            WAIT_SIGNAL_TEST: [
                MessageHandler(text_filters, wait_signal_test),
                cb,
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), CommandHandler("start", cmd_start)],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(conv)
    # groups + channel posts (channel_post updates need explicit filter)
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        on_any_text_message
    ))
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POSTS & filters.TEXT,
        on_any_text_message
    ))

    logger.info("Starting Telegram bot (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
