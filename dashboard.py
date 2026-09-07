"""
MEXC Portfolio Manager — Web Dashboard
لوحة تحكم ويب لإدارة محافظ MEXC Spot (بدون تليجرام).
"""
import os
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import config
from database import (
    init_db, SessionLocal, get_or_create_user, get_portfolios, get_portfolio,
    create_portfolio, add_coin_to_portfolio, remove_coin_from_portfolio,
    close_portfolio, set_portfolio_running, log_action,
    get_signal_settings, list_signal_bots, add_signal_bot, remove_signal_bot,
)
from mexc_client import MexcClient
from rebalancer import Rebalancer
from signal_parser import evaluate_signal

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET") or getattr(config, "DASHBOARD_SECRET", None) or "change-me"
ADMIN_ID = int(getattr(config, "ADMIN_USER_ID", 1) or 1)

app = FastAPI(title="MEXC Portfolio Dashboard", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def require_auth(authorization: Optional[str] = Header(None), x_dashboard_key: Optional[str] = Header(None)):
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token and x_dashboard_key:
        token = x_dashboard_key.strip()
    if not token or not secrets.compare_digest(token, DASHBOARD_SECRET):
        raise HTTPException(status_code=401, detail="غير مصرح — أدخل مفتاح الداشبورد")
    return True


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class CreatePortfolioIn(BaseModel):
    name: str
    investment_usdt: float = Field(gt=0)
    coins: List[str] = []
    allocation_method: str = "equal"
    threshold: float = 2.0


class AddCoinIn(BaseModel):
    symbol: str


class AmountIn(BaseModel):
    amount: float = Field(gt=0)


class AllocIn(BaseModel):
    investment_usdt: float = Field(ge=5)


class SignalThreshIn(BaseModel):
    sell_threshold_m: float = Field(gt=0)
    buy_threshold_m: float = Field(gt=0)


class SignalBotIn(BaseModel):
    bot_username: Optional[str] = None
    bot_id: Optional[int] = None
    label: str = ""


class SignalTestIn(BaseModel):
    text: str


class SettingsIn(BaseModel):
    default_threshold: Optional[float] = None
    default_allocation_method: Optional[str] = None
    default_rebalance_mode: Optional[str] = None
    min_trade_usdt: Optional[float] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pf_dict(p, include_value: bool = True) -> dict:
    coins = [c.symbol for c in p.coins]
    d = {
        "id": p.id,
        "name": p.name,
        "investment_usdt": p.investment_usdt,
        "is_running": p.is_running,
        "status": p.status,
        "allocation_method": p.allocation_method,
        "threshold": p.threshold,
        "coins": coins,
        "started_at": p.started_at.isoformat() if p.started_at else None,
        "stopped_at": p.stopped_at.isoformat() if p.stopped_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
    if include_value and coins:
        try:
            val = get_mexc().get_coins_value(coins)
            d["current_value"] = val["total_usdt"]
            d["assets"] = val.get("assets", {})
            targets = get_reb().calculate_targets(coins, p.allocation_method)
            d["targets"] = targets
        except Exception as e:
            d["current_value"] = None
            d["value_error"] = str(e)
    else:
        d["current_value"] = 0
    return d


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    init_db()


@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}


@app.get("/api/balance")
def api_balance(_: bool = Depends(require_auth)):
    try:
        data = get_mexc().get_portfolio_value()
        free = get_mexc().get_free_usdt()
        return {"total_usdt": data["total_usdt"], "free_usdt": free, "assets": data["assets"]}
    except Exception as e:
        raise HTTPException(500, f"فشل جلب الرصيد: {e}")


@app.get("/api/portfolios")
def api_portfolios(_: bool = Depends(require_auth)):
    if not ADMIN_ID:
        raise HTTPException(400, "ADMIN_USER_ID غير مضبوط")
    db = SessionLocal()
    try:
        get_or_create_user(db, ADMIN_ID)
        pfs = get_portfolios(db, ADMIN_ID, status="active")
        return {"portfolios": [_pf_dict(p) for p in pfs]}
    finally:
        db.close()


@app.get("/api/portfolios/{pf_id}")
def api_portfolio(pf_id: int, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "المحفظة غير موجودة")
        return _pf_dict(p)
    finally:
        db.close()


@app.post("/api/portfolios")
def api_create_portfolio(body: CreatePortfolioIn, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        user = get_or_create_user(db, ADMIN_ID)
        coins = [c.strip().upper() for c in body.coins if c.strip()]
        for c in coins:
            pair = f"{c}/USDT"
            try:
                markets = get_mexc().exchange.load_markets()
                m = markets.get(pair)
                if not (m and m.get("active", True) and m.get("spot", True)):
                    raise HTTPException(400, f"{c} غير متاحة على MEXC Spot")
            except HTTPException:
                raise
            except Exception:
                pass
        p = create_portfolio(
            db, ADMIN_ID, body.name, body.investment_usdt, coins,
            allocation_method=body.allocation_method, threshold=body.threshold,
        )
        log_action(db, ADMIN_ID, "create", body.name, True, p.id)
        return _pf_dict(p)
    finally:
        db.close()


@app.post("/api/portfolios/{pf_id}/start")
def api_start(pf_id: int, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "غير موجودة")
        coins = [c.symbol for c in p.coins]
        if not coins:
            raise HTTPException(400, "المحفظة بدون عملات")
        if p.is_running:
            raise HTTPException(400, "المحفظة شغالة بالفعل — استخدم زيادة الاستثمار")
        result = get_reb().start_portfolio(
            coins=coins, total_usdt=p.investment_usdt,
            method=p.allocation_method, min_trade_usdt=5.0, dry_run=False,
        )
        if result.get("errors") and not result.get("executed"):
            raise HTTPException(400, str(result["errors"]))
        set_portfolio_running(db, p.id, True)
        log_action(db, ADMIN_ID, "start", str(result.get("executed")), True, p.id)
        return {"ok": True, "result": result, "portfolio": _pf_dict(get_portfolio(db, pf_id, ADMIN_ID))}
    finally:
        db.close()


@app.post("/api/portfolios/{pf_id}/stop")
def api_stop(pf_id: int, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "غير موجودة")
        coins = [c.symbol for c in p.coins]
        result = get_reb().stop_portfolio(coins, dry_run=False)
        set_portfolio_running(db, p.id, False)
        log_action(db, ADMIN_ID, "stop", str(result.get("executed")), True, p.id)
        return {"ok": True, "result": result, "portfolio": _pf_dict(get_portfolio(db, pf_id, ADMIN_ID))}
    finally:
        db.close()


@app.post("/api/portfolios/{pf_id}/increase")
def api_increase(pf_id: int, body: AmountIn, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "غير موجودة")
        p.investment_usdt += body.amount
        db.commit()
        buy_result = None
        if p.is_running:
            coins = [c.symbol for c in p.coins]
            buy_result = get_reb().start_portfolio(
                coins=coins, total_usdt=body.amount,
                method=p.allocation_method, min_trade_usdt=5.0, dry_run=False,
            )
        log_action(db, ADMIN_ID, "increase", str(body.amount), True, p.id)
        return {"ok": True, "buy": buy_result, "portfolio": _pf_dict(get_portfolio(db, pf_id, ADMIN_ID))}
    finally:
        db.close()


@app.post("/api/portfolios/{pf_id}/allocation")
def api_alloc(pf_id: int, body: AllocIn, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "غير موجودة")
        p.investment_usdt = body.investment_usdt
        db.commit()
        return {"ok": True, "portfolio": _pf_dict(get_portfolio(db, pf_id, ADMIN_ID))}
    finally:
        db.close()


@app.post("/api/portfolios/{pf_id}/rebalance")
def api_rebalance(pf_id: int, dry_run: bool = True, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "غير موجودة")
        if not p.is_running:
            raise HTTPException(400, "المحفظة متوقفة — شغّلها أولاً")
        coins = [c.symbol for c in p.coins]
        result = get_reb().rebalance_portfolio(
            coins=coins, target_capital=p.investment_usdt,
            method=p.allocation_method, threshold=p.threshold,
            min_trade_usdt=5.0, dry_run=dry_run,
        )
        if not dry_run and result.get("executed"):
            p.last_rebalance = datetime.utcnow()
            db.commit()
            log_action(db, ADMIN_ID, "rebalance", str(result.get("executed")), True, p.id)
        return {"ok": True, "dry_run": dry_run, "result": result, "portfolio": _pf_dict(get_portfolio(db, pf_id, ADMIN_ID))}
    finally:
        db.close()


@app.post("/api/portfolios/{pf_id}/coins")
def api_add_coin(pf_id: int, body: AddCoinIn, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "غير موجودة")
        user = get_or_create_user(db, ADMIN_ID)
        sym = body.symbol.strip().upper()
        ok, msg = add_coin_to_portfolio(db, pf_id, sym, max_coins=user.max_coins_per_portfolio)
        if not ok:
            raise HTTPException(400, msg)
        buy_info = None
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if p.is_running and p.investment_usdt > 0:
            n = len(p.coins) or 1
            usdt_for_new = p.investment_usdt / n
            try:
                buy_info = get_reb().start_portfolio(
                    coins=[sym], total_usdt=usdt_for_new,
                    method="equal", min_trade_usdt=5.0, dry_run=False,
                )
            except Exception as e:
                buy_info = {"errors": [str(e)]}
        log_action(db, ADMIN_ID, "add_coin", sym, True, pf_id)
        return {"ok": True, "message": msg, "buy": buy_info, "portfolio": _pf_dict(get_portfolio(db, pf_id, ADMIN_ID))}
    finally:
        db.close()


@app.delete("/api/portfolios/{pf_id}/coins/{symbol}")
def api_remove_coin(pf_id: int, symbol: str, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "غير موجودة")
        sell_info = None
        try:
            sell_info = get_reb().stop_portfolio([symbol.upper()], dry_run=False)
        except Exception as e:
            sell_info = {"errors": [str(e)]}
        remove_coin_from_portfolio(db, pf_id, symbol)
        log_action(db, ADMIN_ID, "remove_coin", symbol, True, pf_id)
        return {"ok": True, "sell": sell_info, "portfolio": _pf_dict(get_portfolio(db, pf_id, ADMIN_ID))}
    finally:
        db.close()


@app.post("/api/portfolios/{pf_id}/close")
def api_close(pf_id: int, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, ADMIN_ID)
        if not p:
            raise HTTPException(404, "غير موجودة")
        name = p.name
        result = None
        if p.is_running:
            coins = [c.symbol for c in p.coins]
            result = get_reb().stop_portfolio(coins, dry_run=False)
        close_portfolio(db, p.id)
        log_action(db, ADMIN_ID, "close", name, True, pf_id)
        return {"ok": True, "sold": result}
    finally:
        db.close()


# ---- Signals ----
@app.get("/api/signals")
def api_signals(_: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        s = get_signal_settings(db, ADMIN_ID)
        bots = list_signal_bots(db, ADMIN_ID)
        return {
            "enabled": s.enabled,
            "sell_threshold_m": s.sell_threshold_m,
            "buy_threshold_m": s.buy_threshold_m,
            "sell_keywords": s.sell_keywords,
            "buy_keywords": s.buy_keywords,
            "last_signal_at": s.last_signal_at.isoformat() if s.last_signal_at else None,
            "last_signal_action": s.last_signal_action,
            "bots": [
                {"id": b.id, "bot_id": b.bot_id, "bot_username": b.bot_username,
                 "label": b.label, "enabled": b.enabled}
                for b in bots
            ],
        }
    finally:
        db.close()


@app.post("/api/signals/toggle")
def api_sig_toggle(_: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        s = get_signal_settings(db, ADMIN_ID)
        s.enabled = not s.enabled
        db.commit()
        return {"enabled": s.enabled}
    finally:
        db.close()


@app.post("/api/signals/thresholds")
def api_sig_thresh(body: SignalThreshIn, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        s = get_signal_settings(db, ADMIN_ID)
        s.sell_threshold_m = body.sell_threshold_m
        s.buy_threshold_m = body.buy_threshold_m
        db.commit()
        return {"ok": True, "sell_threshold_m": s.sell_threshold_m, "buy_threshold_m": s.buy_threshold_m}
    finally:
        db.close()


@app.post("/api/signals/bots")
def api_sig_bot_add(body: SignalBotIn, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        row = add_signal_bot(db, ADMIN_ID, body.bot_username, body.bot_id, body.label)
        return {"id": row.id, "bot_username": row.bot_username, "bot_id": row.bot_id, "label": row.label}
    finally:
        db.close()


@app.delete("/api/signals/bots/{bot_row_id}")
def api_sig_bot_del(bot_row_id: int, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        ok = remove_signal_bot(db, ADMIN_ID, bot_row_id)
        if not ok:
            raise HTTPException(404, "غير موجود")
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/signals/test")
def api_sig_test(body: SignalTestIn, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        s = get_signal_settings(db, ADMIN_ID)
        action, amount, reason = evaluate_signal(
            body.text, s.sell_threshold_m, s.buy_threshold_m,
            s.sell_keywords, s.buy_keywords,
        )
        return {"action": action, "amount": amount, "reason": reason, "would_execute": action is not None}
    finally:
        db.close()


@app.get("/api/settings")
def api_settings(_: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        u = get_or_create_user(db, ADMIN_ID)
        return {
            "default_threshold": u.default_threshold,
            "default_allocation_method": u.default_allocation_method,
            "default_rebalance_mode": u.default_rebalance_mode,
            "min_trade_usdt": u.min_trade_usdt,
            "max_coins_per_portfolio": u.max_coins_per_portfolio,
        }
    finally:
        db.close()


@app.post("/api/settings")
def api_settings_update(body: SettingsIn, _: bool = Depends(require_auth)):
    db = SessionLocal()
    try:
        u = get_or_create_user(db, ADMIN_ID)
        if body.default_threshold is not None:
            u.default_threshold = body.default_threshold
        if body.default_allocation_method is not None:
            u.default_allocation_method = body.default_allocation_method
        if body.default_rebalance_mode is not None:
            u.default_rebalance_mode = body.default_rebalance_mode
        if body.min_trade_usdt is not None:
            u.min_trade_usdt = body.min_trade_usdt
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Frontend (single-page dark dashboard)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MEXC Portfolio Dashboard</title>
<style>
:root {
  --bg: #0b0f14;
  --panel: #121821;
  --panel2: #1a2332;
  --border: #243044;
  --text: #e8eef7;
  --muted: #8b9bb4;
  --accent: #3b82f6;
  --green: #22c55e;
  --red: #ef4444;
  --amber: #f59e0b;
  --radius: 14px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: "Segoe UI", Tahoma, system-ui, sans-serif;
  background: radial-gradient(1200px 600px at 80% -10%, #1a2740 0%, var(--bg) 55%);
  color: var(--text);
  min-height: 100vh;
}
.hidden { display: none !important; }
#login {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
}
.login-card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 20px;
  padding: 2rem; width: min(400px, 92vw); box-shadow: 0 20px 60px rgba(0,0,0,.4);
}
.login-card h1 { font-size: 1.35rem; margin-bottom: .4rem; }
.login-card p { color: var(--muted); font-size: .9rem; margin-bottom: 1.2rem; }
input, select, textarea {
  width: 100%; background: var(--panel2); border: 1px solid var(--border);
  color: var(--text); border-radius: 10px; padding: .7rem .9rem; font-size: .95rem;
}
input:focus, select:focus, textarea:focus { outline: 2px solid var(--accent); border-color: transparent; }
.btn {
  border: none; border-radius: 10px; padding: .65rem 1rem; font-weight: 600;
  cursor: pointer; font-size: .9rem; transition: .15s ease;
}
.btn:hover { filter: brightness(1.08); transform: translateY(-1px); }
.btn-primary { background: var(--accent); color: #fff; }
.btn-green { background: var(--green); color: #04120a; }
.btn-red { background: var(--red); color: #fff; }
.btn-amber { background: var(--amber); color: #1a1200; }
.btn-ghost { background: var(--panel2); color: var(--text); border: 1px solid var(--border); }
.btn-sm { padding: .4rem .7rem; font-size: .8rem; }
header.app {
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  padding: 1rem 1.4rem; border-bottom: 1px solid var(--border);
  background: rgba(18,24,33,.85); backdrop-filter: blur(10px); position: sticky; top: 0; z-index: 20;
}
header .brand { display: flex; align-items: center; gap: .7rem; font-weight: 700; }
header .brand span { width: 10px; height: 10px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); }
nav.tabs { display: flex; gap: .4rem; flex-wrap: wrap; }
nav.tabs button {
  background: transparent; border: 1px solid transparent; color: var(--muted);
  padding: .45rem .9rem; border-radius: 999px; cursor: pointer; font-weight: 600;
}
nav.tabs button.active { background: var(--panel2); color: var(--text); border-color: var(--border); }
main { padding: 1.2rem 1.4rem 3rem; max-width: 1200px; margin: 0 auto; }
.grid { display: grid; gap: 1rem; }
.grid-2 { grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.grid-3 { grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 1.1rem 1.2rem;
}
.card h3 { font-size: 1rem; margin-bottom: .8rem; }
.stat { font-size: 1.6rem; font-weight: 700; }
.stat small { font-size: .85rem; color: var(--muted); font-weight: 500; }
.muted { color: var(--muted); }
.badge {
  display: inline-flex; align-items: center; gap: .3rem;
  padding: .2rem .55rem; border-radius: 999px; font-size: .75rem; font-weight: 700;
}
.badge-on { background: rgba(34,197,94,.15); color: var(--green); }
.badge-off { background: rgba(139,155,180,.15); color: var(--muted); }
.pf {
  border: 1px solid var(--border); border-radius: 12px; padding: 1rem;
  background: var(--panel2); cursor: pointer; transition: .15s;
}
.pf:hover { border-color: var(--accent); }
.pf-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: .5rem; }
.pf-name { font-weight: 700; }
.actions { display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .9rem; }
.row { display: flex; gap: .6rem; flex-wrap: wrap; align-items: center; }
.table { width: 100%; border-collapse: collapse; font-size: .88rem; }
.table th, .table td { text-align: right; padding: .55rem .4rem; border-bottom: 1px solid var(--border); }
.table th { color: var(--muted); font-weight: 600; }
.toast {
  position: fixed; bottom: 1.2rem; left: 50%; transform: translateX(-50%);
  background: var(--panel2); border: 1px solid var(--border); color: var(--text);
  padding: .7rem 1.2rem; border-radius: 12px; z-index: 50; box-shadow: 0 10px 40px rgba(0,0,0,.4);
  max-width: 90vw;
}
.modal-bg {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); display: flex;
  align-items: center; justify-content: center; z-index: 40; padding: 1rem;
}
.modal {
  background: var(--panel); border: 1px solid var(--border); border-radius: 16px;
  padding: 1.3rem; width: min(480px, 100%); max-height: 90vh; overflow: auto;
}
.modal h3 { margin-bottom: .8rem; }
.field { margin-bottom: .8rem; }
.field label { display: block; font-size: .8rem; color: var(--muted); margin-bottom: .3rem; }
.err { color: var(--red); font-size: .85rem; margin-top: .4rem; }
pre.log {
  background: #0a0e14; border: 1px solid var(--border); border-radius: 10px;
  padding: .8rem; font-size: .8rem; overflow: auto; max-height: 220px; direction: ltr; text-align: left;
}
</style>
</head>
<body>

<div id="login">
  <div class="login-card">
    <h1>🔐 MEXC Dashboard</h1>
    <p>أدخل مفتاح الداشبورد (DASHBOARD_SECRET)</p>
    <div class="field"><input id="keyInput" type="password" placeholder="المفتاح السري" /></div>
    <button class="btn btn-primary" style="width:100%" onclick="doLogin()">دخول</button>
    <div id="loginErr" class="err hidden"></div>
  </div>
</div>

<div id="app" class="hidden">
  <header class="app">
    <div class="brand"><span></span> MEXC Spot Control</div>
    <nav class="tabs">
      <button class="active" data-tab="home" onclick="showTab('home')">الرئيسية</button>
      <button data-tab="portfolios" onclick="showTab('portfolios')">المحافظ</button>
      <button data-tab="signals" onclick="showTab('signals')">الإشارات</button>
      <button data-tab="settings" onclick="showTab('settings')">الإعدادات</button>
    </nav>
    <button class="btn btn-ghost btn-sm" onclick="logout()">خروج</button>
  </header>

  <main>
    <!-- HOME -->
    <section id="tab-home">
      <div class="grid grid-3" style="margin-bottom:1rem">
        <div class="card"><div class="muted">رصيد حر USDT</div><div class="stat" id="statFree">—</div></div>
        <div class="card"><div class="muted">إجمالي المحفظة</div><div class="stat" id="statTotal">—</div></div>
        <div class="card"><div class="muted">محافظ نشطة</div><div class="stat" id="statPfs">—</div></div>
      </div>
      <div class="card">
        <h3>أصول الحساب</h3>
        <div id="assetsTable" class="muted">جاري التحميل...</div>
      </div>
    </section>

    <!-- PORTFOLIOS -->
    <section id="tab-portfolios" class="hidden">
      <div class="row" style="margin-bottom:1rem; justify-content:space-between">
        <h2>المحافظ</h2>
        <button class="btn btn-primary" onclick="openCreatePf()">✨ إنشاء محفظة</button>
      </div>
      <div id="pfList" class="grid grid-2"></div>
      <div id="pfDetail" class="card hidden" style="margin-top:1rem"></div>
    </section>

    <!-- SIGNALS -->
    <section id="tab-signals" class="hidden">
      <div class="card" style="margin-bottom:1rem">
        <div class="row" style="justify-content:space-between">
          <h3>نظام الإشارات</h3>
          <button class="btn btn-sm" id="sigToggleBtn" onclick="toggleSignals()">—</button>
        </div>
        <div class="muted" id="sigMeta" style="margin-top:.5rem"></div>
      </div>
      <div class="grid grid-2">
        <div class="card">
          <h3>حدود المليون (M)</h3>
          <div class="field"><label>حد البيع</label><input id="sellM" type="number" step="0.5"/></div>
          <div class="field"><label>حد الشراء</label><input id="buyM" type="number" step="0.5"/></div>
          <button class="btn btn-primary" onclick="saveThresh()">حفظ</button>
        </div>
        <div class="card">
          <h3>بوتات / قنوات الإشارة</h3>
          <div id="botList" class="muted" style="margin-bottom:.8rem"></div>
          <div class="field"><input id="newBot" placeholder="@username أو آيدي"/></div>
          <button class="btn btn-primary btn-sm" onclick="addBot()">إضافة</button>
        </div>
      </div>
      <div class="card" style="margin-top:1rem">
        <h3>🧪 اختبار رسالة إشارة</h3>
        <textarea id="testText" rows="5" placeholder="الصق رسالة الإشارة هنا..."></textarea>
        <div class="row" style="margin-top:.6rem">
          <button class="btn btn-amber" onclick="testSignal()">تحليل</button>
        </div>
        <pre class="log" id="testOut" style="margin-top:.6rem"></pre>
      </div>
    </section>

    <!-- SETTINGS -->
    <section id="tab-settings" class="hidden">
      <div class="card">
        <h3>إعدادات عامة</h3>
        <div class="field"><label>Threshold %</label><input id="setThresh" type="number" step="0.1"/></div>
        <div class="field"><label>طريقة التوزيع</label>
          <select id="setMethod"><option value="equal">بالتساوي</option></select>
        </div>
        <div class="field"><label>الحد الأدنى للصفقة USDT</label><input id="setMinTrade" type="number" step="1"/></div>
        <button class="btn btn-primary" onclick="saveSettings()">حفظ</button>
      </div>
    </section>
  </main>
</div>

<div id="modal" class="modal-bg hidden">
  <div class="modal" id="modalBody"></div>
</div>
<div id="toast" class="toast hidden"></div>

<script>
const API = '';
let KEY = localStorage.getItem('mexc_dash_key') || '';
let portfolios = [];
let selectedPf = null;

async function api(path, opts = {}) {
  const headers = Object.assign({'Content-Type': 'application/json', 'X-Dashboard-Key': KEY}, opts.headers || {});
  const res = await fetch(API + path, Object.assign({}, opts, { headers }));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.message || res.statusText);
  return data;
}

function toast(msg, ms=3200) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.remove('hidden');
  setTimeout(() => t.classList.add('hidden'), ms);
}

function doLogin() {
  KEY = document.getElementById('keyInput').value.trim();
  if (!KEY) return;
  api('/api/health').then(() => api('/api/balance')).then(() => {
    localStorage.setItem('mexc_dash_key', KEY);
    document.getElementById('login').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
    refreshAll();
  }).catch(e => {
    document.getElementById('loginErr').textContent = e.message || 'مفتاح خاطئ';
    document.getElementById('loginErr').classList.remove('hidden');
  });
}

function logout() {
  localStorage.removeItem('mexc_dash_key'); KEY = '';
  document.getElementById('app').classList.add('hidden');
  document.getElementById('login').classList.remove('hidden');
}

if (KEY) {
  api('/api/balance').then(() => {
    document.getElementById('login').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
    refreshAll();
  }).catch(() => { KEY=''; localStorage.removeItem('mexc_dash_key'); });
}

document.getElementById('keyInput').addEventListener('keydown', e => { if (e.key==='Enter') doLogin(); });

function showTab(name) {
  document.querySelectorAll('main > section').forEach(s => s.classList.add('hidden'));
  document.getElementById('tab-' + name).classList.remove('hidden');
  document.querySelectorAll('nav.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  if (name === 'portfolios') loadPortfolios();
  if (name === 'signals') loadSignals();
  if (name === 'settings') loadSettings();
  if (name === 'home') loadHome();
}

async function refreshAll() { await loadHome(); }

async function loadHome() {
  try {
    const bal = await api('/api/balance');
    document.getElementById('statFree').innerHTML = bal.free_usdt.toFixed(2) + ' <small>USDT</small>';
    document.getElementById('statTotal').innerHTML = bal.total_usdt.toFixed(2) + ' <small>USDT</small>';
    const pfs = await api('/api/portfolios');
    portfolios = pfs.portfolios || [];
    document.getElementById('statPfs').textContent = portfolios.length;
    const assets = Object.entries(bal.assets || {}).sort((a,b) => b[1].usdt_value - a[1].usdt_value);
    if (!assets.length) {
      document.getElementById('assetsTable').textContent = 'لا توجد أصول';
    } else {
      let html = '<table class="table"><tr><th>الأصل</th><th>الكمية</th><th>القيمة</th><th>%</th></tr>';
      for (const [k,v] of assets.slice(0,20)) {
        html += `<tr><td>${k}</td><td>${Number(v.amount).toPrecision(6)}</td><td>${v.usdt_value.toFixed(2)}$</td><td>${v.percent.toFixed(1)}%</td></tr>`;
      }
      html += '</table>';
      document.getElementById('assetsTable').innerHTML = html;
    }
  } catch (e) { toast('خطأ: ' + e.message); }
}

async function loadPortfolios() {
  try {
    const data = await api('/api/portfolios');
    portfolios = data.portfolios || [];
    const el = document.getElementById('pfList');
    if (!portfolios.length) { el.innerHTML = '<div class="muted">لا توجد محافظ. أنشئ واحدة.</div>'; return; }
    el.innerHTML = portfolios.map(p => `
      <div class="pf" onclick="selectPf(${p.id})">
        <div class="pf-head">
          <div class="pf-name">${p.name}</div>
          <span class="badge ${p.is_running?'badge-on':'badge-off'}">${p.is_running?'🟢 شغالة':'⚪ متوقفة'}</span>
        </div>
        <div class="muted">مخصص: ${p.investment_usdt.toFixed(2)} USDT · قيمة: ${(p.current_value||0).toFixed(2)}$</div>
        <div class="muted" style="margin-top:.3rem">${(p.coins||[]).join(' · ') || 'بدون عملات'}</div>
      </div>`).join('');
  } catch (e) { toast(e.message); }
}

async function selectPf(id) {
  try {
    const p = await api('/api/portfolios/' + id);
    selectedPf = p;
    const box = document.getElementById('pfDetail');
    box.classList.remove('hidden');
    const coins = (p.coins||[]).map(c => `<span class="badge badge-off">${c}</span>`).join(' ');
    box.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <h3>${p.name} <span class="badge ${p.is_running?'badge-on':'badge-off'}">${p.is_running?'شغالة':'متوقفة'}</span></h3>
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('pfDetail').classList.add('hidden')">إغلاق</button>
      </div>
      <div class="grid grid-3" style="margin:.8rem 0">
        <div><div class="muted">المخصص</div><div class="stat" style="font-size:1.2rem">${p.investment_usdt.toFixed(2)}$</div></div>
        <div><div class="muted">القيمة الحالية</div><div class="stat" style="font-size:1.2rem">${(p.current_value||0).toFixed(2)}$</div></div>
        <div><div class="muted">Threshold</div><div class="stat" style="font-size:1.2rem">${p.threshold}%</div></div>
      </div>
      <div class="muted" style="margin-bottom:.5rem">العملات</div>
      <div class="row">${coins || '—'}</div>
      <div class="actions">
        <button class="btn btn-green btn-sm" onclick="actStart(${p.id})">▶️ تشغيل</button>
        <button class="btn btn-red btn-sm" onclick="actStop(${p.id})">⏹️ إيقاف وبيع</button>
        <button class="btn btn-primary btn-sm" onclick="actIncrease(${p.id})">💰 زيادة استثمار</button>
        <button class="btn btn-ghost btn-sm" onclick="actAlloc(${p.id})">✏️ تعديل المخصص</button>
        <button class="btn btn-amber btn-sm" onclick="actReb(${p.id}, true)">🔍 معاينة توازن</button>
        <button class="btn btn-amber btn-sm" onclick="actReb(${p.id}, false)">✅ تنفيذ توازن</button>
        <button class="btn btn-ghost btn-sm" onclick="actAddCoin(${p.id})">➕ عملة</button>
        <button class="btn btn-ghost btn-sm" onclick="actRemCoin(${p.id})">🗑️ حذف عملة</button>
        <button class="btn btn-red btn-sm" onclick="actClose(${p.id})">إنهاء المحفظة</button>
      </div>
      <pre class="log" id="pfLog" style="margin-top:.8rem"></pre>`;
  } catch (e) { toast(e.message); }
}

function logPf(obj) {
  const el = document.getElementById('pfLog');
  if (el) el.textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
}

async function actStart(id) {
  try { const r = await api('/api/portfolios/'+id+'/start', {method:'POST'}); toast('تم التشغيل'); logPf(r.result); selectPf(id); loadPortfolios(); }
  catch(e){ toast(e.message); }
}
async function actStop(id) {
  if (!confirm('إيقاف وبيع كل عملات المحفظة؟')) return;
  try { const r = await api('/api/portfolios/'+id+'/stop', {method:'POST'}); toast('تم الإيقاف'); logPf(r.result); selectPf(id); loadPortfolios(); }
  catch(e){ toast(e.message); }
}
async function actIncrease(id) {
  const amount = parseFloat(prompt('المبلغ الإضافي USDT:'));
  if (!amount || amount<=0) return;
  try { const r = await api('/api/portfolios/'+id+'/increase', {method:'POST', body: JSON.stringify({amount})}); toast('تمت الزيادة'); logPf(r); selectPf(id); }
  catch(e){ toast(e.message); }
}
async function actAlloc(id) {
  const investment_usdt = parseFloat(prompt('المخصص الجديد USDT:'));
  if (!investment_usdt || investment_usdt<5) return;
  try { await api('/api/portfolios/'+id+'/allocation', {method:'POST', body: JSON.stringify({investment_usdt})}); toast('تم التعديل'); selectPf(id); }
  catch(e){ toast(e.message); }
}
async function actReb(id, dry) {
  try { const r = await api('/api/portfolios/'+id+'/rebalance?dry_run='+dry, {method:'POST'}); toast(dry?'معاينة':'تم التنفيذ'); logPf(r.result); if(!dry) selectPf(id); }
  catch(e){ toast(e.message); }
}
async function actAddCoin(id) {
  const symbol = prompt('رمز العملة (مثل BTC):');
  if (!symbol) return;
  try { const r = await api('/api/portfolios/'+id+'/coins', {method:'POST', body: JSON.stringify({symbol})}); toast('تمت الإضافة'); logPf(r); selectPf(id); }
  catch(e){ toast(e.message); }
}
async function actRemCoin(id) {
  const symbol = prompt('رمز العملة للحذف (سيتم بيعها فوراً):');
  if (!symbol) return;
  try { const r = await api('/api/portfolios/'+id+'/coins/'+encodeURIComponent(symbol), {method:'DELETE'}); toast('تم الحذف والبيع'); logPf(r); selectPf(id); }
  catch(e){ toast(e.message); }
}
async function actClose(id) {
  if (!confirm('إنهاء نهائي للمحفظة؟ سيتم البيع إن كانت شغالة.')) return;
  try { await api('/api/portfolios/'+id+'/close', {method:'POST'}); toast('تم الإنهاء'); document.getElementById('pfDetail').classList.add('hidden'); loadPortfolios(); }
  catch(e){ toast(e.message); }
}

function openCreatePf() {
  document.getElementById('modal').classList.remove('hidden');
  document.getElementById('modalBody').innerHTML = `
    <h3>إنشاء محفظة</h3>
    <div class="field"><label>الاسم</label><input id="cName"/></div>
    <div class="field"><label>المخصص USDT</label><input id="cInv" type="number" step="1" value="50"/></div>
    <div class="field"><label>العملات (مفصولة بفاصلة)</label><input id="cCoins" placeholder="BTC,ETH,SOL"/></div>
    <div class="row">
      <button class="btn btn-primary" onclick="createPf()">إنشاء</button>
      <button class="btn btn-ghost" onclick="document.getElementById('modal').classList.add('hidden')">إلغاء</button>
    </div>`;
}
async function createPf() {
  const name = document.getElementById('cName').value.trim();
  const investment_usdt = parseFloat(document.getElementById('cInv').value);
  const coins = document.getElementById('cCoins').value.split(/[,\s]+/).filter(Boolean);
  try {
    await api('/api/portfolios', {method:'POST', body: JSON.stringify({name, investment_usdt, coins})});
    document.getElementById('modal').classList.add('hidden');
    toast('تم الإنشاء'); loadPortfolios();
  } catch(e){ toast(e.message); }
}

async function loadSignals() {
  try {
    const s = await api('/api/signals');
    const btn = document.getElementById('sigToggleBtn');
    btn.textContent = s.enabled ? '🟢 مفعّل — اضغط للإيقاف' : '🔴 متوقف — اضغط للتفعيل';
    btn.className = 'btn btn-sm ' + (s.enabled ? 'btn-green' : 'btn-red');
    document.getElementById('sigMeta').textContent =
      (s.last_signal_at ? `آخر إشارة: ${s.last_signal_action} — ${s.last_signal_at}` : 'لا توجد إشارات بعد');
    document.getElementById('sellM').value = s.sell_threshold_m;
    document.getElementById('buyM').value = s.buy_threshold_m;
    const bots = s.bots || [];
    document.getElementById('botList').innerHTML = bots.length
      ? bots.map(b => `<div class="row" style="justify-content:space-between;margin:.3rem 0">
          <span>${b.label || b.bot_username || b.bot_id}</span>
          <button class="btn btn-red btn-sm" onclick="delBot(${b.id})">حذف</button></div>`).join('')
      : 'لا توجد بوتات';
  } catch(e){ toast(e.message); }
}
async function toggleSignals() {
  try { await api('/api/signals/toggle', {method:'POST'}); loadSignals(); } catch(e){ toast(e.message); }
}
async function saveThresh() {
  try {
    await api('/api/signals/thresholds', {method:'POST', body: JSON.stringify({
      sell_threshold_m: parseFloat(document.getElementById('sellM').value),
      buy_threshold_m: parseFloat(document.getElementById('buyM').value),
    })});
    toast('تم الحفظ');
  } catch(e){ toast(e.message); }
}
async function addBot() {
  const raw = document.getElementById('newBot').value.trim();
  if (!raw) return;
  const body = raw.lstrip ? {} : {};
  if (/^-?\d+$/.test(raw)) body.bot_id = parseInt(raw);
  else body.bot_username = raw.replace(/^@/, '');
  try { await api('/api/signals/bots', {method:'POST', body: JSON.stringify(body)}); document.getElementById('newBot').value=''; loadSignals(); }
  catch(e){ toast(e.message); }
}
async function delBot(id) {
  try { await api('/api/signals/bots/'+id, {method:'DELETE'}); loadSignals(); } catch(e){ toast(e.message); }
}
async function testSignal() {
  const text = document.getElementById('testText').value;
  try {
    const r = await api('/api/signals/test', {method:'POST', body: JSON.stringify({text})});
    document.getElementById('testOut').textContent = JSON.stringify(r, null, 2);
  } catch(e){ toast(e.message); }
}

async function loadSettings() {
  try {
    const s = await api('/api/settings');
    document.getElementById('setThresh').value = s.default_threshold;
    document.getElementById('setMethod').value = s.default_allocation_method || 'equal';
    document.getElementById('setMinTrade').value = s.min_trade_usdt;
  } catch(e){ toast(e.message); }
}
async function saveSettings() {
  try {
    await api('/api/settings', {method:'POST', body: JSON.stringify({
      default_threshold: parseFloat(document.getElementById('setThresh').value),
      default_allocation_method: document.getElementById('setMethod').value,
      min_trade_usdt: parseFloat(document.getElementById('setMinTrade').value),
    })});
    toast('تم الحفظ');
  } catch(e){ toast(e.message); }
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(DASHBOARD_HTML)


def main():
    import uvicorn
    port = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8080")))
    uvicorn.run("dashboard:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
