"""
MEXC Multi-Portfolio Rebalancer - Discord Bot
"""
import logging
import asyncio
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import (
    init_db, SessionLocal, get_or_create_user, get_portfolios, get_portfolio,
    create_portfolio, add_coin_to_portfolio, remove_coin_from_portfolio,
    close_portfolio, log_action
)
from mexc_client import MexcClient
from rebalancer import Rebalancer

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

mexc_client = None
rebalancer_inst = None


def get_mexc():
    global mexc_client
    if mexc_client is None:
        mexc_client = MexcClient()
    return mexc_client


def get_rebalancer():
    global rebalancer_inst
    if rebalancer_inst is None:
        rebalancer_inst = Rebalancer(get_mexc())
    return rebalancer_inst


def is_authorized(user_id: int) -> bool:
    if config.ADMIN_DISCORD_ID:
        return str(user_id) == str(config.ADMIN_DISCORD_ID)
    return True


def is_coin_available(symbol: str) -> bool:
    try:
        pair = f"{symbol.upper()}/USDT"
        markets = get_mexc().exchange.load_markets()
        m = markets.get(pair)
        return bool(m and m.get("active", True) and m.get("spot", True))
    except Exception:
        return False


# ==================== HELPERS ====================

def format_full_balance(portfolio: dict) -> str:
    if portfolio["total_usdt"] <= 0:
        return "الحساب فارغ حالياً."
    lines = [f"**إجمالي الحساب:** `{portfolio['total_usdt']:.4f} USDT`\n"]
    sorted_assets = sorted(portfolio["assets"].items(), key=lambda x: x[1]["usdt_value"], reverse=True)
    for asset, data in sorted_assets:
        lines.append(f"• `{asset}`: {data['amount']:.6f} ≈ `{data['usdt_value']:.4f}$` ({data['percent']:.2f}%)")
    return "\n".join(lines)


def format_portfolio_detail(p, targets: dict = None) -> str:
    coins = [c.symbol for c in p.coins]
    method = "بالتساوي" if p.allocation_method == "equal" else "قيمة سوقية"
    mode = "نسبي %" if p.rebalance_mode == "threshold" else "بالوقت"
    lines = [
        f"**{p.name}**",
        f"الحالة: {'🟢 نشطة' if p.status == 'active' else '🔴 منتهية'}",
        f"الاستثمار المستهدف: `{p.investment_usdt:.2f} USDT`",
        f"عدد العملات: {len(coins)}",
        f"طريقة التوزيع: {method}",
        f"وضع الانحراف: {mode}",
        f"Threshold: {p.threshold}%",
        f"الفترة: {p.rebalance_interval_hours} ساعة",
        "",
        "**العملات:**",
    ]
    if targets:
        for s in coins:
            lines.append(f"• `{s}` → {targets.get(s, 0):.1f}%")
    else:
        for s in coins:
            lines.append(f"• `{s}`")
    return "\n".join(lines)


# ==================== VIEWS (BUTTONS) ====================

class MainMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="محافظي", style=discord.ButtonStyle.primary, emoji="📁")
    async def my_portfolios(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message("غير مصرح.", ephemeral=True)
            return
        db = SessionLocal()
        try:
            pfs = get_portfolios(db, interaction.user.id, status="active")
            if not pfs:
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label="إنشاء محفظة", style=discord.ButtonStyle.success, custom_id="create_pf"))
                await interaction.response.send_message("لا توجد محافظ نشطة.", view=CreatePortfolioView(), ephemeral=True)
                return
            view = PortfoliosListView(pfs)
            await interaction.response.send_message("محافظك النشطة:", view=view, ephemeral=True)
        finally:
            db.close()

    @discord.ui.button(label="إنشاء محفظة", style=discord.ButtonStyle.success, emoji="➕")
    async def create_pf(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message("غير مصرح.", ephemeral=True)
            return
        await interaction.response.send_modal(CreatePortfolioModal())

    @discord.ui.button(label="رصيد الحساب", style=discord.ButtonStyle.secondary, emoji="📊")
    async def full_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message("غير مصرح.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            bal = get_mexc().get_portfolio_value()
            text = format_full_balance(bal)
            await interaction.followup.send(text, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"خطأ: `{e}`", ephemeral=True)

    @discord.ui.button(label="الإعدادات العامة", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message("غير مصرح.", ephemeral=True)
            return
        db = SessionLocal()
        try:
            user = get_or_create_user(db, interaction.user.id)
            method = "بالتساوي" if user.default_allocation_method == "equal" else "قيمة سوقية"
            mode = "نسبي %" if user.default_rebalance_mode == "threshold" else "بالوقت"
            text = (
                f"**الإعدادات العامة**\n\n"
                f"• طريقة التوزيع: `{method}`\n"
                f"• وضع الانحراف: `{mode}`\n"
                f"• Threshold: `{user.default_threshold}%`\n"
                f"• الفترة: `{user.default_interval_hours}` ساعة\n"
                f"• حد أدنى للصفقة: `{user.min_trade_usdt}$`\n"
                f"• أقصى عملات/محفظة: `{user.max_coins_per_portfolio}`\n"
            )
            await interaction.response.send_message(text, view=GlobalSettingsView(user), ephemeral=True)
        finally:
            db.close()


class CreatePortfolioView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="إنشاء محفظة جديدة", style=discord.ButtonStyle.success)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CreatePortfolioModal())


class CreatePortfolioModal(discord.ui.Modal, title="إنشاء محفظة جديدة"):
    name = discord.ui.TextInput(label="اسم المحفظة", placeholder="مثلاً: محفظة الذكاء الاصطناعي", max_length=80)
    investment = discord.ui.TextInput(label="مبلغ الاستثمار (USDT)", placeholder="مثلاً: 50", max_length=20)
    coins = discord.ui.TextInput(
        label="العملات (مفصولة بمسافة أو فاصلة)",
        placeholder="BTC ETH ONDO HBAR",
        style=discord.TextStyle.paragraph,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            inv = float(str(self.investment.value).strip())
            if inv < 5:
                await interaction.followup.send("الحد الأدنى للاستثمار 5 USDT.", ephemeral=True)
                return
        except ValueError:
            await interaction.followup.send("مبلغ الاستثمار غير صحيح.", ephemeral=True)
            return

        raw = str(self.coins.value).replace(",", " ").replace("،", " ")
        symbols = [s.strip().upper() for s in raw.split() if s.strip()]
        if not symbols:
            await interaction.followup.send("أضف عملة واحدة على الأقل.", ephemeral=True)
            return

        # Live check
        valid = []
        invalid = []
        for s in symbols:
            if is_coin_available(s):
                valid.append(s)
            else:
                invalid.append(s)

        if not valid:
            await interaction.followup.send(f"كل العملات غير متاحة على MEXC: {', '.join(invalid)}", ephemeral=True)
            return

        db = SessionLocal()
        try:
            user = get_or_create_user(db, interaction.user.id)
            if len(valid) > user.max_coins_per_portfolio:
                await interaction.followup.send(f"أقصى عدد عملات = {user.max_coins_per_portfolio}", ephemeral=True)
                return
            min_needed = user.min_usdt_per_coin * len(valid)
            if inv < min_needed:
                await interaction.followup.send(
                    f"المبلغ قليل. الحد الأدنى لـ {len(valid)} عملات = {min_needed}$",
                    ephemeral=True
                )
                return

            p = create_portfolio(
                db, interaction.user.id, str(self.name.value)[:80], inv, valid,
                allocation_method=user.default_allocation_method,
                rebalance_mode=user.default_rebalance_mode,
                threshold=user.default_threshold,
                interval=user.default_interval_hours
            )
            msg = f"✅ تم إنشاء محفظة **{p.name}**\nالاستثمار: `{p.investment_usdt}$`\nالعملات: {', '.join(valid)}"
            if invalid:
                msg += f"\n\n⚠️ تم تجاهل (غير متاحة): {', '.join(invalid)}"
            await interaction.followup.send(msg, view=PortfolioMenuView(p.id), ephemeral=True)
        finally:
            db.close()


class PortfoliosListView(discord.ui.View):
    def __init__(self, portfolios):
        super().__init__(timeout=300)
        for p in portfolios[:20]:
            self.add_item(PortfolioButton(p))


class PortfolioButton(discord.ui.Button):
    def __init__(self, portfolio):
        super().__init__(
            label=f"{portfolio.name} ({portfolio.investment_usdt:.0f}$)",
            style=discord.ButtonStyle.primary,
            custom_id=f"open_pf_{portfolio.id}"
        )
        self.portfolio_id = portfolio.id

    async def callback(self, interaction: discord.Interaction):
        db = SessionLocal()
        try:
            p = get_portfolio(db, self.portfolio_id, interaction.user.id)
            if not p:
                await interaction.response.send_message("المحفظة غير موجودة.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"المحفظة: **{p.name}**",
                view=PortfolioMenuView(p.id),
                ephemeral=True
            )
        finally:
            db.close()


class PortfolioMenuView(discord.ui.View):
    def __init__(self, portfolio_id: int):
        super().__init__(timeout=300)
        self.portfolio_id = portfolio_id

    @discord.ui.button(label="تفاصيل", style=discord.ButtonStyle.secondary, emoji="📋")
    async def detail(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = SessionLocal()
        try:
            p = get_portfolio(db, self.portfolio_id, interaction.user.id)
            if not p:
                await interaction.response.send_message("غير موجودة.", ephemeral=True)
                return
            symbols = [c.symbol for c in p.coins]
            targets = get_rebalancer().calculate_targets(symbols, p.allocation_method)
            text = format_portfolio_detail(p, targets)
            await interaction.response.send_message(text, view=PortfolioMenuView(self.portfolio_id), ephemeral=True)
        finally:
            db.close()

    @discord.ui.button(label="العملات", style=discord.ButtonStyle.secondary, emoji="🪙")
    async def coins(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = SessionLocal()
        try:
            p = get_portfolio(db, self.portfolio_id, interaction.user.id)
            if not p:
                await interaction.response.send_message("غير موجودة.", ephemeral=True)
                return
            symbols = [c.symbol for c in p.coins]
            text = f"**عملات {p.name}:**\n" + ("\n".join(f"• `{s}`" for s in symbols) if symbols else "لا توجد")
            await interaction.response.send_message(text, view=CoinsManageView(self.portfolio_id), ephemeral=True)
        finally:
            db.close()

    @discord.ui.button(label="زيادة استثمار", style=discord.ButtonStyle.primary, emoji="💰")
    async def increase(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(IncreaseInvestmentModal(self.portfolio_id))

    @discord.ui.button(label="إعادة توازن", style=discord.ButtonStyle.success, emoji="⚖️")
    async def rebalance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "إعادة توازن المحفظة:",
            view=RebalanceView(self.portfolio_id),
            ephemeral=True
        )

    @discord.ui.button(label="إنهاء المحفظة", style=discord.ButtonStyle.danger, emoji="🛑")
    async def close_pf(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = SessionLocal()
        try:
            p = get_portfolio(db, self.portfolio_id, interaction.user.id)
            if p:
                close_portfolio(db, self.portfolio_id)
                await interaction.response.send_message(f"✅ تم إنهاء محفظة **{p.name}**", ephemeral=True)
            else:
                await interaction.response.send_message("غير موجودة.", ephemeral=True)
        finally:
            db.close()


class CoinsManageView(discord.ui.View):
    def __init__(self, portfolio_id: int):
        super().__init__(timeout=300)
        self.portfolio_id = portfolio_id

    @discord.ui.button(label="إضافة عملة", style=discord.ButtonStyle.success, emoji="🔍")
    async def add_coin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddCoinModal(self.portfolio_id))

    @discord.ui.button(label="حذف عملة", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove_coin(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = SessionLocal()
        try:
            p = get_portfolio(db, self.portfolio_id, interaction.user.id)
            if not p or not p.coins:
                await interaction.response.send_message("لا توجد عملات.", ephemeral=True)
                return
            view = discord.ui.View(timeout=120)
            for c in p.coins:
                view.add_item(RemoveCoinButton(self.portfolio_id, c.symbol))
            await interaction.response.send_message("اختر العملة للحذف:", view=view, ephemeral=True)
        finally:
            db.close()

    @discord.ui.button(label="رجوع", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "قائمة المحفظة:",
            view=PortfolioMenuView(self.portfolio_id),
            ephemeral=True
        )


class RemoveCoinButton(discord.ui.Button):
    def __init__(self, portfolio_id: int, symbol: str):
        super().__init__(label=f"حذف {symbol}", style=discord.ButtonStyle.danger)
        self.portfolio_id = portfolio_id
        self.symbol = symbol

    async def callback(self, interaction: discord.Interaction):
        db = SessionLocal()
        try:
            remove_coin_from_portfolio(db, self.portfolio_id, self.symbol)
            await interaction.response.send_message(
                f"✅ تم حذف `{self.symbol}`",
                view=CoinsManageView(self.portfolio_id),
                ephemeral=True
            )
        finally:
            db.close()


class AddCoinModal(discord.ui.Modal, title="إضافة عملة"):
    def __init__(self, portfolio_id: int):
        super().__init__()
        self.portfolio_id = portfolio_id
        self.symbol = discord.ui.TextInput(label="رمز العملة", placeholder="ONDO أو BTC", max_length=15)
        self.add_item(self.symbol)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sym = str(self.symbol.value).strip().upper()
        if not is_coin_available(sym):
            await interaction.followup.send(f"❌ `{sym}` غير متاحة على MEXC Spot/USDT.", ephemeral=True)
            return
        db = SessionLocal()
        try:
            user = get_or_create_user(db, interaction.user.id)
            ok, msg = add_coin_to_portfolio(db, self.portfolio_id, sym, max_coins=user.max_coins_per_portfolio)
            await interaction.followup.send(msg, view=CoinsManageView(self.portfolio_id), ephemeral=True)
        finally:
            db.close()


class IncreaseInvestmentModal(discord.ui.Modal, title="زيادة الاستثمار"):
    def __init__(self, portfolio_id: int):
        super().__init__()
        self.portfolio_id = portfolio_id
        self.amount = discord.ui.TextInput(label="المبلغ الإضافي (USDT)", placeholder="20", max_length=20)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = float(str(self.amount.value).strip())
            if val <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("رقم غير صحيح.", ephemeral=True)
            return
        db = SessionLocal()
        try:
            p = get_portfolio(db, self.portfolio_id, interaction.user.id)
            if not p:
                await interaction.response.send_message("المحفظة غير موجودة.", ephemeral=True)
                return
            p.investment_usdt += val
            db.commit()
            await interaction.response.send_message(
                f"✅ تم زيادة استثمار **{p.name}** بمبلغ {val}$\nالإجمالي: `{p.investment_usdt:.2f}$`",
                view=PortfolioMenuView(self.portfolio_id),
                ephemeral=True
            )
        finally:
            db.close()


class RebalanceView(discord.ui.View):
    def __init__(self, portfolio_id: int):
        super().__init__(timeout=180)
        self.portfolio_id = portfolio_id

    @discord.ui.button(label="معاينة (Dry Run)", style=discord.ButtonStyle.secondary, emoji="🔍")
    async def dry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, dry_run=True)

    @discord.ui.button(label="تنفيذ فعلي", style=discord.ButtonStyle.danger, emoji="✅")
    async def real(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, dry_run=False)

    async def _run(self, interaction: discord.Interaction, dry_run: bool):
        await interaction.response.defer(ephemeral=True)
        db = SessionLocal()
        try:
            p = get_portfolio(db, self.portfolio_id, interaction.user.id)
            if not p or not p.coins:
                await interaction.followup.send("المحفظة فارغة من العملات.", ephemeral=True)
                return
            user = get_or_create_user(db, interaction.user.id)
            symbols = [c.symbol for c in p.coins]
            result = get_rebalancer().execute_rebalance(
                selected_coins=symbols,
                allocation_method=p.allocation_method,
                threshold=p.threshold,
                min_trade_usdt=user.min_trade_usdt,
                dry_run=dry_run
            )
            lines = ["🔍 **معاينة**\n" if dry_run else "✅ **تم التنفيذ**\n"]
            lines.append("**النسب المستهدفة:**")
            for s, pct in result.get("targets", {}).items():
                lines.append(f"• `{s}` → {pct:.1f}%")
            lines.append("")
            if result.get("message"):
                lines.append(result["message"])
            else:
                for o in result["executed"]:
                    lines.append(
                        f"• {o['side'].upper()} `{o['asset']}` "
                        f"{o['amount']:.6f} ≈ {o['usdt_value']:.2f}$ "
                        f"({o['current_pct']:.1f}%→{o['target_pct']:.1f}%)"
                    )
                if result["errors"]:
                    lines.append("\n❌ أخطاء:")
                    for e in result["errors"]:
                        lines.append(f"• {e['error']}")

            log_action(
                db, interaction.user.id,
                "rebalance_dry" if dry_run else "rebalance",
                str(result.get("executed", [])),
                success=not result["errors"],
                portfolio_id=self.portfolio_id
            )
            if not dry_run:
                p.last_rebalance = datetime.utcnow()
                db.commit()

            await interaction.followup.send("\n".join(lines), view=RebalanceView(self.portfolio_id), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"خطأ: `{e}`", ephemeral=True)
        finally:
            db.close()


class GlobalSettingsView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=300)
        self.user_id = user.discord_id

    @discord.ui.button(label="تبديل طريقة التوزيع", style=discord.ButtonStyle.primary)
    async def toggle_method(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = SessionLocal()
        try:
            user = get_or_create_user(db, interaction.user.id)
            user.default_allocation_method = "marketcap" if user.default_allocation_method == "equal" else "equal"
            db.commit()
            method = "بالتساوي" if user.default_allocation_method == "equal" else "قيمة سوقية"
            await interaction.response.send_message(f"✅ طريقة التوزيع: **{method}**", ephemeral=True)
        finally:
            db.close()

    @discord.ui.button(label="تبديل وضع الانحراف", style=discord.ButtonStyle.primary)
    async def toggle_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = SessionLocal()
        try:
            user = get_or_create_user(db, interaction.user.id)
            user.default_rebalance_mode = "time" if user.default_rebalance_mode == "threshold" else "threshold"
            db.commit()
            mode = "نسبي %" if user.default_rebalance_mode == "threshold" else "بالوقت"
            await interaction.response.send_message(f"✅ وضع الانحراف: **{mode}**", ephemeral=True)
        finally:
            db.close()

    @discord.ui.button(label="تعديل Threshold", style=discord.ButtonStyle.secondary)
    async def set_threshold(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ThresholdModal())


class ThresholdModal(discord.ui.Modal, title="تعديل Threshold"):
    value = discord.ui.TextInput(label="نسبة الانحراف %", placeholder="2", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = float(str(self.value.value).strip())
            if not (0 < val <= 50):
                raise ValueError
        except ValueError:
            await interaction.response.send_message("أدخل رقم بين 0 و 50.", ephemeral=True)
            return
        db = SessionLocal()
        try:
            user = get_or_create_user(db, interaction.user.id)
            user.default_threshold = val
            db.commit()
            await interaction.response.send_message(f"✅ Threshold = {val}%", ephemeral=True)
        finally:
            db.close()


# ==================== SLASH COMMANDS ====================

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        logger.error(f"Sync error: {e}")


@bot.tree.command(name="start", description="القائمة الرئيسية لبوت محافظ MEXC")
async def start_cmd(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("غير مصرح لك باستخدام هذا البوت.", ephemeral=True)
        return
    db = SessionLocal()
    try:
        get_or_create_user(db, interaction.user.id)
    finally:
        db.close()

    text = (
        f"مرحباً **{interaction.user.display_name}** 👋\n\n"
        "بوت **محافظ MEXC المتعددة**\n\n"
        "• أنشئ محافظ منفصلة\n"
        "• كل محفظة لها عملاتها وإعداداتها واستثمارها\n"
        "• تحكم كامل (إضافة/حذف عملات، زيادة استثمار، إعادة توازن، إنهاء)\n"
        "• الحد الأدنى 5$ لكل عملة\n"
        "• البحث لحظي عن توفر العملة على MEXC"
    )
    await interaction.response.send_message(text, view=MainMenuView(), ephemeral=True)


@bot.tree.command(name="portfolios", description="عرض محافظك النشطة")
async def portfolios_cmd(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("غير مصرح.", ephemeral=True)
        return
    db = SessionLocal()
    try:
        pfs = get_portfolios(db, interaction.user.id, status="active")
        if not pfs:
            await interaction.response.send_message("لا توجد محافظ نشطة.", view=CreatePortfolioView(), ephemeral=True)
            return
        await interaction.response.send_message("محافظك النشطة:", view=PortfoliosListView(pfs), ephemeral=True)
    finally:
        db.close()


@bot.tree.command(name="balance", description="رصيد الحساب الكامل على MEXC")
async def balance_cmd(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("غير مصرح.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        bal = get_mexc().get_portfolio_value()
        await interaction.followup.send(format_full_balance(bal), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"خطأ: `{e}`", ephemeral=True)


@bot.tree.command(name="create", description="إنشاء محفظة جديدة")
async def create_cmd(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("غير مصرح.", ephemeral=True)
        return
    await interaction.response.send_modal(CreatePortfolioModal())


def main():
    if not config.DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    if not config.MEXC_API_KEY or not config.MEXC_API_SECRET:
        raise ValueError("MEXC_API_KEY and MEXC_API_SECRET are required")
    if not config.DATABASE_URL:
        raise ValueError("DATABASE_URL is required")

    init_db()
    logger.info("Database initialized")
    bot.run(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
