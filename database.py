from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, BigInteger, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    discord_id = Column(BigInteger, unique=True, index=True, nullable=False)
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
    discord_id = Column(BigInteger, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    investment_usdt = Column(Float, default=0.0)
    status = Column(String(20), default="active")

    allocation_method = Column(String(20), default="equal")
    rebalance_mode = Column(String(20), default="threshold")
    threshold = Column(Float, default=2.0)
    rebalance_interval_hours = Column(Integer, default=24)

    last_rebalance = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    coins = relationship("PortfolioCoin", back_populates="portfolio", cascade="all, delete-orphan")


class PortfolioCoin(Base):
    __tablename__ = "portfolio_coins"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    portfolio = relationship("Portfolio", back_populates="coins")


class RebalanceLog(Base):
    __tablename__ = "rebalance_logs"

    id = Column(Integer, primary_key=True, index=True)
    discord_id = Column(BigInteger, index=True)
    portfolio_id = Column(Integer, nullable=True)
    action = Column(String(50))
    details = Column(Text)
    success = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    """Create tables if they don't exist + auto-migrate old telegram_id columns."""
    Base.metadata.create_all(bind=engine)

    # Auto-fix: rename telegram_id → discord_id if the old column still exists
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in ("user_settings", "portfolios", "rebalance_logs"):
            if table not in insp.get_table_names():
                continue
            cols = [c["name"] for c in insp.get_columns(table)]
            if "telegram_id" in cols and "discord_id" not in cols:
                conn.execute(text(f'ALTER TABLE {table} RENAME COLUMN telegram_id TO discord_id'))
                print(f"[migration] Renamed {table}.telegram_id → discord_id")
            elif "telegram_id" in cols and "discord_id" in cols:
                # both exist → drop the old one
                conn.execute(text(f'ALTER TABLE {table} DROP COLUMN telegram_id'))
                print(f"[migration] Dropped leftover {table}.telegram_id")


def get_or_create_user(db, discord_id: int):
    user = db.query(UserSettings).filter(UserSettings.discord_id == discord_id).first()
    if not user:
        user = UserSettings(discord_id=discord_id)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_portfolios(db, discord_id: int, status: str = "active"):
    q = db.query(Portfolio).filter(Portfolio.discord_id == discord_id)
    if status:
        q = q.filter(Portfolio.status == status)
    return q.order_by(Portfolio.created_at.desc()).all()


def get_portfolio(db, portfolio_id: int, discord_id: int = None):
    q = db.query(Portfolio).filter(Portfolio.id == portfolio_id)
    if discord_id:
        q = q.filter(Portfolio.discord_id == discord_id)
    return q.first()


def create_portfolio(db, discord_id: int, name: str, investment: float, coins: list,
                     allocation_method: str = "equal", rebalance_mode: str = "threshold",
                     threshold: float = 2.0, interval: int = 24) -> Portfolio:
    p = Portfolio(
        discord_id=discord_id,
        name=name,
        investment_usdt=investment,
        allocation_method=allocation_method,
        rebalance_mode=rebalance_mode,
        threshold=threshold,
        rebalance_interval_hours=interval,
        status="active"
    )
    db.add(p)
    db.flush()
    for sym in coins:
        db.add(PortfolioCoin(portfolio_id=p.id, symbol=sym.upper()))
    db.commit()
    db.refresh(p)
    return p


def add_coin_to_portfolio(db, portfolio_id: int, symbol: str, max_coins: int = 10) -> tuple:
    p = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
    if not p:
        return False, f"`{symbol}` المحفظة غير موجودة"
    symbols = [c.symbol for c in p.coins]
    symbol = symbol.upper()
    if symbol in symbols:
        return False, f"`{symbol}` موجودة بالفعل"
    if len(symbols) >= max_coins:
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
        p.closed_at = datetime.utcnow()
        db.commit()


def log_action(db, discord_id: int, action: str, details: str, success: bool = True, portfolio_id: int = None):
    log = RebalanceLog(
        discord_id=discord_id,
        portfolio_id=portfolio_id,
        action=action,
        details=details,
        success=success
    )
    db.add(log)
    db.commit()
