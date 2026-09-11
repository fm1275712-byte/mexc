from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, BigInteger, ForeignKey
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
    max_coins_per_portfolio = Column(Integer, default=10)
    min_usdt_per_coin = Column(Float, default=5.0)
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


class SignalSettings(Base):
    __tablename__ = "signal_settings"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    enabled = Column(Boolean, default=False)
    sell_threshold_m = Column(Float, default=1.0)
    buy_threshold_m = Column(Float, default=1.0)
    sell_keywords = Column(String(2000), default="sell,transfer to exchange")
    buy_keywords = Column(String(2000), default="buy,withdrawal from exchange")
    last_signal_at = Column(DateTime, nullable=True)
    last_signal_action = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SignalBot(Base):
    __tablename__ = "signal_bots"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    bot_username = Column(String(255), nullable=True)
    bot_id = Column(BigInteger, nullable=True)
    label = Column(String(255), default="")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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
                print("[migration] Added portfolios.is_running")
            if "started_at" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN started_at TIMESTAMP"))
            if "stopped_at" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN stopped_at TIMESTAMP"))
            if "base_investment" not in cols:
                conn.execute(text("ALTER TABLE portfolios ADD COLUMN base_investment DOUBLE PRECISION DEFAULT 0"))
                print("[migration] Added portfolios.base_investment")


def get_or_create_user(db, telegram_id: int):
    user = db.query(UserSettings).filter(UserSettings.telegram_id == telegram_id).first()
    if not user:
        user = UserSettings(telegram_id=telegram_id)
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


def add_coin_to_portfolio(db, portfolio_id: int, symbol: str, max_coins: int = 10):
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


def get_signal_settings(db, telegram_id: int):
    row = db.query(SignalSettings).filter(SignalSettings.telegram_id == telegram_id).first()
    if not row:
        row = SignalSettings(telegram_id=telegram_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def list_signal_bots(db, telegram_id: int):
    return db.query(SignalBot).filter(
        SignalBot.telegram_id == telegram_id
    ).order_by(SignalBot.created_at.asc()).all()


def add_signal_bot(db, telegram_id: int, bot_username: str = None,
                   bot_id: int = None, label: str = ""):
    row = SignalBot(
        telegram_id=telegram_id,
        bot_username=bot_username.lstrip("@").strip() if bot_username else None,
        bot_id=bot_id,
        label=label or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def remove_signal_bot(db, telegram_id: int, bot_row_id: int) -> bool:
    row = db.query(SignalBot).filter(
        SignalBot.id == bot_row_id,
        SignalBot.telegram_id == telegram_id,
    ).first()
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True
