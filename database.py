from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)

    # Rebalance mode: "threshold" (نسبي %) or "time" (بالوقت)
    rebalance_mode = Column(String(20), default="threshold")

    # For threshold mode
    threshold = Column(Float, default=2.0)          # % deviation

    # For time mode
    rebalance_interval_hours = Column(Integer, default=24)

    # Allocation method: "equal" or "marketcap"
    allocation_method = Column(String(20), default="equal")

    min_trade_usdt = Column(Float, default=5.0)
    max_coins = Column(Integer, default=10)

    auto_rebalance = Column(Boolean, default=False)
    last_rebalance = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Allocation(Base):
    """Stores selected coins only (percent calculated later)"""
    __tablename__ = "allocations"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    symbol = Column(String(20), nullable=False)    # e.g. BTC, ETH
    created_at = Column(DateTime, default=datetime.utcnow)


class RebalanceLog(Base):
    __tablename__ = "rebalance_logs"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, index=True)
    action = Column(String(50))
    details = Column(Text)
    success = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_or_create_user(db, telegram_id: int):
    user = db.query(UserSettings).filter(UserSettings.telegram_id == telegram_id).first()
    if not user:
        user = UserSettings(telegram_id=telegram_id)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_selected_coins(db, telegram_id: int):
    return db.query(Allocation).filter(Allocation.telegram_id == telegram_id).all()


def set_selected_coins(db, telegram_id: int, symbols: list):
    """symbols = ['BTC', 'ETH', 'ONDO']"""
    db.query(Allocation).filter(Allocation.telegram_id == telegram_id).delete()
    for symbol in symbols:
        alloc = Allocation(telegram_id=telegram_id, symbol=symbol.upper())
        db.add(alloc)
    db.commit()


def add_coin(db, telegram_id: int, symbol: str, max_coins: int = 10) -> tuple:
    symbol = symbol.upper()
    existing = get_selected_coins(db, telegram_id)
    symbols = [a.symbol for a in existing]

    if symbol in symbols:
        return False, f"`{symbol}` موجودة بالفعل."

    if len(symbols) >= max_coins:
        return False, f"وصلت للحد الأقصى ({max_coins} عملات)."

    alloc = Allocation(telegram_id=telegram_id, symbol=symbol)
    db.add(alloc)
    db.commit()
    return True, f"✅ تم إضافة `{symbol}`"


def remove_coin(db, telegram_id: int, symbol: str) -> bool:
    symbol = symbol.upper()
    deleted = db.query(Allocation).filter(
        Allocation.telegram_id == telegram_id,
        Allocation.symbol == symbol
    ).delete()
    db.commit()
    return deleted > 0


def log_action(db, telegram_id: int, action: str, details: str, success: bool = True):
    log = RebalanceLog(telegram_id=telegram_id, action=action, details=details, success=success)
    db.add(log)
    db.commit()
