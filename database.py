from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, BigInteger, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    default_threshold = Column(Float, default=2.0)
    default_interval_hours = Column(Integer, default=24)
    default_allocation_method = Column(String(20), default="equal")
    default_rebalance_mode = Column(String(20), default="threshold")
    min_trade_usdt = Column(Float, default=5.0)
    max_coins_per_portfolio = Column(Integer, default=30)  # raised 10 → 30
    min_usdt_per_coin = Column(Float, default=5.0)
    # Multi TP + SL (configurable from Telegram bot)
    tp1_pct = Column(Float, default=3.0)    # الهدف 1 %
    tp2_pct = Column(Float, default=5.0)    # الهدف 2 %
    tp3_pct = Column(Float, default=8.0)    # الهدف 3 %
    tp1_sell_pct = Column(Float, default=40.0)  # نسبة البيع عند الهدف 1
    tp2_sell_pct = Column(Float, default=30.0)  # نسبة البيع عند الهدف 2
    # الباقي يُباع عند الهدف 3 أو يبقى مع الاستوب المرفوع
    stop_loss_pct = Column(Float, default=3.0)
    # legacy single field kept for migration compatibility
    take_profit_pct = Column(Float, default=5.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    investment_usdt = Column(Float, default=0.0)
    base_investment = Column(Float, default=0.0)
    status = Column(String(20), default="active")
    is_running = Column(Boolean, default=False)

    allocation_method = Column(String(20), default="equal")
    rebalance_mode = Column(String(20), default="threshold")
    threshold = Column(Float, default=2.0)
    rebalance_interval_hours = Column(Integer, default=24)

    # Per-portfolio TP/SL (None/0 = use user defaults)
    tp1_pct = Column(Float, nullable=True)
    tp2_pct = Column(Float, nullable=True)
    tp3_pct = Column(Float, nullable=True)
    tp1_sell_pct = Column(Float, nullable=True)
    tp2_sell_pct = Column(Float, nullable=True)
    stop_loss_pct = Column(Float, nullable=True)

    last_rebalance = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    coins = relationship("PortfolioCoin", back_populates="portfolio", cascade="all, delete-orphan")


class PortfolioCoin(Base):
    __tablename__ = "portfolio_coins"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    target_percent = Column(Float, default=0.0)
    # Position tracking for multi-TP / SL
    entry_price = Column(Float, default=0.0)
    tp1_price = Column(Float, default=0.0)
    tp2_price = Column(Float, default=0.0)
    tp3_price = Column(Float, default=0.0)
    tp_price = Column(Float, default=0.0)  # legacy / current next TP
    current_sl_price = Column(Float, default=0.0)
    tp1_order_id = Column(String(64), nullable=True)
    tp2_order_id = Column(String(64), nullable=True)
    tp3_order_id = Column(String(64), nullable=True)
    tp_order_id = Column(String(64), nullable=True)  # legacy
    position_status = Column(String(20), default="idle")  # idle | open | tp1_hit | tp2_hit | tp3_hit | closed
    amount = Column(Float, default=0.0)
    remaining_amount = Column(Float, default=0.0)
    original_sl_price = Column(Float, default=0.0)  # initial SL, used for re-entry
    reentry_price = Column(Float, default=0.0)
    reentry_used = Column(Boolean, default=False)   # only one re-entry per cycle
    reentry_touched = Column(Boolean, default=False)  # price touched reentry zone
    created_at = Column(DateTime, default=datetime.utcnow)

    portfolio = relationship("Portfolio", back_populates="coins")


class RebalanceLog(Base):
    __tablename__ = "rebalance_logs"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    portfolio_id = Column(Integer, nullable=True)
    action = Column(String(50), nullable=False)
    details = Column(String(500), nullable=True)
    success = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ==================== SIGNAL ENGINE ====================

class SignalSource(Base):
    """مصدر إشارة (BlackRock, MacroStrategy, ...)"""
    __tablename__ = "signal_sources"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    name = Column(String(80), nullable=False)
    enabled = Column(Boolean, default=True)
    min_usd = Column(Float, default=15_000_000)
    max_tx_count = Column(Integer, default=3)
    allow_buy = Column(Boolean, default=True)
    allow_sell = Column(Boolean, default=True)
    size_mode = Column(String(20), default="full")
    size_value = Column(Float, default=100.0)
    cooldown_minutes = Column(Integer, default=30)
    notes = Column(String(300), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    buy_portfolio_ids = Column(String(200), default="")
    sell_portfolio_ids = Column(String(200), default="")

    signals = relationship("SignalLog", back_populates="source", cascade="all, delete-orphan")


class SignalLog(Base):
    """سجل كل إشارة وصلت"""
    __tablename__ = "signal_logs"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("signal_sources.id"), nullable=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    raw_text = Column(Text, nullable=True)
    action = Column(String(10), nullable=False)
    reason = Column(String(400), default="")
    executed = Column(Boolean, default=False)
    result_msg = Column(String(500), default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    source = relationship("SignalSource", back_populates="signals")


def init_db():
    Base.metadata.create_all(bind=engine)

    from sqlalchemy import text, inspect
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in ("user_settings", "portfolios", "rebalance_logs"):
            if table not in insp.get_table_names():
                continue
            cols = [c["name"] for c in insp.get_columns(table)]
            if "discord_id" in cols and "telegram_id" not in cols:
                conn.execute(text(f'ALTER TABLE {table} RENAME COLUMN discord_id TO telegram_id'))
                print(f"[migration] Renamed {table}.discord_id → telegram_id")
            elif "discord_id" in cols and "telegram_id" in cols:
                conn.execute(text(f'ALTER TABLE {table} DROP COLUMN discord_id'))
                print(f"[migration] Dropped leftover {table}.discord_id")

        if "portfolios" in insp.get_table_names():
            cols = [c["name"] for c in insp.get_columns("portfolios")]
            if "is_running" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN is_running BOOLEAN DEFAULT FALSE"))
            if "started_at" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN started_at TIMESTAMP"))
            if "stopped_at" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN stopped_at TIMESTAMP"))
            if "base_investment" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN base_investment DOUBLE PRECISION DEFAULT 0"))
            for col in ("tp1_pct", "tp2_pct", "tp3_pct", "tp1_sell_pct", "tp2_sell_pct", "stop_loss_pct"):
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE portfolios ADD COLUMN {col} DOUBLE PRECISION"))

        if "portfolio_coins" in insp.get_table_names():
            cols = [c["name"] for c in insp.get_columns("portfolio_coins")]
            if "target_percent" not in cols:
                conn.execute(text("ALTER TABLE portfolio_coins ADD COLUMN target_percent DOUBLE PRECISION DEFAULT 0"))
            for col, typ in [
                ("entry_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp1_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp2_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp3_price", "DOUBLE PRECISION DEFAULT 0"),
                ("current_sl_price", "DOUBLE PRECISION DEFAULT 0"),
                ("tp_order_id", "VARCHAR(64)"),
                ("tp1_order_id", "VARCHAR(64)"),
                ("tp2_order_id", "VARCHAR(64)"),
                ("tp3_order_id", "VARCHAR(64)"),
                ("position_status", "VARCHAR(20) DEFAULT 'idle'"),
                ("amount", "DOUBLE PRECISION DEFAULT 0"),
                ("remaining_amount", "DOUBLE PRECISION DEFAULT 0"),
                ("original_sl_price", "DOUBLE PRECISION DEFAULT 0"),
                ("reentry_price", "DOUBLE PRECISION DEFAULT 0"),
                ("reentry_used", "BOOLEAN DEFAULT FALSE"),
                ("reentry_touched", "BOOLEAN DEFAULT FALSE"),
            ]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE portfolio_coins ADD COLUMN {col} {typ}"))

        if "user_settings" in insp.get_table_names():
            cols = [c["name"] for c in insp.get_columns("user_settings")]
            for col, default in [
                ("take_profit_pct", "5.0"),
                ("stop_loss_pct", "3.0"),
                ("tp1_pct", "3.0"),
                ("tp2_pct", "5.0"),
                ("tp3_pct", "8.0"),
                ("tp1_sell_pct", "40.0"),
                ("tp2_sell_pct", "30.0"),
            ]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE user_settings ADD COLUMN {col} DOUBLE PRECISION DEFAULT {default}"))
            try:
                conn.execute(text("UPDATE user_settings SET max_coins_per_portfolio = 30 WHERE max_coins_per_portfolio < 30 OR max_coins_per_portfolio IS NULL"))
            except Exception:
                pass


def get_or_create_user(db, telegram_id: int):
    user = db.query(UserSettings).filter(UserSettings.telegram_id == telegram_id).first()
    if not user:
        user = UserSettings(telegram_id=telegram_id, max_coins_per_portfolio=30)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_portfolios(db, telegram_id: int, status: str = "active"):
    q = db.query(Portfolio).filter(Portfolio.telegram_id == telegram_id)
    if status:
        q = q.filter(Portfolio.status == status)
    return q.order_by(Portfolio.created_at.desc()).all()


def get_portfolio(db, portfolio_id: int, telegram_id: int = None):
    q = db.query(Portfolio).filter(Portfolio.id == portfolio_id)
    if telegram_id:
        q = q.filter(Portfolio.telegram_id == telegram_id)
    return q.first()


def create_portfolio(db, telegram_id: int, name: str, investment: float, coins: list,
                     allocation_method: str = "equal", rebalance_mode: str = "threshold",
                     threshold: float = 2.0, interval: int = 24) -> Portfolio:
    p = Portfolio(
        telegram_id=telegram_id,
        name=name,
        investment_usdt=investment,
        base_investment=investment,
        allocation_method=allocation_method,
        rebalance_mode=rebalance_mode,
        threshold=threshold,
        rebalance_interval_hours=interval,
        status="active",
        is_running=False
    )
    db.add(p)
    db.flush()
    for symbol in coins:
        db.add(PortfolioCoin(portfolio_id=p.id, symbol=symbol.upper()))
    db.commit()
    db.refresh(p)
    return p


def add_coin_to_portfolio(db, portfolio_id: int, symbol: str, max_coins: int = 30):
    symbol = symbol.upper().strip()
    p = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
    if not p:
        return False, "المحفظة غير موجودة"
    existing = [c.symbol for c in p.coins]
    if symbol in existing:
        return False, f"`{symbol}` موجودة مسبقاً"
    if len(existing) >= max_coins:
        return False, f"وصلت للحد الأقصى ({max_coins})"
    db.add(PortfolioCoin(portfolio_id=portfolio_id, symbol=symbol))
    db.commit()
    return True, f"✅ تم إضافة `{symbol}`"


def remove_coin_from_portfolio(db, portfolio_id: int, symbol: str) -> bool:
    deleted = db.query(PortfolioCoin).filter(
        PortfolioCoin.portfolio_id == portfolio_id,
        PortfolioCoin.symbol == symbol.upper()
    ).delete()
    db.commit()
    return deleted > 0


def close_portfolio(db, portfolio_id: int):
    p = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
    if p:
        p.status = "closed"
        p.is_running = False
        p.closed_at = datetime.utcnow()
        db.commit()


def set_portfolio_running(db, portfolio_id: int, running: bool):
    p = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
    if p:
        p.is_running = running
        if running:
            p.started_at = datetime.utcnow()
            p.stopped_at = None
        else:
            p.stopped_at = datetime.utcnow()
        db.commit()
        return p
    return None


def update_coin_position(db, coin_id: int, **kwargs):
    """Update position fields on a PortfolioCoin."""
    coin = db.query(PortfolioCoin).filter(PortfolioCoin.id == coin_id).first()
    if not coin:
        return None
    for k, v in kwargs.items():
        if hasattr(coin, k):
            setattr(coin, k, v)
    db.commit()
    db.refresh(coin)
    return coin


def reset_coin_positions(db, portfolio_id: int):
    """Reset all position tracking when portfolio is stopped."""
    coins = db.query(PortfolioCoin).filter(PortfolioCoin.portfolio_id == portfolio_id).all()
    for c in coins:
        c.entry_price = 0.0
        c.tp_price = 0.0
        c.tp1_price = 0.0
        c.tp2_price = 0.0
        c.tp3_price = 0.0
        c.current_sl_price = 0.0
        c.tp_order_id = None
        c.tp1_order_id = None
        c.tp2_order_id = None
        c.tp3_order_id = None
        c.position_status = "idle"
        c.amount = 0.0
        c.remaining_amount = 0.0
        c.original_sl_price = 0.0
        c.reentry_price = 0.0
        c.reentry_used = False
        c.reentry_touched = False
    db.commit()


def get_open_positions(db, telegram_id: int = None):
    """Return all coins with open positions (for the monitor job)."""
    q = db.query(PortfolioCoin).join(Portfolio).filter(
        PortfolioCoin.position_status.in_(["open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit", "waiting_reentry"]),
        Portfolio.is_running == True,
        Portfolio.status == "active",
    )
    if telegram_id:
        q = q.filter(Portfolio.telegram_id == telegram_id)
    return q.all()


def log_action(db, telegram_id: int, action: str, details: str, success: bool = True, portfolio_id: int = None):
    log = RebalanceLog(
        telegram_id=telegram_id,
        portfolio_id=portfolio_id,
        action=action,
        details=details,
        success=success
    )
    db.add(log)
    db.commit()


# -------------------- Signal helpers --------------------

def get_signal_sources(db, telegram_id: int):
    return db.query(SignalSource).filter(SignalSource.telegram_id == telegram_id).order_by(SignalSource.id).all()


def get_signal_source(db, source_id: int, telegram_id: int = None):
    q = db.query(SignalSource).filter(SignalSource.id == source_id)
    if telegram_id:
        q = q.filter(SignalSource.telegram_id == telegram_id)
    return q.first()


def create_signal_source(db, telegram_id: int, name: str, **kwargs) -> SignalSource:
    src = SignalSource(telegram_id=telegram_id, name=name.strip(), **kwargs)
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


def update_signal_source(db, source_id: int, **kwargs):
    src = db.query(SignalSource).filter(SignalSource.id == source_id).first()
    if not src:
        return None
    for k, v in kwargs.items():
        if hasattr(src, k):
            setattr(src, k, v)
    src.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(src)
    return src


def delete_signal_source(db, source_id: int, telegram_id: int):
    src = get_signal_source(db, source_id, telegram_id)
    if src:
        db.delete(src)
        db.commit()
        return True
    return False


def log_signal(db, telegram_id: int, action: str, reason: str = "", raw_text: str = "",
               source_id: int = None, executed: bool = False, result_msg: str = ""):
    entry = SignalLog(
        telegram_id=telegram_id,
        source_id=source_id,
        action=action.upper(),
        reason=reason[:400],
        raw_text=raw_text[:2000] if raw_text else None,
        executed=executed,
        result_msg=result_msg[:500],
    )
    db.add(entry)
    db.commit()
    return entry


def parse_portfolio_ids(ids_str: str) -> list:
    """يقبل: 21,22  أو  #21,#22  أو  21#,22#"""
    if not ids_str:
        return []
    import re
    return [int(x) for x in re.findall(r"\d+", ids_str)]
