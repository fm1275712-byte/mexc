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
    close_portfolio, set_portfolio_running, log_action
)
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
) = range(7)

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
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), CommandHandler("start", cmd_start)],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(conv)

    logger.info("Starting Telegram bot (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
